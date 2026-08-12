# `tests/` — acceptance harness and smoke split

**Bundle version:** v0.9 · **Binds to:** arXiv v1

## The failure this exists for

A missing OOD asset does not crash anything.

`try_spawn_actor('static.prop.roadclosedbarricade')` on a CARLA build without the content pack
returns `None`. The prop is simply absent. The ego drives an empty road, the route reports
`Completed`, the Driving Score is plausible, and nothing in the logs is red. Run the whole
475-route set that way and you get a complete, self-consistent, entirely meaningless result
table.

Vehicles have a sharper version of it. A blueprint id that *is* registered can still resolve to
a different actor through `attribute_filter`, so the route runs with a Tesla standing in for the
OOD vehicle — and still completes.

This directory turns both into a red exit code.

---

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

Step 2 is the cheapest test in the repo and catches the overwhelming majority of broken
installs. Run it before spending GPU-hours, every time.

---

## The smoke split

Nine routes, drawn from the frozen 475. Defined by
[`smoke/SMOKE_SPLIT.tsv`](smoke/SMOKE_SPLIT.tsv) — path plus sha256 into `routes/`, so the split
can never disagree with the benchmark definition without saying so.

| # | tier | category | level | prop | asset |
|---|---|---|---|---|---|
| 1 | core | static | base | `static.prop.trafficwarning` | native |
| 2 | core | static | geometric | `static.prop.concreteroadbarrier` | shipped |
| 3 | extended | static | geometric | `static.prop.roadclosedbarricade` | shipped |
| 4 | core | pedestrian | base | `walker.pedestrian.0001` | native |
| 5 | core | pedestrian | visual | `walker.pedestrian.astronaut` | shipped |
| 6 | extended | pedestrian | visual | `walker.pedestrian.firefighter` | shipped |
| 7 | core | pedestrian | geometric | `walker.pedestrian.boar` | shipped |
| 8 | extended | pedestrian | geometric | `walker.pedestrian.deliveryrobot` | shipped |
| 9 | core | vehicle | base | `vehicle.lincoln.mkz_2020` | native |

`--tier core` selects the six that span three categories × three levels — the minimum the
release plan asks for. The default, `--tier all`, adds three more so that **every one of the six
assets shipped in v0.9 is exercised by its own route**. That matters: the pack is per-asset, so a
split covering four of six would pass on a pack missing the other two.

Two routes were chosen per base route on purpose (24795 for static, 24224 for pedestrian): the
base and shifted variants then share an identical ego route, town and weather, so a difference is
attributable to the prop rather than to the route.

### Why no static-visual and no vehicle-shift routes

Because no v0.9 user can run them. Static `visual_shift` needs `trafficmessageboard`,
`trafficarrowboard` or `europianarrowboardtrailer`; every vehicle shift needs a `vehicle.ood.*`
asset. All twelve are non-redistributable (see `ASSETS.tsv`) and are specified dimensionally
instead. A smoke route that nobody can run is not a test, it is a guaranteed red.

