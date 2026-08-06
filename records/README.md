# OOD-PerceptionBench — derived per-route records (Tier A)

**Bundle version:** `v0.9` · **Binds to:** arXiv v1 · **Seed:** 42 only

This directory is Tier A of the release: the per-route baseline records for all
18 evaluated agents. It lets anyone **re-verify every number in the paper
without a GPU**, and compare a new model against all 17 baselines **without
re-simulating anything**.

The records are a few MB. They ship in-repo; no external hosting, no Zenodo.

---

## 1. The artifact

| File | What |
|---|---|
| `ood_perceptionbench_records_v0.9.csv` | the tidy table, 8,550 rows × 64 columns |
| `ood_perceptionbench_records_v0.9.parquet` | same rows, typed schema |
| `ood_perceptionbench_records_v0.9.meta.json` | version stamp, provenance, sha256 of each artifact **and of the generator**, reconciliation report |
| `rename_map.json` | the `ood.*` blueprint rename, applied by the generator and re-derived by check 2 |

**One row per `(model, category, scenario, route_id, level, prop, seed)`.**

```
18 models x 475 canonical routes = 8,550 rows
   475 = 70 static + 162 pedestrian + 243 vehicle
   18  = 17 end-to-end models + PDM-Lite (privileged ceiling)
```

The CSV is the faithful artifact and is what the verification scripts compare;
the parquet is the same data with a real dtype schema (numerics as `float64`
with IEEE `inf` preserved, flags as nullable booleans) for analysis.

The parquet's dtypes come from `typed_frame()` in `build_records.py`, not from
letting a reader infer them. That distinction matters: identity columns stay
**strings** on purpose. `route_id` inferred as `int64` and `weather_id` as
`float64` are wrong — they are labels, they are never arithmetic, and coercing
them breaks the join against `../routes/MANIFEST.tsv`. Likewise the float
columns are carried from the source JSONs rather than re-parsed from the CSV
text, so no value is a decimal round-trip away from what the simulator emitted.
`SCHEMA.md` lists the intended dtype for every column.

### Version discipline

Every artifact carries `version: v0.9` / `binds_to: arXiv v1` in
`*.meta.json` and in `VERSION`. v1.0 records will bind to arXiv v2 and will
**not** be comparable to these — the v1.0 asset replacement re-runs 12 of the 18
props. Do not mix the two score sets.

---

## 2. Regenerating

```bash
python build_records.py --results-root <root of the raw result tree> --out-dir .
```

Runtime ≈ 5–8 min for the full 18-model scan (8,550 JSONs). The results root is
opened **read-only**; the script never writes outside `--out-dir`.

`--rename-map` defaults to the `rename_map.json` bundled in this directory, so
the published artifact is reproducible from this repository alone — no input
lives outside the bundle.

`--models tcp` restricts to one model for development. A subset run narrows the
cross-model `agent_type` inference (§4) and is **not** valid for the release
artifact — `meta.json` records `partial_run: true` when this happens.

### The generator is the only thing allowed to write these files

One invocation writes **all three** artifacts — `.csv`, `.parquet` and
`.meta.json` — and the meta records the sha256 and byte size of the first two
plus the sha256 of `build_records.py` and of `rename_map.json` themselves. So
the artifact is bound to the exact code that produced it.

**Do not post-process the output.** If the released table needs to change, change
the generator and re-run it. This is not a style preference: it is the defect
this bundle already shipped once. The `vehicle.ood.*` rename was originally
applied by hand to a generated CSV — the data came out correct, but `meta.json`
went on declaring a stale sha256, a stale byte count and a 65-entry column list,
the validator reported 2,910 unexplained mismatches, and `verify.sh` failed at
its first step. Check 1 (`check_meta.py`) exists to catch exactly that, and the
rename now lives inside `build_records.py`.

---

## 3. Verifying

```bash
./verify.sh <paper-repo> <scratch-dir>
```

Four independent checks, all of which must pass:

