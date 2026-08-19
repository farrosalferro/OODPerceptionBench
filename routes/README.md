# OOD-PerceptionBench — canonical routes

**Bundle version:** v0.9 · **Binds to:** arXiv v1 · **475 route XMLs** · **55 base routes**

This directory is the frozen route definition of OOD-PerceptionBench. Everything else in
the benchmark — the baseline records, the acceptance goldens, the paper's tables — is keyed
against these files.

```
static/      70 XMLs   2 scenarios
pedestrian/ 162 XMLs   4 scenarios
vehicle/    243 XMLs   6 scenarios
MANIFEST.tsv          one row per XML, with sha256
validate_routes.py    the acceptance test for this bundle
make_manifest.py      regenerates MANIFEST.tsv
EXCLUSIONS.md         the five deliberately excluded base routes
VERSION               version stamp and arXiv binding
```

## Layout

```
<category>/<scenario>/<level>/route_<base_route_id>_<prop_token>.xml
level ∈ {base, visual_shift, geometric_shift}
```

Each XML holds exactly one route and exactly one scenario, and that scenario names exactly
one OOD blueprint. The blueprint tag differs by scenario family — `obstacle_blueprint`
(static), `pedestrian_blueprint` (pedestrian), and `front_vehicle_model` /
`cut_in_vehicle_model` / `parked_vehicle_model` / `blueprint_name` (vehicle) — but the
invariant holds everywhere: **one file, one route, one prop**.

Three redundant encodings of the prop are kept deliberately consistent, and
`validate_routes.py` checks all three agree:

1. the filename suffix (`..._roadroller.xml`),
2. the `<route id="...">` attribute (equals the filename stem),
3. the blueprint value inside the scenario (`vehicle.ood.roadroller`).

## The design in one paragraph

Every base route appears at three levels with the **same** ego route, town and weather. Only
the obstacle/agent blueprint changes. `base` uses stock CARLA reference props, `visual_shift`
substitutes props of comparable size but unfamiliar appearance, `geometric_shift` substitutes
props whose shape and size are out of distribution. Within a scenario the three levels cover
an identical set of base routes and each level is a complete *route × prop* cross product, so
base-vs-visual and base-vs-geometric are paired over exactly the same support. That parity is
the whole experiment; the validator enforces it, and `EXCLUSIONS.md` explains why five base
routes are dropped from all three levels rather than patched.

Per-category prop counts:

| Category | Base routes | base props | visual props | geometric props | XMLs |
|---|---|---|---|---|---|
| static | 10 | 1 | 3 | 3 | 70 |
| pedestrian | 18 | 3 | 3 | 3 | 162 |
| vehicle | 27 | 3 | 3 | 3 | 243 |

## MANIFEST.tsv

Tab-separated, six leading `#` provenance lines, then a header row and 475 data rows:

| column | meaning |
|---|---|
| `path` | route XML, relative to this directory, forward slashes |
| `sha256` | hex digest of the file |
| `category` | `static` / `pedestrian` / `vehicle` |
| `scenario` | scenario directory name |
| `level` | `base` / `visual_shift` / `geometric_shift` |
| `base_route_id` | the numeric id shared by all variants of a route |
| `prop_blueprint_id` | the CARLA blueprint named inside the XML |

Read it with `pandas.read_csv("MANIFEST.tsv", sep="\t", comment="#")`. The per-file route key
used by the result records is simply `Path(path).stem`, e.g.
`route_24330_armoredvan`.

## Validating

```bash
python3 validate_routes.py                 # stdlib only, no arguments needed
```

The validator re-derives every manifest column from the files, re-hashes all 475 XMLs, and
asserts the counts, the structural parity, the frozen blueprint vocabulary, the frozen
base-route ids, the absence of the five excluded routes, and the absence of any
scaffolding directory or vendor/trademark token. It exits non-zero with a specific message
per failure. Optionally pass `--rename-map <path>` to additionally cross-check the vehicle
blueprint namespace against the rename manifest.

`validate_routes.py` hard-codes the expected vocabulary rather than reading it from the
tree, on purpose: it is the machine-readable definition of the benchmark at v0.9, so a
silent edit to the route XMLs cannot pass.

## Assets — read before running anything

The route XMLs reference **7 stock CARLA reference blueprints** plus **18 imported OOD
blueprints**. Of those 18, only **6 are redistributable** (`walker.pedestrian.astronaut`,
`walker.pedestrian.firefighter`, `walker.pedestrian.boar`,
`walker.pedestrian.deliveryrobot`, `static.prop.concreteroadbarrier`,
`static.prop.roadclosedbarricade`). The other **12 are not** — marketplace terms forbid
redistribution and, for several, forbid AI use outright. v0.9 therefore ships the routes and
the baseline records but **not** those 12 assets; they are specified dimensionally instead.
Replacement assets and a re-run are v1.0 work.

> **A missing blueprint fails silently.** `try_spawn_actor` returns `None`, the prop simply
> never appears, and the route still completes with a plausible Driving Score. Running this
> route set against a CARLA build that does not register every blueprint above produces
> numbers that look fine and mean nothing. Verify blueprint registration before trusting any
> result — that is what the acceptance-test goldens exist for.

The six vehicle blueprints use a neutral `vehicle.ood.*` namespace that is decoupled from
asset provenance, so a v1.0 mesh swap does not rewrite these route XMLs.

## Version discipline

v0.9 routes bind to arXiv v1. When v1.0 replaces the non-redistributable assets, scores
produced under v1.0 are **not** comparable row-for-row with v0.9 scores even where the
blueprint id is unchanged. Always report which bundle version a result came from; `VERSION`
and the `#` header of `MANIFEST.tsv` both carry it.

Protocol note: the published baselines use **three seeds (42, 43, 44)**, averaged per route
(PDM-Lite ceiling is seed 42 only). See the Protocol section of the top-level README.
