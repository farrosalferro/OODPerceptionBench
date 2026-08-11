#!/usr/bin/env python3
"""Load the released records CSV with an explicit dtype schema.

    from records.load import load_records
    df = load_records()                       # beside this file
    df = load_records("path/to/records.csv")  # or anywhere

**Why this exists rather than a second artifact.** The release used to ship a parquet copy
alongside the CSV, so that a reader got real dtypes instead of whatever `read_csv` inferred.
That copy was dropped in favour of this loader, for three reasons:

1. **It was lossy, and silently.** `reaction_value` and `reaction_threshold` are *mixed*: 4,731
   rows hold a number and 116 hold a categorical string (`"lane_change"`, `"-1 -> 2"` — a lane
   index transition). Typing them numeric and coercing nulled all 116, so 232 values present in
   the CSV were absent from the parquet. Nothing compared the two artifacts, so it shipped.
2. **Two artifacts need a check that they agree**, and that check did not exist.
3. **The CSV is 11 MB.** The parquet's size advantage never mattered at this scale.

So the dtype schema lives here, applied to the one authoritative artifact. The mixed columns are
**strings** — that is what they are — and nothing is coerced away. :func:`check_lossless` proves
it, and `verify.sh` runs it.

Identity columns stay strings on purpose: `route_id` inferred as `int64` and `weather_id` as
`float64` are wrong (they are labels, never arithmetic) and coercing them breaks the join
against `../routes/MANIFEST.tsv`.

`SCHEMA.md` documents every column; this file is the executable form of the dtype half.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import List, Optional

DEFAULT_CSV = Path(__file__).resolve().parent / "ood_perceptionbench_records_v0.9.csv"

#: Real numbers. Blank means "not measured", which becomes NaN.
NUMERIC_COLS: List[str] = [
    "seed",
    "score_composed", "score_route", "score_penalty",
    "driving_score", "route_completion", "infraction_penalty",
    "ood_agent_collision_count",
    "n_collisions_layout", "n_collisions_pedestrian", "n_collisions_vehicle",
    "n_red_light", "n_stop_infraction", "n_outside_route_lanes",
    "n_min_speed_infractions", "n_yield_emergency_vehicle_infractions",
    "n_scenario_timeouts", "n_route_dev", "n_vehicle_blocked", "n_route_timeout",
    "n_infractions_scoring",
    "collisions_pedestrians", "collisions_vehicles", "collisions_layout",
    "off_road_infractions",
    "route_length", "duration_game", "duration_system",
    "ttr", "dar", "ttc_at_reaction",
    "t_obs_frame", "t_react_frame", "closing_velocity",
    "v_start", "v_end",
    "final_distance", "final_closing_velocity", "final_ttc",
    "num_reactions",
]

#: Tri-state: True / False / missing. Nullable `boolean`, never bare `bool` -- `bool` would turn
#: "not measured" into False, which for `success` and `ood_agent_hit` is a wrong answer rather
#: than a missing one.
BOOL_COLS: List[str] = [
    "success", "ood_agent_hit", "collided_with_ood_agent",
    "ttr_dar_present", "reaction_detected",
]

#: Numeric-looking but **mixed**, and therefore string. Not an oversight -- see the module
#: docstring. `reaction_threshold` is a number for a braking reaction and the literal
#: `"lane_change"` for a lane-change one; `reaction_value` is a number or a transition like
#: `"-1 -> 2"`. Any schema that types these numeric loses the categorical rows.
MIXED_STRING_COLS: List[str] = ["reaction_value", "reaction_threshold"]


def load_records(path: Optional[str | Path] = None):
    """Return the released records as a `pandas.DataFrame` with the schema above.

    Every other column -- identities, statuses, provenance -- is left as a string.
    """
    import pandas as pd  # imported here so `check_lossless` can run without pandas

    csv_path = Path(path) if path is not None else DEFAULT_CSV
    with open(csv_path, newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh))

    # Read everything as text first, then convert what we mean to convert. Letting read_csv
    # infer and then fixing it up afterwards is how `route_id` becomes an int.
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False, na_values=[])

    for col in NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].replace("", None), errors="coerce")
    for col in BOOL_COLS:
        if col in df.columns:
            df[col] = df[col].map(
                lambda v: True if str(v).strip().lower() == "true"
                else (False if str(v).strip().lower() == "false" else None)
            ).astype("boolean")

    missing = [c for c in NUMERIC_COLS + BOOL_COLS + MIXED_STRING_COLS if c not in header]
    if missing:
        raise ValueError(
            f"{csv_path} is missing {len(missing)} column(s) this schema names: {missing}. "
            f"Either the CSV is not the released artifact, or load.py and the generator have "
            f"drifted apart."
        )
    return df


def check_lossless(path: Optional[str | Path] = None) -> int:
    """Fail if loading drops a value the CSV carries. Returns the number of dropped values.

    This is the check that did not exist when the parquet shipped: it compares what the loader
    produces against the file it produced it from, per column, and reports the first offenders
    by name rather than a count. A numeric column may legitimately turn a blank into NaN; it may
    never turn a *non-blank* into one.
    """
    csv_path = Path(path) if path is not None else DEFAULT_CSV
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    df = load_records(csv_path)

    dropped = 0
    for col in NUMERIC_COLS + BOOL_COLS + MIXED_STRING_COLS:
        if col not in df.columns:
            continue
        present = [i for i, r in enumerate(rows) if (r.get(col) or "").strip() != ""]
        after = df[col].notna()
        lost = [i for i in present if not bool(after.iloc[i])]
        if lost:
            examples = sorted({rows[i][col] for i in lost})[:4]
            print(f"  LOSSY  {col}: {len(lost)} of {len(present)} non-empty values became "
                  f"null; e.g. {examples}")
            dropped += len(lost)
    if dropped == 0:
        print(f"OK: load.py preserves every non-empty value in {csv_path.name} "
              f"({len(rows)} rows x {len(df.columns)} columns)")
    return dropped


if __name__ == "__main__":
    import sys
    sys.exit(1 if check_lossless(sys.argv[1] if len(sys.argv) > 1 else None) else 0)
