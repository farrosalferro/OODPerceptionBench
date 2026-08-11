# OOD-PerceptionBench runner

> **Benchmark release v0.9** — these numbers bind to **arXiv v1** of the paper. A v1.0 release
> (replacement assets + re-run) binds to arXiv v2, and scores from the two are **not**
> comparable. Every report this runner writes carries that stamp.
>
> **This is a FIRST CUT.** The supervision logic is covered by 222 automated tests, but nothing
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

3. **Build the Python environment the evaluator runs in.** A CARLA-only environment is **not**
   enough — the evaluator imports `scenario_runner`, which needs `py_trees` and friends, and the
   failure is `ModuleNotFoundError` *inside the route*, reported as an infrastructure failure
   rather than as a missing dependency. After `setup.sh`, install both:

   ```bash
   pip install -r third_party/carla_garage/Bench2Drive/leaderboard/requirements.txt
   pip install -r third_party/carla_garage/Bench2Drive/scenario_runner/requirements.txt
   # plus CARLA's own Python API from your CARLA build's PythonAPI/carla/dist/
   ```

   Sanity-check it in one line before going further — this is much cheaper than finding out
   nineteen minutes into a route:

   ```bash
   <your-python> -c "import carla, py_trees, numpy; print('ok')"
   ```

4. **Copy and edit a config.** `configs/example.yaml` documents every field.
   There are no defaults for paths, hosts, queues or environments — a missing required field is
   an error that names the field, not a fallback to somebody else's filesystem.

5. **Prove the plumbing before spending GPU-hours.** The shipped reference agent
   (`reference_agent/constant_velocity_agent.py`) drives forward at a constant speed. It scores
   badly on purpose; its job is to show that CARLA starts on the right GPU, the route loads, the
   agent interface binds, criteria attach, and a finalized checkpoint lands in the right place.

   ```bash
   python run_benchmark.py --config configs/reference_agent.yaml --limit 1 --dry-run
   python run_benchmark.py --config configs/reference_agent.yaml --limit 1
   ```

6. **Run the sweep.**

   ```bash
   python run_benchmark.py --config my_config.yaml --workers 4
   ```

   Budget roughly **0.12 GPU-hours per route**, so ≈ 58 GPU-hours for the full 475-route set.
   At 4-way parallelism that is about 15 hours.

Interrupt with `Ctrl-C` at any point. Re-running the same command resumes: completed routes are
skipped and retry budgets carry over.

---

## Bringing your own agent

The runner adds **no API of its own**: it drives the pinned Bench2Drive evaluator, which
drives your agent. Anything that already runs under Bench2Drive works with no code changes.

> **If you are porting a *stock* Leaderboard 2.0 agent, one signature has to change.** This
> README claimed the stock interface worked unchanged until 2026-08-11, and it was wrong — the
> first hardware validation caught it. The pinned Bench2Drive evaluator diverges from stock:
>
> ```python
> # self.agent_instance.setup(args.agent_config)          # stock, commented out upstream
> self.agent_instance.setup(args.agent_config, save_name)  # what actually runs
> ```
>
> A stock `setup(self, path_to_conf_file)` therefore raises
> `TypeError: setup() takes 2 positional arguments but 3 were given` **before the simulation
> starts**, and the route settles as `Failed - Agent couldn't be set up`. Because that is a
> legitimate status, the sweep exits 0 and reports the route complete — so this fails *quietly*
> unless you read the status. Accept the extra argument with a default:
>
> ```python
> def setup(self, path_to_conf_file, save_name=None):
> ```
>
> That keeps the agent valid under both callers. Every agent in this ecosystem does the same;
> carla_garage's own `team_code` agents declare
> `setup(self, path_to_conf_file, route_index=None, traffic_manager=None)`. The shipped
> reference agent is the worked example.

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
| 0 | every planned route has a **settled** result |
| 1 | partial sweep — at least one planned route has no settled result |
| 2 | configuration / preflight error |
| 3 | interrupted by signal |
| 4 | all workers quarantined / no usable GPU |
| 5 | fatal agent misconfiguration (sensor configuration rejected) |

**A model failing routes is not a runner failure.** A model that scores
`Failed - TickRuntime` on all 475 routes has produced a valid benchmark result and that run
exits 0. Exit 1 means we do not *know* the answer for some route — either nothing was written,
or the record on disk was preserved from an earlier attempt and this run never refreshed it.
Script your automation on the exit code — a partial sweep can never exit 0.

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

### `retry` — four separate budgets

- `record_budget` — attempts that **ended on their own** having written a *retryable* record.
- `infra_budget` — **consecutive** attempts that wrote no record at all (timeout, segfault,
  non-zero exit), and attempts that never launched. Consecutive because what it bounds is a
  machine that is broken *now*: any attempt that produces a record of its own clears the count,
  so hiccups scattered hours apart across a long sweep cannot add up to a gate on a route that
  has been running fine. The lifetime total is reported separately (`attempts.infra_total`) and
  gates nothing. **Read every budget as "N attempts on this axis, then accept or give up", not
  "N retries."** `record_budget: 3` gives three record-producing attempts; `infra_budget: 3`
  gives three attempts that wrote nothing. The one place they differ is at zero, and only
  because this is the sole budget consulted *before* an attempt runs: `infra_budget: 0` still
  means one try, since the gate needs a counter that was actually charged. Say `1` if that is
  what you mean.
