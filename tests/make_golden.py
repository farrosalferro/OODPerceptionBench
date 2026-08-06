#!/usr/bin/env python3
"""Build a golden bundle for the smoke split from N replicate runs.

Bundle version : v0.9
Binds to       : arXiv v1

A golden is not "the numbers I got once". Closed-loop CARLA is not bit-reproducible -- frame
timing, sensor delivery and physics all vary a little between runs and a lot between machines.
A golden whose tolerance was guessed is either so tight that every honest install fails it, or
so loose that it never catches anything.

So this tool refuses to invent one. It takes >= 2 independent replicate runs of the same split
with the same agent on the same build, MEASURES the run-to-run spread of the Driving Score, and
derives the tolerance from it. The replicate values are written into the bundle so a reader can
audit the tolerance rather than trust it.

It also refuses to write a bundle if assertions A1..A3 do not hold in *every* replicate.
Goldens minted on a broken install are worse than no goldens at all: they bake the breakage in
and make the acceptance test certify it forever.

Standard library only.

Usage
-----
    python3 make_golden.py \\
        --replicate /runs/smoke_rep1 --replicate /runs/smoke_rep2 --replicate /runs/smoke_rep3 \\
        --reference-agent pdmlite \\
        --reference-agent-version <commit-or-tag> \\
        --carla-version 0.9.15 \\
        --content-pack-version v0.9 \\
        --out goldens/pdmlite_seed42_v0.9.golden.json

Exit status
-----------
    0  bundle written
    1  an assertion failed in some replicate, or the runs disagree -- nothing written
    2  usage / IO error
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "smoke"))

from materialize import (  # noqa: E402
    BINDS_TO,
    BUNDLE_VERSION,
    DEFAULT_ROUTES_ROOT,
    DEFAULT_SPLIT,
    SplitError,
    blueprint_in_xml,
    load_split,
    sha256_of,
    verify as verify_split,
)

sys.path.insert(0, HERE)
from check_acceptance import (  # noqa: E402
    CLEAN_STATUSES,
    GOLDEN_SCHEMA_ID,
    driving_score,
    locate_result,
    read_checkpoint,
)

DEFAULT_REFERENCE_TSV = os.path.join(HERE, "reference", "pdmlite_seed42_reference.tsv")


def load_reference(path: str) -> dict:
    """Published seed-42 observations, for an informational comparison. Never gating."""
    out: dict = {}
    if not os.path.isfile(path):
        return out
    header = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            f = line.split("\t")
            if header is None:
                header = f
                continue
            out[f[0]] = dict(zip(header, f))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--replicate", action="append", required=True, metavar="DIR",
                    help="output root of one smoke-split run (repeatable; >= 2 required)")
    ap.add_argument("--split", default=DEFAULT_SPLIT)
    ap.add_argument("--routes-root", default=DEFAULT_ROUTES_ROOT)
    ap.add_argument("--tier", choices=("core", "all"), default="all")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", required=True, help="golden bundle to write (*.golden.json)")

    ap.add_argument("--reference-agent", required=True,
                    help="name of the reference agent, e.g. pdmlite")
    ap.add_argument("--reference-agent-version", required=True,
                    help="commit / tag / checkpoint identity of the reference agent")
    ap.add_argument("--carla-version", required=True)
    ap.add_argument("--content-pack-version", required=True,
                    help="version of the OOD content pack these goldens are valid for")
    ap.add_argument("--content-pack-sha256", default=None)
    ap.add_argument("--runner-version", default=None)
    ap.add_argument("--gpu", default=None, help="e.g. 'NVIDIA RTX A6000, driver 550.54.14'")
    ap.add_argument("--notes", default=None)

    ap.add_argument("--min-tolerance", type=float, default=1.0,
                    help="floor on the DS tolerance in DS points (default 1.0). The floor "
                         "exists because replicates on one machine can agree exactly while a "
                         "different machine still differs slightly.")
    ap.add_argument("--tolerance-factor", type=float, default=2.0,
                    help="tolerance = max(min_tolerance, factor * largest observed spread)")
    ap.add_argument("--allow-single-replicate", action="store_true",
                    help="NOT RECOMMENDED. Write a bundle from one run; the tolerance is then "
                         "the unmeasured floor and the bundle records that fact.")
    args = ap.parse_args()

    print(f"OOD-PerceptionBench golden builder  [{BUNDLE_VERSION}, binds to {BINDS_TO}]")

    reps = list(dict.fromkeys(args.replicate))
    if len(reps) < 2 and not args.allow_single_replicate:
        print(f"\nERROR: {len(reps)} replicate given. At least 2 are required so the tolerance "
              f"can be measured rather than guessed.\n"
              f"       Three is the recommendation. Pass --allow-single-replicate only if you "
              f"accept an unmeasured tolerance, and say so in --notes.", file=sys.stderr)
        return 2
    for d in reps:
        if not os.path.isdir(d):
            print(f"\nERROR: replicate directory does not exist: {d}", file=sys.stderr)
            return 2

    try:
        rows = load_split(args.split, args.tier)
    except (SplitError, OSError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
    problems = verify_split(rows, args.routes_root)
    if problems:
        print(f"\nERROR: the split does not agree with the frozen route bundle:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 2
    for r in rows:
        with open(os.path.join(args.routes_root, r["path"]), encoding="utf-8") as fh:
            r["prop_blueprint_id"] = blueprint_in_xml(fh.read())

    print(f"split      : {args.split}  (tier {args.tier}, {len(rows)} route(s))")
    print(f"replicates : {len(reps)}")
    for d in reps:
        print(f"             {d}")

    reference = load_reference(DEFAULT_REFERENCE_TSV)

    failures: list[str] = []
    routes_out: dict = {}
    spreads: list[float] = []

    for row in rows:
        rel = row["path"]
        stem = os.path.splitext(os.path.basename(rel))[0]
        expected = row["prop_blueprint_id"]

        per_rep = []
        for d in reps:
            path, why = locate_result(d, rel, stem, args.seed)
            if path is None:
                failures.append(f"{rel}: replicate {d}: {why}")
                continue
            cp = read_checkpoint(path, stem)
            if cp.record is None:
                failures.append(f"{rel}: replicate {d}: {cp.error}")
                continue
            rec = cp.record
            ttr = rec.get("ttr_dar")
            if not isinstance(ttr, dict):
                failures.append(f"{rel}: replicate {d}: A2 -- no ttr_dar block in the record")
                continue
            observed = ttr.get("agent_type")
            if observed != expected:
                failures.append(
                    f"{rel}: replicate {d}: A1 -- route asks for {expected!r}, actor that "
                    f"spawned is {observed!r}")
                continue
            status = rec.get("status")
            if status not in CLEAN_STATUSES:
                failures.append(f"{rel}: replicate {d}: A3 -- status {status!r}")
                continue
            ds = driving_score(rec)
            if ds is None:
                failures.append(f"{rel}: replicate {d}: no scores.score_composed")
                continue
            scores = rec.get("scores") or {}
            per_rep.append({
                "replicate": d,
                "status": status,
                "driving_score": round(float(ds), 4),
                "route_completion": scores.get("score_route"),
                "infraction_penalty": scores.get("score_penalty"),
                "observed_agent_type": observed,
            })

        if len(per_rep) != len(reps):
            continue  # already recorded in failures

        statuses = {p["status"] for p in per_rep}
        if len(statuses) > 1:
            failures.append(
                f"{rel}: replicates disagree on status {sorted(statuses)}. An unstable route "
                f"cannot be a golden -- investigate before pinning it.")
            continue

        vals = [p["driving_score"] for p in per_rep]
        spread = max(vals) - min(vals)
        spreads.append(spread)
        median_ds = round(statistics.median(vals), 4)

        entry = {
            "route_sha256": row["sha256"],
            "expected_blueprint_id": expected,
            "observed_agent_type": per_rep[0]["observed_agent_type"],
            "status": per_rep[0]["status"],
            "driving_score": median_ds,
            "route_completion": per_rep[0]["route_completion"],
            "infraction_penalty": per_rep[0]["infraction_penalty"],
            "replicates": per_rep,
            "ds_spread": round(spread, 4),
        }
        ref = reference.get(rel)
        if ref:
            try:
                entry["published_seed42_driving_score"] = float(ref["driving_score"])
                entry["published_seed42_delta"] = round(
                    median_ds - float(ref["driving_score"]), 4)
            except (KeyError, TypeError, ValueError):
                pass
        routes_out[rel] = entry

    if failures:
        print(f"\nREFUSING TO WRITE: {len(failures)} assertion failure(s) across the "
              f"replicates.\n")
        for f in failures:
            print(f"  {f}")
        print("\nA golden minted on an install that fails A1..A3 would certify the breakage "
              "forever.\nFix the install (start with probe_blueprints.py), then re-run the "
              "replicates.")
        return 1

    max_spread = max(spreads) if spreads else 0.0
    tolerance = max(args.min_tolerance, round(args.tolerance_factor * max_spread, 4))

    bundle = {
        "schema": GOLDEN_SCHEMA_ID,
        "bundle_version": BUNDLE_VERSION,
        "binds_to": BINDS_TO,
        "reportable": False,
        "split": {
            "name": "smoke",
            "tier": args.tier,
            "path": os.path.basename(args.split),
            "sha256": sha256_of(args.split),
            "n_routes": len(rows),
        },
        "reference_agent": {
            "name": args.reference_agent,
            "version": args.reference_agent_version,
        },
        "environment": {
            "carla_version": args.carla_version,
            "content_pack_version": args.content_pack_version,
            "content_pack_sha256": args.content_pack_sha256,
            "gpu": args.gpu,
            "os": platform.platform(),
            "python": platform.python_version(),
            "runner_version": args.runner_version,
        },
        "protocol": {
            "seed": args.seed,
            "repetitions": 1,
            "n_replicates": len(reps),
        },
        "tolerance": {
            "driving_score_abs": tolerance,
            "policy": (
                f"max({args.min_tolerance}, {args.tolerance_factor} x largest observed "
                f"run-to-run spread). Largest spread across {len(reps)} replicate(s) was "
                f"{round(max_spread, 4)} DS points."
                + ("  UNMEASURED: built from a single replicate, so the floor is doing all "
                   "the work." if len(reps) < 2 else "")
            ),
            "max_observed_spread": round(max_spread, 4),
        },
        "generated": {
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "by": "tests/make_golden.py",
            "procedure": "tests/goldens/GENERATING.md",
            "notes": args.notes,
        },
        "routes": routes_out,
    }

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2)
        fh.write("\n")

    print(f"\nwrote {out}")
    print(f"  routes             : {len(routes_out)}")
    print(f"  max observed spread: {max_spread:.4f} DS points")
    print(f"  tolerance          : +/-{tolerance:.4f} DS points")
    drifted = [(k, v["published_seed42_delta"]) for k, v in routes_out.items()
               if abs(v.get("published_seed42_delta") or 0.0) > tolerance]
    if drifted:
        print(f"\n  INFO: {len(drifted)} route(s) differ from the published seed-42 sweep by "
              f"more than the tolerance:")
        for k, d in drifted:
            print(f"        {k}  delta={d:+.2f}")
        print("        This is not an error -- different hardware and a different agent build "
              "legitimately\n        move closed-loop scores. It is worth understanding before "
              "you publish the bundle.")
    print("\nNext: commit the bundle, then re-run check_acceptance.py -- it should now exit 0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
