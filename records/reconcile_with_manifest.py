#!/usr/bin/env python3
"""Reconcile the records table against the canonical route manifest.

The records must cover the benchmark exactly: every model must have one row per
canonical route, no extras, no silent drops. This joins on the route key
(category, scenario, level, base_route_id, prop) rather than counting, so a
mis-keyed row cannot cancel a missing one.

It also cross-checks the blueprint id: the manifest carries the post-rename
`prop_blueprint_id` read out of the route XML; the records carry `agent_type`,
which is recovered independently from the result JSONs and then put through the
same rename map. Agreement means the rename landed consistently on both sides —
routes and records — from two independent starting points.

    python reconcile_with_manifest.py --records ood_perceptionbench_records_v0.9.csv \
                                      --manifest ../routes/MANIFEST.tsv

Exits non-zero on any unexplained discrepancy.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

EXPECTED_ROUTES = {"pedestrian": 162, "static": 70, "vehicle": 243}
EXPECTED_TOTAL = 475


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    args = ap.parse_args()

    man = pd.read_csv(args.manifest, sep="\t", comment="#", dtype=str)
    man["route_key"] = (man["category"] + "|" + man["scenario"] + "|"
                        + man["level"] + "|" + man["base_route_id"] + "|"
                        + man["path"].map(lambda p: Path(p).stem.split("_", 2)[2]))
    print(f"manifest routes: {len(man)}")
    per_cat = man.groupby("category").size().to_dict()
    print(f"  per category: {per_cat}")
    ok = len(man) == EXPECTED_TOTAL and per_cat == EXPECTED_ROUTES
    print(f"  [{'PASS' if ok else 'FAIL'}] manifest is the canonical "
          f"{EXPECTED_TOTAL} (70/162/243)")

    rec = pd.read_csv(args.records, dtype=str, keep_default_na=False)
    rec["route_key"] = (rec["category"] + "|" + rec["scenario"] + "|"
                        + rec["level"] + "|" + rec["route_id"] + "|" + rec["prop"])

    man_keys = set(man["route_key"])
    models = sorted(rec["model"].unique())
    print(f"\nrecords: {len(rec)} rows, {len(models)} models")

    print(f"\n{'model':18s} {'rows':>5s} {'covered':>7s} {'missing':>7s} "
          f"{'extra':>6s} {'dup':>4s}")
    print("-" * 56)
    total_bad = 0
    for model in models:
        sub = rec[rec["model"] == model]
        keys = set(sub["route_key"])
        missing = man_keys - keys
        extra = keys - man_keys
        dup = len(sub) - len(keys)
        bad = len(missing) + len(extra) + dup
        total_bad += bad
        print(f"{model:18s} {len(sub):5d} {len(keys & man_keys):7d} "
              f"{len(missing):7d} {len(extra):6d} {dup:4d}"
              + ("" if bad == 0 else "   <-- MISMATCH"))
        for k in sorted(missing)[:3]:
            print(f"      missing: {k}")
        for k in sorted(extra)[:3]:
            print(f"      extra:   {k}")
    ok &= total_bad == 0
    print("-" * 56)
    print(f"  [{'PASS' if total_bad == 0 else 'FAIL'}] every model covers every "
          f"canonical route exactly once "
          f"({len(models)} x {EXPECTED_TOTAL} = {len(models) * EXPECTED_TOTAL} rows)")

    # ---- blueprint-id cross-check ---------------------------------------
    bp = man.set_index("route_key")["prop_blueprint_id"].to_dict()
    rec_bp = rec.copy()
    rec_bp["manifest_bp"] = rec_bp["route_key"].map(bp)
    # Rows whose agent_type could not be recovered from any result JSON have no
    # claim to make; the sentinel rows likewise (ADMLP, 9 rows, documented).
    checkable = rec_bp[(rec_bp["agent_type"] != "")
                       & (rec_bp["agent_type_source"] != "sentinel")
                       & rec_bp["manifest_bp"].notna()]
    disagree = checkable[checkable["agent_type"] != checkable["manifest_bp"]]
    print(f"\nblueprint-id cross-check (records agent_type vs manifest "
          f"prop_blueprint_id; both post-rename)")
    print(f"  checkable rows: {len(checkable)} / {len(rec)}")
    print(f"  disagreements:  {len(disagree)}")
    for _, r in disagree.head(10).iterrows():
        print(f"      {r['model']} {r['route_key']}: "
              f"records={r['agent_type']} manifest={r['manifest_bp']}")
    ok &= len(disagree) == 0
    print(f"  [{'PASS' if len(disagree) == 0 else 'FAIL'}] blueprint ids agree "
          f"on both sides of the ood.* rename")

    # ---- documented, expected non-coverage -------------------------------
    n_sentinel = int((rec["agent_type_source"] == "sentinel").sum())
    n_missing_at = int((rec["agent_type_source"] == "missing").sum())
    print(f"\nexplained gaps:")
    print(f"  agent_type sentinel ('unknown'): {n_sentinel} rows "
          f"(ADMLP, perception-free baseline - expected, scores 0 OOD hits)")
    print(f"  agent_type unresolved:           {n_missing_at} rows")
    ok &= n_missing_at == 0

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
