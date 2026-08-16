# Hardware-validation issue register

**Measured:** 2026-08-11 through 2026-08-12 · **Release:** v0.9 / arXiv v1

This is the decision list produced by the first runs of the published artifact against CARLA
0.9.15. It records defects and unresolved validation gaps; it does not repair them. Evidence is
from the H0–H5 validation log. A later change should name the issue it closes and repeat the
smallest hardware experiment that exposed it.

## Open defects and decisions

| ID | Observed defect or ambiguity | Evidence and impact | Decision needed |
|---|---|---|---|
| HV-01 | Environment activation does not prove which Python runs the evaluator. | H2 first selected a CARLA-only environment that lacked `py_trees`. H5 then showed that `conda activate` plus bare `python3` can still resolve to system Python, producing three no-record exits and worker quarantine on a healthy GPU. | Add an exact-interpreter example or a preflight that prints the executable and imports `carla`, `py_trees`, and the configured agent before launching CARLA. |
| HV-02 | A listening CARLA port is not a readiness signal. | H1 saw the listener about 5.5 minutes before `get_world()` became usable. H2–H4 also observed the evaluator's first RPC blocking for its full 900-second timeout while a separate client could query the server. This adds uncharged 15-minute stalls. | Define a bounded world-readiness probe and decide whether a failed probe is preflight, infrastructure retry, or route time. |
| HV-03 | `--check-gpus` can print an unusable CUDA/Vulkan pair. | On a one-GPU host it listed Vulkan adapter 1 (`llvmpipe`) and generated an example pairing it with nonexistent CUDA device 1. H1 ignored that line; a first-time reader might not. | Filter software Vulkan adapters and generate pairs only from real CUDA devices, or label non-pairs explicitly. |
| HV-04 | Golden provenance records the generator's Python, not necessarily the replicate runtime. | H5's three replicates used Python 3.10.15, but invoking `make_golden.py` with system `python3` initially stamped 3.8.10. The file was regenerated correctly, but the tool cannot detect the disagreement. | Read runtime provenance from replicate reports, add an explicit argument, or reject mixed/unknown provenance. |
| HV-06 | One content-pack SHA is requested for a three-archive release. | The v0.9 pack has three independently checksummed archives, while `make_golden.py` accepts one `--content-pack-sha256`. H5 used and documented a deterministic composite hash, but that construction is not specified by the release. | Define a canonical manifest/composite digest or allow an ordered set of archive digests. |
| HV-07 | The measured PDM-Lite entrypoint is not identified by its checkout commit alone. | The two debug-agent files used for H5 were untracked in the agent checkout. The landed bundle follows the regeneration command and records the checkout commit, so those exact file contents cannot be reproduced from that commit alone. | Publish the exact agent files or bind the golden to a retrievable artifact digest. |
| HV-08 | The measured PDM environment has a declared dependency mismatch. | Every H5 route emitted a SciPy warning: installed NumPy 1.23.0 is below SciPy's declared minimum 1.23.5. The 27 accepted routes completed, but this is not a clean reproducible environment specification. | Freeze and publish a tested environment, or reconcile the NumPy/SciPy pins and regenerate the golden if runtime behavior changes. |
| HV-09 | The fresh-clone criterion has not been exercised on a host where the internal mounts do not exist. | H0 used a fresh GitHub clone and found no private paths in tests; H2–H5 ran real routes from that clone with all paths supplied by config. The host itself still had the maintainers' internal storage mounted, so the strongest H10 wording remains unproved. | Repeat the nine-route smoke run on a genuinely external host or narrow H10 to the portability property actually measured. |
| HV-10 | The public install does not declare a test environment at the top level. | H0's documented pytest command failed because pytest was absent; an isolated environment had to install it. H2 likewise found that a CARLA-only environment was insufficient for real routes. | Add a release/test requirements file or a single authoritative environment recipe. |
| HV-11 | The validation handoff's literal setup command omitted a required argument. | H0's bare `./setup.sh` stopped before patching because `--upstream-dir` is mandatory. The README had the correct command, but the release-validation ticket did not. | Keep the ticket command synchronized with the public quick start. |
| HV-13 | The release gate's golden-claim check is one-directional. | With one real bundle present, `tools/check_release_ready.py` reported the golden claim as passing even while the README table still said `none`; it only rejects the inverse mismatch (a claim when zero bundles exist). H6 needed a separate truth audit to catch the stale prose. | Make the gate compare both directions and add a regression test, after deciding the exact accepted README wording. |

## Defects repaired before this consolidation ticket

These remain here because they were real published-artifact failures, not hypothetical review
findings. H6 did not change their implementation.

| ID | Defect | Repair already measured |
|---|---|---|
| HV-R1 | Repository hygiene/link checks traversed the installed third-party checkout and failed a correct fresh install. | A published H0 follow-up excluded installed upstream files; the gate returned to its expected state. |
| HV-R2 | The shipped constant-velocity agent used the stock one-argument `setup`, while the pinned evaluator passes a second `save_name`; it also wrote nothing under `SAVE_PATH`. | Published commit `2c467b1b501a39e815ebe57d8b527586f0aff6c5` repaired the interface and logging. The fresh H2 rerun completed a real scenario tick and wrote `reference_agent.txt`. |
| HV-R3 | A single self-clearing busy RPC port exhausted the shipped infrastructure budget and left a route unsettled. | H4 measured the event at the shipped budget and recovered losslessly with `--retry-infra-exhausted`; the published reference config now uses the schema's 3/3 defaults. The underlying brief port occupancy remains a measured environmental event. |
| HV-R4 | The harness self-test assumed the default golden directory contained only the example bundle. | H5 isolated the example in a temporary test directory. All 34 self-tests pass with the real bundle present. |
| HV-R5 | The documented missing-asset negative check only re-read an existing result root, so removing an asset could not change its verdict. | Upstream commit `400597145b422d85a69a1b99548053fbe6c3e8f4` documents executing the affected route in a fresh root while the asset is absent, matching H5's measured Tesla-fallback proof. |
| HV-R6 | The generated golden embedded three private absolute replicate roots under each of nine routes. | Upstream commit `400597145b422d85a69a1b99548053fbe6c3e8f4` emits portable `replicate_1/2/3` labels. Regeneration from the unchanged H5 roots produced zero private strings with identical scores, statuses, actor IDs, spread, and tolerance. The unpublished path-bearing commit was amended before any push. |

## Observations that are not defects

- `Failed - TickRuntime` is a settled benchmark outcome. Three construction routes produced it
  consistently with the constant-velocity agent; exit 0 was correct.
- `Exiting abnormally (error code: 143)` after a finalized result is the runner's SIGTERM
  teardown of CARLA, not a failed route.
- A runner exit 0 means every planned route has a settled answer; it does not mean every model
  route status is `Completed`.
- The SSHFS loss during H3 was external storage failure. The attempt was discarded and repeated
  from a fresh output root.
- Cross-GPU simulator placement and the 475-route scale run are unvalidated scope gaps, not
  successful validations. The SLURM backend was repaired and validated on a real scheduler at
  two-way concurrency on a single node (H9, closed 2026-08-15); its full 475-route scale and
  multi-node fan-out remain unvalidated.
