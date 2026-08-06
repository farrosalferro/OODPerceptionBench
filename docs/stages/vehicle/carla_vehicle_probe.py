#!/usr/bin/env python3
"""
carla_vehicle_probe.py — Stage A: vehicle spawn-smoke (vehicle validation pipeline).

The vehicle analogue of pedestrian_check/stages/carla_walker_probe.py, TEST-ONLY: it does NOT
collide anything into the vehicle (PDM-Lite's routes do the integration test). It answers the
prerequisite question the route check CANNOT: *is this a real, registered, spawnable, driveable,
correctly-rendered vehicle that every requested scenario will actually receive* — before we trust
any route status.

Connects to the /media standalone CARLA (localhost:2000) via the client interpreter's carla 0.9.15 wheel
(the install-target rule). The runbook operator owns server lifecycle. Checks, in order:

  1. REGISTRATION (fallback guard at the source): `blueprint_library.filter(<id>)` must return
     exactly the vehicle. If empty, CARLA's create_blueprint falls back to category "car" ->
     silently spawns vehicle.tesla.model3 -> a route "Completes" with the wrong actor -> false
     pass. HARD-FAIL here instead.

  2. ATTRIBUTE PRECONDITIONS (**vehicle-only, no walker equivalent**). create_blueprint applies
     each scenario's `attribute_filter` BEFORE choosing a blueprint:

         blueprints = library.filter(model)
         for key, value in attribute_filter.items():
             blueprints = [x for x in blueprints if check_attribute_value(x, key, value)]
         blueprint = rng.choice(blueprints)        # <-- ValueError if the list is now EMPTY

     and `check_attribute_value` returns False when the blueprint simply LACKS the attribute.
     An empty list raises ValueError, which lands in the SAME `except` branch as an unknown model
     -> vehicle.tesla.model3. So a fully-registered vehicle can still be silently replaced.
     VehicleOpensDoorTwoWaysModified filters {"has_dynamic_doors": True}; every OOD vehicle
     imported so far carries generation 0 with base_type/special_type EMPTY, so this is a live
     risk, not a hypothetical. We re-implement check_attribute_value verbatim and evaluate every
     REQUESTED scenario's filter against the real blueprint. Any scenario that would fall back is
     a HARD FAIL (drop it from the manifest's `scenarios:` or fix the asset's VehicleFactory entry).

  3. SPAWN + TYPE: spawn it; assert `actor.type_id == <id>` (second fallback guard).
  4. RENDER: capture RGB from two angles for the visual gate (catches a grey/broken material).
  5. GEOMETRY: bounding_box extent -> realized L/W/H, wheel count, and the informational
     Z-score shift classification vs the `vehicle_car` cluster.
  6. DRIVEABLE: apply VehicleControl(throttle) for N ticks; assert it actually translates
     (wheels/physics are wired, not a static prop-with-a-vehicle-id).
  7. DOORS (informational unless VehicleOpensDoorTwoWaysModified is requested): try open_door.

  python carla_vehicle_probe.py --vehicle_name <V> --blueprint_id vehicle.<make>.<model> \
      --gate_dir <dir> --out verdict.json [--scenarios A,B,...] [--host localhost] [--port 2000]
  (--out is required: the JSON verdict IS the stage's output.)

Verdict ok == registered AND attr_gate_ok AND type_ok AND driveable.
(The visual check is a human gate in the runbook operator; this script only PRODUCES the renders.)
"""
import argparse
import json
import math
import os
import sys
import time

import carla

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vehicle_common as vc  # noqa: E402
from vehicle_common import make_verdict, write_verdict  # noqa: E402

STAGE = "spawn_smoke"

# UWheeledVehicleMovementComponent::Mass default. CARLA reports it verbatim
# (CarlaWheeledVehicle.cpp: `PhysicsControl.Mass = Vehicle4W->Mass`), so a vehicle sitting
# exactly here never had its mass authored.
UE4_DEFAULT_MASS_KG = 1500.0


# --- site config bootstrap (adds --config; no machine-specific defaults anywhere) ------------
_SC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)
import site_config as _site_config  # noqa: E402


def speed(actor):
    v = actor.get_velocity()
    return math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)


def horiz_dist(a, b):
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2)


