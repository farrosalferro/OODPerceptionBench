# OOD-PerceptionBench runner

Evaluate a CARLA Leaderboard 2.0 agent on the OOD-PerceptionBench route set, on one machine or
on a SLURM cluster, from a single configuration file.

```bash
python run_benchmark.py --config my_config.yaml
```

> **Release v0.9, which matches arXiv v1 of the paper.** Scores from v0.9 and v1.0 are not
> comparable, and every report this runner writes says which release it targeted.
>
> **What has been tested on real hardware:** single routes and two-worker sweeps on one GPU,
> interrupt and resume, and the nine-route acceptance run, all with CARLA 0.9.15. The SLURM
> backend has run one full route category on one node, with up to six jobs at once. **The full
> 475-route set has never been run, and placing workers on several GPUs of one machine is not
> yet tested.** [`STATUS.md`](STATUS.md) has the details.

---

## Quickstart

1. **Install CARLA 0.9.15** and the Bench2Drive leaderboard and scenario_runner checkout.
   `setup.sh` in the repository root does this at the pinned commits.

2. **Find your GPU mapping.** CUDA and Vulkan number GPUs independently, and CARLA renders with
   Vulkan:

   ```bash
   python run_benchmark.py --check-gpus
   ```

   Copy the pairs into the config. On a one-GPU machine both are `0`. Software rasterizers such
   as `llvmpipe` are listed but never offered as a pair.

3. **Build the Python environment the evaluator runs in.** Use Python 3.10 and the environment
   the published PDM-Lite numbers were reproduced with, plus your agent's own packages:

   ```bash
   pip install -r env/requirements-pdmlite.txt    # from the repository root
   ```

   It brings the `carla==0.9.15` wheel, `py_trees`, and `numpy==1.23.5`, the one numpy that
   both this Bench2Drive pin and `scipy` accept. A CARLA-only environment is **not** enough: the
   evaluator imports `scenario_runner`, which needs `py_trees`. Do not use the Bench2Drive
   `requirements.txt` files on Python 3.10; their `opencv-python` and `numpy` pins have no
   Python 3.10 wheels. On a minimal Ubuntu, `opencv-python` also needs
   `sudo apt-get install libgl1 libglib2.0-0`.

   **Give `environment.python` as an absolute path.** The default, `python3`, is whatever
   `PATH` holds after `environment.activate` runs, which need not be the environment you built.

   Before every real (non-`--dry-run`) sweep, the runner runs `environment.python` once, with
   the same activation, `agent.env` and `PYTHONPATH` a route gets, and imports `carla`,
   `py_trees`, `numpy`, `scipy` and your agent module. If an import fails it exits 2 before any
   simulator starts. If all succeed, it writes `<out>/_runner/env_provenance.json`, recording the
   interpreter, the package versions, your `agent.env`, and a fingerprint of your agent's code
   (the entrypoint's sha256, the git commit, and whether the agent's directory is clean). The
   fingerprint is taken once, at that point; a file edited mid-sweep is not caught.
   `--skip-env-preflight` skips the check, for an interpreter that exists only on compute
   nodes, and the report records that it was skipped.

4. **Copy and edit a config.** `configs/example.yaml` documents every field. Paths, hosts,
   queues and environments have no defaults; a missing required field is an error that names
   it.

5. **Test the setup before spending GPU-hours.** The reference agent
   (`reference_agent/constant_velocity_agent.py`) drives forward at a constant speed. It scores
   badly on purpose; it shows that CARLA starts on the right GPU, the route loads, the agent
   interface works and a finished result lands in the right place.

   ```bash
   python run_benchmark.py --config configs/reference_agent.yaml --limit 1 --dry-run
   python run_benchmark.py --config configs/reference_agent.yaml --limit 1
   ```

6. **Run the sweep.**

   ```bash
   python run_benchmark.py --config my_config.yaml --workers 4
   ```

   Budget about **0.14 GPU-hours per route**: about 67 GPU-hours for all 475 routes, per model
   and seed, or about 17 hours with 4 workers. This was measured with one model on one route
   category (`STATUS.md`, H8).

Press `Ctrl-C` at any time. Running the same command again resumes: finished routes are
skipped and retry budgets carry over.

---

## Bringing your own agent

The runner adds no API of its own. It runs the pinned Bench2Drive evaluator, which runs your
agent, so an agent that already runs under Bench2Drive works unchanged.

