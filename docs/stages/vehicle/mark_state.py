#!/usr/bin/env python3
"""
mark_state.py — runbook operator helper to update the resumable vehicle-test state file (the client interpreter).

  mark_state.py --vehicle <V> --manifest <m> --stage <stage> [--done] [--not-done] \
                [--approved true|false] [--verdict_file <verdict.json>]
  mark_state.py --vehicle <V> --show     # print current state

Stages: cook_ingest (optional), spawn_smoke, route_check. Lets the the vehicle validation pipeline runbook operator
checkpoint each stage (and the visual-gate approval) without inlining python.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vehicle_common as vc  # noqa: E402


# --- site config bootstrap (adds --config; no machine-specific defaults anywhere) ------------
_SC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)
import site_config as _site_config  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    _site_config.add_config_arg(ap)
    ap.add_argument("--vehicle", required=True)
    ap.add_argument("--manifest", default="")
    ap.add_argument("--stage")
    ap.add_argument("--done", action="store_true")
    ap.add_argument("--not-done", dest="not_done", action="store_true")
    ap.add_argument("--approved")
    ap.add_argument("--verdict_file")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    _site_config.apply_config_arg(a)

    st = vc.load_or_init_veh_state(a.vehicle, a.manifest)

    if a.show or not a.stage:
        print(json.dumps(st, indent=2))
        return

    verdict = None
    if a.verdict_file and os.path.exists(a.verdict_file):
        with open(a.verdict_file) as f:
            verdict = json.load(f)

    approved = None
    if a.approved is not None:
        approved = a.approved.lower() in ("1", "true", "yes", "y")

    done = True if a.done else (False if a.not_done else st["stages"].get(a.stage, {}).get("done", False))
    vc.veh_mark_stage(st, a.stage, done=done, verdict=verdict, approved=approved)
    print(json.dumps({"vehicle": a.vehicle, "stage": a.stage, "done": done,
                      "approved": approved, "state_path": vc.veh_state_path(a.vehicle)}, indent=2))


if __name__ == "__main__":
    main()