def look_at(cam_loc, target):
    """CARLA Rotation pointing a camera (forward = +X) from cam_loc toward target."""
    dx, dy, dz = target.x - cam_loc.x, target.y - cam_loc.y, target.z - cam_loc.z
    yaw = math.degrees(math.atan2(dy, dx))
    pitch = math.degrees(math.atan2(dz, math.sqrt(dx * dx + dy * dy)))
    return carla.Rotation(pitch=pitch, yaw=yaw, roll=0.0)


def capture(world, bl, cam_loc, target, out_path, ticks=12):
    """Spawn an RGB camera, capture one frame to out_path, destroy it. Returns success bool."""
    cam_bp = bl.find("sensor.camera.rgb")
    cam_bp.set_attribute("image_size_x", "1024")
    cam_bp.set_attribute("image_size_y", "768")
    cam_bp.set_attribute("fov", "70")
    cam = world.spawn_actor(cam_bp, carla.Transform(cam_loc, look_at(cam_loc, target)))
    saved = {"done": False}

    def on_img(img):
        if not saved["done"]:
            img.save_to_disk(out_path)
            saved["done"] = True

    cam.listen(on_img)
    try:
        for _ in range(ticks):
            world.tick()
            if saved["done"]:
                break
            time.sleep(0.02)
    finally:
        cam.stop()
        cam.destroy()
    return saved["done"]


# --------------------------------------------------------------------------
# Verbatim re-implementation of CarlaDataProvider.create_blueprint's inner
# check_attribute_value (scenario_runner/srunner/scenariomanager/carla_data_provider.py).
# The semantics that matter: a MISSING attribute is False, and the comparison is
# type-dispatched. Keep this in lockstep with upstream.
# --------------------------------------------------------------------------
def check_attribute_value(blueprint, name, value):
    if not blueprint.has_attribute(name):
        return False
    attribute_type = blueprint.get_attribute(name).type
    if attribute_type == carla.ActorAttributeType.Bool:
        return blueprint.get_attribute(name).as_bool() == value
    elif attribute_type == carla.ActorAttributeType.Int:
        return blueprint.get_attribute(name).as_int() == value
    elif attribute_type == carla.ActorAttributeType.Float:
        return blueprint.get_attribute(name).as_float() == value
    elif attribute_type == carla.ActorAttributeType.String:
        return blueprint.get_attribute(name).as_str() == value
    return False


def read_attributes(bp):
    """Snapshot the vehicle attributes the scenarios (and blueprint_dimensions.csv) care about."""
    out = {}
    for name in ("base_type", "special_type", "generation", "number_of_wheels",
                 "has_dynamic_doors", "has_lights", "role_name"):
        if not bp.has_attribute(name):
            out[name] = None          # MISSING — this is what trips the attribute filters
            continue
        attr = bp.get_attribute(name)
        try:
            if attr.type == carla.ActorAttributeType.Bool:
                out[name] = attr.as_bool()
            elif attr.type == carla.ActorAttributeType.Int:
                out[name] = attr.as_int()
            elif attr.type == carla.ActorAttributeType.Float:
                out[name] = attr.as_float()
            else:
                out[name] = attr.as_str()
        except Exception:  # noqa: BLE001
            out[name] = str(attr)
    return out


def evaluate_attr_gate(bl, blueprint_id, scenarios):
    """
    For each requested scenario, replay create_blueprint's filter step and decide whether OUR
    blueprint would survive it. Returns (per_scenario_list, offenders).
    """
    matches = list(bl.filter(blueprint_id))
    per_scenario = []
    offenders = []
    for class_name in scenarios:
        spec = vc.VEH_SCENARIO_BY_CLASS[class_name]
        afilter = spec["attr_filter"]
        row = {
            "class_name": class_name,
            "subfolder": spec["subfolder"],
            "blueprint_attr": spec["blueprint_attr"],
            "attribute_filter": afilter,
        }
        if not afilter:
            # None or {} -> the `for ... in {}.items()` loop is a no-op; nothing is filtered.
            row.update({"survivors": len(matches), "would_fallback": False, "failed_keys": []})
        else:
            survivors = list(matches)
            failed_keys = []
            for key, value in afilter.items():
                kept = [x for x in survivors if check_attribute_value(x, key, value)]
                if not kept and survivors:
                    failed_keys.append(key)
                survivors = kept
            would_fallback = len(survivors) == 0
            row.update({
                "survivors": len(survivors),
                "would_fallback": would_fallback,
                "failed_keys": failed_keys,
            })
            if would_fallback:
                offenders.append(row)
        per_scenario.append(row)
    return per_scenario, offenders


