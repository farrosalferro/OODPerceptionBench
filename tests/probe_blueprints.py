#!/usr/bin/env python3
"""Pre-flight: are the blueprints the smoke split needs actually registered and spawnable?

Bundle version : v0.9
Binds to       : arXiv v1

This is the cheapest form of the acceptance test and the one to run FIRST. It needs a running
CARLA server but no agent, no route, no GPU-hours -- seconds, not minutes.

It answers the question the route-level test cannot answer cleanly: *is this blueprint real*.

  1. REGISTRATION. ``blueprint_library.filter(<id>)`` must return the blueprint. If it returns
     nothing, CARLA's ``create_blueprint`` silently falls back -- for a walker or a prop the
     actor is simply absent, for a vehicle ``attribute_filter`` can hand back a Tesla. Either
     way the route still completes with a plausible Driving Score. Fail here instead.
  2. SPAWN + TYPE. Spawn it and assert ``actor.type_id == <id>``. Registration alone is not
     enough: an id can be registered and still resolve to another actor.
  3. DIMENSIONS (informational). Report ``bounding_box.extent`` so the realised L/W/H can be
     compared against the Appendix dimension tables and the classifier notebooks. Never gating
     -- a dimension surprise is a research finding, not an install failure.

Requires the ``carla`` Python package (0.9.15) and a reachable server. Run it on the machine
that will run the benchmark, against the CARLA build that will run the benchmark.

Usage
-----
    python3 probe_blueprints.py                          # localhost:2000, split's 9 blueprints
    python3 probe_blueprints.py --port 20000 --tier core
    python3 probe_blueprints.py --blueprint walker.pedestrian.astronaut
    python3 probe_blueprints.py --json probe.json

Exit status
-----------
    0  every blueprint registered and spawned with a matching type_id
    1  at least one blueprint is missing, or spawned as something else
    2  usage / connection error (nothing was probed)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "smoke"))

from materialize import (  # noqa: E402
    BINDS_TO,
    BUNDLE_VERSION,
    DEFAULT_SPLIT,
    SplitError,
    load_split,
)


@dataclass
class ProbeResult:
    blueprint_id: str
    registered: bool = False
    filter_matches: list = field(default_factory=list)
    spawned: bool = False
    spawned_type_id: Optional[str] = None
    type_ok: bool = False
    extent: Optional[dict] = None
    realized_lwh_m: Optional[dict] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.registered and self.spawned and self.type_ok


def probe_one(world, blueprint_library, blueprint_id: str, spawn_points) -> ProbeResult:
    import carla  # local import: this module is import-safe without carla

    r = ProbeResult(blueprint_id=blueprint_id)

    matches = [bp.id for bp in blueprint_library.filter(blueprint_id)]
    r.filter_matches = matches
    if blueprint_id not in matches:
        r.error = (
            f"blueprint_library.filter({blueprint_id!r}) did not return it "
            f"(got {matches[:5] or 'nothing'}). It is NOT registered in this build. "
            f"Every route naming it will run with the wrong actor -- or with no actor -- and "
            f"still report a Driving Score."
        )
        return r
    r.registered = True

    bp = blueprint_library.find(blueprint_id)
    actor = None
    for i, base in enumerate(spawn_points):
        tf = carla.Transform(
            carla.Location(x=base.location.x, y=base.location.y, z=base.location.z + 0.6),
            base.rotation,
        )
        actor = world.try_spawn_actor(bp, tf)
        if actor is not None:
            break
        if i > 40:
            break
    if actor is None:
        r.error = ("registered, but try_spawn_actor returned None at every candidate transform. "
                   "The blueprint exists; its mesh/collision may be broken, or the map is "
                   "congested. Re-run against a freshly started server.")
        return r

    try:
        r.spawned = True
        r.spawned_type_id = actor.type_id
        r.type_ok = actor.type_id == blueprint_id
        if not r.type_ok:
            r.error = (f"spawned actor is {actor.type_id!r}, not {blueprint_id!r}. "
                       f"The id resolved to a different actor -- this is the silent "
                       f"substitution the benchmark must never run with.")
        try:
            ext = actor.bounding_box.extent
            r.extent = {"x": round(ext.x, 4), "y": round(ext.y, 4), "z": round(ext.z, 4)}
            r.realized_lwh_m = {"L": round(ext.x * 2, 3), "W": round(ext.y * 2, 3),
                                "H": round(ext.z * 2, 3)}
        except Exception:  # noqa: BLE001 -- dimensions are informational only
            pass
    finally:
        try:
            actor.destroy()
        except Exception:  # noqa: BLE001
            pass
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=2000)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--split", default=DEFAULT_SPLIT)
    ap.add_argument("--tier", choices=("core", "all"), default="all")
    ap.add_argument("--blueprint", action="append", default=None,
                    help="probe these ids instead of the split's (repeatable)")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    print(f"OOD-PerceptionBench blueprint probe  [{BUNDLE_VERSION}, binds to {BINDS_TO}]")

    if args.blueprint:
        wanted = list(dict.fromkeys(args.blueprint))
        print(f"blueprints: {len(wanted)} from --blueprint")
    else:
        try:
            rows = load_split(args.split, args.tier)
        except (SplitError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        wanted = list(dict.fromkeys(r["prop_blueprint_id"] for r in rows))
        print(f"blueprints: {len(wanted)} distinct, from {os.path.basename(args.split)} "
              f"(tier {args.tier})")

    try:
        import carla  # noqa: F401
    except ImportError as exc:
        print(f"\nERROR: the 'carla' Python package is not importable ({exc}).\n"
              f"       This probe must run in the environment that runs the benchmark, "
              f"against CARLA 0.9.15.", file=sys.stderr)
        return 2

    import carla  # noqa: F811

    try:
        client = carla.Client(args.host, args.port)
        client.set_timeout(args.timeout)
        version = client.get_server_version()
        world = client.get_world()
        blueprint_library = world.get_blueprint_library()
        carla_map = world.get_map()
        spawn_points = carla_map.get_spawn_points()
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR: could not talk to CARLA at {args.host}:{args.port} ({exc}).\n"
              f"       Start the server first; the probe does not manage its lifecycle.",
              file=sys.stderr)
        return 2

    print(f"server    : {args.host}:{args.port}  version {version}")
    print(f"map       : {carla_map.name}  ({len(spawn_points)} spawn point(s))")
    if not spawn_points:
        print("\nERROR: the loaded map exposes no spawn points; cannot spawn-test.",
              file=sys.stderr)
        return 2

    results = [probe_one(world, blueprint_library, bid, spawn_points) for bid in wanted]

    print()
    width = max(len(r.blueprint_id) for r in results)
    for r in results:
        tag = "ok  " if r.ok else "FAIL"
        dims = ""
        if r.realized_lwh_m:
            d = r.realized_lwh_m
            dims = f"  L={d['L']:.2f} W={d['W']:.2f} H={d['H']:.2f}"
        print(f"  [{tag}] {r.blueprint_id:<{width}}{dims}")
        if r.error:
            print(f"         {r.error}")

    n_bad = sum(1 for r in results if not r.ok)
    print()
    if n_bad:
        print("=" * 78)
        print(f"FAILED: {n_bad} of {len(results)} blueprint(s) are missing or resolve to "
              f"something else.")
        print("Do NOT run the benchmark against this build: routes will complete, scores will")
        print("look plausible, and the OOD stimulus will not be there.")
        print("Install the content pack (assets/INSTALL.md) and re-probe.")
        print("=" * 78)
    else:
        print(f"PASSED: all {len(results)} blueprint(s) registered and spawned with a "
              f"matching type_id.")

    if args.json_out:
        payload = {
            "schema": "ood-perceptionbench/blueprint-probe/1",
            "bundle_version": BUNDLE_VERSION,
            "binds_to": BINDS_TO,
            "host": args.host, "port": args.port,
            "server_version": str(version),
            "map": str(carla_map.name),
            "ok": n_bad == 0,
            "results": [asdict(r) for r in results],
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        print(f"report written to {args.json_out}")

    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