**A stock Leaderboard 2.0 agent needs one change.** The pinned evaluator calls
`setup(path_to_conf_file, save_name)` with two arguments. A stock `setup(self, path_to_conf_file)`
raises `TypeError` before the simulation starts, and the route settles as
`Failed - Agent couldn't be set up`. That is a valid route status, so the sweep exits 0 and the
mistake is easy to miss. Accept the extra argument with a default, as the reference agent does:

```python
def setup(self, path_to_conf_file, save_name=None):
```

Model-specific setup goes in the config, never in the runner:

```yaml
agent:
  entrypoint: /path/to/my_agent.py
  config: "/path/to/model_config.py+/path/to/checkpoint.pth"   # passed through to your agent
  track: SENSORS
  pythonpath:
    - /path/to/my_model_repo          # placed ahead of scenario_runner/leaderboard
  env:
    PLANNER_TYPE: traj                # exported in every route's job script
environment:
  python: /path/to/conda/envs/my_env/bin/python3
  activate:
    - source /path/to/conda/etc/profile.d/conda.sh
    - conda activate my_env
```

`agent.env` may not set a variable the runner owns, such as `PORT`, `TM_PORT`, `SEED`,
`CUDA_VISIBLE_DEVICES` or `CARLA_ROOT`. Each has its own config field, and the runner refuses
the name at startup and names that field. `PYTHONPATH` and `LD_LIBRARY_PATH` are allowed: the
runner adds to them rather than replacing them.

Every attempt writes its exact command to
`<out>/_runner/jobs/<scenario>/<level>/<route>_seed42.sh`. When something goes wrong, read
that file first.

---

## Output layout

Result paths mirror the route tree:

```
<out>/<scenario>/<level>/results/<route>_seed42.json     # leaderboard checkpoint
<out>/<scenario>/<level>/logs/<route>_seed42/            # agent SAVE_PATH
<out>/_runner/jobs/<scenario>/<level>/<route>_seed42.sh  # exactly what ran
<out>/_runner/logs/<scenario>/<level>/<route>_seed42.{out,err}
<out>/_runner/state.json                                 # attempt ledger (resume)
<out>/_runner/env_preflight.sh, env_provenance.json      # interpreter check (Quickstart step 3)
<out>/_runner/report.json, report.md                     # final report
```

---

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | every planned route has a **settled** result |
| 1 | partial sweep: at least one planned route has no settled result |
| 2 | configuration or preflight error |
| 3 | interrupted by a signal |
| 4 | all workers quarantined, or no usable GPU |
| 5 | the agent's sensor configuration was rejected |

**A model failing routes is not a runner failure.** A model that scores `Failed - TickRuntime`
on every route has a valid result, and the run exits 0. Exit 0 does not mean every route is
`Completed`; it means every route has an answer. Exit 1 means some route has none. Use the exit
code in scripts: a partial sweep never exits 0.

**Simulator start-up.** The patched evaluator waits 60 s after starting CARLA, then asks the
world for its settings every 10 s and logs `world ready after N s`. If the world has not
answered within `OODPB_WORLD_READY_S` seconds, the evaluator exits with status **75**, which
the runner counts as an infrastructure failure. The default is two thirds of
`execution.route_timeout_s`, at most 1800 s. To change it, set it in `agent.env` or export it in
`environment.activate`. A value set only in the shell you start the runner from is ignored
(the job script unsets it), so it cannot change a run unrecorded. It must stay below the route
timeout, because start-up time counts against the route's clock: a deadline at or above
`execution.route_timeout_s` never fires, and the attempt is killed as a timeout instead, which
does not count towards quarantining the worker.

---

## Configuration notes

`configs/example.yaml` is the full reference. YAML needs PyYAML; JSON, and TOML on Python 3.11
and later, need only the standard library.

**`gpus`: two numbers per GPU.** Locally, `cuda` pins the agent (`CUDA_VISIBLE_DEVICES`);
under SLURM it is only the key that identifies the GPU, and the job keeps the
`CUDA_VISIBLE_DEVICES` the scheduler gives it. `vulkan` pins the CARLA server
(`-graphicsadapter`). CUDA numbers must be unique, and so must Vulkan numbers on one host. If
`vulkan` is missing it is assumed equal to `cuda`, with a warning. Under SLURM,
`slurm.vulkan_index_scope: allocation` allows every one-GPU job to use Vulkan adapter 0. Use it only after checking, inside a job,
that the GPU's UUID or PCI address matches on the CUDA and Vulkan sides.

