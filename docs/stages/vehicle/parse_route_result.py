#!/usr/bin/env python3
"""
parse_route_result.py — parse one PDM-Lite route checkpoint into a pass/fail verdict (VEHICLE).

For the TEST-ONLY vehicle pipeline, "the route works" means: the route reached the end
(status Completed/Perfect) AND the harness finished cleanly (entry_status Finished) AND the
vehicle under test actually took part — i.e. it did NOT silently fall back to a stock actor
(vehicle.tesla.model3) AND the scenario was NOT skipped / the vehicle did NOT fail to spawn.

Status semantics (leaderboard utils/statistics_manager.py):
  "Perfect"           reached end, zero infractions
  "Completed"         reached end, with infractions      -> still a PASS for "does it work"
  "Failed - <msg>"    did not reach end / crash / timeout -> FAIL

The checkpoint JSON nests the single route record at  _checkpoint.records[0]  with top-level
entry_status. (Behavioral scoring — no-infraction / TTR-DAR thresholds — is the heavier benchmark
tier, intentionally NOT gated here.)

--------------------------------------------------------------------------------------------
THE ONE PLACE THIS DIVERGES FROM THE PEDESTRIAN PARSER — fallback attribution.

The pedestrian parser can treat ANY `walker.*` fallback as ours, because the 4 pedestrian
scenarios request a walker only via `pedestrian_blueprint` (their blockers are vehicles/props).
That reasoning is INVALID here: the vehicle scenarios legitimately request generic `vehicle.*`
blockers of their own —
    ParkingCutInModified   blocker: request_new_actor("vehicle.*", ..., {"base_type":"car","generation":2})
    AccidentModified       accident actors: request_new_actor("vehicle.*", ..., same filter)
so a `vehicle.*` fallback line may belong to a blocker, not to the asset under test. We therefore
match OUR fallback EXACTLY against --blueprint_id (plus a residual `.template` placeholder), and
report anything else as informational `other_fallback`. Without --blueprint_id we cannot attribute
at all and say so loudly instead of guessing.
--------------------------------------------------------------------------------------------

Three log-grep guards (belt-and-suspenders on top of Stage A registration + the attribute gate);
all need --log:
  1. Fallback: create_blueprint's line ("Actor model <id> not available. Using instead <x>").
     For vehicles this fires for BOTH causes — an unregistered id AND a registered id whose
     scenario attribute_filter left the candidate list empty (rng.choice([]) -> ValueError ->
     same except branch). Either way the route ran with the WRONG actor -> FAIL.
  2. Spawn-skip / vehicle-absent: the leaderboard skips a scenario whose setup raises
     ("Skipping scenario '<name>' due to setup error: ...", "Cannot spawn actor <id> ...",
     "Couldn't spawn the parked vehicle|parked actor|leading vehicle|an obstacle actor").
     When that happens the route STILL records Completed/100 because the ego drove an EMPTY
     route — a false pass where the vehicle never appeared. Documented for real in record.md:
     the dumptruck's bounding box made try_spawn_actor return None on several routes, which is
     why those routes were excluded from the benchmark.
  3. Tesla sighting: if the fallback target is vehicle.tesla.model3 we name it explicitly, since
     that is the category default every one of the 6 scenarios lands on.

  python parse_route_result.py --checkpoint <route.json> --out verdict.json \
       [--blueprint_id vehicle.<make>.<model>] [--log <route.stdout.log>] [--scenario <ClassName>]
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vehicle_common as vc  # noqa: E402
from vehicle_common import make_verdict, write_verdict  # noqa: E402

STAGE = "route_check"
_FALLBACK_RE = re.compile(r"Actor model (\S+) not available\. Using instead (\S+)")
_SKIP_RE = re.compile(r"Skipping scenario '([^']*)' due to setup error: (.*)")
_CANNOT_SPAWN_RE = re.compile(r"Cannot spawn actor (\S+) at position")
_FAILED_VEH_RE = re.compile(
    r"Couldn't spawn (?:the parked vehicle|the parked actor|the leading vehicle|"
    r"an obstacle actor|the vehicle in front of the ego)"
)
# SIGKILL signature. bash job control prints e.g.
#   run_policecar.sh: line 79: 1353005 Killed   python .../leaderboard_evaluator_...
# On this box that is virtually always the OOM killer (observed 2026-08-01: the evaluator hit
# ~11 GB anon-rss on Town11 and was reaped). Distinguishing it from an asset defect matters:
# an OOM leaves an EMPTY records[] that otherwise reads as a generic "harness crashed".
_KILLED_RE = re.compile(r"\b\d+\s+Killed\b|\bKilled\s+python\b")


# --- site config bootstrap (adds --config; no machine-specific defaults anywhere) ------------
_SC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)
import site_config as _site_config  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    _site_config.add_config_arg(ap)
    ap.add_argument("--checkpoint", required=True, help="route checkpoint json (CHECKPOINT_ENDPOINT)")
    ap.add_argument("--blueprint_id", default=None, help="vehicle.<make>.<model> (for the log greps)")
    ap.add_argument("--log", default=None, help="route stdout log to grep for fallback + spawn-skip")
    ap.add_argument("--scenario", default=None, help="scenario class name (label + skip matching)")
    ap.add_argument("--route_id", default=None,
                    help="expected route id (e.g. route_26406_roadroller) — guards against "
                         "reading a STALE checkpoint left by a previous vehicle at the same path")
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
            # Distinguish "the box died" from "the asset is broken" — they look identical at the
            # checkpoint level (empty records[]) but mean completely different things.
            killed = False
            if a.log and os.path.exists(a.log):
                with open(a.log, errors="ignore") as f:
                    killed = any(_KILLED_RE.search(line) for line in f)
            data["harness_killed"] = killed
            if killed:
                raise RuntimeError(
                    "the evaluator process was KILLED (SIGKILL) before any route finished — on "
                    "this host that is the OOM killer, not an asset defect. The route says "
                    "nothing about the vehicle; free memory (large maps like Town11/Town12 peak "
                    "around 11GB) and re-run this scenario. Check `dmesg -T | grep -i oom`."
                )
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

        # Stale-checkpoint guard. The run script rm -f's the checkpoint before each run, so this
        # normally cannot fire — but a resume path that re-parses WITHOUT re-running would
        # otherwise read a previous vehicle's record at the same slug-keyed path as a pass.
        # The leaderboard does NOT record the bare XML route id: RouteScenario decorates it as
        # `RouteScenario_<xml id>_rep<N>` (observed: 'RouteScenario_route_26406_policecar_rep0'
        # for XML id 'route_26406_policecar'). An equality test therefore NEVER matches and
        # reports every route as a stale checkpoint, masking the real failure — which is exactly
        # what happened on the first PoliceCar run. Accept either form; ids are unique per file
        # so containment is safe.
        route_id_match = None
        if a.route_id is not None:
            got = r.get("route_id") or ""
            route_id_match = (got == a.route_id) or (a.route_id in got)
        data["expected_route_id"] = a.route_id
        data["route_id_match"] = route_id_match

        # --- log greps: fallback + spawn-skip / vehicle-absent (one pass over the log) ---
        fallback_detected = False
        our_fallback = []
        other_fallback = []
        skipped_scenarios = []   # [{"scenario","reason"}]
        cannot_spawn = []        # blueprint ids the sim couldn't spawn
        spawn_failed = False     # scenario setup raised "Couldn't spawn the <...>"
        log_present = bool(a.log and os.path.exists(a.log))
        data["log_present"] = log_present
        if log_present:
            with open(a.log, errors="ignore") as f:
                for line in f:
                    m = _FALLBACK_RE.search(line)
                    if m:
                        model, replacement = m.group(1), m.group(2)
                        entry = {"model": model, "replacement": replacement}
                        # EXACT attribution only — see the module docstring. A `vehicle.*`
                        # fallback here may belong to a scenario's own generic blocker.
                        is_ours = (
                            model.endswith(".template")
                            or (a.blueprint_id is not None and model == a.blueprint_id)
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
                    if _FAILED_VEH_RE.search(line):
                        spawn_failed = True
            data["fallback_check"] = "checked" if a.blueprint_id else "checked_unattributed"
        else:
            # The runbook operator always tees the route stdout to --log; a missing log means the
            # identity + spawn guards could NOT run here. Surface it loudly (most relevant on
            # resume, when Stage A is skipped and these greps are the only runtime guards).
            data["fallback_check"] = "skipped_no_log"
        data["fallback_detected"] = fallback_detected
        data["our_fallback"] = our_fallback
        data["other_fallback"] = other_fallback  # e.g. a scenario's own vehicle.* blocker
        data["fell_back_to_default"] = [
            e for e in our_fallback if e["replacement"] == vc.FALLBACK_BLUEPRINT
        ]

        # Which skips belong to OUR scenario (test routes carry exactly one scenario, but match by
        # name when we know it so an unrelated skip can't fail us).
        our_skips = [s for s in skipped_scenarios
                     if (not a.scenario) or s["scenario"].startswith(a.scenario)]
        our_cannot_spawn = [c for c in cannot_spawn
                            if (not a.blueprint_id) or c == a.blueprint_id]
        vehicle_absent = bool(our_skips) or spawn_failed or bool(our_cannot_spawn)
        data["skipped_scenarios"] = skipped_scenarios
        data["cannot_spawn_actors"] = cannot_spawn
        data["vehicle_absent"] = vehicle_absent

        ok = (passed_status and harness_ok and not fallback_detected and not vehicle_absent
              and route_id_match is not False)
        warnings = []
        if data.get("fallback_check") == "skipped_no_log":
            warnings.append(
                "identity + spawn greps SKIPPED (no route log) — this PASS rests on route status "
                "only; re-run with --log, and note Stage A registration + the attribute gate are "
                "the primary guards"
            )
        if data.get("fallback_check") == "checked_unattributed":
            warnings.append(
                "--blueprint_id not given: a fallback line could NOT be attributed to the asset "
                "under test vs a scenario's own vehicle.* blocker. Everything landed in "
                "other_fallback; re-run with --blueprint_id for a real identity guard."
            )
        if other_fallback:
            warnings.append(
                f"non-matching fallback lines seen (informational, probably a scenario blocker): "
                f"{other_fallback}"
            )
        data["warnings"] = warnings

        err = None
        if route_id_match is False:
            err = (f"STALE CHECKPOINT: {a.checkpoint} records route_id "
                   f"{r.get('route_id')!r} but this run expected {a.route_id!r} — you are "
                   f"reading a previous run's result. Re-run the route (the run script clears "
                   f"the checkpoint first).")
        elif not passed_status:
            err = f"route status {status!r} (did not reach end / crashed / timed out)"
        elif not harness_ok:
            err = f"harness entry_status {entry_status!r} (not Finished)"
        elif fallback_detected:
            err = (f"the vehicle under test fell back to a stock actor mid-scenario "
                   f"({our_fallback}) — route 'completed' but with the WRONG actor (false pass). "
                   f"Cause is either an unregistered id OR a scenario attribute_filter that left "
                   f"no candidate (see Stage A's scenario_attr_gate).")
        elif vehicle_absent:
            err = (f"vehicle NEVER SPAWNED / scenario skipped (false pass): "
                   f"skipped={our_skips or skipped_scenarios}, cannot_spawn={our_cannot_spawn}, "
                   f"failed_spawn={spawn_failed}. Route recorded {status!r}/"
                   f"{(data.get('scores') or {}).get('score_composed')} but the ego drove an EMPTY "
                   f"route — the vehicle did not take part. Most likely its bounding box does not "
                   f"fit the scenario's spawn point (this is exactly why several dumptruck routes "
                   f"were excluded from the benchmark — see record.md).")
        write_verdict(a.out, make_verdict(STAGE, ok, data=data, error=err))
        print(json.dumps({"ok": ok, "scenario": a.scenario, "status": status,
                          "passed_status": passed_status, "fallback_detected": fallback_detected,
                          "our_fallback": our_fallback, "other_fallback": other_fallback,
                          "vehicle_absent": vehicle_absent, "skipped_scenarios": skipped_scenarios,
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
    # sys.exit(main()), NOT a bare main(): the pedestrian original returns 0/3/1 from main()
    # but never propagates it, so the PROCESS always exits 0 and a `success_on="exit0"` caller
    # would read every route as a pass. Callers should still key on verdict.ok, but the exit
    # code must not lie.
    sys.exit(main())