| # | Script | Asserts | Result |
|---|---|---|---|
| 1 | `check_meta.py` | `meta.json` describes the files that are actually here: sha256 + byte size of the CSV and parquet, `n_rows`, `n_columns`, and the `columns` list against the CSV header in order | **PASS** — all digests and the 64-column list match |
| 2 | `validate_against_frozen.py` | every one of the 8,550 rows matches the frozen per-model CSVs that back the paper, on all 35 metric-bearing columns | **PASS** — 0 undeclared mismatches; 2,910 rows differ by the declared `ood.*` rename (§5.3) and 1 by a declared diagnostic delta (§5.1) |
| 3 | `reconcile_with_manifest.py` | each model covers each of the 475 canonical routes in `../routes/MANIFEST.tsv` exactly once; blueprint ids agree with the route XMLs | **PASS** — 18 × 475, 0 missing / 0 extra / 0 duplicate; 0 blueprint disagreements on 8,541 checkable rows |
| 4 | `reproduce_table1.py` | **Table 1 regenerates exactly** — see below | **PASS** — A, B and C |

Reference environment: `pandas 2.0.3 / numpy 1.22.0 / scipy 1.10.1 / pyarrow
17.0.0`. Check 4 runs the paper's statistics scripts and therefore needs
`scipy`; the other three do not.

The results in that table are what this bundle produces today, not an
aspiration — they are regenerated by re-running `verify.sh`, and every one of
them is reproduced in the sections below with its actual counts.

### Additional checks run once (not in `verify.sh`)

- **Success Rate cross-validated independently** against the `success` column that
  the authors' original analysis tool (in the private working tree, not shipped)
  computes by its own separate code path: **8,550 rows compared, 0 mismatches.**
- **Determinism** — the cross-model `agent_type` map is set-based and therefore
  order-independent; verified identical under 3 random row shuffles.
- **Internal consistency** — `success` ⟺ (status ∈ {Completed, Perfect} ∧
  `n_infractions_scoring` = 0): 0 disagreements. `ood_agent_hit` ⟺
  (`ood_agent_collision_count` > 0): 0 disagreements. No sentinel row scores a
  hit.
- **No NA-like tokens** in any identity column, so a default-dtype `read_csv`
  cannot silently NaN a join key; `route_id` / `prop` / `prop_raw` survive the
  round-trip.
- **Source tree untouched** — 0 files modified anywhere under the raw results root.
  **Paper repo untouched** — 0 files modified.

### The acceptance test that matters

`reproduce_table1.py` copies the paper's statistics scripts **unmodified** into a
scratch tree whose `eval/` is materialised from the records, runs them, and
compares three ways:

- **A. Headline aggregates** — full nested deep-diff of `final_stats_summary.json`
  against the committed frozen file. **0 differences.**
- **B. LaTeX** — `tables/table1_headline.tex` and `table_percell_granular.tex`
  regenerate **byte-identical** to the committed files.
- **C. Per-cell table** — `final_stats_cells.csv` is identical to a
  same-interpreter recomputation from the frozen paper CSVs, and no per-cell
  significance verdict moves.

Every locked headline figure of the paper is asserted individually:

| Figure | Expected | From records |
|---|---|---|
| mean DS drop, visual (`a`) | 4.9 | **4.930** |
| mean DS drop, geometric (`b`) | 13.2 | **13.221** |
| geometric / visual ratio | ≈2.7× | **2.682** |
| models visually robust (`K`) | 12/17 | **12/17** |
| significant geometric regression | 14/17 | **14/17** |
| cells geometric-deeper | 46/51 (90.2%) | **46/51 (0.902)** |
| γ-test p | <0.001 | **1.86e-08** |
| paired Cohen's `d_z` | 1.14 | **1.139** |
| OOD-collision rate base→visual→geometric (pp) | 18.9 → 29.5 → 46.1 | **18.93 → 29.54 → 46.07** |
| visual shift Δ collisions (pp) | +10.6 | **+10.61** |
| … its p | ≈8.8e-7 | **8.83e-07** |

**No statistic was adjusted to make these match.** The two genuine deltas found
during verification are recorded in §5 and neither moves a published number.

---