**Consequence, stated plainly:** a green run here certifies that the *shipped half* of the
benchmark is installed correctly. It says nothing about the other twelve assets. Closing that gap
is v1.0 work — see [Coverage gaps](#coverage-gaps-at-v09) below.

### Not reportable

Nine routes cannot approximate a claim computed over 55 base routes; subsampling changes the
counts the paper's headline results *are*. **Never publish a score from this split.** Its value
is the goldens, not its routes. Every artifact it produces carries `"reportable": false`.

---

## The assertions

Checked for every route, in this order:

| | Assertion | What it proves |
|---|---|---|
| **A1** | **`blueprint_spawned`** | the actor that actually spawned has the `type_id` the route XML asked for |
| A2 | `criteria_attached` | the record carries a `ttr_dar` block — the criterion patch landed and its events survived the statistics manager |
| A3 | `route_completed` | status is `Completed`/`Perfect` (or matches the golden) |
| A4 | `ds_within_tolerance` | Driving Score is within the golden's *measured* tolerance |

**The ordering is the point.** A3 and A4 both pass on a broken install — that is exactly what
makes the failure silent. Only A1 does not.

### How A1 is observed

Not from the route XML, and not from any log grep. The `TTRDARCriterion` holds a reference to
the live actor the scenario spawned and writes `self._agent.type_id` into the record as
`ttr_dar.agent_type`. That is a direct observation of the spawned actor, available in the
standard leaderboard checkpoint with no sidecar process and no CARLA recorder parsing.

Verified against the published sweep: across all **475** routes with PDM-Lite, the observed
`agent_type` equals the route XML's blueprint id **475/475**, with zero fallbacks. The mechanism
is sound on every route, not only these nine.

The expectation is re-derived from the route XML on every run — never read from the split's own
`prop_blueprint_id` column — so editing the split cannot lower the bar.

### Absence of evidence is a failure

If the record carries no `ttr_dar` block, or the criterion recorded `"unknown"`, **A1 FAILS**. It
is never reported as skipped and never as passed. On an install where the asset is missing, "we
could not check" is precisely what the evidence looks like.

---

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | every assertion passed **and** a golden bundle covered every route |
| 1 | at least one assertion FAILED |
| 2 | usage / IO / bundle-integrity error — nothing was assessed |
| 3 | **INCONCLUSIVE** — A1–A3 passed but no goldens were available, so A4 never ran |

**3 is not a pass.** Script your automation on the exit code and treat anything non-zero as
"this install is not known to be good".

---

## Goldens

v0.9 ships [`goldens/pdmlite_seed42_v0.9.golden.json`](goldens/pdmlite_seed42_v0.9.golden.json),
measured on 2026-08-12 from three independent PDM-Lite output roots on CARLA 0.9.15. Every route
had replicate scores `[100.0, 100.0, 100.0]`; the largest spread was 0.0 DS and the resulting
tolerance is ±1.0 DS. The bundle covers `base` for all three categories, plus `visual_shift` and
`geometric_shift` for **pedestrian** and `geometric_shift` for **static** — every level the six
redistributable v0.9 assets permit. Static `visual_shift` and both vehicle shifts remain v1.0
work because their twelve assets do not ship.

The measured bundle ships alongside its format, generator, and procedure:

- [`goldens/README.md`](goldens/README.md) — what a golden is, and is not
- [`goldens/GENERATING.md`](goldens/GENERATING.md) — the full procedure, PDM-Lite as reference
  agent, ≈ 1–2 GPU-hours
- [`goldens/golden_schema.json`](goldens/golden_schema.json) — the file format
- [`goldens/EXAMPLE.golden.json`](goldens/EXAMPLE.golden.json) — a worked example, ignored by the
  harness by filename so it can never be mistaken for real
- [`make_golden.py`](make_golden.py) — builds a bundle from ≥ 2 replicate runs

`make_golden.py` derives the tolerance from the **measured run-to-run spread** rather than
guessing it, keeps every replicate value in the bundle so the number can be audited, and
**refuses to write anything** if A1–A3 fail in any replicate — a golden minted on a broken
install pins the breakage and makes the harness certify it forever.

For orientation, [`reference/pdmlite_seed42_reference.tsv`](reference/pdmlite_seed42_reference.tsv)
carries the published seed-42 values for these nine routes (all `Completed`, all DS 100.00).
**It is not a golden** — no cross-machine spread was ever measured for it, so it carries no
defensible tolerance. `check_acceptance.py` does not read it; `make_golden.py` reports the delta
against it as INFO only.

---

## Files

| Path | What |
|---|---|
| `smoke/SMOKE_SPLIT.tsv` | the split: paths + sha256 + tier + why each route is in it |
| `smoke/materialize.py` | copy the split out of `routes/`, verifying sha256; emits a runner manifest |
| `probe_blueprints.py` | pre-flight: registration + spawn + `type_id`, per blueprint. Needs CARLA, not a GPU sweep |
| `check_acceptance.py` | the harness: A1–A4 over a run's output |
| `make_golden.py` | build a golden bundle from replicate runs |
| `selftest.py` | 34 tests of the harness itself; no CARLA, no GPU, runs in CI |
| `configs/golden_generation.yaml.template` | runner config for the golden-generation runs |
| `goldens/` | measured v0.9 bundle, format, regeneration procedure, and ignored example |
| `reference/` | published seed-42 observations, for orientation only |

The split's routes are **not** committed a second time. They are materialised on demand from
`routes/`, with the sha256 checked every time, so a duplicate copy can never drift away from the
benchmark definition.

### Running the self-tests

```bash
python3 tests/selftest.py        # or: python3 -m unittest selftest -v
```

They build synthetic leaderboard checkpoints and drive the real scripts, asserting that a
missing asset, a Tesla substitution, a dropped criterion, an unfinalised checkpoint, a
mismatched golden and a partial golden each produce the right exit code. They are what stops the
harness from silently ceasing to detect things.

---

## Coverage gaps at v0.9

Honest list of what this does **not** cover, and what closes it:

1. **Static `visual_shift` and all vehicle shifts** — no shippable asset exists. Closed at v1.0
   by adding one route per replacement asset.
2. **Vehicle blueprint tags** — only `front_vehicle_model` (`hard_break`) is exercised. The
   `cut_in_vehicle_model`, `parked_vehicle_model` and `blueprint_name` paths are not. Closing
   this is cheap (one base route each, ≈ 30 s of simulation) and should happen alongside (1).
3. **Scenario families** — 2 of the 12 canonical scenarios are exercised. The split is a smoke
   test, not coverage; `tools/check_route_coverage.py` is what asserts every scenario class
   resolves.
4. **Cross-machine spread** — the bundle has three independent roots on one RTX 3090 host; its
   ±1.0 floor has not yet been confirmed by running the split on a second hardware/driver stack.

None of these weaken A1 on the routes that *are* in the split, which is where the defensive value
sits.
