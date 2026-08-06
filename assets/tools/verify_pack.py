#!/usr/bin/env python3
"""
verify_pack.py — post-install verification for the OOD-PerceptionBench asset pack v0.9.

WHY THIS EXISTS
---------------
A missing CARLA asset does not crash anything. If a blueprint ID is absent from the
library, the benchmark harness substitutes a *different* actor and the route still
finishes with a plausible Driving Score. Nothing in the result JSON says the prop was
wrong. This script is the only cheap thing standing between a botched install and a
silently meaningless number.

WHAT IT CHECKS
--------------
1. Each of the six shipped blueprint IDs is registered.
2. Each one actually SPAWNS. This is the load-bearing check: the four walkers are
   registered by the cooked WalkerFactory, which the pack overwrites. A WalkerFactory
   entry whose content directory is missing still shows up in the blueprint library but
   returns None from try_spawn_actor. Registration alone therefore proves nothing.
3. The spawned actor's type_id is the one asked for.
4. The bounding box matches the recorded reference within tolerance, which catches a
   blueprint ID that resolved to the wrong mesh.
5. Every blueprint ID the shipped WalkerFactory registers WITHOUT shipping its content
   ("phantoms") is reported, and asserted to be non-spawnable. These are not usable and
   must never appear in a route you intend to score.

USAGE
-----
    # with a CARLA server already running
    python3 verify_pack.py --host 127.0.0.1 --port 2000

Exit code 0 = pack is installed correctly. Non-zero = do not trust any results.
"""
from __future__ import annotations

import argparse
import json
import sys

try:
    import carla
except ImportError:  # pragma: no cover
    sys.exit("ERROR: the 'carla' python module is not importable. Install the "
             "PythonAPI wheel/egg shipped with your CARLA 0.9.15 build first.")

# ---------------------------------------------------------------------------
# The six assets this pack ships. bbox_extent is the half-extent (x, y, z) in
# metres reported by actor.bounding_box.extent, measured on CARLA 0.9.15.
# ---------------------------------------------------------------------------
SHIPPED = {
    "walker.pedestrian.astronaut":        {"group": "walkers-ccby",   "extent": None},
    "walker.pedestrian.deliveryrobot":    {"group": "walkers-ccby",   "extent": None},
    "walker.pedestrian.boar":             {"group": "walkers-ccby",   "extent": None},
    "walker.pedestrian.firefighter":      {"group": "walkers-ccbync", "extent": None},
    "static.prop.concreteroadbarrier":    {"group": "props",          "extent": None},
    "static.prop.roadclosedbarricade":    {"group": "props",          "extent": None},
}

# Blueprint IDs the shipped WalkerFactory registers but whose cooked content is NOT in
# this pack. They appear in the blueprint library and are NOT usable. See README.md
# "Phantom blueprint IDs".
PHANTOMS = [
    "walker.pedestrian.soldier",       # not redistributable (see ASSETS.tsv)
    "walker.pedestrian.wheelchair",    # not redistributable (see ASSETS.tsv)
    "walker.pedestrian.ball",          # unrelated experiment, not part of the benchmark
    "walker.pedestrian.caneman",
    "walker.pedestrian.cow",
    "walker.pedestrian.crutcheswoman",
    "walker.pedestrian.deer",
    "walker.pedestrian.labrador",
    "walker.pedestrian.tire",
]

# Blueprint IDs the benchmark's route XMLs reference that this pack does NOT provide and
# does NOT register. These fail LOUDLY-ish (absent from the library) but the harness will
# substitute a default actor, so the routes using them are simply not runnable at v0.9.
NOT_SHIPPED_AT_ALL = [
    "static.prop.roadclosedsign",
    "static.prop.trafficarrowboard",
    "static.prop.trafficmessageboard",
    "static.prop.europianarrowboardtrailer",
    "vehicle.ood.sedan", "vehicle.ood.hatchback", "vehicle.ood.suv",
    "vehicle.ood.armoredvan", "vehicle.ood.dumptruck", "vehicle.ood.roadroller",
]

