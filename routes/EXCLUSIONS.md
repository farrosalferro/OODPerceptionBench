# Deliberately excluded routes

**Bundle version:** v0.9 · **Binds to:** arXiv v1

Five base routes that exist in the underlying CARLA scenario pool are **deliberately absent
from this benchmark**. Each one is removed from **all three levels** — `base`,
`visual_shift` and `geometric_shift` — not just from the level where the problem appeared.

> **These gaps are intentional. Do not "fix" them.**
> Restoring any of these routes breaks base/visual/geometric parity and invalidates the
> paired statistics the paper reports. `validate_routes.py` asserts their absence and will
> fail if they reappear.

---

## The five routes

| Category | Scenario | Base route | Town | Failing prop | Failure |
|---|---|---|---|---|---|
| vehicle | `parked_obstacle_two_ways` | 3457 | Town13 | `vehicle.ood.dumptruck` | `try_spawn_actor` returns `None` — the bounding box clips static map geometry |
| vehicle | `parked_obstacle_two_ways` | 2664 | Town12 | `vehicle.ood.dumptruck` | `try_spawn_actor` returns `None` — the bounding box clips static map geometry |
| vehicle | `invading_turn` | 3564 | — | `vehicle.ood.dumptruck` | spawns, then intersects an adjacent object and is launched by the physics solver |
| pedestrian | `dynamic_object_crossing` | 17752 | — | oversized geometric walker (`walker.pedestrian.wheelchair`) | cannot be placed — narrow sidewalk / wall geometry does not admit the larger bounding box |
| pedestrian | `vehicle_turning_route_pedestrian` | 3737 | — | oversized geometric walker (`walker.pedestrian.wheelchair`) | cannot be placed — narrow sidewalk / wall geometry does not admit the larger bounding box |

All five failures are **geometric-level** spawn failures: the OOD agent selected for the
geometric shift is materially larger than the reference agent, and on these particular
routes the map has no room for it.

## Why removal on *every* level, rather than a spawn fix

Three options were considered:

1. **Drop only the geometric variant.** Rejected: the scenario would then contribute base
   and visual samples with no geometric counterpart, so the paired base-vs-geometric test
   would silently be computed over a different route set than base-vs-visual. The paper's
   central comparison is *between* the two shift types; unequal support makes that
   comparison unsound.
2. **Nudge the spawn transform until the large prop fits.** Rejected: it changes the
   obstacle's spatial relationship to the ego and to the road on the geometric level only.
   Observability, time-to-contact and drivable space would all differ from the base level,
   introducing a per-level confound exactly where the effect is being measured.
3. **Remove the route from all three levels.** Adopted. It costs a small amount of
   statistical power and preserves perfect parity: every base route in the bundle carries a
   complete set of base, visual and geometric variants.

## Effect on the counts

| Scenario | Base routes before | Excluded | Base routes shipped |
|---|---|---|---|
| `vehicle/parked_obstacle_two_ways` | 5 | 3457, 2664 | 3 (2668, 25865, 25896) |
| `vehicle/invading_turn` | 5 | 3564 | 4 (2790, 2802, 3572, 3575) |
| `pedestrian/dynamic_object_crossing` | 5 | 17752 | 4 (24211, 24224, 24252, 24333) |
| `pedestrian/vehicle_turning_route_pedestrian` | 5 | 3737 | 4 (2164, 3731, 10857, 11381) |

Every other scenario ships its full 5 base routes, except that the two `static` scenarios
ship 5 each by construction. The benchmark therefore has **55 base routes**
(10 static + 18 pedestrian + 27 vehicle) and **475 route XMLs**:

- static: 10 base routes × (1 reference + 3 visual + 3 geometric) = **70**
- pedestrian: 18 base routes × (3 reference + 3 visual + 3 geometric) = **162**
- vehicle: 27 base routes × (3 reference + 3 visual + 3 geometric) = **243**

`n = 55` is the unit count behind the paper's per-cell paired tests, and it is the reason no
reduced/subsampled split is publishable (see the Protocol section of the top-level `README.md`).

## Provenance

The exclusion decision was taken during spawn calibration and is recorded in the project
log under *"Route Exclusion and Spawn Calibration"* and *"Spawn calibration — dumptruck"*.
The paper documents it in the appendix section on spawn calibration.
