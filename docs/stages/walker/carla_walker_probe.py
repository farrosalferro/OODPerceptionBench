#!/usr/bin/env python3
"""
carla_walker_probe.py — Stage A: pedestrian (walker) spawn-smoke (pedestrian validation pipeline).

The walker analogue of import_automation/stages/carla_probe_test.py, but TEST-ONLY: it does
NOT collide a vehicle into the walker (PDM-Lite's route does the integration test). It answers
the prerequisite question the route check CANNOT: *is this a real, registered, functional,
correctly-rendered walker* — before we trust any route status.

Connects to the /media standalone CARLA (localhost:2000) via the client interpreter's carla 0.9.15 wheel
(the install-target rule). The runbook operator owns server lifecycle. Checks, in order:

  1. REGISTRATION (the Tesla-fallback guard at the source): `blueprint_library.filter(<id>)`
     must return exactly the walker. If empty, CARLA's create_blueprint would silently fall
     back to a vehicle (Tesla) in the scenarios → a route could "Complete" with the wrong actor
     → false pass. HARD-FAIL here instead.
  2. SPAWN + TYPE: spawn it; assert `actor.type_id == <id>` (second fallback guard).
  3. ANIMATE: apply carla.WalkerControl for N ticks; assert it actually translates
     (skeletal BP is functional, not a frozen/broken mesh).
  4. RENDER: capture RGB from two angles for the visual gate (catches a grey/broken material).
  5. BBOX: report bounding_box.extent (extent.x feeds scenario_helper.apply_leading_edge_offset)
     vs the reference adult (DEFAULT_WALKER_EXTENT_X = 0.1877). Informational, not gating.

  python carla_walker_probe.py --walker_name <W> --blueprint_id walker.pedestrian.<id> \
      --gate_dir <dir> --out verdict.json [--host localhost] [--port 2000]
  (--out is required: the JSON verdict IS the stage's output.)

Verdict ok == registered AND type_ok AND animated. (The visual check is a human gate in the
runbook operator; this script only PRODUCES the renders.)
"""
import argparse
import math
import os
import sys
import time

import carla

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from walker_common import make_verdict, write_verdict  # noqa: E402

STAGE = "spawn_smoke"
REFERENCE_ADULT_EXTENT_X = 0.1877  # scenario_helper.DEFAULT_WALKER_EXTENT_X (adult reference)


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


