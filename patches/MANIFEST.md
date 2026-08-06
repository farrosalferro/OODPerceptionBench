# Patch manifest

**Version stamp: 0.9.0 — corresponds to arXiv v1.**
Base: `autonomousvision/carla_garage` @ `beb3433407f42c1adced312b877a61fe04f338ba`
(branch `leaderboard_2`, commit date 2025-12-28; pinned 2026-08-03).

26 patches, 26 files, ~9,990 added / ~808 removed lines. Bench2Drive is vendored inside
carla_garage upstream-side, so every path below is relative to the carla_garage repository root.

Two tiers:

- **REQUIRED** (21) — in the dependency closure of the 475 canonical routes and the metrics
  pipeline. Omitting any one of them breaks the benchmark.
- **INCLUDED** (5) — scenarios we authored that the canonical 475 routes do **not** instantiate.
  They ship because they are part of this project's scenario work, they are MIT-clean, and CARLA
  imports every module in `srunner/scenarios/` regardless. They are flagged so nobody mistakes
  them for part of the measured benchmark.

---

## Layer 0 — scenario-runner core

| Patch | File | +/− | Why |
|---|---|---:|---|
| `010` | `srunner/scenariomanager/traffic_events.py` | +9 / −0 | Declares `TrafficEventType.TTR_DAR_MEASUREMENT` and three planning-metric event types. **`statistics_manager.py` references all four at module import time**, so omitting these nine lines is an immediate `AttributeError` on every route. Not in the original file list — found by dependency closure. |
| `020` | `srunner/scenariomanager/scenarioatomics/atomic_criteria.py` | +3496 / −278 | `TTRDARCriterion` (time-to-reaction / deceleration-at-reaction) and the OOD-collision criteria that identify whether a collision involved the OOD actor specifically. The largest and most important hunk in the set. Also carries the deferred planning-metric criteria, commented out. |
| `030` | `srunner/scenariomanager/scenarioatomics/atomic_behaviors.py` | +1296 / −357 | Behaviours the `*_modified` scenarios are built on — OOD-actor spawning with blueprint-attribute control, door-opening, hard-brake. |
| `040` | `srunner/tools/scenario_helper.py` | +32 / −0 | `apply_leading_edge_offset()`. Shifts a pedestrian spawn point forward by the difference in bounding-box half-extent so a *larger* OOD actor's **leading edge** crosses the lane boundary at the same moment the reference actor's does. Without it, geometric shift is confounded with a timing change, which would invalidate the paper's central comparison. |

## Layers 1–2 — scenario definitions used by the canonical route set

All ten define classes the 475 routes instantiate by name. **Seven were untracked in the private
working tree** and are therefore invisible to `git diff` — they are the reason the patch set is
built from a scratch git index rather than a diff (see `README.md` in this directory).

| Patch | File | +/− | Classes | Routes | git state |
|---|---|---:|---|---:|---|
| `110` | `srunner/scenarios/construction_obstacle_two_ways_modified.py` | +473 / −0 | `ConstructionObstacleModified`, `ConstructionObstacleTwoWaysModified` | 70 | tracked |
| `120` | `srunner/scenarios/dynamic_object_crossing_modified.py` | +364 / −0 | `DynamicObjectCrossingModified` | 36 | tracked |
| `130` | `srunner/scenarios/invading_turn_modified.py` | +247 / −0 | `InvadingTurnModified` | 36 | tracked |
| `140` | `srunner/scenarios/hard_break_modified.py` | +167 / −0 | `HardBreakRouteModified` | 45 | **untracked** |
| `150` | `srunner/scenarios/parked_obstacle_modified.py` | +360 / −0 | `ParkedObstacleModified`, `ParkedObstacleTwoWaysModified` | 72 | **untracked** |
| `160` | `srunner/scenarios/parking_crossing_pedestrian_modified.py` | +288 / −0 | `ParkingCrossingPedestrianModified` | 45 | **untracked** |
| `170` | `srunner/scenarios/parking_cut_in_modified.py` | +213 / −0 | `ParkingCutInModified` | 45 | **untracked** |
| `180` | `srunner/scenarios/pedestrian_crossing_modified.py` | +282 / −0 | `PedestrianCrossingModified` | 45 | **untracked** |
| `190` | `srunner/scenarios/vehicle_opens_door_modified.py` | +298 / −0 | `VehicleOpensDoorTwoWaysModified` | 45 | **untracked** |
| `200` | `srunner/scenarios/vehicle_turning_route_pedestrian_modified.py` | +277 / −0 | `VehicleTurningRoutePedestrianModified` | 36 | **untracked** |

