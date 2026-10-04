# OOD-PerceptionBench — per-route records

**Bundle version:** `v0.9` · **Binds to:** arXiv v1 · **Seeds:** 42, 43, 44 (PDM-Lite: 42 only)

This folder holds the per-route results of all 18 evaluated agents: 17 end-to-end models plus
the PDM-Lite expert. With it you can re-check every number in the paper **without a GPU**, and
compare a new model against the 17 baselines **without re-simulating anything**.

The records are a few MB and ship in this repository. Background, verification findings and
known caveats are in [`NOTES.md`](NOTES.md).

## 1. The artifact

| File | What |
|---|---|
| `ood_perceptionbench_records_v0.9.csv` | the tidy table, 24,700 rows × 64 columns; the **only** artifact |
| `load.py` | the dtype schema, applied at read time; `python3 load.py` proves it drops nothing |
| `ood_perceptionbench_records_v0.9.meta.json` | version stamp, provenance, sha256 of the artifact **and of the generator**, reconciliation report |
| `rename_map.json` | the `ood.*` blueprint rename, applied by the generator |

**One row per `(model, category, scenario, route_id, level, prop, seed)`.**

```
17 E2E models × 475 × 3 seeds + PDM-Lite × 475 = 24,700 rows
   475 = 70 static + 162 pedestrian + 243 vehicle
```

```python
from records.load import load_records
df = load_records()          # numerics float64, flags nullable boolean,
                             # identity columns str, mixed columns str
```

> **Keep identity columns as strings.** `route_id` read as `int64` or `weather_id` as `float64`
> is wrong: they are labels, and coercing them breaks the join against
> [`../routes/MANIFEST.tsv`](../routes/MANIFEST.tsv). [`SCHEMA.md`](SCHEMA.md) documents every
> column.

**Version discipline.** Every artifact carries `version: v0.9` / `binds_to: arXiv v1` in
`*.meta.json` and in `VERSION`. v1.0 records will bind to arXiv v2 and will **not** be
comparable to these: v1.0 re-runs 12 of the 18 props. **Do not mix the two score sets.**

## 2. Regenerating

```bash
python build_records.py --results-root <root of the raw result tree> --out-dir .
```

This takes about 15 min for all 18 models (24,700 JSONs). The results root is opened
**read-only**; the script never writes outside `--out-dir`. `--rename-map` defaults to the
bundled `rename_map.json`, so nothing outside this repository is needed.

`--models tcp` restricts the run to one model, for development only. A subset run narrows the
cross-model `agent_type` inference (§4) and is **not** valid for the release artifact;
`meta.json` then records `partial_run: true`.

