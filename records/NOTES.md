# OOD-PerceptionBench records — verification notes

**Bundle version:** `v0.9` · **Binds to:** arXiv v1

This file is the background to [`README.md`](README.md). It records what verifying the records
found, the checks run once outside `verify.sh`, the current results of `verify.sh`, the known
caveats, and where an independent reviewer should look first. You do not need it to load or
use the records; read it if you want to audit them. Section numbers in §5 are kept stable
because code comments and [`SCHEMA.md`](SCHEMA.md) cite them.

## 1. Artifact history

### The parquet copy was removed

> **A parquet copy used to ship here and was removed on 2026-08-11.** It was lossy and silently
> so: `reaction_value` and `reaction_threshold` are *mixed* — 4,731 rows hold a number, 116 hold
> a categorical string (`"lane_change"`, `"-1 -> 2"`) — and typing them numeric coerced all 116
> to null, so **232 values present in the CSV were absent from the parquet**. Nothing compared
> the two artifacts, so it shipped that way. One artifact and a documented loader is a better
> trade than two artifacts and a consistency check we did not have. No headline number was ever
> affected: both columns are TTR/DAR secondary metrics, which the paper does not use.

Check 2 (`load.py`) is the check that did not exist when the parquet silently dropped 232
values. `pyarrow` is no longer required by anything here.

### The generator is the only thing allowed to write these files

One invocation writes **both** artifacts — `.csv` and `.meta.json` — and the meta records the
sha256 and byte size of the CSV plus the sha256 of `build_records.py` and of `rename_map.json`
themselves. So the artifact is bound to the exact code that produced it.

**Do not post-process the output.** If the released table needs to change, change the generator
and re-run it. This is not a style preference: it is the defect this bundle already shipped
once. The `vehicle.ood.*` rename was originally applied by hand to a generated CSV — the data
came out correct, but `meta.json` went on declaring a stale sha256, a stale byte count and a
65-entry column list, the validator reported 8,395 unexplained mismatches, and `verify.sh`
failed at its first step. Check 1 (`check_meta.py`) exists to catch exactly that, and the
rename now lives inside `build_records.py`.

## 2. Additional checks run once (not in `verify.sh`)

- **Success Rate cross-validated independently** against the `success` column that the
  authors' original analysis tool (in the private working tree, not shipped) computes by its
  own separate code path: **24,700 rows compared, 0 mismatches.**
- **Determinism** — the cross-model `agent_type` map is set-based and therefore
  order-independent; verified identical under 3 random row shuffles.
- **Internal consistency** — `success` ⟺ (status ∈ {Completed, Perfect} ∧
  `n_infractions_scoring` = 0): 0 disagreements. `ood_agent_hit` ⟺
  (`ood_agent_collision_count` > 0): 0 disagreements. No sentinel row scores a hit.
- **No NA-like tokens** in any identity column, so a default-dtype `read_csv` cannot silently
  NaN a join key; `route_id` / `prop` / `prop_raw` survive the round-trip.
- **Source tree untouched** — 0 files modified anywhere under the raw results root.
  **Paper repo untouched** — 0 files modified.

## 3. Current verification results

These are what this bundle produces today; re-running `verify.sh` regenerates them.

| # | Script | Result |
|---|---|---|
| 1 | `check_meta.py` | **PASS** — all digests and the 64-column list match |
| 2 | `load.py` | **PASS** — 24,700 × 64, 0 dropped |
| 3 | `validate_against_frozen.py` | **PASS** — 0 undeclared mismatches; 8,395 rows differ by the declared `ood.*` rename (§5.3), 0 declared diagnostic deltas (§5.1) |
| 4 | `reconcile_with_manifest.py` | **PASS** — 52 model-seeds × 475 = 24,700, 0 missing / 0 extra / 0 duplicate; 0 blueprint disagreements on 24,673 checkable rows |
| 5 | `reproduce_table1.py` | **PASS** — A, B and C |

Check 5 in detail:

- **A. Headline aggregates** — full nested deep-diff of `final_stats_summary.json` against the
  committed frozen file. **0 differences.**
- **B. LaTeX** — `tables/table1_headline.tex` and `table_percell_granular.tex` regenerate
  **byte-identical** to the committed files.
- **C. Per-cell table** — `final_stats_cells.csv` is identical to a same-interpreter
  recomputation from the frozen paper CSVs, and no per-cell significance verdict moves.

