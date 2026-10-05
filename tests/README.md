# `tests/` — acceptance harness and smoke split

**Bundle version:** v0.9 · **Binds to:** arXiv v1

This folder checks that your install really runs the benchmark. A missing object does not crash
anything: on CARLA without the content pack, `try_spawn_actor('static.prop.roadclosedbarricade')`
returns `None`, the ego drives an empty road, and the route reports `Completed` with a
plausible score. A registered vehicle id can also resolve to a Tesla through `attribute_filter`,
and the route still completes. These tests turn both cases into a red exit code.

## Quick start

```bash
# 1. copy the split's routes out of the frozen bundle (verifies sha256 as it goes)
python3 tests/smoke/materialize.py --out /scratch/smoke_routes

# 2. PRE-FLIGHT: are the blueprints even registered?  Seconds, no agent, no GPU-hours.
#    Start CARLA first; this does not manage its lifecycle.
python3 tests/probe_blueprints.py --host localhost --port 2000     # must exit 0

# 3. run the split with any agent (see tests/configs/golden_generation.yaml.template)
python3 runner/run_benchmark.py --config /scratch/smoke.yaml --out /scratch/smoke_run

# 4. assert
python3 tests/check_acceptance.py --results-root /scratch/smoke_run --json /scratch/report.json
```

Always run the probe (step 2) before the split. It is the cheapest test here and catches most
broken installs.

## The smoke split

Nine routes from the frozen 475, defined by [`smoke/SMOKE_SPLIT.tsv`](smoke/SMOKE_SPLIT.tsv)
(path plus sha256 into `routes/`).

| # | tier | category | level | prop | asset |
|---|---|---|---|---|---|
| 1 | core | static | base | `static.prop.trafficwarning` | native |
| 2 | core | static | geometric | `static.prop.concreteroadbarrier` | shipped |
| 3 | extended | static | geometric | `static.prop.roadclosedbarricade` | shipped |
| 4 | core | static | geometric | `static.prop.roadclosedbarricade` (Town12) | shipped |
| 5 | core | pedestrian | base | `walker.pedestrian.0001` | native |
| 6 | core | pedestrian | visual | `walker.pedestrian.astronaut` | shipped |
| 7 | extended | pedestrian | visual | `walker.pedestrian.firefighter` | shipped |
| 8 | core | pedestrian | geometric | `walker.pedestrian.deliveryrobot` | shipped |
| 9 | core | vehicle | base | `vehicle.lincoln.mkz_2020` | native |

- `--tier core` runs seven routes: six that span three categories × three levels, plus the
  Town12 route. The default, `--tier all`, adds two more so that each of the five assets shipped
  in v0.9 has its own route.
- Static routes share base route 24795 and pedestrian routes share 24224, so base and shifted
  variants have the same ego route, town and weather.
- Route 4 (base route 2513) is the one exception: it is the only route set in Town12. Town11,
  12 and 13 ship in the separate `AdditionalMaps_0.9.15` download, and 301 of the 475 routes need
  them. Every other split route is in Town02, Town03 or Town04, so without route 4 an install
  missing the additional maps would pass.
- There are no static `visual_shift` or vehicle-shift routes: they need assets that cannot be
  redistributed (see [`../NOTICE`](../NOTICE) §3). Pedestrian `geometric_shift` is covered by the
  delivery robot only. A green run certifies only the shipped part of the benchmark. See
  [Coverage gaps](#coverage-gaps-at-v09).
- **Not reportable. Never publish a score from this split.** Nine routes cannot stand in for the
  full set. Every artifact it produces carries `"reportable": false`.

## The assertions

Checked for every route, in this order:

| | Assertion | What it proves |
|---|---|---|
| **A1** | **`blueprint_spawned`** | the actor that actually spawned has the `type_id` the route XML asked for |
| A2 | `criteria_attached` | the record carries a `ttr_dar` block — the criterion patch landed and its events survived the statistics manager |
| A3 | `route_completed` | status is `Completed`/`Perfect` (or matches the golden) |
| A4 | `ds_within_tolerance` | Driving Score is within the golden's *measured* tolerance |

A3 and A4 both pass on a broken install. Only A1 catches it, so **if A1 fails, stop**.

A1 reads `ttr_dar.agent_type`, which the `TTRDARCriterion` writes from the live spawned actor
(`self._agent.type_id`). With PDM-Lite it matched the route XML's blueprint id on **475/475**
routes. The expected id always comes from the route XML, never from the split's
`prop_blueprint_id` column. If the record has no `ttr_dar` block, or the criterion recorded
`"unknown"`, **A1 FAILS**; it is never reported as skipped or passed.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | every assertion passed **and** a golden bundle covered every route |
| 1 | at least one assertion FAILED |
| 2 | usage / IO / bundle-integrity error — nothing was assessed |
| 3 | **INCONCLUSIVE** — A1–A3 passed but no goldens were available, so A4 never ran |

**3 is not a pass.** Treat any non-zero exit as "this install is not known to be good".

## Goldens

v0.9 ships [`goldens/pdmlite_seed42_v0.9.golden.json`](goldens/pdmlite_seed42_v0.9.golden.json):
three independent PDM-Lite runs on CARLA 0.9.15, every route `[100.0, 100.0, 100.0]`, tolerance
±1.0 DS. It covers `base` for all three categories, `visual_shift` and `geometric_shift` for
pedestrian, and `geometric_shift` for static. `make_golden.py` builds a bundle from ≥ 2 replicate
runs, takes the tolerance from the measured spread, and refuses to write if A1–A3 fail in any
replicate. Details: [`goldens/README.md`](goldens/README.md) and
[`goldens/GENERATING.md`](goldens/GENERATING.md) (≈ 1–2 GPU-hours).

## Files

| Path | What |
|---|---|
| `smoke/SMOKE_SPLIT.tsv` | the split: paths + sha256 + tier + why each route is in it |
| `smoke/materialize.py` | copy the split out of `routes/`, verifying sha256; emits a runner manifest |
| `probe_blueprints.py` | pre-flight: registration + spawn + `type_id`, per blueprint. Needs CARLA, not a GPU sweep |
| `check_acceptance.py` | the harness: A1–A4 over a run's output |
| `make_golden.py` | build a golden bundle from replicate runs |
| `selftest.py` | 70 tests of the harness itself; no CARLA, no GPU, runs in CI |
| `configs/golden_generation.yaml.template` | runner config for the golden-generation runs |
| `goldens/` | measured v0.9 bundle, format, regeneration procedure, and ignored example |
| `reference/` | published seed-42 values for the nine routes (all `Completed`, DS 100.00). **Not a golden**: no measured tolerance, and `check_acceptance.py` does not read it |

The split's routes are not committed twice; `materialize.py` copies them from `routes/` and
checks the sha256 each time. Run the self-tests with `python3 tests/selftest.py` (or
`python3 -m unittest selftest -v`).

## Coverage gaps at v0.9

1. **Static `visual_shift` and all vehicle shifts**: no shippable asset yet (v1.0 adds one route
   per replacement asset).
2. **Vehicle blueprint tags**: only `front_vehicle_model` (`hard_break`) is exercised, not
   `cut_in_vehicle_model`, `parked_vehicle_model` or `blueprint_name`.
3. **Scenario families**: 2 of the 12 canonical scenarios; `tools/check_route_coverage.py` checks
   that every scenario class resolves.
4. **Cross-machine spread**: the bundle comes from one RTX 3090 host; the ±1.0 floor is not yet
   confirmed on a second hardware/driver stack.