`setup.sh` asserts all twelve class names exist after applying, and
`tools/check_route_coverage.py` re-derives the requirement from the route XMLs themselves rather
than from this table, so the two cannot silently drift apart.

## Layer 3 — leaderboard

| Patch | File | +/− | Why |
|---|---|---:|---|
| `310` | `leaderboard/utils/statistics_manager.py` | +456 / −165 | Consumes the TTR/DAR and OOD-collision events into the per-route result JSON. **This file was committed in the private repo, so it is absent from the uncommitted working diff** — a patch set derived from `git status` alone would drop it, and every result record would silently lose its secondary metrics. Explicitly verified. |
| `320` | `leaderboard/utils/checkpoint_tools.py` | +26 / −0 | `_sanitize_floats()`. The TTR/DAR criterion legitimately emits `float('inf')` when nothing is closing, and numpy scalars leak in from the criteria. Without this, `json.dump` raises **mid-write** and truncates the result file — a corrupt record rather than a missing one. |
| `330` | `leaderboard/leaderboard_evaluator.py` | +26 / −7 | Three things: (a) the agent-construction signature every model wrapper in this benchmark relies on; (b) CARLA is no longer started in its own process group, so it dies with the per-route wrapper instead of orphaning and leaking VRAM across retries; (c) crash cleanup kills our CARLA **by PID** instead of `pkill`-ing on `-graphicsadapter=N`, which used to kill a co-tenant CARLA sharing the same physical GPU. |
| `340` | `leaderboard/autoagents/autonomous_agent_local.py` | +134 / −0 | New file. `team_code/autopilot.py` (upstream, unmodified) does `from leaderboard.autoagents import autonomous_agent_local`, but Bench2Drive's leaderboard does not provide it. Without this file the **PDM-Lite reference agent cannot be imported**, and PDM-Lite is what generates the acceptance goldens. |

## Layer 4 — weather determinism

| Patch | File | +/− | Why |
|---|---|---:|---|
| `410` | `leaderboard/team_code/config.py` | +2 / −0 | Adds `GlobalConfig.shuffle_weather`, default `False`. |
| `420` | `leaderboard/team_code/config_report.py` | +2 / −0 | Same flag in the reporting config. |
| `430` | `leaderboard/team_code/data_agent.py` | +3 / −1 | Gates the weather shuffle on that flag. Upstream randomises weather unconditionally in datagen mode; weather is part of an OOD route's definition, and randomising it would break comparability between a route's three levels. Three lines, but they are the enforcement half. |

## Layer 9 — authored but not exercised by the canonical route set

These define scenario classes that **no route in `routes/` instantiates**. They belong to an
earlier iteration of the benchmark. They are shipped for completeness and are safe — each
guards its optional import of the deferred planning criteria with `try/except ImportError`, so
CARLA's unguarded module sweep over `srunner/scenarios/` cannot trip on them.

| Patch | File | +/− |
|---|---|---:|
| `910` | `srunner/scenarios/accident_two_ways_modified.py` | +420 / −0 |
| `920` | `srunner/scenarios/invading_turn_heavy.py` | +297 / −0 |
| `930` | `srunner/scenarios/obstacle_approaching_ego.py` | +233 / −0 |
| `940` | `srunner/scenarios/pedestrian_crossing_stop.py` | +348 / −0 |
| `950` | `srunner/scenarios/reverse_vehicle.py` | +243 / −0 |

---

## How this set was verified

1. **Fork point.** `git merge-base` against `origin/leaderboard_2` is
   `1a51f3a52ca1663f12a24f9738cfae631bdc53fb`, exactly three private commits behind HEAD and one
   commit behind the branch tip. The only upstream commit ahead of the fork point (`beb3433`)
   touches `docs/history.md` alone, which no patch here goes near — so the delta is identical
   whether measured against the fork point or the pin.
2. **Apply.** All 26 apply and reverse cleanly against a pristine checkout of the pinned SHA.
3. **Fidelity.** After applying, all 26 files are **byte-identical** to the private working tree
   that produced the published results.
4. **Syntax.** All 26 parse under Python 3.
5. **Import closure.** Every `from srunner.… import X` / `from leaderboard.… import X` in the
   patched files resolves to a name that exists in the patched tree — checked statically,
   excluding `try/except ImportError`-guarded imports. Zero unresolved.
6. **Route closure.** All twelve scenario types named in the 475 canonical route XMLs resolve to
   a class in the patched tree.
7. **Scrub.** No cluster paths, hostnames, private environment names, or credentials.

Steps 2, 4, 6 and 7 run in CI on every push.