Every locked headline figure of the paper is asserted individually. These are the **3-seed
average-per-route** figures (seeds 42/43/44; paper commit `a52528d`):

| Figure | Expected | From records |
|---|---|---|
| mean DS drop, visual (`a`) | 5.0 | **5.047** |
| mean DS drop, geometric (`b`) | 12.8 | **12.802** |
| geometric / visual ratio | ≈2.5× | **2.537** |
| models visually robust (`K`) | 9/17 | **9/17** |
| significant geometric regression | 17/17 | **17/17** |
| cells geometric-deeper | 45/51 (88.2%) | **45/51 (0.882)** |
| γ-test p | <0.001 | **9.15e-08** |
| paired Cohen's `d_z` | 1.05 | **1.052** |
| OOD-collision rate base→visual→geometric (pp) | 18.6 → 29.5 → 46.2 | **18.55 → 29.46 → 46.16** |
| visual shift Δ collisions (pp) | +10.9 | **+10.91** |
| … its p | ≈3.2e-7 | **3.20e-07** |

**No statistic was adjusted to make these match.** The two genuine deltas found during
verification are recorded in §5 and neither moves a published number.

## 4. OOD-collision attribution details

The `fallback` step of the attribution (README §4) is what supplies a type for the four models
whose stale `statistics_manager.py` fork dropped the `ttr_dar` payload.

The sentinel is excluded when *building* the map but preserved when *filling* rows. That
asymmetry is load-bearing: a single `"unknown"` otherwise makes an otherwise-unanimous variant
look ambiguous and silently empties the whole vehicle map (§5.2). It is a deliberate
**divergence** from the authors' original collision-enrichment tool, which has no sentinel
concept — not a port of it.

`agent_type` holds the **released, post-rename** blueprint id (`vehicle.ood.armoredvan`, not
`vehicle.inkas.amv`). The attribution above runs *before* the rename, against the pre-rename
ids that the raw collision messages actually carry; the rename is applied to the column
afterwards, when the counts are already fixed. To join against the raw result tree or the
frozen analysis CSVs — both of which predate the rename — use `prop_raw`, not `agent_type`.

## 5. Findings from verification — read these

### 5.1 No diagnostic delta remains (the one historical case is resolved)

Setting aside the declared `ood.*` rename of §5.3 — which touches only the vehicle cells, only
the `agent_type` column, and no number — **every** (model, category, seed) cell now reproduces
the frozen paper CSVs exactly.

An earlier build carried one diagnostic-only delta here: for
`admlp / static / construction_obstacle / geometric_shift / route 24785 / roadclosedsign`
(seed 42), the frozen `agent_type` was blank while the records recovered
`static.prop.roadclosedsign` from the cross-model fallback (that row's result JSON has an empty
records list; `collided_with_ood_agent` is `False` on both sides, so no published number was
ever affected). The paper's collision re-enrich (paper commit `4cb5618`) resolved that variant,
so the frozen eval now carries the same value and the two sides agree — the declared delta is
gone and `validate_against_frozen.py` reports **0** of them.

### 5.2 The `"unknown"` sentinel would have silently corrupted the vehicle metric

ADMLP emits `agent_type: "unknown"` on exactly 9 vehicle rows, one per variant. Treating that
as a real type makes all 9 vehicle variants "ambiguous", empties the vehicle fallback map, and
strips `agent_type` from 976 rows — which silently zeroes the OOD-collision metric for
BridgeDrive, DiffAD, HiP-AD and SparseDrive V2 in the largest category. Caught by check 1;
fixed by `SENTINEL_AGENT_TYPES` in `build_records.py`.

### 5.3 The `ood.*` rename is a declared difference, not a mismatch

The six OOD vehicle blueprints ship under a neutral namespace (`vehicle.inkas.amv` →
`vehicle.ood.armoredvan`, and five more), so that a benchmark publishing collision rates does
not also publish live trademarks. The frozen paper CSVs predate that rename. So on
**8,395 rows** — every row across the 18 models whose resolved blueprint is one of the six —
the records' resolved blueprint id differs from the frozen file's by exactly the rename map:

```
seed 42: 2,910 (= 17 models x 162 = 2,754 + ADMLP 156)
   162 = 6 OOD props x 27 vehicle base routes; ADMLP's 156 carry the
   "unknown" sentinel per OOD prop, and a sentinel is never renamed
seeds 43/44:      5,485
        =         8,395
```