## 4. Metric definitions (read before reusing)

**Driving Score / DS** — `score_composed` on the route record. `driving_score`
is the leaderboard's aggregate label and is identical per route; the paper's
pipeline reads `driving_score`, and both columns are shipped.

**ΔDS** = `mean(DS at shift) − mean(DS at base)` per (model, category) cell.
Negative ⇒ regression. The paper's headline `a`/`b` are the *signed DS drop*
(`−mean ΔDS`, positive ⇒ regression).

**OOD-collision hit** (`ood_agent_hit`, alias `collided_with_ood_agent`) — the
paper's **second headline metric**, not in the stock leaderboard. True iff at
least one collision message in the route record names the OOD prop's own actor
type. Attribution:

1. `agent_type` from the record's `ttr_dar` payload where present (`record`);
2. otherwise back-filled from a **cross-model** `variant → agent_type` map,
   built only from variants on which every model that recorded a type agrees
   (`fallback`). This is what supplies a type for the four models whose stale
   `statistics_manager.py` fork dropped the payload;
3. the literal sentinel `"unknown"` is **left in place, never back-filled**
   (`sentinel`) — 9 ADMLP vehicle rows. Those rows score 0 hits, as they must.

The sentinel is excluded when *building* the map but preserved when *filling*
rows. That asymmetry is load-bearing: a single `"unknown"` otherwise makes an
otherwise-unanimous variant look ambiguous and silently empties the whole
vehicle map (§5.2). It is a deliberate **divergence** from the authors' original
collision-enrichment tool, which has no sentinel concept — not a port of it.

`agent_type` holds the **released, post-rename** blueprint id
(`vehicle.ood.armoredvan`, not `vehicle.inkas.amv`). The attribution above runs
*before* the rename, against the pre-rename ids that the raw collision messages
actually carry; the rename is applied to the column afterwards, when the counts
are already fixed. To join against the raw result tree or the frozen analysis
CSVs — both of which predate the rename — use `prop_raw`, not `agent_type`.

**Success Rate** (`success`) — Bench2Drive Eq. 1: status ∈ {Completed, Perfect}
**and** every infraction list empty. The skip-set is **not just `min_speed`**:
this benchmark stuffs measurement payloads (`ttr_dar`, `ttr_dar_analytic`,
`interaction_correctness`, `ic_analytic`) into the infractions dict, and a naive
port of the official tool would wrongly fail clean routes. The skip-set is exactly
`{min_speed_infractions, ttr_dar, ttr_dar_analytic, interaction_correctness,
ic_analytic}` — `INFRACTION_SKIP_KEYS` in `build_records.py`.

**TTR / DAR** — carried where present, `ttr_dar_present` flags availability.
Five models (ADMLP, BridgeDrive, DiffAD, HiP-AD, SparseDrive V2) have **no**
payload on any route; recorded as missing, never fabricated. These columns are
**unvalidated** and excluded from all headline tooling.

**PDM-Lite** is privileged (ground-truth perception). Its rows are present, but
it is excluded from the N=17 and from every statistical test.

**ADMLP** is a perception-free baseline, degenerate by design (~100%
`Failed - TickRuntime`). That is the result, not a bug — do not filter it out.

### Statistical protocol (locked — do not change)

Unit is one **(model, category) cell**, 17 × 3 = 51. Per-cell β-test pairs on
`(scenario, route_id, seed)` with variants averaged per side, paired Wilcoxon
signed-rank. Across-cell γ-test is a paired Wilcoxon over the 51 cells with
paired Cohen's `d_z`.

---

## 5. Findings from verification — read these

### 5.1 One diagnostic-only delta vs the frozen CSVs (records are *more* correct)

Setting aside the declared `ood.*` rename of §5.3 — which touches only the 18
vehicle cells, only the `agent_type` column, and no number — 53 of 54
(model, category) cells reproduce the frozen paper CSVs **exactly**. The 54th
differs in **one row, one column**:

```
admlp / static / construction_obstacle / geometric_shift / route 24785 / roadclosedsign
    agent_type:  records = "static.prop.roadclosedsign"    frozen = "" (empty)
```