def main():
    ap = argparse.ArgumentParser()
    _site_config.add_config_arg(ap)
    ap.add_argument("--walker_name", required=True)
    ap.add_argument("--blueprint_id", required=True, help="e.g. walker.pedestrian.boar")
    ap.add_argument("--gate_dir", required=True, help="dir to write the RGB gate renders")
    ap.add_argument("--out", required=True, help="JSON verdict path (the verdict IS the output)")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=2000)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--max_ticks", type=int, default=50)
    ap.add_argument("--speed", type=float, default=1.5, help="WalkerControl speed (m/s)")
    ap.add_argument("--move_thresh", type=float, default=0.3, help="min horizontal travel (m) to call it animated")
    a = ap.parse_args()
    _site_config.apply_config_arg(a)

    data = {"walker_name": a.walker_name, "blueprint_id": a.blueprint_id}
    actors = []
    original_settings = None
    world = None
    try:
        client = carla.Client(a.host, a.port)
        client.set_timeout(a.timeout)
        world = client.get_world()
        data["map"] = world.get_map().name
        bl = world.get_blueprint_library()

        # 1. REGISTRATION — the source-level Tesla-fallback guard.
        matches = [bp.id for bp in bl.filter(a.blueprint_id)]
        registered = a.blueprint_id in matches
        data["registered"] = registered
        data["filter_matches"] = matches
        if not registered:
            raise RuntimeError(
                f"blueprint not registered on server: {a.blueprint_id} "
                f"(filter returned {matches or 'nothing'}). It must be cooked + ingested into "
                f"{world.get_map().name}'s build AND the server (re)launched. WITHOUT it, the "
                f"pedestrian scenarios silently spawn a Tesla (create_blueprint fallback) and the "
                f"route would falsely 'Complete'."
            )
        walker_bp = bl.find(a.blueprint_id)

        # synchronous mode for deterministic ticking
        original_settings = world.get_settings()
        s = world.get_settings()
        s.synchronous_mode = True
        s.fixed_delta_seconds = 0.05
        world.apply_settings(s)

        # 2. SPAWN + TYPE — try road spawn points until one takes.
        spawn_points = world.get_map().get_spawn_points()
        walker = None
        used_sp = None
        for sp in spawn_points[:20]:
            tf = carla.Transform(
                carla.Location(sp.location.x, sp.location.y, sp.location.z + 1.0),
                sp.rotation,
            )
            walker = world.try_spawn_actor(walker_bp, tf)
            if walker is not None:
                used_sp = sp
                break
        if walker is None:
            raise RuntimeError("could not spawn the walker at any of the first 20 spawn points")
        actors.append(walker)
        walker.set_simulate_physics(True)
        data["spawned_type_id"] = walker.type_id
        type_ok = walker.type_id == a.blueprint_id
        data["type_ok"] = type_ok
        if not type_ok:
            raise RuntimeError(
                f"spawned actor type_id={walker.type_id!r} != requested {a.blueprint_id!r} "
                f"(fallback occurred — not a real walker)"
            )

        # settle onto the ground
        for _ in range(15):
            world.tick()

        # 5. BBOX (read after spawn/settle)
        ext = walker.bounding_box.extent
        data["bbox_extent"] = {"x": round(ext.x, 4), "y": round(ext.y, 4), "z": round(ext.z, 4)}
        data["realized_LWH_m"] = {
            "L": round(2 * ext.x, 3), "W": round(2 * ext.y, 3), "H": round(2 * ext.z, 3)
        }
        data["reference_adult_extent_x"] = REFERENCE_ADULT_EXTENT_X
        data["extent_x_vs_adult_ratio"] = round(ext.x / REFERENCE_ADULT_EXTENT_X, 3) if ext.x else None

        # 4. RENDER — two angles for the visual gate, framed on the walker.
        os.makedirs(a.gate_dir, exist_ok=True)
        wloc = walker.get_transform().location
        fwd = used_sp.get_forward_vector()
        right = used_sp.get_right_vector()
        target = carla.Location(wloc.x, wloc.y, wloc.z + max(0.8, ext.z))
        d, h = 4.5, 1.5
        front_loc = carla.Location(wloc.x + fwd.x * d, wloc.y + fwd.y * d, wloc.z + h)
        tq_loc = carla.Location(
            wloc.x + (fwd.x + right.x) * d * 0.7,
            wloc.y + (fwd.y + right.y) * d * 0.7,
            wloc.z + h,
        )
        rgb = {}
        front_png = os.path.join(a.gate_dir, "spawn_front.png")
        tq_png = os.path.join(a.gate_dir, "spawn_threequarter.png")
        if capture(world, bl, front_loc, target, front_png):
            rgb["front"] = front_png
        if capture(world, bl, tq_loc, target, tq_png):
            rgb["threequarter"] = tq_png
        data["rgb_paths"] = rgb

        # 3. ANIMATE — apply WalkerControl and confirm it actually moves.
        start = walker.get_transform().location
        control = carla.WalkerControl()
        control.speed = a.speed
        fv = walker.get_transform().get_forward_vector()
        control.direction = carla.Vector3D(fv.x, fv.y, 0.0)
        max_speed = 0.0
        for _ in range(a.max_ticks):
            walker.apply_control(control)
            world.tick()
            max_speed = max(max_speed, speed(walker))
        end = walker.get_transform().location
        displacement = horiz_dist(start, end)
        animated = displacement >= a.move_thresh
        data.update({
            "displacement_m": round(displacement, 3),
            "max_speed_mps": round(max_speed, 3),
            "animated": bool(animated),
        })

        ok = registered and type_ok and animated
        err = None
        if not animated:
            err = (f"walker spawned & correct type but did not move under WalkerControl "
                   f"(displacement {displacement:.2f}m < {a.move_thresh}m) — broken/static skeletal BP?")
        write_verdict(a.out, make_verdict(STAGE, ok, data=data, error=err))
        print({"ok": ok, "registered": registered, "type_ok": type_ok, "animated": data["animated"],
               "displacement_m": data["displacement_m"], "realized_LWH_m": data["realized_LWH_m"],
               "rgb_paths": rgb})
        return 0 if ok else 3
    except Exception as e:
        import traceback
        write_verdict(a.out, make_verdict(STAGE, False, data=data,
                                          error=f"{e!r}\n{traceback.format_exc()}"))
        print("WALKER PROBE ERROR:", repr(e), file=sys.stderr)
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
