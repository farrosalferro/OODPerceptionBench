# OOD-PerceptionBench runner

> **Benchmark release v0.9** — these numbers bind to **arXiv v1** of the paper. A v1.0 release
> (replacement assets + re-run) binds to arXiv v2, and scores from the two are **not**
> comparable. Every report this runner writes carries that stamp.
>
> **This is a FIRST CUT.** The supervision logic is covered by 153 automated tests, but nothing
> here has been run against a real CARLA server or a real GPU. Read `STATUS.md` before trusting
> it with GPU-hours.

Evaluate a CARLA Leaderboard 2.0 agent on the OOD-PerceptionBench route set, on one machine or
on a SLURM cluster, from a single configuration file.

```bash
python run_benchmark.py --config my_config.yaml
```

---

## Quickstart

1. **Install CARLA 0.9.15** and prepare the Bench2Drive leaderboard + scenario_runner checkout
   (`setup.sh` in the repo root does this against the pinned SHAs).

2. **Learn your GPU mapping.** CUDA and Vulkan index devices independently, and CARLA renders
   with Vulkan:

   ```bash
   python run_benchmark.py --check-gpus
   ```

   Record the pairs in the config. On a single-GPU machine both are `0` and you can move on.

3. **Copy and edit a config.** `configs/example.yaml` documents every field.
   There are no defaults for paths, hosts, queues or environments — a missing required field is
   an error that names the field, not a fallback to somebody else's filesystem.

4. **Prove the plumbing before spending GPU-hours.** The shipped reference agent
   (`reference_agent/constant_velocity_agent.py`) drives forward at a constant speed. It scores
   badly on purpose; its job is to show that CARLA starts on the right GPU, the route loads, the
   agent interface binds, criteria attach, and a finalized checkpoint lands in the right place.

   ```bash
   python run_benchmark.py --config configs/reference_agent.yaml --limit 1 --dry-run
   python run_benchmark.py --config configs/reference_agent.yaml --limit 1
   ```

5. **Run the sweep.**

   ```bash
   python run_benchmark.py --config my_config.yaml --workers 4
   ```

   Budget roughly **0.12 GPU-hours per route**, so ≈ 58 GPU-hours for the full 475-route set.
   At 4-way parallelism that is about 15 hours.

Interrupt with `Ctrl-C` at any point. Re-running the same command resumes: completed routes are
skipped and retry budgets carry over.

---

## Bringing your own agent

The runner uses the **stock CARLA Leaderboard 2.0 `AutonomousAgent` interface, unchanged**.
There is no runner-specific API to implement. Anything that already runs under Bench2Drive
works with no code changes.

Model-specific setup goes in the config, never in the runner:

```yaml
agent:
  entrypoint: /path/to/my_agent.py
  config: "/path/to/model_config.py+/path/to/checkpoint.pth"   # opaque; your agent parses it
  track: SENSORS
  pythonpath:
    - /path/to/my_model_repo          # prepended ahead of scenario_runner/leaderboard
  env:
    PLANNER_TYPE: traj                # written verbatim into the per-route script
environment:
  activate:
    - source /path/to/conda/etc/profile.d/conda.sh
    - conda activate my_env
```

`agent.pythonpath` and `agent.env` are the whole mechanism. Surveying the internal
orchestrators, every model-specific difference reduced to those two — including the model whose
bundled `leaderboard/` must precede the shared one so an import resolves correctly. The runner
therefore contains zero model-specific code.

`agent.env` may not name a variable the runner owns — `PORT`, `TM_PORT`, `SEED`,
`PYTHONHASHSEED`, `CUDA_VISIBLE_DEVICES`, `GPU_RANK`, `CARLA_ROOT`, `CHECKPOINT_ENDPOINT` and
the rest of the runner's exports. Each of those has its own config field, and a value in
`agent.env` would be fighting the runner for control of worker isolation or the protocol seed,
silently. The runner rejects those names at startup and tells you which field to use instead.
`PYTHONPATH` and `LD_LIBRARY_PATH` are *not* reserved — the runner appends to both, so an entry
there composes rather than replaces.

Every attempt writes its exact command line to
`<out>/_runner/jobs/<scenario>/<level>/<route>_seed42.sh`. When something misbehaves, read that
file — it is the ground truth for what ran.

---

## Output layout

Result paths **mirror** the route tree, so the `{scenario}/{level}/` component can never be
dropped:

```
<out>/<scenario>/<level>/results/<route>_seed42.json     # leaderboard checkpoint
<out>/<scenario>/<level>/logs/<route>_seed42/            # agent SAVE_PATH
<out>/_runner/jobs/<scenario>/<level>/<route>_seed42.sh  # exactly what ran
<out>/_runner/logs/<scenario>/<level>/<route>_seed42.{out,err}
<out>/_runner/state.json                                 # attempt ledger (resume)
<out>/_runner/report.json, report.md                     # final report
```

---

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | every planned route has a **final** record on disk |
| 1 | partial sweep — at least one planned route has no final record |
| 2 | configuration / preflight error |
| 3 | interrupted by signal |
| 4 | all workers quarantined / no usable GPU |
| 5 | fatal agent misconfiguration (sensor configuration rejected) |

**A model failing routes is not a runner failure.** A model that scores
`Failed - TickRuntime` on all 475 routes has produced a valid benchmark result and that run
exits 0. Exit 1 means we do not *know* the answer for some route. Script your automation on the
exit code — a partial sweep can never exit 0.

---

## Configuration reference

`configs/example.yaml` is the annotated reference. YAML needs PyYAML; TOML (Python ≥ 3.11) and
JSON need nothing beyond the standard library. The format is chosen by file extension.

