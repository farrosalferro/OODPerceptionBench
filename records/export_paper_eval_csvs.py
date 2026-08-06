#!/usr/bin/env python3
"""Repoint the paper's statistics pipeline at the published records.

The paper's table builders do NOT read the 766 GB result tree directly (a
common misreading). The dependency chain is:

    <RESULTS_ROOT>   (766 GB, ~498k JSONs)
      -> eval/json_to_csv_results_v2.py + enrich_ood_agent_collisions.py
      -> <paper-repo>/eval/<model>/{pedestrian,static,vehicle}.csv     <-- the seam
      -> docs/stats/compute_abstract_placeholders.py  -> final_stats_*.{csv,json}
      -> docs/stats/build_table1_headline.py          -> tables/table1_headline.tex

So "repointing at the published file" means regenerating that seam - the
per-(model, category) analysis CSVs - from the few-MB records table instead of
the 766 GB tree. This script does exactly that, and nothing else. Downstream
scripts are then run completely unmodified.

    python export_paper_eval_csvs.py \
        --records ood_perceptionbench_records_v0.9.csv \
        --out-dir <somewhere>/eval

    # then, in a tree whose eval/ is that directory:
    python docs/stats/compute_abstract_placeholders.py
    python docs/stats/compute_pdmlite_ceiling.py
    python docs/stats/build_table1_headline.py

`reproduce_table1.py` runs precisely this flow end-to-end and asserts the
resulting LaTeX is byte-identical to the committed tables.

NOTE ON OWNERSHIP: this script writes only where you point it. It deliberately
does NOT edit anything inside the paper repository - that tree belongs to the
paper repository. See README.md, "Driving the paper's statistics from these records".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

# UniAD-Base ships pedestrian/static split into *_base/*_tiny; the headline
# cohort uses *_base and drops *_tiny (locked, docs/paper_state.md).
ANALYSIS_FILE_OVERRIDES = {
    "uniad": {"pedestrian": "pedestrian_base.csv", "static": "static_base.csv"},
}

# Exactly the columns the downstream statistics consume. `load_cell` requires
# scenario/route_id/variant/seed/level/driving_score; the OOD-hit block reads
# collided_with_ood_agent. The rest is carried for human inspection.
ANALYSIS_COLUMNS = [
    "scenario", "level", "route_id", "variant", "seed", "scenario_name",
    "status", "score_route", "score_penalty", "score_composed",
    "driving_score", "route_completion", "infraction_penalty",
    "collided_with_ood_agent", "ood_agent_collision_count",
]


def export(records: Path, out_dir: Path) -> dict:
    df = pd.read_csv(records, dtype=str, keep_default_na=False)

    missing = [c for c in ANALYSIS_COLUMNS if c not in df.columns and c != "variant"]
    if missing:
        raise SystemExit(f"records are missing required columns: {missing}")

    # The statistics pipeline joins on the ON-DISK prop token, which predates
    # the `ood.*` rename (results under the source tree are keyed by `sedane` / `amv`).
    # `prop_raw` is that token; `prop` is the released, renamed name. Using the
    # raw token here keeps the analysis join stable while the release-facing
    # `prop` column carries the neutral vehicle.ood.* naming.
    df["variant"] = df["prop_raw"]

    written = {}
    for (model, category), sub in df.groupby(["model", "category"]):
        fname = ANALYSIS_FILE_OVERRIDES.get(model, {}).get(category, f"{category}.csv")
        out = out_dir / model / fname
        out.parent.mkdir(parents=True, exist_ok=True)
        sub[ANALYSIS_COLUMNS].to_csv(out, index=False)
        written[f"{model}/{fname}"] = len(sub)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--records", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    args = ap.parse_args()

    written = export(args.records, args.out_dir.resolve())
    for name in sorted(written):
        print(f"  {name:34s} {written[name]:4d} rows")
    print(f"\nWrote {len(written)} analysis CSVs "
          f"({sum(written.values())} rows) to {args.out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
