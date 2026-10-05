# Runner status

> **Runner v0.9.0.dev0, for OOD-PerceptionBench release v0.9 (arXiv v1).**
>
> **Read this before trusting the runner with GPU-hours.**

The runner's logic (resume, retry accounting, worker isolation, exit codes, seed handling) is
covered by 377 automated tests that run against a stand-in evaluator. Its behaviour with a real
CARLA 0.9.15 was tested on the dates in §2: single routes, two workers sharing one GPU, port
isolation, interrupt and resume, real failure accounting, the nine-route acceptance run, and the
SLURM backend on one node.

**Not yet tested:** the full 475-route set, workers spread over several GPUs of one machine, and
SLURM across several nodes. Nothing below should be read as tested unless it says so.

---

## 1. Automated tests

```
python -m unittest discover -s tests -t .    ->  377 tests
```

No GPU, CARLA, network or third-party package is needed; the four tests that load the shipped
YAML templates need PyYAML and are skipped without it.

| File | Tests | What it checks |
|---|---:|---|
| `test_ports.py` | 13 | Port allocation is deterministic and collision-free up to 64 workers; overlapping or out-of-range blocks are rejected; a bound port reads busy. |
| `test_port_locks.py` | 15 | A second run on an overlapping block is refused at once; locks are released only after reaping; unusable lock directories, symlinks and FIFOs are handled safely. |
| `test_results.py` | 22 | Which checkpoints count as finished; every status seen in about 4,000 real records is classified; unknown statuses are flagged. |
| `test_plan.py` | 23 | Result paths keep the `{scenario}/{level}/` part; an edited route file is caught by its sha256; budgets persist across restarts. |
| `test_report_and_state.py` | 17 | No incomplete sweep exits 0; the ledger is written atomically and a corrupt one is kept, not reset. |
| `test_config_and_jobscript.py` | 46 | Required fields, unknown keys, unique CUDA and Vulkan numbers, reserved `agent.env` names; the job script pins both GPUs, uses the given ports and exports the runner's variables last. |
| `test_shipped_configs.py` | 4 | Every shipped config template loads and validates once its placeholders are filled. |
| `test_result_preservation.py` | 14 | A failed launch leaves an existing result untouched and charges the infrastructure budget; interrupts charge nothing. |
| `test_settlement_model.py` | 13 | The retry rules of `DESIGN.md` §6A for killed, never-started and degenerate routes. |
| `test_verification_findings.py` | 24 | Parses the 24-cell table in `DESIGN.md` §6A.5 and checks the code against every cell; recovery with `--retry-infra-exhausted`; the config digest. |
| `test_hard_death_and_infra_zero.py` | 28 | Death by signal is read from the exit status; `infra_budget: 0` still runs each route once; `--dry-run` writes nothing. |
| `test_backend_concurrency.py` | 14 | The loop opens exactly the backend's slots; SLURM concurrency is `slurm.max_parallel`. |
| `test_integration_local.py` | 24 | The full loop against a stand-in evaluator: resume, retry, timeout, quarantine, busy ports and every exit code. |
| `test_env_preflight.py` | 18 | The interpreter check and the agent-code fingerprint. |
| `test_world_ready.py` | 8 | The simulator start-up deadline (evaluator exit 75). |
| `test_gpus.py` | 8 | `--check-gpus` parsing; software rasterizers are never offered. |
| `test_reap.py` | 2 | Reaping sends the requested signal and still escalates to SIGKILL. |
| `test_reference_agent_interface.py` | 3 | The reference agent accepts the pinned evaluator's `setup()` call. |
| `test_slurm_backend.py` | 74 | The SLURM backend against a stand-in scheduler, including the fault paths in §2, several replayed from output captured on a real SLURM 24.11.5 scheduler. |
| `test_slurm_replay.py` | 7 | The replay helper reproduces the captured scheduler output exactly. |

The tests cannot show what a real process writes to a real stream. Several defects listed under
"Fixed defects" passed every test until a real run or a careful reading found them.

---

## 2. Hardware evidence

**CLOSED** means the criterion was observed on real hardware. **PARTIAL** and **OPEN** say what
is missing.

