# Deliberately excluded from the patch set

The private working tree that produced the published results differs from the pinned upstream in
**47 tracked files** (three private commits plus 23 uncommitted modifications), *plus* seven
untracked scenario modules that no `git` diff reports at all.

Of those 54 files, **26 ship** (19 tracked + all 7 untracked — see [`MANIFEST.md`](MANIFEST.md)).
This file records the other **28** and why each was left out, so that the omissions are auditable
rather than invisible.

Patch extraction here is a **selection** problem, not a diff. Shipping unrelated local work is a
privacy and clarity problem; omitting a needed hunk breaks `setup.sh` silently for every user.
Both directions were checked (see "How this set was verified" in `MANIFEST.md`).

---

## A. Unrelated local work — must not ship

| Path | Size | Why excluded |
|---|---:|---|
| `team_code/train.py` | ~2,000 lines changed | Local training-loop experimentation, unrelated to this benchmark. |
| `team_code/sensor_agent.py` | +2 | Local agent tweak, unrelated. |
| `tools/download_data.sh` | ±4 | Local dataset-fetching convenience. |
| `leaderboard_autopilot/leaderboard/leaderboard_evaluator_local.py` | ±99 | A *different*, carla_garage-internal leaderboard copy. Not the harness this benchmark runs on. |
| `leaderboard_autopilot/leaderboard/scenarios/scenario_manager_local.py` | ±21 | Same. |
| `leaderboard_autopilot/…/scenarioatomics/atomic_behaviors.py` | ±596 | Same. |
| `.gitignore` | +4 | Local ignore rules. |
| `max_num_jobs.txt` | ±2 | Private cluster job-cap file; meaningless outside that scheduler. |
| `docs/common_mistakes_in_benchmarking_ad.md` | ±21 | Upstream carla_garage's own article; our edit is a local annotation. |

## B. Internal operations notes — must not ship

| Path | Why excluded |
|---|---|
| `…/perception/eval/yagi29_slurm_gpu_pinning.md` | Cluster-specific debugging notes: node names, GPU-pinning behaviour, scheduler quirks. Content of the two most recent private commits. Useless publicly, and it names internal infrastructure. |
| `…/scripts/ood_benchmark/metrics_analysis.md` | Internal analysis working notes. |
| `…/scripts/ood_benchmark/metrics_significance.md` | Internal analysis working notes. |
| `…/scripts/ood_benchmark/perception_metrics_ttr_dar_summary.md` | Internal analysis working notes. The publishable version of this material is `docs/`. |
| `…/scripts/ood_benchmark/ttr_dar_definition.md` | Internal draft; superseded by `docs/`. |
| `…/scripts/ood_benchmark/planning_metrics_ic_summary.md` | Planning benchmark — a different, unpublished project. |
| `…/scripts/ood_benchmark/planning_metrics_interaction_correctness.md` | Same. |

## C. Superseded first-generation tooling

These are the v1 (`ood_benchmark/family_*`) generation's scripts. They hardcode cluster paths,
node names and conda environments, and they are replaced by the portable runner in `runner/` and
the record tools in `tools/`.

| Path |
|---|
| `evaluate_routes_slurm_pdm_lite_mine.py` |
| `evaluate_routes_slurm_tfpp_mine.py` |
| `…/ood_benchmark/simlingo/evaluate_routes_slurm_simlingo_mine.py` |
| `…/ood_benchmark/uniad/evaluate_routes_slurm_uniad_mine.py` |
| `…/ood_benchmark/vad/evaluate_routes_slurm_vad_mine.py` |
| `generate_route_files.py` |
| `json_to_csv_results.py`, `json_to_csv_analysis.py`, `analytic_loader.py` |

---

## D. Judgement calls — excluded, but arguable

These are benchmark-adjacent rather than clearly out of scope. They are excluded by default;
each is a one-line addition to `tools/dev/patch_manifest.tsv` if a reviewer disagrees.

| Path | Argument for shipping | Argument against (why it is excluded) |
|---|---|---|
| `srunner/scenarios/static_object_obstacle.py` (+338, new) | Our authored scenario, same class of file as the Layer-9 patches. | Defines `StaticObjectObstacle` / `StaticObjectObstacleTwoWays`, which **no canonical route instantiates** and which no v1 route file references either. Unlike the Layer-9 five it is not named in any workstream brief, so it has no positive reason to ship. |
| `leaderboard/leaderboard_evaluator_debug.py` (+574, new) | Invoked by the asset-import validation runs. | A debug fork of the evaluator with a parallel code path. If the published import procedures end up needing it, it should be added deliberately, together with the procedure that calls it — not smuggled in via the benchmark patch set. **Flagged for the procedures workstream.** (A second, untracked fork, `leaderboard_evaluator_debug_no_init.py`, is excluded on the same grounds.) |
| `leaderboard/data/bench2drive220.xml` (±4) | Tracked change in the benchmark tree. | Comments out one route of the *Bench2Drive-220* benchmark — a different benchmark's data file, which we neither use nor redistribute. |

## E. Not a patch at all

| Path | Where it goes instead |
|---|---|
| `generate_route_files_v2_perception.py` | Untracked in the private tree; it *generates* the 475 route XMLs and holds the blueprint-ID lists. It is our own standalone code, not a change to upstream, so it belongs in `tools/` — delivered by the route-freeze workstream, not here. |