# --------------------------------------------------------------------------
# Server-free self-test for the attribute gate (mirrors dimension_check.py --selftest).
#
# The gate is this pipeline's one genuinely new guard, and its live negative control
# (manifests/_NEG_NoDoors.yaml) is only runnable if some registered vehicle actually lacks
# has_dynamic_doors — which cannot be known offline. evaluate_attr_gate is pure, so we pin its
# behaviour against stub blueprints instead. Run: carla_vehicle_probe.py --selftest
# --------------------------------------------------------------------------
def _selftest():
    class _Attr:
        def __init__(self, val):
            self.value = val
            self.type = carla.ActorAttributeType.Bool

        def as_bool(self):
            return self.value

    class _BP:
        def __init__(self, bid, attrs):
            self.id = bid
            self._a = attrs

        def has_attribute(self, n):
            return n in self._a

        def get_attribute(self, n):
            return _Attr(self._a[n])

    class _Lib:
        def __init__(self, bps):
            self._b = bps

        def filter(self, pat):
            return [b for b in self._b if b.id == pat]

    ALL = list(vc.DEFAULT_SCENARIOS)
    DOOR = "VehicleOpensDoorTwoWaysModified"
    cases = [
        ("has_dynamic_doors=True", {"has_dynamic_doors": True}, ALL, []),
        ("has_dynamic_doors=False", {"has_dynamic_doors": False}, ALL, [DOOR]),
        # THE case that matters: OOD vehicles carry generation 0 with empty base_type, and the
        # attribute may simply not be declared. check_attribute_value returns False for a
        # missing attribute -> empty candidate list -> ValueError -> silent Tesla.
        ("attribute MISSING", {}, ALL, [DOOR]),
        # The documented escape hatch: drop the scenario from the manifest and the gate clears.
        ("MISSING + 5-scenario manifest", {}, [s for s in ALL if s != DOOR], []),
    ]
    failures = 0
    for label, attrs, scenarios, want in cases:
        _, offenders = evaluate_attr_gate(_Lib([_BP("vehicle.x.y", attrs)]), "vehicle.x.y", scenarios)
        got = [o["class_name"] for o in offenders]
        ok = got == want
        failures += (not ok)
        print(f"  {'OK ' if ok else 'FAIL'} {label:<32} offenders={got} (want {want})")
    print(f"  -> {len(cases) - failures}/{len(cases)} passed")
    return 0 if failures == 0 else 1


