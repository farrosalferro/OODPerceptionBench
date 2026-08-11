# Records schema — `ood-perceptionbench/records/1`

**64 columns · 8,550 rows · one row per `(model, category, scenario, route_id, level, prop, seed)`**

The authoritative column list — names and order — is `columns` in
`*.meta.json`, which `build_records.py` writes from the DataFrame it just wrote
to disk. `check_meta.py` asserts it against the CSV header on every run of
`verify.sh`, so this document and the artifact cannot silently disagree.

`non-null` counts are out of 8,550 and are a property of the data, not of the
generator — see the notes. The `dtype` column is the dtype `load.py` applies; the CSV
is untyped text so that the literal token `Infinity` (1,367 occurrences in the
secondary-metric columns) survives a round-trip unchanged.

---

## Identity

| column | dtype | non-null | notes |
|---|---|---|---|
| `model` | string | 8550 | eval key: 17 E2E + `pdmlite` |
| `category` | string | 8550 | `pedestrian` / `static` / `vehicle` |
| `scenario` | string | 8550 | CARLA scenario family |
| `route_id` | string | 8550 | base route id (string — do not coerce) |
| `level` | string | 8550 | `base` / `visual_shift` / `geometric_shift` |
| `prop` | string | 8550 | **post-rename** prop token (the `ood.*` namespace); joins to the route manifest |
| `seed` | int64 | 8550 | always 42 |
| `prop_raw` | string | 8550 | on-disk token, pre-rename (`sedane`, `amv`). **Join key for the raw result tree.** |
| `variant` | string | 8550 | legacy alias of `prop_raw`, kept for existing tooling |
| `scenario_name` | string | 8548 | e.g. `DynamicObjectCrossingModified_1` |
| `town_name` | string | 8548 | |
| `weather_id` | string | 8548 | |

> The 2 rows short of 8,550 have an **empty `records` list** in their result
> JSON — an infrastructure failure that produced no record at all. They are
> retained (dropping them would silently change a denominator). Both are
> identified in `*.meta.json`; they are
> `admlp / static / construction_obstacle / geometric_shift / 24785 / roadclosedsign`
> and
> `bridgedrive / static / construction_obstacle_two_ways / visual_shift / 1833 / europianarrowboardtrailer`
> (the latter is the known BridgeDrive silent-hang route).

## Primary outcome

| column | dtype | non-null | notes |
|---|---|---|---|
| `status` | string | 8548 | `Completed`, `Perfect`, `Failed - TickRuntime`, … |
| `success` | boolean | 8550 | Bench2Drive SR (Eq. 1). Skip-set is **not** just `min_speed` — it is `INFRACTION_SKIP_KEYS` in `build_records.py` |
| `score_composed` | float64 | 8548 | **Driving Score.** `score_route × score_penalty` |
| `score_route` | float64 | 8548 | Route Completion |
| `score_penalty` | float64 | 8548 | Infraction Penalty |
| `driving_score` | float64 | 8546 | leaderboard aggregate label; equals `score_composed` per route. **This is the column the paper's statistics read.** |
| `route_completion` | float64 | 8546 | leaderboard aggregate label |
| `infraction_penalty` | float64 | 8546 | leaderboard aggregate label |

> `driving_score` is non-null on 8,546 vs `score_composed`'s 8,548: two further
> rows have a record but no leaderboard `values` block — both `hydra_next`
> (`Failed - Simulation crashed` and `Failed - Agent crashed`), which still
> carry a valid `score_composed`. Both columns are shipped so a consumer can see
> the difference rather than guess. The paper's statistics read `driving_score`
> and `load_cell` drops its NaNs, so those two rows are excluded there — this is
> the frozen behaviour and is reproduced exactly.

## OOD-collision attribution — the paper's second headline metric

| column | dtype | non-null | notes |
|---|---|---|---|
| `ood_agent_hit` | boolean | 8550 | ≥1 collision whose actor type is the OOD prop's own |
| `collided_with_ood_agent` | boolean | 8550 | identical; the name the frozen analysis uses |
| `ood_agent_collision_count` | int64 | 8550 | number of such collision events |
| `agent_type` | string | 8550 | resolved OOD actor type, **post-rename** — the released `vehicle.ood.*` blueprint ids. Joins to `prop_blueprint_id` in `../routes/MANIFEST.tsv` |
| `agent_type_source` | string | 8550 | `record` (6,635) / `fallback` (1,906) / `sentinel` (9) |

`sentinel` = the literal `"unknown"` written by the criterion when it could not
identify the actor (9 ADMLP vehicle rows). Preserved, never back-filled; those
rows score 0 hits. Excluding it when *building* the inference map is load-bearing
— see README §5.2.