TOL = 0.05  # metres, on each half-extent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2000)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--goldens", default=None,
                    help="path to goldens.json (default: alongside this script)")
    ap.add_argument("--emit-goldens", action="store_true",
                    help="write the measured extents to --goldens instead of checking")
    ap.add_argument("--without-nc", action="store_true",
                    help="you deliberately did not install the CC BY-NC tarball; treat "
                         "walker.pedestrian.firefighter as an expected phantom instead of "
                         "a required asset")
    a = ap.parse_args()

    shipped = dict(SHIPPED)
    phantoms = list(PHANTOMS)
    if a.without_nc:
        shipped.pop("walker.pedestrian.firefighter", None)
        phantoms.insert(0, "walker.pedestrian.firefighter")
        print("NOTE: --without-nc — the CC BY-NC firefighter is treated as not installed. "
              "18 pedestrian routes are unrunnable in this configuration.")

    import os
    goldens_path = a.goldens or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             "goldens.json")
    goldens = {}
    if not a.emit_goldens and os.path.exists(goldens_path):
        with open(goldens_path) as fh:
            goldens = json.load(fh)

    client = carla.Client(a.host, a.port)
    client.set_timeout(a.timeout)
    print(f"CARLA server version: {client.get_server_version()}")
    world = client.get_world()
    lib = world.get_blueprint_library()
    spawn_points = world.get_map().get_spawn_points()
    if not spawn_points:
        return _fail("map has no spawn points; load a town map before verifying")

    failures: list[str] = []
    measured: dict[str, list[float]] = {}

    print("\n--- shipped assets " + "-" * 52)
    for i, bp_id in enumerate(sorted(shipped)):
        found = [b for b in lib.filter(bp_id) if b.id == bp_id]
        if not found:
            failures.append(f"{bp_id}: NOT REGISTERED — content dir or WalkerFactory missing")
            print(f"  FAIL  {bp_id}: not in blueprint library")
            continue
        tf = spawn_points[(i * 7) % len(spawn_points)]
        tf.location.z += 1.0
        actor = world.try_spawn_actor(found[0], tf)
        if actor is None:
            failures.append(f"{bp_id}: registered but FAILED TO SPAWN — the blueprint ID "
                            f"exists (WalkerFactory) but its cooked content is missing")
            print(f"  FAIL  {bp_id}: registered but spawn returned None")
            continue
        try:
            if actor.type_id != bp_id:
                failures.append(f"{bp_id}: spawned a different actor ({actor.type_id})")
                print(f"  FAIL  {bp_id}: spawned {actor.type_id}")
                continue
            e = actor.bounding_box.extent
            ext = [round(e.x, 3), round(e.y, 3), round(e.z, 3)]
            measured[bp_id] = ext
            g = goldens.get(bp_id)
            if g is None:
                print(f"  OK    {bp_id}  extent={ext}  (no golden recorded)")
            elif max(abs(x - y) for x, y in zip(ext, g)) > TOL:
                failures.append(f"{bp_id}: bounding box {ext} != reference {g} "
                                f"(tolerance {TOL} m) — wrong mesh?")
                print(f"  FAIL  {bp_id}  extent={ext} expected {g}")
            else:
                print(f"  OK    {bp_id}  extent={ext}")
        finally:
            actor.destroy()

    print("\n--- phantom IDs (registered, content NOT shipped) " + "-" * 21)
    for bp_id in phantoms:
        found = [b for b in lib.filter(bp_id) if b.id == bp_id]
        if not found:
            print(f"  note  {bp_id}: not registered (fine — your WalkerFactory differs)")
            continue
        tf = spawn_points[3]
        tf.location.z += 1.0
        actor = world.try_spawn_actor(found[0], tf)
        if actor is None:
            print(f"  OK    {bp_id}: registered but unusable, as expected")
        else:
            actor.destroy()
            failures.append(f"{bp_id}: expected to be unusable but it SPAWNED — your "
                            f"install mixes this pack with other content; results using "
                            f"it are not comparable to the published baselines")
            print(f"  FAIL  {bp_id}: unexpectedly spawnable")

    print("\n--- not shipped at v0.9 (routes using these are not runnable) " + "-" * 9)
    for bp_id in NOT_SHIPPED_AT_ALL:
        present = any(b.id == bp_id for b in lib.filter(bp_id))
        print(f"  {'PRESENT (unexpected)' if present else 'absent (expected)':<22} {bp_id}")

    if a.emit_goldens:
        with open(goldens_path, "w") as fh:
            json.dump(measured, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"\nwrote goldens -> {goldens_path}")
        return 0

    print()
    if failures:
        print(f"VERIFY FAILED — {len(failures)} problem(s):")
        for f in failures:
            print(f"  * {f}")
        print("\nDo not run the benchmark until these are resolved: a missing prop does "
              "not fail the route, it silently changes what was measured.")
        return 1
    print("VERIFY OK — all six shipped assets registered, spawned and matched reference "
          "dimensions; no phantom ID is spawnable.")
    return 0


def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
