#!/usr/bin/env python3
"""
dimension_check.py — shift classification (see the procedure document).

Pure function of (L, W, H, anchor) -> per-axis delta, delta_max, verdict, by the
20% weakest-link rule vs `trafficwarning`. Matches the static classifier
notebook and the published classification exactly.

  Visual    : delta_d <= 0.20 for ALL of L, W, H
  Geometric : delta_d  > 0.20 for AT LEAST ONE of L, W, H

CLI:
  dimension_check.py --L 2.74 --W 3.23 --H 3.30 [--expect visual|geometric]
  dimension_check.py --selftest        # validates against committed static_classification.json

Emits JSON to stdout. With --expect, exits non-zero on verdict mismatch
(the classification gate: shift_type is human-declared and hard-verified, never inferred).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ANCHOR_LWH, VISUAL_DELTA_MAX  # noqa: E402


# --- site config bootstrap (adds --config; no machine-specific defaults anywhere) ------------
_SC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)
import site_config as _site_config  # noqa: E402


def classify(L, W, H, anchor=ANCHOR_LWH, threshold=VISUAL_DELTA_MAX):
    dims = {"length": float(L), "width": float(W), "height": float(H)}
    aL, aW, aH = anchor
    anchors = {"length": aL, "width": aW, "height": aH}
    deltas = {f"delta_{k}": abs(dims[k] - anchors[k]) / anchors[k] for k in dims}
    delta_max = max(deltas.values())
    verdict = "visual" if delta_max <= threshold else "geometric"
    return {
        "dimensions": dims,
        "anchor": {"length": aL, "width": aW, "height": aH},
        "threshold": threshold,
        "deltas": {**deltas, "delta_max": delta_max},
        "verdict": verdict,
    }


# committed labels use "level_1_visual"/"level_2_geometric"; map to our verdict
def _norm(label):
    return "visual" if "visual" in label else "geometric"


def selftest(ref=None):
    """Re-classify the committed static props and confirm the labels reproduce.

    The reference table is `data/fixed/static_classification.json` in the benchmark repo;
    pass --reference to point elsewhere, otherwise it resolves from the site config's
    `bench_root`.
    """
    if ref is None:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import site_config
        ref = os.path.join(site_config.get("bench_root"), "data", "fixed",
                           "static_classification.json")
    with open(ref) as f:
        rows = json.load(f)
    fails = []
    for r in rows:
        d = r["dimensions"]
        res = classify(d["length"], d["width"], d["height"])
        expect = _norm(r["shift_type"])
        # also cross-check our delta_max against the committed one
        dm_ok = abs(res["deltas"]["delta_max"] - r["deltas"]["delta_max"]) < 1e-3
        if res["verdict"] != expect or not dm_ok:
            fails.append({
                "blueprint_id": r["blueprint_id"],
                "got": res["verdict"], "expect": expect,
                "got_delta_max": round(res["deltas"]["delta_max"], 4),
                "ref_delta_max": r["deltas"]["delta_max"],
            })
    out = {"n": len(rows), "passed": len(rows) - len(fails), "failures": fails}
    print(json.dumps(out, indent=2))
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    _site_config.add_config_arg(ap)
    ap.add_argument("--L", type=float)
    ap.add_argument("--W", type=float)
    ap.add_argument("--H", type=float)
    ap.add_argument("--expect", choices=["visual", "geometric"])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--reference", default=None,
                    help="path to static_classification.json (default: from the site config)")
    args = ap.parse_args()
    _site_config.apply_config_arg(args)

    if args.selftest:
        sys.exit(selftest(args.reference))

    if None in (args.L, args.W, args.H):
        ap.error("provide --L --W --H (or --selftest)")

    res = classify(args.L, args.W, args.H)
    if args.expect:
        res["expected"] = args.expect
        res["match"] = (res["verdict"] == args.expect)
    print(json.dumps(res, indent=2))
    if args.expect and not res["match"]:
        sys.exit(3)


if __name__ == "__main__":
    main()