That row's result JSON has an **empty records list** (an infrastructure
failure): no status, no score, no collisions. `collided_with_ood_agent` is
`False` on both sides, so **no published number is affected**. The records value
is the correct one — 139 other rows recover `static.prop.roadclosedsign` for
that variant directly from their own JSON, and 41 more resolve to it by
fallback. The frozen file simply had a weaker map when it was written.

### 5.2 The `"unknown"` sentinel would have silently corrupted the vehicle metric

ADMLP emits `agent_type: "unknown"` on exactly 9 vehicle rows, one per variant.
Treating that as a real type makes all 9 vehicle variants "ambiguous", empties
the vehicle fallback map, and strips `agent_type` from 976 rows — which silently
zeroes the OOD-collision metric for BridgeDrive, DiffAD, HiP-AD and SparseDrive
V2 in the largest category. Caught by check 1; fixed by
`SENTINEL_AGENT_TYPES` in `build_records.py`.

### 5.3 The `ood.*` rename is a declared difference, not a mismatch

The six OOD vehicle blueprints ship under a neutral namespace
(`vehicle.inkas.amv` → `vehicle.ood.armoredvan`, and five more), so that a
benchmark publishing collision rates does not also publish live trademarks. The
frozen paper CSVs predate that rename. So on **2,910 rows** — every row across
the 18 models whose resolved blueprint is one of the six — the records' resolved
blueprint id differs from the frozen file's by exactly the rename map:

```
17 models x 162 = 2,754   (162 = 6 OOD props x 27 vehicle base routes)
        + ADMLP     156   (one row per OOD prop carries the "unknown" sentinel,
                           and a sentinel is never renamed)
        =         2,910
```

`validate_against_frozen.py` **declares** this rather than ignoring it. A row is
explained only if `records == rename_map[frozen]` for the same
`rename_map.json` the generator used; the count must come out at exactly 2,910
on a full run, and **too few fails as loudly as too many** — losing the rename
on some rows is as much a defect as over-applying it. The per-cell breakdown
prints on every run.

Why this cannot touch a number: `agent_type` is a diagnostic column. The
statistics pipeline reads `driving_score` and `collided_with_ood_agent`, and the
OOD-collision attribution is computed **inside the generator, before the
rename**, against the pre-rename ids that the raw collision messages actually
contain. `collided_with_ood_agent` and `ood_agent_collision_count` are compared
against the frozen CSVs unrenamed, and agree on all 8,550 rows.

### 5.4 Provenance note — scipy version drift (not a records bug)

The paper's committed per-cell statistics file (`docs/stats/final_stats_cells.csv`
in the paper repository) **cannot be reproduced** by re-running the paper's own
statistics script today, even from the paper's own frozen CSVs: 16 of 102 per-cell
p-values differ (11 `p_visual`, 5 `p_geometric`).

- Every affected cell is in the **static** category with `n_pairs` 2–10, exactly
  where scipy's exact-vs-normal-approximation Wilcoxon switch changed.
- This is **not** caused by the records — proved by check C, which recomputes
  from the frozen CSVs under the same interpreter and gets the records' values.
- **Zero of 102 significance stars move.** `K_cells` 43 = 43, geometric-significant
  cells 33 = 33, `final_stats_summary.json` deep-diff = 0, and both LaTeX tables
  are byte-identical.

So nothing published changes, but that CSV is not bit-reproducible on a current
scipy. **If you are reproducing the per-cell table, pin your scipy version.**
Reference environment used here: `pandas 2.0.3 / numpy 1.22.0 / scipy 1.10.1`.

---

## 6. Driving the paper's statistics from these records

The paper's statistics pipeline consumes one CSV per model per category. That is
the seam: `export_paper_eval_csvs.py` materialises exactly those files from the
published records, so the paper's own scripts then run **unmodified** and produce
byte-identical output. No re-simulation, and no access to the raw result tree.

```bash
python export_paper_eval_csvs.py \
    --records ood_perceptionbench_records_v0.9.csv \
    --out-dir <paper-repo>/eval
```