| # | State | Observed evidence | Remaining limit |
|---|---|---|---|
| H1 | **CLOSED, 2026-08-11** | A real route reached `Completed` (DS 50.0); its checkpoint and agent log landed in the mirrored paths. | Shows the plumbing works, not model quality. |
| H2 | **CLOSED, 2026-08-11** | Two workers ran eight routes on one GPU, twice, with both CARLAs live together on separate port blocks. | More workers and several nodes are untested. |
| H3 | **OPEN (one-GPU partial), 2026-08-11** | On a one-GPU host, agent and simulator both used CUDA 0 / Vulkan 0. | Needs a host with two or more GPUs to show distinct mappings do not collapse onto adapter 0. |
| H4 | **CLOSED, 2026-08-11/12** | Normal teardown and a real mid-sweep Ctrl-C reaped every evaluator and CARLA; no process or listener survived. | None. |
| H5 | **CLOSED, 2026-08-11** | Each worker kept exactly its configured ports; nothing moved silently. | Local backend only. |
| H6 | **CLOSED, 2026-08-12** | Ctrl-C exited 3 with state and report written, charging no budget. A resume ran exactly the unfinished routes; a third run launched nothing. | None for the local two-worker case. |
| H7 | **CLOSED, 2026-08-11/12** | Real `Failed - TickRuntime` results settled at a zero retry budget; eight deliberate setup failures each charged one record attempt. | Hard deaths (segfault, abort, out-of-memory kill) were not induced on hardware. |
| H8 | **PARTIAL, 2026-08-13** | One model (TFPP) ran the 70-route static category, seed 42, as four one-GPU jobs: **0.136 GPU-h per route**. | One model on one category. The full 475-route run is unmeasured. |
| H9 | **CLOSED, 2026-08-15; re-run 2026-10-05** | The SLURM backend ran the 70-route static category, seed 42, on one node. See below. | One node only; the full 475-route scale and several nodes are untested. |
| H10 | **PARTIAL, 2026-08-11/12** | A fresh GitHub clone ran setup twice (26/26 patches), the tests, a 475-route dry run, real routes and the acceptance flow, with every path from the config. | The host still had the maintainers' internal storage mounted. Repeating the nine-route run on a host without it would close this. |

### SLURM (H9)

**2026-08-15.** At two jobs at a time, every route settled with a real checkpoint (60
`Failed - TickRuntime`, 9 `Completed`, 1 `Failed - Agent got blocked`, all model results).
Concurrent jobs used different GPUs and separate ports, and every job was cleaned up. For nine
routes run on both backends, SLURM matched the local backend's statuses and retry accounting.

**Fault paths fixed on 2026-10-04**, each with tests that replay output captured from a real
SLURM 24.11.5 scheduler:

- **Missing accounting is no longer read as a normal exit.** When a job has left the queue but
  `sacct` fails or has no record, the runner waits up to 180 s, then cancels the job and settles
  the attempt as a fault, which never spends the model's retries. A failing `squeue` is not
  taken to mean the job left the queue; if neither `squeue` nor `sacct` can say, the run stops
  with an error naming the job. `squeue` runs without the caller's `SQUEUE_*` variables, because
  a `SQUEUE_STATES` default in a user's shell would hide a running job. The three that choose
  which federation clusters it asks (`SQUEUE_FEDERATION`, `SQUEUE_LOCAL`, `SQUEUE_SIBLING`) are
  kept; this has not been tried on a federated cluster.
- **Queue time is no longer counted as runtime.** Runtime counts only while the job is seen
  running (in `squeue`, or in `sacct` when `squeue` itself fails), and stops when the job is
  paused or leaves the queue. If `sacct` still says running after the job has left the queue,
  the runner waits up to `execution.route_timeout_s` from leaving, then cancels the job and
  settles a timeout. A job that ends before it
  is seen running takes its runtime from `sacct`, or records 0 s with
  `runtime unknown (never observed RUNNING)`.
- **A previous checkpoint survives a job the runner cannot identify.** See "Set-aside
  checkpoints" in [`README.md`](README.md#slurm). If `sbatch` accepts a job but prints no job
  id, the runner looks for the job by its name and a per-submission tag
  (`--comment=oodbench:<32 hex>`).
- The `SQUEUE_*` handling and the runtime cut-off at leaving the queue were added after the
  2026-10-05 re-run and are covered by tests only.

**2026-10-05 re-run** of the same category on one node, with up to six jobs in flight. Every
route settled, the run exited 0, and no set-aside file was left behind.

- A job cancelled while queued settled as an infrastructure retry with runtime 0 s.
- A job cancelled while running settled as an infrastructure retry; the retry reproduced the
  earlier status and score.
- Stopping the runner cancelled its two jobs, charged no budget, and the resumed run finished
  the category.
- Missing accounting and an `sbatch` reply without a job id could not be produced on demand;
  those paths are covered by replayed output only.
- The reference agent's scores differed from the 2026-08-15 run on 16 of 70 routes (4 of them
  between `Completed` and `Failed - TickRuntime`; category mean +0.38). Three of those routes had been run twice on 2026-08-15, and two of them had
  differed between those two runs too. This fits the agent's run-to-run variation, and the
  backend change does not touch the simulation, but this run does not prove it.

### Measurement notes

- H1–H7 and H10 ran on one host with one RTX 3090 (driver 580.82.09).
- The current acceptance bundle (`tests/goldens/`) was made on 2026-10-05 on an RTX 6000 Ada
  (driver 570.211.01), from three separate runs of the nine-route smoke split. Every route
  scored DS 100.0 in all three, and the tolerance is ±1.0 DS. On the earlier bundle, removing
  one shipped prop and re-running its route gave a normal-looking `Completed`, DS 100.0, with a
  Tesla in its place, and the acceptance check rejected it.
