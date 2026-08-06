#!/usr/bin/env python3
"""Validate generated records against the frozen per-model CSVs the paper used.

The paper's statistics pipeline reads `eval/<model>/{pedestrian,static,vehicle}.csv`
from a frozen snapshot. Those CSVs are the provenance of every published number.
If this generator reproduces them cell-for-cell, then any table built from the
generated records is - by construction - the table the paper reports.

This is a STRICTER test than re-running the statistics: it compares 8,550 rows
individually rather than 51 aggregates, so an error that cancels in the mean
still fails here.

ONE difference is expected and is DECLARED, not suppressed: the released records
carry the neutral `vehicle.ood.*` blueprint ids, while the frozen CSVs predate
that rename and carry the original vendor ids. See DECLARED_RENAME below — the
transform is re-derived from the same rename map the generator used, applies to
exactly one non-metric column, and its observed count is asserted.

    python validate_against_frozen.py --records ood_perceptionbench_records_v0.9.csv \
                                      --frozen-eval /path/to/paper/eval

Exits non-zero on ANY mismatch. Prints a per-(model,category) report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

MODELS = [
    "admlp", "bridgedrive", "diffad", "drivemoe", "drivetransformer",
    "hipad", "hydra_next", "lead", "minddrive", "orion", "orion_lite",
    "pdmlite", "simlingo", "sparsedrive_v2", "tcp", "tfpp", "uniad", "vad",
]
CATEGORIES = ["pedestrian", "static", "vehicle"]

# UniAD-Base ships pedestrian/static as split *_base/*_tiny files; the paper
# uses *_base and drops *_tiny.
FROZEN_FILE_OVERRIDES = {
    "uniad": {"pedestrian": "pedestrian_base.csv", "static": "static_base.csv"},
}

KEY = ["scenario", "level", "route_id", "variant", "seed"]

DEFAULT_RENAME_MAP = Path(__file__).resolve().parent / "rename_map.json"

# ---------------------------------------------------------------------------
# DECLARED TRANSFORM — the ood.* blueprint rename.
#
# The six OOD vehicle blueprints were renamed into a neutral namespace before
# release (`vehicle.inkas.amv` -> `vehicle.ood.armoredvan`, and five more), to
# keep live trademarks out of a benchmark that publishes collision rates. The
# generator applies that rename; the frozen paper CSVs predate it. So on every
# vehicle row whose resolved blueprint is one of the six, records and frozen
# legitimately differ — and that difference is the whole of the 2,910-row
# discrepancy this check used to report as "undeclared mismatches".
#
# This is a DECLARATION, not a mute button. Four properties make it safe:
#
#   1. SCOPE.    Only `agent_type` may be explained this way, and `agent_type`
#                is not in METRIC_COLS — no published number reads it. The OOD
#                collision metric is computed inside the generator against the
#                PRE-rename ids (the raw collision messages carry those), and
#                `collided_with_ood_agent` / `ood_agent_collision_count` are
#                compared here unchanged. RENAME_COL is asserted against
#                METRIC_COLS at runtime.
#   2. EXACTNESS. A row qualifies only if records == rename_map[frozen], for the
#                exact map the generator used. Any other value is still a
#                mismatch. A row where frozen already holds a `vehicle.ood.*`
#                id, or where records holds a vendor id, does not qualify.
#   3. COUNT.    The observed number must equal EXPECTED_RENAME_DELTAS on a full
#                run. TOO FEW fails just as loudly as too many: silently losing
#                the rename on some rows is exactly as bad as over-applying it.
#   4. VISIBILITY. The per-cell breakdown is printed on every run, PASS or FAIL.
# ---------------------------------------------------------------------------
RENAME_COL = "agent_type"

# 2,910 = every row across the 18 models whose resolved blueprint is one of the
# six renamed OOD vehicles. Composition, verified against the artifact:
#   17 models x 162 = 2,754   (162 = 6 OOD props x 27 vehicle base routes)
#  + ADMLP             156    (one row per OOD prop carries the `"unknown"`
#                              sentinel instead, and a sentinel is never renamed)
#  = 2,910
EXPECTED_RENAME_DELTAS = 2910

# Columns compared exactly (string-normalised). These are every column that
# feeds a published number, plus the identity/meta columns.
EXACT_COLS = [
    "scenario_name", "status",
    "score_route", "score_penalty", "score_composed",
    "route_length", "duration_game", "duration_system",
    "driving_score", "route_completion", "infraction_penalty",
    "collisions_pedestrians", "collisions_vehicles", "collisions_layout",
    "off_road_infractions",
    "collided_with_ood_agent", "ood_agent_collision_count",
    "agent_type",
    "ttr", "dar", "ttc_at_reaction", "reaction_detected",
    "t_obs_frame", "t_react_frame", "closing_velocity",
    "reaction_cause", "reaction_value", "reaction_threshold",
    "v_start", "v_end",
    "final_distance", "final_closing_velocity", "final_ttc",
    "num_reactions", "all_reactions",
]

# Columns that feed a published number. A difference in ANY of these is a hard
# failure and can never be declared "known" - see KNOWN_DELTAS below.
METRIC_COLS = {
    "status", "score_route", "score_penalty", "score_composed",
    "driving_score", "route_completion", "infraction_penalty",
    "collisions_pedestrians", "collisions_vehicles", "collisions_layout",
    "off_road_infractions",
    "collided_with_ood_agent", "ood_agent_collision_count",
}

# ---------------------------------------------------------------------------
# DECLARED DELTAS.
#
# Differences from the frozen CSVs that are understood, justified, and proven
# not to move any published number. Each must be listed EXACTLY - one entry per
# (model, category, row key, column). Anything else fails.
#
# This is an audit list, not a mute button: an entry on a METRIC_COLS column is
# rejected outright, and every entry is printed on every run.
# ---------------------------------------------------------------------------
KNOWN_DELTAS = {
    (
        "admlp", "static",
        ("construction_obstacle", "geometric_shift", "24785", "roadclosedsign", "42"),
        "agent_type",
    ): (
        "static.prop.roadclosedsign", "",
        "The result JSON for this route has an EMPTY records list (an "
        "infrastructure failure): no status, no score, no collision events. The "
        "frozen CSV left agent_type blank because the map available when it was "
        "written could not resolve this variant; the records recover it from the "
        "cross-model fallback. 139 other rows read the same value directly out "
        "of their own JSON, so the records value is the correct one. "
        "collided_with_ood_agent is False on BOTH sides - no published number "
        "is affected. Diagnostic column only.",
    ),
}


def norm(series: pd.Series) -> pd.Series:
    """Normalise for comparison: everything to a canonical string.

    Handles '100' vs '100.0', 'True' vs True, '' vs NaN - representational
    differences between a csv round-trip and a fresh DataFrame that are not
    value differences.
    """
    def one(v):
        if v is None:
            return ""
        s = str(v).strip()
        if s.lower() in ("", "nan", "none"):
            return ""
        if s.lower() == "true":
            return "True"
        if s.lower() == "false":
            return "False"
        try:
            f = float(s)
        except (TypeError, ValueError):
            return s
        import math as _m
        if _m.isinf(f):
            return "inf" if f > 0 else "-inf"
        if _m.isnan(f):
            return ""
        if f == int(f) and abs(f) < 1e15:
            return str(int(f))
        return repr(round(f, 9))
    return series.map(one)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", required=True, type=Path)
    ap.add_argument("--frozen-eval", required=True, type=Path)
    ap.add_argument("--models", nargs="+", default=MODELS)
    ap.add_argument("--max-examples", type=int, default=5)
    ap.add_argument("--rename-map", type=Path, default=DEFAULT_RENAME_MAP,
                    help="the SAME map the generator applied (default: the copy "
                         "bundled beside this script)")
    args = ap.parse_args()

    # The declared transform is re-derived from the map, never hardcoded here.
    if RENAME_COL in METRIC_COLS:
        print(f"FATAL: {RENAME_COL!r} feeds a published number and cannot carry "
              f"a declared transform.", file=sys.stderr)
        return 2
    if not args.rename_map.exists():
        print(f"FATAL: rename map not found: {args.rename_map}", file=sys.stderr)
        return 2
    bp_map = json.loads(args.rename_map.read_text()).get("blueprint_id_map", {})
    if not bp_map:
        print(f"FATAL: {args.rename_map} has an empty blueprint_id_map",
              file=sys.stderr)
        return 2

    rec = pd.read_csv(args.records, dtype=str, keep_default_na=False)
    total_mismatch = 0
    total_rows = 0
    missing_files = []
    declared_seen: set = set()
    rename_seen = 0
    rename_by_cell: dict = {}

    print(f"{'model':18s} {'cat':11s} {'rows':>5s} {'frozen':>6s} "
          f"{'keys+':>5s} {'keys-':>5s}  mismatched columns")
    print("-" * 100)

    for model in args.models:
        for cat in CATEGORIES:
            fname = FROZEN_FILE_OVERRIDES.get(model, {}).get(cat, f"{cat}.csv")
            fpath = args.frozen_eval / model / fname
            if not fpath.exists():
                missing_files.append(str(fpath))
                print(f"{model:18s} {cat:11s} {'':>5s} {'MISSING FROZEN FILE'}")
                continue
            fro = pd.read_csv(fpath, dtype=str, keep_default_na=False)
            mine = rec[(rec["model"] == model) & (rec["category"] == cat)].copy()

            # Records use `prop_raw` for the on-disk token; the frozen CSVs
            # predate the rename, so join on the raw token.
            mine["variant"] = mine["prop_raw"]

            mi = mine.set_index(KEY)
            fi = fro.set_index(KEY)
            extra = sorted(set(mi.index) - set(fi.index))
            missing = sorted(set(fi.index) - set(mi.index))
            common = sorted(set(mi.index) & set(fi.index))

            bad_cols = {}
            for col in EXACT_COLS:
                if col not in fi.columns:
                    continue
                if col not in mi.columns:
                    bad_cols[col] = f"ABSENT-IN-RECORDS({len(common)})"
                    continue
                a = norm(mi.loc[common, col])
                b = norm(fi.loc[common, col])
                ne = (a.values != b.values)
                if not ne.any():
                    continue
                n_undeclared = 0
                for i in range(len(common)):
                    if not ne[i]:
                        continue
                    key = common[i]
                    got, want = a.iloc[i], b.iloc[i]
                    decl = KNOWN_DELTAS.get((model, cat, key, col))
                    if decl is not None and decl[0] == got and decl[1] == want:
                        declared_seen.add((model, cat, key, col))
                        continue
                    # Declared transform: exactly the ood.* rename, on exactly
                    # the one non-metric column it is allowed to touch.
                    if col == RENAME_COL and bp_map.get(want) == got:
                        rename_seen += 1
                        rename_by_cell[(model, cat)] = \
                            rename_by_cell.get((model, cat), 0) + 1
                        continue
                    n_undeclared += 1
                    if n_undeclared <= max(args.max_examples, 1):
                        print(f"      MISMATCH {col} {key}: records={got!r} "
                              f"frozen={want!r}")
                if n_undeclared:
                    bad_cols[col] = n_undeclared
            nbad = sum(v for v in bad_cols.values() if isinstance(v, int))
            total_mismatch += nbad + len(extra) + len(missing)
            total_rows += len(common)
            status = "OK" if (not bad_cols and not extra and not missing) else str(bad_cols)
            print(f"{model:18s} {cat:11s} {len(mine):5d} {len(fro):6d} "
                  f"{len(extra):5d} {len(missing):5d}  {status}")

    print("-" * 100)
    print(f"compared rows: {total_rows}   undeclared mismatches: {total_mismatch}")

    # ---- declared transform: print it, count it, police it ---------------
    full_run = set(args.models) == set(MODELS)
    print(f"\nDECLARED TRANSFORM - the ood.* blueprint rename, on the single "
          f"non-metric column {RENAME_COL!r}:")
    for old, new in sorted(bp_map.items()):
        print(f"  - {old:32s} -> {new}")
    print(f"  rows explained: {rename_seen}"
          + (f" (expected {EXPECTED_RENAME_DELTAS})" if full_run else
             "  [subset run - count not asserted]"))
    for (model, cat), n in sorted(rename_by_cell.items()):
        print(f"      {model:18s} {cat:11s} {n:5d}")
    bad_rename = False
    if full_run and rename_seen != EXPECTED_RENAME_DELTAS:
        print(f"  REJECTED: expected exactly {EXPECTED_RENAME_DELTAS} renamed "
              f"rows, observed {rename_seen}. "
              + ("The rename was not applied to every row it should reach."
                 if rename_seen < EXPECTED_RENAME_DELTAS
                 else "The rename reached rows it should not."))
        bad_rename = True
    else:
        print(f"  [PASS] every one of these rows differs by the rename map and "
              f"nothing else; {RENAME_COL!r} feeds no published number, and the "
              f"OOD-collision columns are compared unrenamed and agree.")

    # ---- declared deltas: print them all, and police them ----------------
    bad_declared = False
    if KNOWN_DELTAS:
        print(f"\nDECLARED DELTAS ({len(KNOWN_DELTAS)}) - documented, "
              f"non-metric, zero impact on any published number:")
        for (model, cat, key, col), (got, want, why) in KNOWN_DELTAS.items():
            print(f"  - {model}/{cat} {key} [{col}]: "
                  f"records={got!r} frozen={want!r}")
            print(f"      {why}")
            if col in METRIC_COLS:
                print("      REJECTED: this column feeds a published number and "
                      "cannot be declared.")
                bad_declared = True
            if (model, cat, key, col) not in declared_seen:
                print("      REJECTED: declared but NOT observed - the list is "
                      "stale, re-verify it.")
                bad_declared = True

    if missing_files:
        print(f"missing frozen files: {missing_files}")
    ok = (total_mismatch == 0 and not missing_files and not bad_declared
          and not bad_rename)
    print("\nRESULT:", f"PASS - records reproduce the frozen paper CSVs on every "
          f"metric column ({rename_seen} rows differ by the declared ood.* "
          f"rename, {len(KNOWN_DELTAS)} declared diagnostic delta"
          f"{'' if len(KNOWN_DELTAS) == 1 else 's'})"
          if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