> **`agent_type` carries one id, and it is the released one.** An earlier draft
> of this schema shipped a redundant second column, `agent_type_renamed`, holding
> the post-rename value while `agent_type` kept the original vendor id. That is
> gone: 2,910 rows would have published a live trademark (`vehicle.inkas.amv`,
> `vehicle.caterpillar.dumptruck`, `vehicle.hamm.roadroller`) in a column of a
> benchmark that reports collision rates. The rename now happens inside
> `build_records.py`, in place, driven by the bundled `rename_map.json`.
>
> The pre-rename id is **not** lost: `prop_raw` keeps the on-disk token, and
> `rename_map.json` inverts the blueprint mapping. If you are joining against the
> raw result tree or against the frozen analysis CSVs, both of which predate the
> rename, join on `prop_raw`, not on `agent_type`.
>
> Ordering note for anyone modifying the generator: the OOD-collision attribution
> runs **before** the rename, because the collision messages in the raw JSONs name
> the pre-rename ids. Renaming first would zero the second headline metric while
> leaving the column looking perfectly well-formed. `apply_rename()` raises rather
> than let that happen.
>
> One place the old name survives, correctly: `meta.json` → `rename_stats` →
> `"agent_type_renamed": 2910`. That is a **counter** — the number of rows whose
> `agent_type` the rename changed, alongside `prop_renamed` / `prop_unchanged` —
> not a column. The authoritative column list is `meta.json` → `columns`, and
> `check_meta.py` asserts it against the CSV header.

## Infraction event counts

`n_<key>` for each of the 12 leaderboard infraction keys, `int64`, non-null on
all 8,550:

`n_collisions_layout` · `n_collisions_pedestrian` · `n_collisions_vehicle` ·
`n_red_light` · `n_stop_infraction` · `n_outside_route_lanes` ·
`n_min_speed_infractions` · `n_yield_emergency_vehicle_infractions` ·
`n_scenario_timeouts` · `n_route_dev` · `n_vehicle_blocked` · `n_route_timeout`

| column | dtype | notes |
|---|---|---|
| `n_infractions_scoring` | int64 | total over the keys **not** in the SR skip-set. `success` ⟺ status is Completed/Perfect **and** this is 0 |

## Leaderboard infraction rates

Per-km / aggregate rates from the leaderboard `values` block (distinct from the
raw counts above), all `float64`, non-null 8546:

`collisions_pedestrians` · `collisions_vehicles` · `collisions_layout` ·
`off_road_infractions`

## Run metadata

| column | dtype | non-null |
|---|---|---|
| `route_length` | float64 | 8548 |
| `duration_game` | float64 | 8548 |
| `duration_system` | float64 | 8548 |

## Secondary metrics (TTR / DAR) — **UNVALIDATED**

Carried verbatim from `record['ttr_dar']` (top level, not nested). Excluded from
all headline tooling. Missing is recorded as missing and never fabricated.

| column | dtype | non-null | notes |
|---|---|---|---|
| `ttr_dar_present` | boolean | 8550 | payload existed at all |
| `ttr` | float64 | 4847 | time-to-react |
| `dar` | float64 | 4847 | distance-at-react |
| `ttc_at_reaction` | float64 | 4683 | |
| `reaction_detected` | boolean | 6644 | |
| `t_obs_frame` | float64 | 6590 | |
| `t_react_frame` | float64 | 4847 | |
| `closing_velocity` | float64 | 4847 | |
| `reaction_cause` | string | 4847 | e.g. `P2_deceleration` |
| `reaction_value` | float64 | 4847 | |
| `reaction_threshold` | float64 | 4847 | |
| `v_start` | float64 | **0** | never populated by the criterion — column kept for schema stability |
| `v_end` | float64 | **0** | ditto |
| `final_distance` | float64 | 1752 | |
| `final_closing_velocity` | float64 | 1752 | |
| `final_ttc` | float64 | 85 | |
| `num_reactions` | int64 | 8550 | 0 where no payload |
| `all_reactions` | string | 4847 | JSON blob |

### Coverage caveat — two distinct failure modes, do not conflate

**Payload entirely absent** (`ttr_dar_present = 0.0` on all 475 routes):

`bridgedrive` · `diffad` · `hipad` · `sparsedrive_v2`

These four ran through a stale `statistics_manager.py` fork that silently
dropped the criterion events. There is no `ttr_dar` key at all.

**Payload present but every field null** (`ttr_dar_present = 0.9979`):

`admlp` — the criterion attached and terminated, but ADMLP is ~100%
`Failed - TickRuntime`, so nothing was ever measured. `ttr`/`dar` are empty on
all 475 rows even though the dict exists. (The 0.21% gap is the single
empty-record row above.)

The remaining 13 models are at `1.0`. Both modes are expected, neither is a
defect, and neither is fabricated. Distinguish them with `ttr_dar_present`
(structure) versus `ttr`/`dar` non-null (measurement). Per-model coverage is in
`*.meta.json` under `ttr_dar_present_by_model`.

## Provenance

| column | dtype | notes |
|---|---|---|
| `source_relpath` | string | path of the source JSON **relative to the results root**. Deliberately not absolute, so the records stay portable |
