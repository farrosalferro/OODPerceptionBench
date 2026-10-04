# What the patch set leaves out

The working tree that produced the published results changed 54 files relative to the pinned
upstream. **26 ship as patches** (listed in [`MANIFEST.md`](MANIFEST.md)). The other 28 were left
out on purpose:

- **Unrelated local work** (9 files): training-loop experiments, local agent tweaks, dataset
  scripts, a separate carla_garage-internal leaderboard copy, and local config files.
- **Internal notes** (7 files): working notes on analysis and on our compute cluster, and notes
  for a different, unpublished project.
- **First-generation tooling** (9 files): older evaluation and route-generation scripts that
  hardcode our cluster setup. The portable runner in [`runner/`](../runner/) and the tools in
  [`tools/`](../tools/) replace them.

Separately, the route generator is not a patch at all: it is our own standalone code, not a change
to upstream.

Three files are benchmark-adjacent and excluded by default. Each is a one-line addition to
`tools/dev/patch_manifest.tsv` if you think it should ship:

| Path | Why it is excluded |
|---|---|
| `srunner/scenarios/static_object_obstacle.py` (new, +338) | Defines `StaticObjectObstacle` / `StaticObjectObstacleTwoWays`, which no canonical route uses. |
| `leaderboard/leaderboard_evaluator_debug.py` (new, +574) | A debug fork of the evaluator used during asset-import checks. It would need to ship together with a procedure that calls it. A second fork, `leaderboard_evaluator_debug_no_init.py`, is excluded for the same reason. |
| `leaderboard/data/bench2drive220.xml` (±4) | Comments out one route of Bench2Drive-220, a different benchmark's data file that we neither use nor redistribute. |