**Do not post-process the output.** One invocation writes both the `.csv` and the `.meta.json`,
and the meta records the sha256 of the CSV, of `build_records.py` and of `rename_map.json`. To
change the table, change the generator and re-run it. Check 1 fails otherwise
([why](NOTES.md#the-generator-is-the-only-thing-allowed-to-write-these-files)).

## 3. Verifying

```bash
./verify.sh <paper-repo> <scratch-dir>
```

Five checks, all of which must pass:

| # | Script | Asserts |
|---|---|---|
| 1 | `check_meta.py` | `meta.json` matches the files here: CSV sha256 and size, `n_rows`, `n_columns`, and the column list in order |
| 2 | `load.py` | the loader preserves every non-empty value in the CSV |
| 3 | `validate_against_frozen.py` | all 24,700 rows match the frozen per-model CSVs behind the paper, on all 35 metric-bearing columns; the only allowed difference is the declared `ood.*` rename on exactly 8,395 rows |
| 4 | `reconcile_with_manifest.py` | each (model, seed) covers each of the 475 routes in `../routes/MANIFEST.tsv` exactly once; blueprint ids agree with the route XMLs |
| 5 | `reproduce_table1.py` | **Table 1 regenerates exactly** (the acceptance test) |

Check 5 copies the paper's statistics scripts **unmodified** into a scratch tree fed only by the
records, runs them, and requires: (A) `final_stats_summary.json` deep-diffs to 0 against the
frozen file; (B) `tables/table1_headline.tex` and `table_percell_granular.tex` regenerate
byte-identical; (C) `final_stats_cells.csv` equals a same-interpreter recomputation from the
frozen paper CSVs. The current results and the headline figures it asserts are in
[`NOTES.md`](NOTES.md#3-current-verification-results).

Reference environment: `pandas 2.0.3 / numpy 1.22.0 / scipy 1.10.1`. Check 5 needs `scipy`;
the others do not. **If you are reproducing the per-cell table, pin your scipy version**
([why](NOTES.md#54-provenance-note--scipy-version-drift-not-a-records-bug)).

## 4. Metric definitions (read before reusing)

**Driving Score / DS** — `score_composed` on the route record. `driving_score` is the
leaderboard's aggregate label and is identical per route; the paper's pipeline reads
`driving_score`, and both columns are shipped.

**ΔDS** = `mean(DS at shift) − mean(DS at base)` per (model, category) cell. Negative ⇒
regression. The paper's headline `a`/`b` are the *signed DS drop* (`−mean ΔDS`, positive ⇒
regression).

**OOD-collision hit** (`ood_agent_hit`, alias `collided_with_ood_agent`) — the paper's
**second headline metric**, not in the stock leaderboard. True iff at least one collision
message in the route record names the OOD prop's own actor type. Attribution:

1. `agent_type` from the record's `ttr_dar` payload where present (`record`);
2. otherwise back-filled from a **cross-model** `variant → agent_type` map, built only from
   variants on which every model that recorded a type agrees (`fallback`);
3. the literal sentinel `"unknown"` is **left in place, never back-filled** (`sentinel`) —
   9 ADMLP vehicle rows. Those rows score 0 hits.

The sentinel is excluded when *building* the map but kept when *filling* rows; see
[NOTES §5.2](NOTES.md#52-the-unknown-sentinel-would-have-silently-corrupted-the-vehicle-metric).
`agent_type` holds the **released, post-rename** blueprint id (`vehicle.ood.armoredvan`, not
`vehicle.inkas.amv`). To join against the raw result tree or the frozen analysis CSVs, both of
which predate the rename, **use `prop_raw`, not `agent_type`**.

**Success Rate** (`success`) — Bench2Drive Eq. 1: status ∈ {Completed, Perfect} **and** every
infraction list empty. The skip-set is **not just `min_speed`**: this benchmark stores
measurement payloads in the infractions dict, so a naive port of the official tool would wrongly
fail clean routes. The skip-set is exactly `{min_speed_infractions, ttr_dar, ttr_dar_analytic,
interaction_correctness, ic_analytic}` — `INFRACTION_SKIP_KEYS` in `build_records.py`.

**TTR / DAR** — carried where present; `ttr_dar_present` flags availability. Five models
(ADMLP, BridgeDrive, DiffAD, HiP-AD, SparseDrive V2) have **no** payload on any route; recorded
as missing, never fabricated. These columns are **unvalidated** and excluded from all headline
tooling. Do not build a claim on them without re-deriving them yourself.

**PDM-Lite** is privileged (ground-truth perception). Its rows are present, but it is excluded
from the N=17 and from every statistical test. It is seed 42 only.

**ADMLP** is a perception-free baseline, degenerate by design (~100% `Failed - TickRuntime`).
That is the result, not a bug — do not filter it out.

**Statistical protocol (locked — do not change).** The unit is one **(model, category) cell**,
17 × 3 = 51. The per-cell β-test pairs on `(scenario, route_id, seed)` with variants averaged
per side, paired Wilcoxon signed-rank. The across-cell γ-test is a paired Wilcoxon over the 51
cells with paired Cohen's `d_z`.

## 5. Findings from verification

See [`NOTES.md` §5](NOTES.md#5-findings-from-verification--read-these) (§5.1–§5.4: no
remaining diagnostic delta, the `"unknown"` sentinel, the `ood.*` rename, scipy drift). None of
them moves a published number.

## 6. Driving the paper's statistics from these records

The paper's statistics pipeline reads one CSV per model per category. `export_paper_eval_csvs.py`
writes exactly those files from the records, so the paper's own scripts run **unmodified** and
produce byte-identical output, with no re-simulation and no access to the raw result tree:

```bash
python export_paper_eval_csvs.py \
    --records ood_perceptionbench_records_v0.9.csv \
    --out-dir <paper-repo>/eval
```

`reproduce_table1.py` (check 5) proves this end to end on every run.

## 7. Files

Besides the artifact files in §1 and the five check scripts in §3 (run together by `verify.sh`):
`build_records.py` is **the generator** and the only thing that writes the artifacts;
`export_paper_eval_csvs.py` repoints the paper's statistics at the records (§6);
[`SCHEMA.md`](SCHEMA.md) is the column-by-column schema; [`NOTES.md`](NOTES.md) holds the
verification findings, caveats, history and review guide; `VERSION` is the version stamp.

## 8. Guarantees

- The raw results tree (`--results-root`) is **opened read-only**; the generator has no write
  path outside `--out-dir`.
- Seeds 42, 43, 44 — the paper's 3-seed average-per-route. Seed 42 is derived independently
  from the raw result tree and validated cell-for-cell against the frozen paper CSVs; seeds
  43/44 mirror the frozen eval (the authoritative 3-seed analysis snapshot). Only PDM-Lite (the
  ceiling reference) is seed-42-only; Driving Score and the OOD-collision metric are full
  3-seed. See [`SCHEMA.md`](SCHEMA.md) and the `seed_note` in `*.meta.json`.
- Nothing was re-simulated and nothing was re-cooked to produce these records.
- The published CSV and `meta.json` were written by one invocation of the bundled
  `build_records.py` against the raw result tree. Nothing was edited afterwards, and
  `check_meta.py` is what keeps that true.

The checks share a code lineage with the generator; they are not an independent
reimplementation. See [`NOTES.md`](NOTES.md#6-known-caveats) for caveats and
[where a reviewer should look first](NOTES.md#7-what-an-independent-reviewer-should-attack).