- `tickruntime_budget` — its own axis, default **0**, matching the reference sweeps:
  `Failed - TickRuntime` means the agent is slower than CARLA's tick budget, which is a
  model-side property that retrying does not fix.
- `killed_budget` — attempts the runner **killed** (wall clock, fault, quarantine) while a
  crash-shaped record was on disk. That record is ambiguous by construction: it is what a dying
  simulator writes, and also what a route that finished and hung in teardown leaves behind. It
  gets its own bounded axis so a kill can neither spend the model's record retries nor leave the
  route unable to ever settle.

They are separate so that a bad GPU cannot consume a route's *record* retries and leave behind a
result-shaped artifact that was actually produced by infrastructure. Which budget an attempt
charges is decided by **how the attempt ended**, never by what happens to be on disk — the full
table is normative in `DESIGN.md` §6A.

`worker_quarantine_after` consecutive infra failures pull a worker from the pool: one worker
failing while others progress is the signature of a wedged GPU.

`infra_budget` is the one budget that never settles a route: exhausting it means *we do not know
this route's answer*, so the run exits 1 rather than presenting whatever is on disk as a result.
That verdict is persisted, so it survives into later runs — deliberately, but not for ever.
Repair the machine and re-run with **`--retry-infra-exhausted`**:

```bash
python run_benchmark.py --config my_config.yaml --retry-infra-exhausted
```

It clears the infrastructure counter of exactly the routes that hit the gate, and nothing else:
every result file, every settlement bit and every other budget is untouched, and the lifetime
infra count still records what happened. It buys attempts, never answers — which is why, unlike
`--resume-mode none`, it does not need `--force`. Using it is recorded in the report.

Two things worth knowing before you reach for it. It is applied to the **whole ledger** before
planning, so `--limit` and a narrowed `--routes` do not scope it. And it is safe to preview:
`--dry-run` writes nothing at all — it never saves the ledger — so combining the two shows you
which routes this would unlock and what would then run, without spending the recovery you asked
it to describe.

### Resuming a tree written by an older runner

Three different questions get three different answers, and the runner tells you about each:

| what changed | how you find out |
|---|---|
| a setting you chose | `config_digest` differs → *"produced by a DIFFERENT configuration"* |
| the runner build | `runner.version` is stamped into every report |
| the **rules** — which budget a cell charges, whether it settles | `accounting_epoch` in the ledger → a warning naming both epochs |

The third exists because the second and third are easy to conflate. Adding a config key at its
default does **not** move the digest (a key nobody set is not a setting they changed), but the
`DESIGN.md` §6A model changed alongside one such key — before epoch 2, every final retryable
record charged the `record` axis, including attempts that ended abnormally, which now charge the
separate bounded `killed` axis. Resuming across that boundary is fine and the counters carry over
untouched; routes still in flight may simply settle after a different number of attempts. You are
told so you can decide whether that matters, not because anything is wrong.

### Complete vs. settled

A route counts as complete only when its result file is final **and** the ledger says this run
settled it. The two come apart in one direction that matters: a launch that fails preserves the
record it was about to replace (it must never be destroyed), but preserving a record is not
answering the route. Such a route is reported under *"no settled result"* with
`unsettled_reason: unrefreshed_record` and the run exits 1.

`unsettled_reason` separates three different problems that share one headline number, and it is
decided ledger-first: `not_reached` (the planning loop never got to this route — it stops at a
fatal agent abort), then `unrefreshed_record` (a record is on disk from an earlier attempt and
the retries this run planned never ran), then `no_record` (nothing final was ever written).

A model failing routes is still not a runner failure: `Failed - TickRuntime` on all 475 routes,
or a retryable record whose retry budget is spent, are settled results and exit 0.

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

188 tests covering the port allocator (at worker counts far above any real GPU count), the
finalization predicate and status taxonomy, path mirroring, manifest integrity, the resume and
budget decision, the attempt-accounting model of `DESIGN.md` §6A, the exit contract, the ledger,
the generated job script, and backend concurrency — plus 22 end-to-end tests (19 in
`tests/test_integration_local.py`, and three more for infra-gate recovery and for the trap
under a simulator that crashes into the shared stderr) that drive the full
supervision loop against a stand-in evaluator, exercising resume, retry, timeout kill,
quarantine and every exit code.

One of them parses the normative table in `DESIGN.md` §6A.5 and drives the settle path for all
24 of its cells, so the document and the code cannot drift apart silently — that is a defect
this component has already had twice.

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
| Report says routes were skipped with "budget already spent" | a previous run exhausted their *record* retries. The record on disk is the answer; investigate the logs before deciding it is wrong. |
| Report says the **infrastructure** retry budget is gone | the machine, not the model. Fix it, then re-run with `--retry-infra-exhausted` — no result file is touched and no other budget moves. |
| Resuming an output root warns "produced by a DIFFERENT configuration" | some setting really did change. Adding a key that holds its default does *not* trigger this (see `DESIGN.md` §6A.11); a changed agent or CARLA build should use a fresh output root. |

---

## Documents

- `DESIGN.md` — the locked decisions, with rationale. Read before changing anything structural.
- `STATUS.md` — what is done, what is untested, and what must be validated on hardware.