**`ports`: fixed per worker, and checked.** Worker *i* gets `rpc_base + i*stride` and
`tm_base + i*stride`; CARLA uses three ports from its RPC port, so `stride` must be at least 4.
At startup the local backend locks every port in its block (one file per port under `/tmp`, or
`OODPB_PORT_LOCK_DIR`). A port locked by another oodbench run is refused at once. It then checks
that no port is in use, waiting up to `execution.port_release_timeout_s` (90 s) for a busy one to
free up before it refuses to run. It never moves to other ports by itself. The locks only
protect runs that share the lock directory, so containers on one host should mount a shared one.

**`resume.mode`.**

| Mode | Behaviour |
|---|---|
| `skip_terminal` (default) | skip a route only when its result is a final, legitimate outcome |
| `skip_any_final` | skip any finished result, including a crash; matches the old internal `--skip_if_final` |
| `none` | re-run everything (needs `--force`) |

`skip_any_final` has one hazard: a route interrupted while it held a `Failed - Agent crashed`
result is never retried after a resume, although the same run would have retried it.

**`retry`: four budgets.** Each counts attempts of one kind:

- `record_budget`: attempts that ended normally and wrote a retryable result.
- `infra_budget`: **consecutive** attempts that wrote nothing (timeout, crash, failed launch).
  A healthy attempt resets the count.
- `tickruntime_budget`: default 0. `Failed - TickRuntime` means the agent is slower than
  CARLA's tick budget, and retrying does not fix that.
- `killed_budget`: attempts that ended abnormally (killed by the runner at the wall clock or
  for quarantine, or a fault such as a signal, a node failure or preemption) while a
  crash-shaped result was on disk. That result might be the model's or an artefact of the
  ending, so it gets its own bounded budget.

A broken GPU therefore cannot use up a route's model retries. Exhausting `infra_budget` leaves
the route **unsettled**: the run exits 1, and later runs keep it that way. After fixing the
machine, re-run with `--retry-infra-exhausted`. That resets the infrastructure count of exactly
those routes and changes nothing else. It applies to the whole ledger, ignoring `--limit` and
`--routes`, and combined with `--dry-run` it shows what would run without saving anything. Do
not add it to scripts unconditionally: a route that always hangs would then be retried for ever.
The exact rules are in `DESIGN.md` §6A, and a test checks the code against them.

After `worker_quarantine_after` consecutive infrastructure failures, a local worker slot is
taken out of the pool. The usual causes are a stuck GPU or a port that stays busy. Timeouts do
not count towards this.

**`routes.manifest`.** Point it at `routes/MANIFEST.tsv`. The runner then checks every route
file's sha256, so an edited route is detected. `strict_manifest: true` makes a mismatch a
startup error.

**Resuming after a config change.** A report warns *"produced by a DIFFERENT configuration"*
when a setting changed since the output root was written. Adding a key at its default value
does not trigger it (`DESIGN.md` §6A.11). The report also records the runner version, and the
ledger records which version of the retry rules it was written under; resuming a ledger written
under older rules prints a warning naming both versions. The counters carry over unchanged, but
routes still in progress may settle after a different number of attempts.

---

## SLURM

```yaml
execution:
  backend: slurm
slurm:
  partition: <your-partition>
  max_parallel: 8
  time: "02:00:00"
  mem: 24G
  gres: "gpu:1"
  vulkan_index_scope: host  # host | allocation
```

One job per route, with the same planning, resume, retry and report logic as the local backend.
The number of jobs in flight is `slurm.max_parallel`; `execution.workers` is ignored. The
runner counts only the jobs it submitted itself, and spaces submissions by
`slurm.submit_interval_s`.

- `slurm.extra_directives` entries are copied into the job script as they are, so each must be
  a complete `#SBATCH ...` line. `sbatch` stops reading directives at the first other line, and
  bash then runs that line as a command.
- There is no early check that CARLA is still alive. A simulator that freezes mid-route is
  caught only by `execution.route_timeout_s` and retried under `infra_budget`, so size both for
  your agent.
- Submit one route and read the generated job script before starting a large sweep.

