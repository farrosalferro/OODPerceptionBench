# OOD-PerceptionBench — canonical routes

**Bundle version:** v0.9 · **Binds to:** arXiv v1 · **475 route XMLs** · **55 base routes**

This folder is the frozen route set. The baseline records, the acceptance goldens and the
paper's tables are all keyed to these files.

| Category | Scenarios | Base routes | base props | visual props | geometric props | XMLs |
|---|---|---|---|---|---|---|
| static | 2 | 10 | 1 | 3 | 3 | 70 |
| pedestrian | 4 | 18 | 3 | 3 | 3 | 162 |
| vehicle | 6 | 27 | 3 | 3 | 3 | 243 |

```
<category>/<scenario>/<level>/route_<base_route_id>_<prop_token>.xml
level ∈ {base, visual_shift, geometric_shift}
```

Every base route appears at all three levels with the **same** ego route, town and weather;
only the obstacle/agent blueprint changes. Each level is a complete *route × prop* cross
product, so base-vs-visual and base-vs-geometric are paired over exactly the same routes. Each
XML holds **one route, one scenario, one prop**. The prop is encoded three ways, which must
agree: the filename suffix, the `<route id>` (equal to the filename stem), and the blueprint
inside the scenario (`obstacle_blueprint`, `pedestrian_blueprint`, or for vehicles
`front_vehicle_model` / `cut_in_vehicle_model` / `parked_vehicle_model` / `blueprint_name`).

## Check the routes

```bash
python3 validate_routes.py                 # stdlib only, no arguments needed
```

It re-hashes all 475 XMLs, re-derives every manifest column, and checks the counts, the
three-level parity, the frozen blueprint vocabulary and base-route ids. It also checks that the
five excluded routes, scaffolding directories and vendor/trademark tokens are absent. It exits
non-zero with a message per failure. `--rename-map <path>` also cross-checks the vehicle
`vehicle.ood.*` namespace against the rename manifest.

## Files

| File | What |
|---|---|
| `MANIFEST.tsv` | tab-separated; six `#` provenance lines, a header, then one row per XML: `path`, `sha256`, `category`, `scenario`, `level`, `base_route_id`, `prop_blueprint_id`. Read with `pandas.read_csv("MANIFEST.tsv", sep="\t", comment="#")`. The route key used in the records is `Path(path).stem`, e.g. `route_24330_armoredvan`. |
| `validate_routes.py` | the acceptance test for this folder; it hard-codes the v0.9 vocabulary on purpose |
| `make_manifest.py` | regenerates `MANIFEST.tsv` |
| [`EXCLUSIONS.md`](EXCLUSIONS.md) | the five base routes left out of all three levels, and why |
| `VERSION` | version stamp and arXiv binding |

## Assets — read before running anything

The XMLs use **7 stock CARLA blueprints** plus **18 new OOD blueprints**. Only **5** of the 18
are redistributable (`walker.pedestrian.astronaut`, `walker.pedestrian.firefighter`,
`walker.pedestrian.deliveryrobot`, `static.prop.concreteroadbarrier`,
`static.prop.roadclosedbarricade`). The other **13 are not**: ten are marketplace assets whose
terms forbid redistribution and AI use, two are third-party game IP, and one (the boar) is a free
model whose creator limits it to personal use ([`../NOTICE`](../NOTICE) §3). v0.9 does not ship
them; they are specified by size instead, and replacements for twelve of them are v1.0 work. The six vehicle blueprints use a neutral
`vehicle.ood.*` namespace, so a v1.0 mesh swap does not change these XMLs.

> **A missing blueprint fails silently.** `try_spawn_actor` returns `None`, the prop never
> appears, and the route still completes with a plausible Driving Score. Verify blueprint
> registration with the acceptance test in [`../tests/`](../tests/) before trusting any result.

## Versions and protocol

v0.9 routes bind to arXiv v1. Scores produced under v1.0 are **not** comparable row-for-row
with v0.9 scores, even where the blueprint id is unchanged. Always report which bundle version
a result came from; `VERSION` and the `#` header of `MANIFEST.tsv` both carry it.

The published baselines use **three seeds (42, 43, 44)**, averaged per route (PDM-Lite ceiling
is seed 42 only). See the [Protocol](../README.md#protocol) section of the top-level README.