`validate_against_frozen.py` **declares** this rather than ignoring it. A row is explained only
if `records == rename_map[frozen]` for the same `rename_map.json` the generator used; the count
must come out at exactly 8,395 on a full run, and **too few fails as loudly as too many** —
losing the rename on some rows is as much a defect as over-applying it. The per-cell breakdown
prints on every run.

Why this cannot touch a number: `agent_type` is a diagnostic column. The statistics pipeline
reads `driving_score` and `collided_with_ood_agent`, and the OOD-collision attribution is
computed **inside the generator, before the rename**, against the pre-rename ids that the raw
collision messages actually contain. `collided_with_ood_agent` and `ood_agent_collision_count`
are compared against the frozen CSVs unrenamed, and agree on all 24,700 rows.

### 5.4 Provenance note — scipy version drift (not a records bug)

The paper's committed per-cell statistics file (`docs/stats/final_stats_cells.csv` in the paper
repository) **cannot be reproduced** by re-running the paper's own statistics script today,
even from the paper's own frozen CSVs: 16 of 102 per-cell p-values differ (11 `p_visual`,
5 `p_geometric`).

- Every affected cell is in the **static** category with `n_pairs` 2–10, exactly where scipy's
  exact-vs-normal-approximation Wilcoxon switch changed.
- This is **not** caused by the records — proved by check C, which recomputes from the frozen
  CSVs under the same interpreter and gets the records' values.
- **Zero of 102 significance stars move.** `K_cells` 43 = 43, geometric-significant cells
  33 = 33, `final_stats_summary.json` deep-diff = 0, and both LaTeX tables are byte-identical.

So nothing published changes, but that CSV is not bit-reproducible on a current scipy. **If you
are reproducing the per-cell table, pin your scipy version.** Reference environment used here:
`pandas 2.0.3 / numpy 1.22.0 / scipy 1.10.1`.

## 6. Known caveats

- **The TTR/DAR columns are published but unvalidated.** They are present for some models and
  absent for others, for historical reasons; they are documented as unvalidated, excluded from
  all headline tooling, and the two distinct missing-data modes are distinguished in
  [`SCHEMA.md`](SCHEMA.md). Do not build a claim on them without re-deriving them yourself.
- **These records were verified by the checks in README §3, not by an independent
  reimplementation.** The checks are strong — artifact-to-metadata integrity, row-level
  equality against the paper's frozen CSVs, full coverage against the route manifest, and
  byte-identical regeneration of the published tables — but they share a code lineage with the
  generator. §7 lists where an independent reviewer should look first.

## 7. What an independent reviewer should attack

1. `SENTINEL_AGENT_TYPES` — the asymmetry (excluded when building the map, kept when filling
   rows) is deliberate and load-bearing, and it is a **divergence** from the authors' original
   collision-enrichment tool, which has no sentinel concept at all and would fold `"unknown"`
   into the candidate set. An earlier comment in `build_records.py` wrongly claimed parity with
   that tool; it now says the opposite. Confirm that no other sentinel value exists, and that
   the divergence is the behaviour that reproduces the frozen CSVs (check 3 does).
2. The **rename ordering** in `build_records.py` — OOD-collision attribution must run before
   `apply_rename()`, because the raw collision messages name pre-rename ids. `apply_rename()`
   raises if called first, but confirm the guard actually fires, and that
   `EXPECTED_RENAME_DELTAS` in `validate_against_frozen.py` (8,395) is asserted in both
   directions.
3. `RESULT_DIR_OVERRIDES` — `uniad → uniad_base` and `pdmlite/vehicle → pdmlite_v2`. The plain
   `pdmlite` vehicle directory holds 343 stale JSONs; if the override were wrong, row counts
   would still look plausible.
4. `find_result_jsons` depth — the leading `{scenario}/{level}/` path component has already
   produced a false "0/78" once in this project's history.
5. The `variant` = `prop_raw` join in `export_paper_eval_csvs.py` — using the post-rename
   `prop` instead would silently break the pairing for 972 rows.
6. Missing-data handling — 2 rows have no record, 2 more have no leaderboard `values`; confirm
   none of these silently change a denominator.
7. That the pairing really is on `(scenario, route_id, seed)` with variants averaged per side —
   nothing in this directory implements it (the paper's unmodified script does), but the
   *export* must preserve the columns it needs.
