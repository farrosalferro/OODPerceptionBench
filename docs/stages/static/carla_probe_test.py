#!/usr/bin/env python3
"""
carla_probe_test.py — Agent 5: Tier-2 driven-probe collision test (see the procedure document).

Connects to the /media standalone CARLA server (localhost:2000) via the client interpreter's carla
0.9.15 client (the install-target rule: CARLA_ROOT=/media, but the py3.10 wheel is the only usable
client — the /media egg is py3.7). Spawns static.prop.<asset_name.lower()>, spawns a probe
vehicle a few metres away facing it, attaches a collision sensor, applies throttle, and
asserts the sensor fires AGAINST THE PROP and the vehicle decelerates (does not pass through).

This is the only behavioral test of the COOKED artifact's physical blocking (the PDM-Lite
route expert would avoid the obstacle, so the route check can't test collision).

  python carla_probe_test.py --asset_name <A> [--host localhost] [--port 2000] \
      [--out verdict.json] [--max_ticks 200]

The runbook operator owns server lifecycle (launches /media server, waits for :2000, tears down).
"""
import argparse
import json
import math
import os
import sys
import time

import carla


def write_verdict(out, ok, data, error=None):
    if not out:
        return
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        json.dump({"stage": "probe", "ok": bool(ok), "data": data,
                   "error": error, "ts": time.time()}, f, indent=2)


def speed(actor):
    v = actor.get_velocity()
    return math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset_name", required=True)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=2000)
    ap.add_argument("--out", default=None)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--max_ticks", type=int, default=200)
    ap.add_argument("--gap", type=float, default=9.0, help="metres between vehicle and prop")
    a = ap.parse_args()

    blueprint_id = f"static.prop.{a.asset_name.lower()}"
    data = {"asset_name": a.asset_name, "blueprint_id": blueprint_id}
    actors = []
    sensor = None
    original_settings = None
    world = None
    try:
        client = carla.Client(a.host, a.port)
        client.set_timeout(a.timeout)
        world = client.get_world()
        data["map"] = world.get_map().name

        bl = world.get_blueprint_library()
        prop_bp = bl.find(blueprint_id)
        if prop_bp is None:
            raise RuntimeError(f"blueprint not found on server: {blueprint_id} "
                               "(was it cooked + ingested + server (re)launched?)")
        veh_bp = bl.filter("vehicle.*")[0]
        col_bp = bl.find("sensor.other.collision")

        # synchronous mode for deterministic ticking
        original_settings = world.get_settings()
        s = world.get_settings()
        s.synchronous_mode = True
        s.fixed_delta_seconds = 0.05
        world.apply_settings(s)

        # placement: pick a road spawn point; prop `gap` metres ahead, vehicle facing it
        sp = world.get_map().get_spawn_points()[0]
        fwd = sp.get_forward_vector()
        prop_loc = carla.Location(sp.location.x + fwd.x * a.gap,
                                  sp.location.y + fwd.y * a.gap,
                                  sp.location.z + 0.2)
        prop_tf = carla.Transform(prop_loc, sp.rotation)
        veh_tf = carla.Transform(carla.Location(sp.location.x, sp.location.y, sp.location.z + 0.3),
                                 sp.rotation)

        prop = world.spawn_actor(prop_bp, prop_tf)
        actors.append(prop)
        vehicle = world.spawn_actor(veh_bp, veh_tf)
        actors.append(vehicle)
        data["vehicle"] = veh_bp.id

        collisions = []

        def on_collision(ev):
            other = ev.other_actor
            collisions.append({
                "other_id": other.id,
                "other_type": other.type_id,
                "is_prop": other.id == prop.id,
                "impulse": math.sqrt(ev.normal_impulse.x ** 2 + ev.normal_impulse.y ** 2
                                     + ev.normal_impulse.z ** 2),
            })

        sensor = world.spawn_actor(col_bp, carla.Transform(), attach_to=vehicle)
        sensor.listen(on_collision)

        # settle
        for _ in range(10):
            world.tick()

        max_speed = 0.0
        speed_trace = []
        hit_tick = None
        speed_at_hit = None
        for t in range(a.max_ticks):
            vehicle.apply_control(carla.VehicleControl(throttle=0.7))
            world.tick()
            sp_now = speed(vehicle)
            max_speed = max(max_speed, sp_now)
            speed_trace.append(round(sp_now, 2))
            prop_hits = [c for c in collisions if c["is_prop"]]
            if prop_hits and hit_tick is None:
                hit_tick = t
                speed_at_hit = sp_now
                # keep ticking a bit to see if it stops (doesn't pass through)
            if hit_tick is not None and t >= hit_tick + 25:
                break

        post_speed = speed(vehicle)
        prop_hits = [c for c in collisions if c["is_prop"]]
        decelerated = (speed_at_hit is not None) and (post_speed < max(0.5, 0.6 * speed_at_hit))

        data.update({
            "collided_with_prop": bool(prop_hits),
            "n_collisions": len(collisions),
            "collision_sample": collisions[:5],
            "max_speed_mps": round(max_speed, 2),
            "speed_at_hit_mps": None if speed_at_hit is None else round(speed_at_hit, 2),
            "post_speed_mps": round(post_speed, 2),
            "decelerated": bool(decelerated),
            "hit_tick": hit_tick,
            "speed_trace": speed_trace,
        })
        ok = bool(prop_hits) and bool(decelerated)
        err = None
        if not prop_hits:
            err = "probe vehicle never collided with the prop (no physical blocking / wrong placement)"
        elif not decelerated:
            err = "vehicle hit the prop but did not decelerate (passed through / non-blocking collision)"
        write_verdict(a.out, ok, data, err)
        print(json.dumps({"ok": ok, **{k: data[k] for k in
              ("collided_with_prop", "decelerated", "max_speed_mps", "post_speed_mps")}}, indent=2))
        return 0 if ok else 3
    except Exception as e:
        import traceback
        write_verdict(a.out, False, data, error=f"{e!r}\n{traceback.format_exc()}")
        print("PROBE ERROR:", repr(e), file=sys.stderr)
        return 1
    finally:
        try:
            if sensor is not None:
                sensor.stop()
                sensor.destroy()
            for ac in actors:
                ac.destroy()
            if world is not None and original_settings is not None:
                world.apply_settings(original_settings)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
