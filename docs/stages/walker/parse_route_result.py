#!/usr/bin/env python3
"""
parse_route_result.py — parse one PDM-Lite route checkpoint into a pass/fail verdict.

For the TEST-ONLY pedestrian pipeline, "the route works" means: the route reached the end
(status Completed/Perfect) AND the harness finished cleanly (entry_status Finished) AND the
walker actually took part — i.e. it did NOT silently fall back to a vehicle (Tesla) mid-scenario
AND the scenario was NOT skipped / the walker did NOT fail to spawn.

Status semantics (leaderboard utils/statistics_manager.py):
  "Perfect"           reached end, zero infractions
  "Completed"         reached end, with infractions      -> still a PASS for "does it work"
  "Failed - <msg>"    did not reach end / crash / timeout -> FAIL

The checkpoint JSON nests the single route record at  _checkpoint.records[0]  with top-level
entry_status. (Behavioral scoring — no-infraction / TTR-DAR thresholds — is the heavier
benchmark tier, intentionally NOT gated here.)

Two log-grep guards (belt-and-suspenders on top of Stage A registration); both need --log:
  1. Tesla fallback: create_blueprint fallback line ("Actor model <id> not available. Using
     instead <x>"). If our blueprint_id fell back, FAIL — route 'completed' with the WRONG actor.
  2. Spawn-skip / walker-absent: the leaderboard skips a scenario whose setup raises
     ("Skipping scenario '<name>' due to setup error: ...", "Cannot spawn actor <id> ...",
     "Failed to spawn an adversary"). When that happens the route STILL records Completed/100
     because the ego drove an EMPTY route — a false pass where the walker never appeared. If our
     scenario was skipped or our walker couldn't spawn, FAIL. (Seen with a large OOD walker whose
     collision footprint doesn't fit a tight sidewalk spawn point.)

  python parse_route_result.py --checkpoint <route.json> --out verdict.json \
       [--blueprint_id walker.pedestrian.<id>] [--log <route.stdout.log>] [--scenario <ClassName>]
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from walker_common import make_verdict, write_verdict  # noqa: E402

STAGE = "route_check"
_FALLBACK_RE = re.compile(r"Actor model (\S+) not available\. Using instead (\S+)")
_SKIP_RE = re.compile(r"Skipping scenario '([^']*)' due to setup error: (.*)")
_CANNOT_SPAWN_RE = re.compile(r"Cannot spawn actor (\S+) at position")
_FAILED_ADV_RE = re.compile(r"Failed to spawn an adversary|Couldn't spawn the walker substitute")


# --- site config bootstrap (adds --config; no machine-specific defaults anywhere) ------------
_SC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)
import site_config as _site_config  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    _site_config.add_config_arg(ap)
    ap.add_argument("--checkpoint", required=True, help="route checkpoint json (CHECKPOINT_ENDPOINT)")
    ap.add_argument("--blueprint_id", default=None, help="walker.pedestrian.<id> (for the log greps)")
    ap.add_argument("--log", default=None, help="route stdout log to grep for fallback + spawn-skip")
    ap.add_argument("--scenario", default=None, help="scenario class name (label + skip matching)")
    ap.add_argument("--out", required=True, help="JSON verdict path")
    a = ap.parse_args()
    _site_config.apply_config_arg(a)

    data = {"scenario": a.scenario, "blueprint_id": a.blueprint_id, "checkpoint_path": a.checkpoint}
    try:
        if not os.path.exists(a.checkpoint):
            raise FileNotFoundError(
                f"checkpoint not found: {a.checkpoint} — the route likely crashed before recording "
                f"(check the run log / CARLA server log)."
            )
        with open(a.checkpoint) as f:
            ck = json.load(f)

        records = ck.get("_checkpoint", {}).get("records") or ck.get("records") or []
        data["entry_status"] = ck.get("entry_status")
        if not records:
            raise RuntimeError("no route records in checkpoint (harness crashed before any route finished)")

        r = records[0]
        status = r.get("status", "")
        data.update({
            "route_id": r.get("route_id"),
            "status": status,
            "num_infractions": r.get("num_infractions"),
            "scores": r.get("scores"),
            "town_name": r.get("town_name"),
        })
        passed_status = status.startswith("Completed") or status.startswith("Perfect")
        entry_status = ck.get("entry_status")
        harness_ok = (entry_status is None) or (entry_status == "Finished")
        data["passed_status"] = passed_status
        data["harness_ok"] = harness_ok

        # --- log greps: Tesla fallback + spawn-skip / walker-absent (one pass over the log) ---
        fallback_detected = False
        our_fallback = []
        other_fallback = []
        skipped_scenarios = []   # [{"scenario","reason"}]
        cannot_spawn = []        # blueprint ids the sim couldn't spawn
        spawn_failed = False     # scenario setup raised "Failed to spawn an adversary"
        log_present = bool(a.log and os.path.exists(a.log))
        data["log_present"] = log_present
        if log_present:
            with open(a.log, errors="ignore") as f:
                for line in f:
                    m = _FALLBACK_RE.search(line)
                    if m:
                        model, replacement = m.group(1), m.group(2)
                        entry = {"model": model, "replacement": replacement}
                        # ANY walker fallback is necessarily OURS: these scenarios request a walker
                        # only via pedestrian_blueprint (blockers are vehicles/props). Also catch a
                        # residual '.template' placeholder, and the exact manifest id when given.
                        # Non-walker fallbacks (a blocker) are informational only.
                        is_ours = (
                            model.startswith("walker.")
                            or model.endswith(".template")
                            or (a.blueprint_id and model == a.blueprint_id)
                        )
                        if is_ours:
                            our_fallback.append(entry)
                            fallback_detected = True
                        else:
                            other_fallback.append(entry)
                    ms = _SKIP_RE.search(line)
                    if ms:
                        skipped_scenarios.append({"scenario": ms.group(1), "reason": ms.group(2).strip()})
                    mc = _CANNOT_SPAWN_RE.search(line)
                    if mc:
                        cannot_spawn.append(mc.group(1))
                    if _FAILED_ADV_RE.search(line):
                        spawn_failed = True
            data["fallback_check"] = "checked"
        else:
            # The runbook operator always tees the route stdout to --log; a missing log means the
            # walker-identity + spawn guards could NOT run here. Surface it loudly (most relevant on
            # resume, when Stage A is skipped and these greps are the only runtime guards).
            data["fallback_check"] = "skipped_no_log"
        data["fallback_detected"] = fallback_detected
        data["our_fallback"] = our_fallback
        data["other_fallback"] = other_fallback  # e.g. a blocker prop; surfaced but not failing

        # Which skips belong to OUR scenario (test routes carry exactly one scenario, but match by
        # name when we know it so an unrelated skip can't fail us).
        our_skips = [s for s in skipped_scenarios
                     if (not a.scenario) or s["scenario"].startswith(a.scenario)]
        our_cannot_spawn = [c for c in cannot_spawn
                            if (not a.blueprint_id) or c == a.blueprint_id]
        walker_absent = bool(our_skips) or spawn_failed or bool(our_cannot_spawn)
        data["skipped_scenarios"] = skipped_scenarios
        data["cannot_spawn_actors"] = cannot_spawn
        data["walker_absent"] = walker_absent

        ok = passed_status and harness_ok and not fallback_detected and not walker_absent
        warnings = []
        if data.get("fallback_check") == "skipped_no_log":
            warnings.append(
                "walker-identity + spawn greps SKIPPED (no route log) — this PASS rests on route "
                "status only; re-run with --log, and note Stage A registration is the primary guard"
            )
        data["warnings"] = warnings

        err = None
        if not passed_status:
            err = f"route status {status!r} (did not reach end / crashed / timed out)"
        elif not harness_ok:
            err = f"harness entry_status {entry_status!r} (not Finished)"
        elif fallback_detected:
            err = (f"walker fell back to a stock actor mid-scenario "
                   f"({our_fallback}) — route 'completed' but with the WRONG actor (false pass)")
        elif walker_absent:
            err = (f"walker NEVER SPAWNED / scenario skipped (false pass): "
                   f"skipped={our_skips or skipped_scenarios}, cannot_spawn={our_cannot_spawn}, "
                   f"failed_adversary={spawn_failed}. Route recorded {status!r}/"
                   f"{(data.get('scores') or {}).get('score_composed')} but the ego drove an EMPTY "
                   f"route — the walker did not take part. Likely its collision footprint is too "
                   f"large for the scenario's spawn point.")
        write_verdict(a.out, make_verdict(STAGE, ok, data=data, error=err))
        print(json.dumps({"ok": ok, "scenario": a.scenario, "status": status,
                          "passed_status": passed_status, "fallback_detected": fallback_detected,
                          "walker_absent": walker_absent, "skipped_scenarios": skipped_scenarios,
                          "cannot_spawn_actors": cannot_spawn,
                          "fallback_check": data.get("fallback_check"), "warnings": warnings,
                          "scores": data.get("scores")}, indent=2))
        return 0 if ok else 3
    except Exception as e:
        import traceback
        write_verdict(a.out, make_verdict(STAGE, False, data=data,
                                          error=f"{e!r}\n{traceback.format_exc()}"))
        print("PARSE ERROR:", repr(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    main()