**Set-aside checkpoints.** Before it submits a job, the runner copies the route's existing
checkpoint to `<checkpoint>.aside-<ns>`. If the route has none, it writes a small
`<checkpoint>.unsettled-<ns>` file instead. Either file is removed once the job has ended. If
the runner cannot tell which job is the route's, or cannot confirm a cancelled job has ended,
it stops and leaves the file in place. While any such file exists, a SLURM run refuses to
start and lists them. For each one:

1. Stop any job of the earlier run that is still queued or running
   (`squeue -u $USER -o '%i %j %k'`, then `scancel <id>`), and confirm with `sacct -j <id>` that
   it has ended. An `.unsettled-` file names its job.
2. For an `.aside-` file, either restore it (`mv <checkpoint>.aside-<ns> <checkpoint>`) or
   delete it so the route runs again. Delete an `.unsettled-` file once its job has ended.
3. If the checkpoint exists again, a job wrote it after the copy was made; keep whichever you
   trust.

Then resume.

---

## Running the tests

No GPU, CARLA or network is needed. Only the four tests that load the shipped YAML templates
need PyYAML, and they are skipped without it.

```bash
python -m unittest discover -s tests -t .
```

There are 377 tests. They cover the port allocator and port locks, result parsing, resume and
the retry budgets, the exit codes, the job script, the shipped configs, and both backends. The
SLURM tests replay output captured from a real scheduler. The end-to-end tests run the whole
loop against a stand-in evaluator. Anything that needs a real simulator is covered only by the
hardware runs in `STATUS.md`.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| exit 2, "held by another oodbench run on this machine" | another sweep holds part of this port block; the message names its lock file (`fuser -v <lockfile>` finds the process). Wait for it, stop it, or move `ports.rpc_base` / `ports.tm_base`. |
| exit 2, "reserved port(s) already in use" | another run or a leftover CARLA still holds the block after the 90 s wait. Free the ports or move `ports.rpc_base`. |
| exit 2, "environment preflight: ... cannot import ..." | `environment.python` lacks a package or is not the interpreter you think (the log shows `sys.executable`). Use an absolute path; see Quickstart step 3. |
| exit 2, "leaderboard.work_dir=... has no leaderboard/data/weather.xml" | `work_dir` must be the Bench2Drive checkout itself, the directory holding `leaderboard/` and `scenario_runner/`. |
| exit 2, "agent.env may not set variable(s) the runner owns" | use the config field the error names. |
| exit 2, "gpus[i].vulkan=N is already claimed" | two entries share a Vulkan adapter. Fix the mapping. |
| exit 5 immediately | the agent's `sensors()` was rejected for the configured `track`. It would fail the same way on every route. |
| route `.out` log ends in "world NOT ready after N s" | CARLA started but its world never answered (evaluator exit 75). A stuck GPU or a very slow map load; check the GPU or raise `OODPB_WORLD_READY_S`. |
| "Exiting abnormally (error code: 143)" after a finished result | normal: the runner stopped CARLA with SIGTERM once the route was done. |
| every route `Failed - Agent couldn't be set up` | an import error, a missing checkpoint, or a stock one-argument `setup()`. Read `<out>/_runner/logs/.../*.err`. |
| many `Failed - TickRuntime` | the agent is slower than CARLA's tick budget. This is a model result, not a fault. |
| throughput far below `workers × 1 route` | the `vulkan` numbers are probably wrong and every CARLA is on one GPU. Run `--check-gpus`. |
| a worker is quarantined | probably a stuck GPU, or a port that stayed busy. Check the GPU before reusing it. |
| SLURM: fewer jobs than expected | concurrency is `slurm.max_parallel`, not `execution.workers`. |
| SLURM: run refuses to start, listing `.aside-` or `.unsettled-` files | an earlier run stopped with a job it could not account for; see "Set-aside checkpoints". |
| report: routes skipped with "budget already spent" | an earlier run used up their model retries. The result on disk is the answer; read the logs before deciding it is wrong. |
| report: the infrastructure budget is spent | the machine, not the model. Fix it, then re-run with `--retry-infra-exhausted`. |
| warning: "produced by a DIFFERENT configuration" | a setting changed. A changed agent or CARLA build should use a fresh output root. |

---

## Documents

- [`DESIGN.md`](DESIGN.md): the design decisions and their reasons. Read it before changing
  anything structural.
- [`STATUS.md`](STATUS.md): what has been tested on hardware, and what is still open.