`reproduce_table1.py` proves exactly this end-to-end on every run: it copies the
paper's scripts into a scratch tree fed only by the records, and deep-diffs the
result against the committed frozen artefacts.

---

## 7. Files

| File | Role |
|---|---|
| `build_records.py` | **the generator** — the one documented entry point, and the only thing that writes the three artifacts |
| `rename_map.json` | the `ood.*` blueprint rename the generator applies; bundled so the artifact is reproducible from this repo alone |
| `export_paper_eval_csvs.py` | repoint the paper's statistics at the records |
| `check_meta.py` | check 1 — `meta.json` vs the artifacts actually present |
| `validate_against_frozen.py` | check 2 — row-level vs the paper's frozen CSVs |
| `reconcile_with_manifest.py` | check 3 — coverage vs `../routes/MANIFEST.tsv` |
| `reproduce_table1.py` | check 4 — **the acceptance test** |
| `verify.sh` | runs all four |
| `SCHEMA.md` | column-by-column schema |
| `VERSION` | version stamp |

## 7b. Known caveats

- **The TTR/DAR columns are published but unvalidated.** They are present for some
  models and absent for others, for historical reasons; they are documented as
  unvalidated, excluded from all headline tooling, and the two distinct
  missing-data modes are distinguished in `SCHEMA.md`. Do not build a claim on
  them without re-deriving them yourself.
- **These records were verified by the four checks in §3, not by an independent
  reimplementation.** The checks are strong — artifact-to-metadata integrity,
  row-level equality against the paper's frozen CSVs, full coverage against the
  route manifest, and byte-identical regeneration of the published tables — but
  they share a code lineage with the generator. §9 lists where an independent
  reviewer should look first.

## 9. What an independent reviewer should attack

1. `SENTINEL_AGENT_TYPES` — the asymmetry (excluded when building the map, kept
   when filling rows) is deliberate and load-bearing, and it is a **divergence**
   from the authors' original collision-enrichment tool, which has no sentinel
   concept at all and would fold `"unknown"` into the candidate set. An earlier
   comment in `build_records.py` wrongly claimed parity with that tool; it now
   says the opposite. Confirm that no other sentinel value exists, and that the
   divergence is the behaviour that reproduces the frozen CSVs (check 2 does).
2. The **rename ordering** in `build_records.py` — OOD-collision attribution
   must run before `apply_rename()`, because the raw collision messages name
   pre-rename ids. `apply_rename()` raises if called first, but confirm the
   guard actually fires, and that `EXPECTED_RENAME_DELTAS` in
   `validate_against_frozen.py` (2,910) is asserted in both directions.
3. `RESULT_DIR_OVERRIDES` — `uniad → uniad_base` and `pdmlite/vehicle →
   pdmlite_v2`. The plain `pdmlite` vehicle directory holds 343 stale JSONs; if
   the override were wrong, row counts would still look plausible.
4. `find_result_jsons` depth — the leading `{scenario}/{level}/` path component
   has already produced a false "0/78" once in this project's history.
5. The `variant` = `prop_raw` join in `export_paper_eval_csvs.py` — using the
   post-rename `prop` instead would silently break the pairing for 972 rows.
6. Missing-data handling — 2 rows have no record, 2 more have no leaderboard
   `values`; confirm none of these silently change a denominator.
7. That the pairing really is on `(scenario, route_id, seed)` with variants
   averaged per side — nothing in this directory implements it (the paper's
   unmodified script does), but the *export* must preserve the columns it needs.

## 8. Guarantees

- The raw results tree (`--results-root`) is **opened read-only**; the generator
  has no write path outside `--out-dir`.
- Seed 42 only. The multiseed tree is a paper-side robustness appendix and is
  deliberately **not** part of this release.
- Nothing was re-simulated and nothing was re-cooked to produce these records.
- The published CSV, parquet and `meta.json` were all written by one invocation
  of the bundled `build_records.py` against the raw result tree. Nothing was
  edited afterwards, and `check_meta.py` is what keeps that true.