def main():
    if "--selftest" in sys.argv:
        return _selftest()
    ap = argparse.ArgumentParser()
    _site_config.add_config_arg(ap)
    ap.add_argument("--selftest", action="store_true",
                    help="run the server-free attribute-gate self-test and exit")
    ap.add_argument("--vehicle_name", required=True)
    ap.add_argument("--blueprint_id", required=True, help="e.g. vehicle.ood.roadroller")
    ap.add_argument("--gate_dir", required=True, help="dir to write the RGB gate renders")
    ap.add_argument("--out", required=True, help="JSON verdict path (the verdict IS the output)")
    ap.add_argument("--scenarios", default=None,
                    help="comma list of scenario class names to gate on (default: all 6)")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=2000)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--max_ticks", type=int, default=80,
                    help="ticks of throttle for the driveable check (0.05s each)")
    ap.add_argument("--throttle", type=float, default=0.7)
    ap.add_argument("--move_thresh", type=float, default=1.5,
                    help="min horizontal travel (m) to call it driveable (heavy OOD vehicles are slow)")
    ap.add_argument("--spawn_tries", type=int, default=30,
                    help="how many map spawn points to try (large vehicles need more headroom)")
    ap.add_argument("--settle_ticks", type=int, default=60,
                    help="max ticks to wait for the vehicle to stop bouncing after the drop")
    ap.add_argument("--max_speed_min", type=float, default=8.0,
                    help="peak speed (m/s) the vehicle must reach during the drive test; this is\n                         the primary driveability signal (a grounded chassis never passes it)")
    ap.add_argument("--end_speed_min", type=float, default=4.0,
                    help="mean speed (m/s) over the LAST ~1s of the drive test; below this the\n                         vehicle lurched and stalled rather than accelerating away")
    ap.add_argument("--ground_hit_thresh", type=int, default=25,
                    help="collisions with road/sidewalk/terrain during the drive test above\n                         which the chassis is judged to be grounding out")
    ap.add_argument("--night", action="store_true",
                    help="also render the vehicle at NIGHT with the parked-obstacle light "
                         "mask (hazards+position) from front/rear/3q, twice, to verify lights")
    ap.add_argument("--rest_speed_max", type=float, default=0.2,
                    help="speed (m/s) below which the vehicle counts as at rest before the drive test")
    a = ap.parse_args()
    _site_config.apply_config_arg(a)

    if a.scenarios:
        scenarios = [s.strip() for s in a.scenarios.split(",") if s.strip()]
    else:
        scenarios = list(vc.DEFAULT_SCENARIOS)

    data = {"vehicle_name": a.vehicle_name, "blueprint_id": a.blueprint_id,
            "gated_scenarios": scenarios}
    actors = []
    original_settings = None
    world = None
    try:
        unknown = [s for s in scenarios if s not in vc.VEH_SCENARIO_BY_CLASS]
        if unknown:
            raise ValueError(f"unknown scenario(s): {unknown}; valid: {sorted(vc.VEH_SCENARIO_BY_CLASS)}")

        client = carla.Client(a.host, a.port)
        client.set_timeout(a.timeout)
        world = client.get_world()
        data["map"] = world.get_map().name
        bl = world.get_blueprint_library()

        # ---- 1. REGISTRATION — the source-level fallback guard. -------------
        matches = [bp.id for bp in bl.filter(a.blueprint_id)]
        registered = a.blueprint_id in matches
        data["registered"] = registered
        data["filter_matches"] = matches
        if not registered:
            raise RuntimeError(
                f"blueprint not registered on server: {a.blueprint_id} "
                f"(filter returned {matches or 'nothing'}). It must be cooked + ingested into "
                f"this build AND the server (re)launched. WITHOUT it, every vehicle scenario "
                f"silently spawns {vc.FALLBACK_BLUEPRINT} (create_blueprint category fallback) and "
                f"the route would falsely 'Complete'. NOTE: the id is `vehicle.<Make>.<Model>` from "
                f"the asset's VehicleFactory entry — it is NOT the Package.json vehicles[].name "
                f"(e.g. package entry 'suv_import' registers as 'vehicle.ood.suv'), so check the "
                f"factory entry before assuming the cook failed."
            )
        vehicle_bp = bl.find(a.blueprint_id)
        data["blueprint_attributes"] = read_attributes(vehicle_bp)

        # ---- 2. ATTRIBUTE PRECONDITIONS (vehicle-only). ---------------------
        per_scenario, offenders = evaluate_attr_gate(bl, a.blueprint_id, scenarios)
        data["scenario_attr_gate"] = per_scenario
        attr_gate_ok = not offenders
        data["attr_gate_ok"] = attr_gate_ok
        if not attr_gate_ok:
            names = ", ".join(
                f"{o['class_name']} (filter {o['attribute_filter']}, unmet {o['failed_keys']})"
                for o in offenders
            )
            raise RuntimeError(
                f"{a.blueprint_id} IS registered but would be silently replaced by "
                f"{vc.FALLBACK_BLUEPRINT} in: {names}. CarlaDataProvider.create_blueprint applies "
                f"the scenario's attribute_filter first; an empty result raises ValueError and "
                f"falls through to the category default. Blueprint attributes seen: "
                f"{data['blueprint_attributes']}. FIX EITHER: (a) set the missing attribute on the "
                f"asset's VehicleFactory entry (e.g. HasDynamicDoors for "
                f"VehicleOpensDoorTwoWaysModified) and re-cook, OR (b) remove those scenarios from "
                f"the manifest's `scenarios:` list. Do NOT run the routes as-is — they would score "
                f"a plausible number for a Tesla."
            )

        # synchronous mode for deterministic ticking
        original_settings = world.get_settings()
        s = world.get_settings()
        s.synchronous_mode = True
        s.fixed_delta_seconds = 0.05
        world.apply_settings(s)

        # ---- 3. SPAWN + TYPE — try road spawn points until one takes. -------
        # Escalating lift: we call world.try_spawn_actor DIRECTLY, so we do NOT get the
        # z_offset=0.2 that CarlaDataProvider.request_new_actor adds. A tall OOD vehicle
        # (dumptruck half-height ~1.56 m) can intersect the ground at a small lift and fail
        # for a reason that has nothing to do with the asset being broken. Try low first
        # (closest to what the scenarios actually do), then escalate.
        spawn_points = world.get_map().get_spawn_points()
        vehicle = None
        used_sp = None
        used_lift = None
        tried = 0
        for lift in (0.3, 1.0, 2.0):
            for sp in spawn_points[:a.spawn_tries]:
                tried += 1
                tf = carla.Transform(
                    carla.Location(sp.location.x, sp.location.y, sp.location.z + lift),
                    sp.rotation,
                )
                vehicle = world.try_spawn_actor(vehicle_bp, tf)
                if vehicle is not None:
                    used_sp = sp
                    used_lift = lift
                    break
            if vehicle is not None:
                break
        data["spawn_points_tried"] = tried
        data["spawn_lift_m"] = used_lift
        if vehicle is None:
            raise RuntimeError(
                f"could not spawn the vehicle at any of the first {tried} map spawn points — its "
                f"collision footprint may be too large. This is the same failure that makes a "
                f"scenario silently skip (route still records Completed while the ego drives an "
                f"EMPTY road); see parse_route_result.py's spawn-skip guard."
            )
        actors.append(vehicle)
        data["spawned_type_id"] = vehicle.type_id
        type_ok = vehicle.type_id == a.blueprint_id
        data["type_ok"] = type_ok
        if not type_ok:
            raise RuntimeError(
                f"spawned actor type_id={vehicle.type_id!r} != requested {a.blueprint_id!r} "
                f"(fallback occurred — not the vehicle under test)"
            )

        # settle onto the ground with the handbrake on, then CONFIRM it is actually at rest.
        # A heavy vehicle dropped from `used_lift` is still bouncing/sliding after ~1s; if we
        # start the throttle test mid-bounce, residual velocity inflates `displacement_m` and a
        # vehicle with no working wheel physics can slide past the threshold => false "driveable".
        vehicle.apply_control(carla.VehicleControl(hand_brake=True))
        rest_speed = None
        for i in range(a.settle_ticks):
            world.tick()
            rest_speed = speed(vehicle)
            if i >= 20 and rest_speed < a.rest_speed_max:
                break
        data["settle_ticks_used"] = i + 1
        data["rest_speed_mps"] = round(rest_speed, 3) if rest_speed is not None else None
        data["at_rest"] = bool(rest_speed is not None and rest_speed < a.rest_speed_max)

        # Rollover / engine failure — a tall narrow OOD vehicle can land on its side, which
        # would otherwise show up as a confusing "not driveable".
        try:
            fs = vehicle.get_failure_state()
            data["failure_state"] = str(fs)
            data["rolled_over"] = (fs == carla.VehicleFailureState.Rollover)
        except Exception as e:  # noqa: BLE001
            data["failure_state"] = None
            data["failure_state_error"] = repr(e)
            data["rolled_over"] = False

        # ---- 5. GEOMETRY (read after spawn/settle) --------------------------
        ext = vehicle.bounding_box.extent
        L, W, H = round(2 * ext.x, 3), round(2 * ext.y, 3), round(2 * ext.z, 3)
        data["bbox_extent"] = {"x": round(ext.x, 4), "y": round(ext.y, 4), "z": round(ext.z, 4)}
        data["realized_LWH_m"] = {"L": L, "W": W, "H": H}
        data["shift_classification"] = vc.classify_shift(L, W, H)
        try:
            pc = vehicle.get_physics_control()
            data["wheel_count"] = len(pc.wheels)
            data["mass_kg"] = round(pc.mass, 1)
        except Exception as e:  # noqa: BLE001
            data["wheel_count"] = None
            data["physics_control_error"] = repr(e)

        # ---- 4. RENDER — two angles for the visual gate, framed on the vehicle.
        os.makedirs(a.gate_dir, exist_ok=True)
        vloc = vehicle.get_transform().location
        fwd = used_sp.get_forward_vector()
        right = used_sp.get_right_vector()
        target = carla.Location(vloc.x, vloc.y, vloc.z + max(0.8, ext.z))
        # frame the whole vehicle: back off with its size, not a fixed distance
        d = max(7.0, 2.2 * max(L, W, H))
        h = max(2.0, 0.9 * H)
        front_loc = carla.Location(vloc.x + fwd.x * d, vloc.y + fwd.y * d, vloc.z + h)
        tq_loc = carla.Location(
            vloc.x + (fwd.x + right.x) * d * 0.7,
            vloc.y + (fwd.y + right.y) * d * 0.7,
            vloc.z + h,
        )
        rgb = {}
        front_png = os.path.join(a.gate_dir, "spawn_front.png")
        tq_png = os.path.join(a.gate_dir, "spawn_threequarter.png")
        if capture(world, bl, front_loc, target, front_png):
            rgb["front"] = front_png
        if capture(world, bl, tq_loc, target, tq_png):
            rgb["threequarter"] = tq_png
        data["rgb_paths"] = rgb

        # ---- 6. DRIVEABLE — throttle, and require SUSTAINED motion. ---------
        # A bare "did it move at all" threshold is not enough. vehicle.ood.policecar cleared the
        # old 1.5 m bar (7.1 m in 4 s) and was passed as driveable, yet in 20 s of full throttle
        # it covers ~11 m and logs ~500 collisions against static.road: its chassis collision
        # geometry grounds out, so it lurches, grinds and stops. That defect went undetected here
        # and only surfaced ~2 h later as a hard_break route failure. So we now also:
        #   * watch for ground collisions during the drive (the actual signature), and
        #   * require speed at the END of the run, not just net displacement,
        # which separates "accelerating away" from "lurched once then stuck".
        ground_hits = []
        col_sensor = None
        try:
            col_bp = bl.find("sensor.other.collision")
            col_sensor = world.spawn_actor(col_bp, carla.Transform(), attach_to=vehicle)
            col_sensor.listen(lambda e: ground_hits.append(getattr(e.other_actor, "type_id", "?")))
        except Exception:  # noqa: BLE001
            col_sensor = None

        start = vehicle.get_transform().location
        control = carla.VehicleControl(throttle=a.throttle, steer=0.0, brake=0.0, hand_brake=False)
        max_speed = 0.0
        final_speeds = []
        for i in range(a.max_ticks):
            vehicle.apply_control(control)
            world.tick()
            sp = speed(vehicle)
            max_speed = max(max_speed, sp)
            if i >= a.max_ticks - 20:          # last ~1 s of the run
                final_speeds.append(sp)
        end = vehicle.get_transform().location
        displacement = horiz_dist(start, end)
        end_speed = sum(final_speeds) / len(final_speeds) if final_speeds else 0.0

        if col_sensor is not None:
            try:
                col_sensor.stop(); col_sensor.destroy()
            except Exception:  # noqa: BLE001
                pass
        road_hits = [t for t in ground_hits if "road" in t or "sidewalk" in t or "terrain" in t]
        data["drive_collisions_total"] = len(ground_hits)
        data["drive_collisions_with_ground"] = len(road_hits)
        data["drive_collision_types"] = sorted(set(ground_hits))[:8]
        data["end_speed_mps"] = round(end_speed, 3)

        # PEAK speed is the robust signal, not END speed. A healthy vehicle can drive the whole
        # available road and then hit a building, ending at 0 m/s -- indistinguishable from
        # "stuck" if judged on the end alone (seen 2026-08-02: ood.suv did 186 m at
        # 32.9 m/s then hit a building, end 0.0, while policecar never passed 4.9 m/s).
        # end_speed is kept, but only as a warning.
        grounded = len(road_hits) >= a.ground_hit_thresh
        driveable = (displacement >= a.move_thresh) and (max_speed >= a.max_speed_min) \
            and not grounded
        if driveable and end_speed < a.end_speed_min:
            warnings_pre = data.setdefault("_early_warnings", [])
            warnings_pre.append(
                f"peak {max_speed:.1f} m/s but only {end_speed:.2f} m/s at the end — most likely "
                f"ran out of road (collisions: {sorted(set(ground_hits))[:4]}), not a defect")
        data.update({
            "displacement_m": round(displacement, 3),
            "max_speed_mps": round(max_speed, 3),
            "driveable": bool(driveable),
        })
        vehicle.apply_control(carla.VehicleControl(hand_brake=True))
        for _ in range(5):
            world.tick()

        # ---- 6b. NIGHT + LIGHTS (opt-in) --------------------------------------
        # A route frame cannot settle "do this asset's lights work": the benchmark night routes
        # run busy multi-lane highways, so the front camera shows a dozen lit background actors
        # and our parked vehicle is one dark shape among them. This renders the SAME light mask
        # ParkedObstacleModified applies to a parked obstacle
        # (RightBlinker|LeftBlinker|Position -> hazards + parking lights), at night, close up,
        # with nothing else in frame. Two captures a few ticks apart so an alternating indicator
        # cannot read as "broken" from a single unlucky frame.
        if a.night:
            night = {}
            orig_weather = world.get_weather()
            try:
                w = world.get_weather()
                w.sun_altitude_angle = -90.0
                w.cloudiness = 30.0
                w.fog_density = 3.0
                w.precipitation = 0.0
                w.precipitation_deposits = 0.0
                world.set_weather(w)

                mask = (carla.VehicleLightState.RightBlinker
                        | carla.VehicleLightState.LeftBlinker
                        | carla.VehicleLightState.Position)
                vehicle.set_light_state(carla.VehicleLightState(mask))
                night["light_mask_applied"] = "RightBlinker|LeftBlinker|Position"
                for _ in range(10):
                    world.tick()
                try:
                    night["light_state_readback"] = str(vehicle.get_light_state())
                except Exception as e:  # noqa: BLE001
                    night["light_state_readback_error"] = repr(e)

                vloc2 = vehicle.get_transform().location
                tgt = carla.Location(vloc2.x, vloc2.y, vloc2.z + max(0.6, ext.z * 0.6))
                dn = max(6.0, 1.6 * max(L, W, H))
                hn = max(1.2, 0.7 * H)
                views = {
                    "night_rear": carla.Location(vloc2.x - fwd.x * dn, vloc2.y - fwd.y * dn, vloc2.z + hn),
                    "night_front": carla.Location(vloc2.x + fwd.x * dn, vloc2.y + fwd.y * dn, vloc2.z + hn),
                    "night_threequarter": carla.Location(
                        vloc2.x - (fwd.x - right.x) * dn * 0.75,
                        vloc2.y - (fwd.y - right.y) * dn * 0.75,
                        vloc2.z + hn),
                }
                shots = {}
                for phase_i in (0, 1):          # two moments: catch the indicator on AND off
                    for name, loc in views.items():
                        out_png = os.path.join(a.gate_dir, f"{name}_t{phase_i}.png")
                        if capture(world, bl, loc, tgt, out_png):
                            shots[f"{name}_t{phase_i}"] = out_png
                    for _ in range(9):          # advance ~0.45s between the two captures
                        world.tick()
                night["renders"] = shots
                night["ok"] = bool(shots)
            except Exception as e:  # noqa: BLE001
                night["ok"] = False
                night["error"] = repr(e)
            finally:
                try:
                    world.set_weather(orig_weather)
                except Exception:
                    pass
            data["night_lights"] = night

        # ---- 7. DOORS — informational; gating already happened in step 2. ---
        door_info = {"attempted": False, "ok": None, "error": None}
        if data["blueprint_attributes"].get("has_dynamic_doors"):
            door_info["attempted"] = True
            try:
                vehicle.open_door(carla.VehicleDoor.FL)
                for _ in range(10):
                    world.tick()
                vehicle.close_door(carla.VehicleDoor.FL)
                for _ in range(5):
                    world.tick()
                door_info["ok"] = True
            except Exception as e:  # noqa: BLE001
                door_info["ok"] = False
                door_info["error"] = repr(e)
        data["door_check"] = door_info

        # ---- warnings: never gating, but the human gate must see them. ------
        warnings = []
        if not data["at_rest"]:
            warnings.append(
                f"vehicle was still moving ({data['rest_speed_mps']} m/s) when the drive test "
                f"started — 'driveable' may be residual sliding rather than wheel physics"
            )
        if data.get("rolled_over"):
            warnings.append("vehicle reports failure_state Rollover after the drop — it landed "
                            "on its side; the drive result below is not meaningful")
        # A fallback substitutes vehicle.tesla.model3 (4.79 x 2.16 x 1.49 m). If the asset is
        # within ~15% of that on every dimension, the human at the visual gate cannot reliably
        # tell a fallback from the real thing by silhouette — so say so and lean on type_id.
        tesla = (4.79, 2.16, 1.49)
        if all(abs(v - t) / t <= 0.15 for v, t in zip((L, W, H), tesla)):
            warnings.append(
                f"realized dims {L}x{W}x{H} are within 15% of {vc.FALLBACK_BLUEPRINT} "
                f"({tesla[0]}x{tesla[1]}x{tesla[2]}) on every axis — a silent fallback would be "
                f"hard to spot by eye at the visual gate; rely on type_ok/registered, and check "
                f"colour and badging in the renders"
            )
        wc = data.get("wheel_count")
        bp_wheels = data["blueprint_attributes"].get("number_of_wheels")
        if wc is not None and bp_wheels is not None and wc != bp_wheels:
            warnings.append(f"physics wheel count {wc} != blueprint number_of_wheels {bp_wheels}")
        # Mass is read straight off the movement component
        # (CarlaWheeledVehicle.cpp: `PhysicsControl.Mass = Vehicle4W->Mass`), whose UE4 default
        # is 1500 kg. An asset sitting exactly on it almost certainly never had Mass set in
        # BP_<Name> -> VehicleMovementComponent -> Vehicle Setup. Non-gating (it spawns and
        # drives fine) but it changes collision dynamics in the moving scenarios, so surface it.
        # Found on the first real run: 4 of 7 custom vehicles were at the default, including a
        # road roller that should weigh 8-12 t.
        if data.get("mass_kg") is not None and abs(data["mass_kg"] - UE4_DEFAULT_MASS_KG) < 0.5:
            warnings.append(
                f"mass is exactly {UE4_DEFAULT_MASS_KG} kg — the UE4 WheeledVehicleMovementComponent "
                f"default, i.e. Mass was probably never set on this asset. Check "
                f"BP_<Name> > VehicleMovementComponent > Vehicle Setup > Mass; it affects collision "
                f"dynamics in hard_break / parking_cut_in / invading_turn"
            )
        warnings.extend(data.pop("_early_warnings", []))
        data["warnings"] = warnings

        ok = registered and attr_gate_ok and type_ok and driveable
        err = None
        if not driveable and data.get("drive_collisions_with_ground", 0) >= a.ground_hit_thresh:
            err = (f"CHASSIS GROUNDS OUT: {data['drive_collisions_with_ground']} collisions with "
                   f"road/sidewalk/terrain during {a.max_ticks} ticks of throttle "
                   f"(types={data.get('drive_collision_types')}). The body's collision geometry "
                   f"sits below the wheel contact plane, so friction overwhelms drive torque: the "
                   f"vehicle lurches ({displacement:.1f}m), grinds and stops (end speed "
                   f"{end_speed:.2f} m/s). Check the chassis collision primitive in BP_<Name>'s "
                   f"PhysicsAsset and whether wheel radius matches the visual wheel. This breaks "
                   f"any scenario that needs the actor to DRIVE (hard_break, parking_cut_in, "
                   f"invading_turn).")
        elif not driveable and max_speed < a.max_speed_min:
            err = (f"vehicle moved {displacement:.1f}m but peaked at only {max_speed:.2f} m/s "
                   f"(min {a.max_speed_min}; end {end_speed:.2f}) — it lurched and stalled rather than "
                   f"accelerating away. wheel_count={data.get('wheel_count')}, "
                   f"ground_collisions={data.get('drive_collisions_with_ground')}")
        elif not driveable:
            err = (f"vehicle spawned & correct type but did not move under throttle "
                   f"{a.throttle} for {a.max_ticks} ticks (displacement "
                   f"{displacement:.2f}m < {a.move_thresh}m) — wheels/physics not wired? "
                   f"wheel_count={data.get('wheel_count')}, "
                   f"failure_state={data.get('failure_state')}, "
                   f"at_rest_before_test={data.get('at_rest')}")
        write_verdict(a.out, make_verdict(STAGE, ok, data=data, error=err))
        print(json.dumps({
            "ok": ok, "registered": registered, "attr_gate_ok": attr_gate_ok,
            "type_ok": type_ok, "driveable": data["driveable"],
            "displacement_m": data["displacement_m"], "realized_LWH_m": data["realized_LWH_m"],
            "shift_classification": data["shift_classification"],
            "wheel_count": data.get("wheel_count"),
            "failure_state": data.get("failure_state"),
            "at_rest": data.get("at_rest"),
            "blueprint_attributes": data["blueprint_attributes"],
            "warnings": warnings,
            "rgb_paths": rgb,
        }, indent=2))
        return 0 if ok else 3
    except Exception as e:
        import traceback
        write_verdict(a.out, make_verdict(STAGE, False, data=data,
                                          error=f"{e!r}\n{traceback.format_exc()}"))
        print("VEHICLE PROBE ERROR:", repr(e), file=sys.stderr)
        return 1
    finally:
        try:
            for ac in actors:
                ac.destroy()
            if world is not None and original_settings is not None:
                world.apply_settings(original_settings)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