- A worker's own port was sometimes still busy after the previous CARLA was reaped (about 1
  launch in 30). With `infra_budget: 1` this left a route unsettled, so
  `configs/reference_agent.yaml` now uses the defaults (3/3).
- The reference agent, which runs no model, used 0.044–0.079 GPU-h per route. For a real model
  plan on about 0.14 (H8).

### What to test next (cheapest first)

1. The nine-route acceptance run on a host without the maintainers' internal storage (H10).
2. Two workers on a host with two or more GPUs (H3).
3. The full 475-route set with a real model (H8).
4. SLURM at full scale and across several nodes, and an early liveness check so a frozen
   simulator does not have to wait out `route_timeout_s` (H9).

---

## 3. Known gaps

| Gap | Impact | Plan |
|---|---|---|
| **No liveness check** | A CARLA that hangs without exiting is caught only by `execution.route_timeout_s`. Measured: a hung route logs one agent tick and then nothing; at `infra_budget: 3` with a 3600 s timeout it costs three hours before it is skipped. | A checkpoint-age stall detector is designed, not implemented. |
| **No CARLA version check** | The content pack is built for CARLA 0.9.15. On another version the new objects can be missing, and routes then score normally without them. | Add a preflight that fails on a version mismatch. Until then, run the acceptance test in `tests/`. |
| **No blueprint-spawn check** | The runner cannot tell whether the intended object actually spawned. | That is what `tests/` checks. |
| **An unexpected exception mid-sweep exits 2 with no report** | Exit 2 suggests a config error when the sweep died later. Results on disk are fine and a re-run resumes. | Write the report and exit 1 once the sweep has started. |
| **Signal deaths are inferred from `128 + N`** | Safe for the pinned evaluator (it exits only 0 or 255). An evaluator you configure that deliberately exits 129–192 would be read as killed by a signal. | A `trap` in the job script; needs hardware to test. |
| **Port locks are per lock directory** | Runs in containers with private `/tmp` directories do not see each other's locks; a CARLA started by hand takes none. Only the startup port check guards those. | Containers sharing a host should mount one lock directory. |
| SLURM: no array jobs | One `sbatch` per route: 475 submissions. | Deferred. |
| SLURM: ports on a shared node | Two independent runs on one node could pick the same ports. | Consider deriving the port base from the job id. |
| No machine-readable progress | Progress is log lines only. | `state.json` is updated continuously and can be polled. |
| Linux only | Process handling uses `/proc` and process groups. | Out of scope; CARLA and this benchmark are Linux. |

---

## 4. A deliberate difference from the internal tools

`resume.mode` defaults to `skip_terminal`. The internal orchestrators' `--skip_if_final` is
kept, exactly, as `resume.mode: skip_any_final`, but it is not the default: with it, a route
interrupted while it held a `Failed - Agent crashed` result is never retried after a resume,
although the same run would have retried it. This changes which routes are retried, not how any
route is scored, so it cannot move a published number.

---

## 5. Fixed defects

Each was reproduced first, then fixed with a test that fails on the old code. None changes how
a route is scored. They changed whether a route got its fair attempts, or whether two workers
could share a simulator, GPU or seed.

**Found by review, before any hardware run (2026-08).** About two dozen, over five review
rounds; each round found real defects in the previous one. The main ones:

- A failed launch deleted the result it was about to replace, and charged the model's retries
  instead of the infrastructure budget.
- Ctrl-C, timeouts and other kills charged budgets, so a few interrupts could freeze a crash
  result in as the route's answer. The rules in `DESIGN.md` §6A replaced the patches; a test now
  checks the code against its table.
- `agent.env` could override the runner's own `PORT`, `SEED` or `CUDA_VISIBLE_DEVICES`.
- Two config entries could share a Vulkan adapter, putting two CARLAs on one GPU.
- `slurm.max_parallel` limited nothing, and slot numbers could wrap onto another job's ports.
- Deaths by SIGABRT or out-of-memory kill were read as normal exits, because the stderr patterns
  did not match what a real shell writes. Signals are now read from the exit status.
- `infra_budget: 0` skipped every route; `--dry-run` wrote to the ledger.

**SLURM backend (2026-08-09, before H9).** Eight defects found against a stand-in scheduler,
including: the results directory was never created, signal kills charged the model's retries,
queue time counted as runtime, `RUNNING` read as finished, and the job script overrode the
scheduler's GPU allocation. All were fixed and then confirmed on a real scheduler (H9).

**Hang accounting (2026-08-13).** Found by the first real model run. Wall-clock timeouts
counted towards quarantine, so three hanging routes in a row quarantined the only worker; the
quarantine message named only a stuck GPU when the real cause was a busy port; and the recovery
hint encouraged retrying a permanently hanging route for ever.

**Port block (2026-10-03).** A `--dry-run`, or a run refused for a busy block, still reaped the
block's CARLAs, killing another sweep's simulator. A run started right after another on the same
block was refused instead of waiting for the ports to free. Two runs could both find the block
free and each kill the other's simulators; the per-port locks fix this.

**SLURM fault paths (2026-10-04).** See §2.