The fields worth understanding before a long run:

### `gpus` — two indices, not one

```yaml
gpus:
  - {cuda: 0, vulkan: 0}
  - {cuda: 1, vulkan: 1}
```

`cuda` pins the **agent** via `CUDA_VISIBLE_DEVICES`. `vulkan` pins the **CARLA server** via
`-graphicsadapter`, which does *not* honour `CUDA_VISIBLE_DEVICES`. If you set only the first,
every simulator renders on adapter 0 while the agents spread across the machine: one GPU
saturates, throughput collapses, and nothing errors. Omitting `vulkan` assumes it equals `cuda`
and prints a warning. Use `--check-gpus` to confirm.

Both indices must be unique across the list; a repeated `vulkan` is the same collapse in slow
motion. To run several workers per GPU, list the GPU once and set
`execution.allow_gpu_stacking: true`.

### `ports` — deterministic, and probed

Worker *i* gets `rpc_base + i*stride` and `tm_base + i*stride`. A CARLA instance occupies three
consecutive ports from its RPC port (RPC, streaming, secondary), so `stride` must be ≥ 4.

The whole block is probed at startup and the runner **refuses to run** if any of it is busy. It
will not relocate itself: silently shifting is how two concurrent runs end up sharing a
simulator. If the block is taken, free it or move `rpc_base`/`tm_base`.

### `resume.mode`

| Mode | Behaviour |
|---|---|
| `skip_terminal` (default) | skip a route only when its record is a legitimate outcome |
| `skip_any_final` | skip any finalized record, including one left by a crash |
| `none` | re-run everything (requires `--force`) |

`skip_any_final` reproduces the internal orchestrator's `--skip_if_final` exactly. It carries a
real hazard: interrupt a sweep while a route holds a `Failed - Agent crashed` checkpoint, resume,
and that route is accepted forever without ever being retried — even though the same run *would*
have retried it had it not been interrupted. `skip_terminal` makes within-run and across-run
retry semantics agree.

### `retry` — two separate budgets

- `record_budget` — attempts where the simulator wrote a *retryable* record.
- `infra_budget` — attempts that wrote no record at all (timeout, segfault, non-zero exit).
- `tickruntime_budget` — its own axis, default **0**, matching the reference sweeps:
  `Failed - TickRuntime` means the agent is slower than CARLA's tick budget, which is a
  model-side property that retrying does not fix.

They are separate so that a bad GPU cannot consume a route's *record* retries and leave behind a
result-shaped artifact that was actually produced by infrastructure.

`worker_quarantine_after` consecutive infra failures pull a worker from the pool: one worker
failing while others progress is the signature of a wedged GPU.

### `routes.manifest` — recommended

Point it at the frozen `routes/MANIFEST.tsv`. The runner then checks the discovered route set
against it by path **and sha256**. This catches an *edited* route XML, which no directory-name
heuristic would — and an edited route silently changes the benchmark definition.
`strict_manifest: true` makes any mismatch a startup error.

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
```

One job per route, same config object, same planning/resume/retry/reporting logic. Concurrency
is `slurm.max_parallel` — **not** `execution.workers`, which sizes the local pool and is ignored
here — and it is gated on **our own submitted job IDs**, not on a `squeue` name grep.
Submission is rate-limited.

**The SLURM backend has never been run against a real scheduler.** Submit a single route and
read the generated `.sbatch` before launching a sweep.

---

## Running the tests

No GPU, no CARLA, no network, no third-party packages:

```bash
python -m unittest discover -s tests -t .
```

153 tests covering the port allocator (at worker counts far above any real GPU count), the
finalization predicate and status taxonomy, path mirroring, manifest integrity, the resume and
budget decision, the exit contract, the ledger, the generated job script, and backend
concurrency — plus 19 end-to-end tests that drive the full supervision loop against a stand-in
evaluator, exercising resume, retry, timeout kill, quarantine and every exit code.

What they do **not** cover is anything that requires a running simulator. See `STATUS.md`.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| exit 2, "reserved port(s) already in use" | another run, or a leftover simulator. Free the ports or move `ports.rpc_base`. The runner will not relocate silently. |
| exit 5 immediately | the agent's `sensors()` was rejected for the configured `track`. Fix the sensor set; it would fail identically on all 475 routes. |
| Every route `Failed - Agent couldn't be set up` | import error or missing checkpoint. Read `<out>/_runner/logs/.../*.err`. |
| Many `Failed - TickRuntime` | the agent is slower than CARLA's tick budget. Model-side; retrying does not fix it. |
| Throughput far below `workers × 1 route` | the `vulkan` indices are probably wrong and every simulator is on one GPU. Run `--check-gpus`. |
| A worker gets quarantined | that GPU is likely wedged. Probe it before reusing it. |
| exit 2, "agent.env may not set variable(s) the runner owns" | your model config sets one of the runner's own variables. Use the config field the error names. |
| exit 2, "gpus[i].vulkan=N is already claimed" | two entries share a Vulkan adapter, which would put both simulators on one GPU. Run `--check-gpus`. |
| SLURM: fewer jobs in flight than expected | concurrency is `slurm.max_parallel`; `execution.workers` does nothing under this backend. |
| Report says routes were skipped with "budget already spent" | a previous run exhausted their retries. Investigate the logs, then delete those result files to force a re-run. |

---

## Documents

- `DESIGN.md` — the locked decisions, with rationale. Read before changing anything structural.
- `STATUS.md` — what is done, what is untested, and what must be validated on hardware.
