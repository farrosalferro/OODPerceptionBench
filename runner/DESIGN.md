# OOD-PerceptionBench portable runner — design

> **Artifact version:** runner design v1, drafted for **OOD-PerceptionBench release v0.9**,
> which binds to **arXiv v1** of the paper. A v1.0 release (replacement assets + re-run) binds
> to arXiv v2; scores produced by the two are **not** comparable and the runner stamps which
> one it targeted into every report it writes.
>
> **Status of the accompanying code: FIRST CUT. Not validated on real hardware.** See
> `STATUS.md` for exactly what is and is not done. This document is the part meant to be
> durable — it fixes the decisions that are expensive to change later.

---

## 0. What this component is

The benchmark is 475 closed-loop CARLA routes. Evaluating one model is ~58 GPU-hours. Today
that is only possible with a stack of cluster-specific orchestrators that hardcode absolute
storage paths, specific hostnames, an SSH submit host, conda environment names, SLURM
partitions, and a cap-gating protocol built out of a text file on disk. None of that can ship.

The runner replaces them with:

```
run_benchmark.py --config <file> [--routes DIR] [--out DIR] [--agent PATH] [--workers N]
```

and inverts the control flow. The old orchestrators *generate* a bash script per route and
hand it to a scheduler. The runner *owns* the loop: it plans the route set, allocates
resources deterministically, launches, supervises, retries under an explicit budget, and
returns an exit code that is honest about whether the sweep finished.

Two backends, one config object: `local` (a worker pool on one machine) and `slurm` (one job
per route). The local backend is the primary, documented path.

---

## 1. Design principles

1. **Every silent failure mode in this benchmark is a correctness failure.** A missing prop
   spawns nothing and the route still returns a plausible driving score. A partial sweep that
   exits 0 gets aggregated and published. The runner is designed around the assumption that
   *the visible output looks fine when things are wrong*, so anything ambiguous fails loud.
2. **Collision-proof by construction, then verified anyway.** Port and GPU assignment are pure
   functions of the worker index. There is no search, no randomness, no "find a free one".
   Startup then probes the whole reserved block and refuses to run if anything is occupied.
3. **Derived, not composed.** Result paths mirror the route tree's relative path rather than
   being reassembled from parts. The known trap — dropping the `{scenario}/{level}/` component
   and making a finished sweep look empty — becomes unrepresentable.
4. **Zero site defaults.** No path, hostname, environment name, partition, or account has a
   default. A required field that is missing is a startup error naming the field, not a
   fallback to somebody else's filesystem.
5. **Faithful to the lab's hard-won defences.** Submission rate limiting, finalized-result
   skipping, bounded resubmission and orphan reaping each exist because something broke. They
   are carried over, generalised, and documented — not dropped.

---

## 2. Decision D1 — configuration surface

**One file. YAML (primary), TOML or JSON also accepted, chosen by extension.** YAML needs
PyYAML; TOML uses stdlib `tomllib` (Python ≥ 3.11); JSON needs nothing. The runner itself has
**no third-party dependency** if you use TOML or JSON.

Only these CLI flags override the file, because they are the ones that legitimately change per
invocation: `--routes`, `--out`, `--agent`, `--workers`, `--backend`, `--resume-mode`,
`--seed`, `--dry-run`, `--limit`. Everything else lives in the file so that a run is
reproducible from one artifact.

### Schema (v1)

```yaml
version: 1

benchmark:
  release: v0.9          # stamped into every report
  arxiv_version: v1      # which paper version these numbers bind to
  seed: 42               # protocol seed; overriding is allowed but flagged
  repetitions: 1

carla:
  root: <dir>            # REQUIRED. must contain CarlaUE4.sh
  client_timeout_s: 900  # -> --timeout

leaderboard:
  work_dir: <dir>              # REQUIRED. Bench2Drive checkout root (weather.xml lookup)
  root: <dir>                  # REQUIRED. <work_dir>/leaderboard
  scenario_runner_root: <dir>  # REQUIRED. <work_dir>/scenario_runner
  evaluator: leaderboard/leaderboard_evaluator.py   # relative to leaderboard.root

agent:
  entrypoint: <file>     # REQUIRED. stock CARLA Leaderboard 2.0 AutonomousAgent
  config: ""             # opaque string passed verbatim to --agent-config
  track: SENSORS         # SENSORS | MAP
  pythonpath: []         # prepended before scenario_runner/leaderboard roots, in order
  env: {}                # extra env vars, written verbatim. May not name a runner-owned
                         # variable (PORT, TM_PORT, SEED, CUDA_VISIBLE_DEVICES, ...)
  working_dir: null      # cd here before the evaluator; null inherits

environment:
  activate: []           # shell lines run before python (conda/venv/module). empty by default
  ld_library_path_prepend: []
  python: python3

execution:
  backend: local         # local | slurm
  workers: 1             # LOCAL pool size; ignored when backend == slurm
  route_timeout_s: 3600
  poll_interval_s: 10
  post_kill_cooldown_s: 10
  allow_gpu_stacking: false

gpus:                    # ordered list; worker i uses gpus[i % len(gpus)].
                         # Both `cuda` and `vulkan` must be unique across the list.
  - cuda: 0
    vulkan: 0

ports:
  rpc_base: 20000
  tm_base: 30000
  stride: 10
  probe: true

retry:
  record_budget: 3       # attempts after a retryable *record* was written
  infra_budget: 3        # attempts that died without writing any record
  tickruntime_budget: 0  # 0 = match the lab: TickRuntime is model-side, do not retry
  worker_quarantine_after: 3

resume:
  mode: skip_terminal    # skip_terminal | skip_any_final | none

output:
  root: <dir>            # REQUIRED
  record_carla: false

routes:
  root: <dir>            # REQUIRED
  manifest: null         # optional path to routes/MANIFEST.tsv
  strict_manifest: false

slurm:                   # only read when backend == slurm
  partition: null
  account: null
  qos: null
  nodelist: null
  exclude: null
  time: "02:00:00"
  cpus_per_task: 4
  mem: 24G
  gres: "gpu:1"
  max_parallel: 8      # concurrency under this backend (NOT execution.workers)
  submit_interval_s: 1.0
  extra_directives: []
```

**Why `agent.env`, `agent.pythonpath` and `agent.working_dir` exist.** Surveying all ~18
internal orchestrators, every model-specific difference reduces to (a) extra environment
variables, (b) `PYTHONPATH` ordering — one model needs its own bundled `leaderboard/` ahead of
the shared one so an import resolves to the right `team_code` — and (c) `cd`-ing to the model's
repository root before launching. Expressing all three as opaque config means the runner
contains **zero** model-specific code and a new model needs no runner change. This is the
mechanism that makes Tier B ("evaluate your own model") real.

**`agent.env` is opaque, but it is not sovereign.** It is model-supplied config, and the
generated script also exports the runner's own variables — the RPC and traffic-manager ports,
the two GPU indices, the protocol seed, the checkpoint path. If a model config could set one of
those, it would be fighting the runner for control of worker isolation or determinism, and
**every symptom of winning that fight is silent**: `PORT`/`TM_PORT` puts two workers on one
simulator, `CUDA_VISIBLE_DEVICES` unpins the GPU, `SEED`/`PYTHONHASHSEED` changes the seed per
model while the result filename still says `_seed42`. Two independent defences, because either
alone is one refactor away from being lost:

1. **Reserved names are rejected at config load**, with an error naming the config field to use
   instead (`oodbench.jobscript.RESERVED_ENV` → `oodbench.config.build`). The reserved set is
   exactly the variables the runner exports unconditionally.
2. **Runner-owned exports are emitted last.** Order in the generated script is
   `environment.activate` → `agent.env` → everything the runner owns. So even a reserved name
   that reached the script by some other path is overwritten rather than winning.

`PYTHONPATH` and `LD_LIBRARY_PATH` are deliberately **not** reserved: the runner *appends* to
both (`…:"${VAR:-}"`), so an `agent.env` entry composes with it instead of replacing it.

**Validation is strict and total.** Unknown top-level keys and unknown keys inside known
sections are errors, not warnings — a typo in a config key must never silently fall back to a
default. Required paths are existence-checked at startup with the field name in the message.

---

## 3. Decision D2 — deterministic port allocation

This is the decision most likely to cost a week if improvised, so it is fixed here first.

### What CARLA actually consumes per instance

| Port | How it is set | Notes |
|---|---|---|
| RPC `P` | `-carla-rpc-port=P` | the one thing we control explicitly |
| Streaming `P+1` | implicit | CARLA derives it from the RPC port |
| Secondary `P+2` | implicit | multi-GPU/secondary-server channel; reserved regardless |
| Traffic manager `T` | `--traffic-manager-port=T` | independent of `P`; a separate RPC service |

So one CARLA instance owns a **3-port contiguous window** plus **one** TM port.

### The allocator

```
worker i ∈ [0, W):
    rpc(i) = ports.rpc_base + i * ports.stride
    tm(i)  = ports.tm_base  + i * ports.stride
```

Pure function of the worker index. Not of the route, not of the attempt number, not of
scheduling order, not of the GPU. **A worker owns its ports for the entire sweep.** Because at
most one route runs in a worker slot at a time, two routes can never contend for a port under
*any* ordering, restart or crash-recovery path — the property holds without needing to reason
about the schedule at all.

Startup assertions (all fatal, exit 2):

- `stride ≥ 4` (3-port CARLA window + 1 margin); default 10.
- Both blocks fit under 65535 and start above 1024.
- The RPC block `[rpc_base, rpc_base + (W-1)*stride + 3)` and the TM block
  `[tm_base, tm_base + (W-1)*stride + 1)` are **disjoint**.
- The flattened list of every reserved port contains no duplicates. Redundant given the above;
  kept as a cheap invariant check that a unit test can drive at `W = 64` on a 1-GPU laptop —
  the allocator is verifiable independently of hardware, which is the point.

### The upward-scan hazard, and why probing is mandatory

The vendored `leaderboard_evaluator.py` does **not** use the port it is given verbatim. It calls
`find_free_port(args.port)`, which scans **upward with no upper bound** until it can bind. Same
for the traffic-manager port. If worker *i*'s RPC port is occupied — most plausibly by its own
zombie CARLA from a previous attempt — the evaluator will happily walk into worker *i+1*'s
window and two live CARLA servers end up interleaved. Nothing crashes; the routes just quietly
share a simulator.

Two defences, both required:

1. **Probe the entire reserved block at startup** using the *same* bind call `find_free_port`
   uses (`bind(("localhost", port))`, no `SO_REUSEADDR`), plus a `0.0.0.0` probe because that
   is what the CARLA server itself binds. If any port is busy, abort with the offending port
   number and the suggestion to move `ports.rpc_base`. Never auto-shift the base — silently
   relocating means two concurrent runs on the same host can overlap.
2. **Reap and re-verify the worker's own window immediately before every launch.** If it is
   still busy after the reap and cooldown, that worker is quarantined rather than allowed to
   wander upward.

Given a genuinely free port, `find_free_port` returns it unchanged, so the port the runner
allocated is the port CARLA binds — which is also what makes reaping by port possible (§7).

**Not adopted:** the internal orchestrators pick a port with
`comm -23 <(seq base base+49) <(ss -Htan ...) | shuf | head -n 1` — a random free port from a
50-wide window, chosen in a shell subprocess. It has a check-to-use race (the port can be taken
between `ss` and `bind`), it is non-deterministic so a collision is not reproducible, and the
window it draws from is not exclusively owned. Determinism plus exclusive ownership removes the
race instead of narrowing it.

---

## 4. Decision D3 — GPU pinning needs **two** independent indices

The single most common way to think this is done and be wrong.

- The **agent** (PyTorch) is pinned with `CUDA_VISIBLE_DEVICES`.
- The **CARLA server** renders with Vulkan and is pinned with `-graphicsadapter=<n>`, surfaced
  by the evaluator as `--gpu-rank`. **Vulkan does not honour `CUDA_VISIBLE_DEVICES`.**

Setting only `CUDA_VISIBLE_DEVICES` puts every worker's *simulator* on adapter 0 while their
*agents* spread across the machine — the box looks busy, one GPU is saturated, throughput
collapses, and nothing errors. One of the internal local runners has exactly this shape.

Worse, the CUDA index and the Vulkan adapter index are **not guaranteed equal**. They are
independent enumerations (CUDA ordering is affected by `CUDA_DEVICE_ORDER`; Vulkan has its
own). On a single-GPU machine both are 0 and the distinction is invisible, which is precisely
why it survives testing and then bites on a multi-GPU host.

**Decision:** `gpus` is a list of explicit `{cuda, vulkan}` pairs. `vulkan` may be omitted, in
which case it defaults to `cuda` **and the runner prints a warning once**, because that default
is an assumption about the host, not a fact. `run_benchmark.py --check-gpus` prints the CUDA
device list with PCI bus IDs beside the Vulkan adapter list so the user can write the mapping
down once.

**Both indices must be unique across the list**, and for asymmetric reasons. A repeated `cuda`
is an obvious duplicate entry. A repeated `vulkan` is the silent one: it puts two CARLA
*servers* on one physical GPU while their agents sit on different ones — exactly the collapse
described above, with nothing to notice it by except throughput. Validating only `cuda` (as the
first cut did) leaves the more dangerous half of the pair unchecked, so both are now errors.
Several workers per GPU is expressed by listing the GPU **once** and setting
`execution.allow_gpu_stacking`, not by repeating an adapter index.

Worker→GPU is `gpus[i % len(gpus)]` — deterministic. If `workers > len(gpus)` the runner
refuses unless `execution.allow_gpu_stacking: true`, because two CARLA servers on one GPU is a
throughput and VRAM hazard the internal runbook explicitly warns against.

---

## 5. Decision D4 — result layout and resume

### Layout: mirrored, not composed

```
<routes.root>/<REL>/<stem>.xml
    ->  <output.root>/<REL>/results/<stem>_seed<SEED>.json      # --checkpoint
        <output.root>/<REL>/logs/<stem>_seed<SEED>/             # SAVE_PATH
        <output.root>/_runner/jobs/<REL>/<stem>_seed<SEED>.sh
        <output.root>/_runner/logs/<REL>/<stem>_seed<SEED>.{out,err}
```

`REL` is `route_xml.relative_to(routes.root).parent`, verbatim. For the canonical tree that is
`{category}/{scenario}/{level}` or `{scenario}/{level}` depending on where you point `--routes`.

The known trap is that result paths carry a leading `{scenario}/{level}/` component and code
that reassembles the path by concatenating known parts drops it, then reports a completed sweep
as empty and re-runs 475 routes. Mirroring makes that unrepresentable: the same
`route_xml -> result_path` function is used to plan, to resume, and to report, so the three can
never disagree. There is one function; if it were wrong, nothing would work at all rather than
resume being subtly wrong.

The seed is in the **filename**, so a resumed run under a different seed cannot silently mix
with an earlier one.

### Finalization predicate

A result file is **final** iff all of:

- it exists and parses as JSON
- `_checkpoint.progress` is a list of length ≥ 2
- `progress[0] >= progress[1]` and `progress[1] > 0`
- `_checkpoint.records` is non-empty

This matches the internal `result_is_final()` except for `progress[1] > 0`, added so that a
degenerate `[0, 0]` cannot pass. The change is strictly conservative — it can only ever cause
the runner to re-run something, never to skip something unfinished.

### Status taxonomy

Verified against the vendored `statistics_manager.py`/`leaderboard_evaluator.py` and against
~4,000 real records from the seed-42 sweep:

| Status | Observed | Retried? | Rationale |
|---|---|---|---|
| `Perfect` | — | no | success, 0 infractions |
| `Completed` | 3556 | no | success |
| `Failed - Agent got blocked` | 43 | no | legitimate benchmark outcome |
| `Failed - Agent deviated from the route` | 1 | no | legitimate |
| `Failed - Agent timed out` | — | no | route-timeout criterion; legitimate |
| `Failed - TickRuntime` | 177 | budget, **default 0** | model-side (agent slower than the tick budget). The internal orchestrator does not retry it and the runbook says not to; a fully degenerate model would otherwise burn the entire retry budget on every route |
| `Failed` (bare) | 221 | yes | ambiguous; the internal orchestrator retries it, and bare-`Failed` records do appear in the published data after budget exhaustion |
| `Failed - Agent couldn't be set up` | — | yes | usually a deterministic import/checkpoint error, but transient on a wedged GPU |
| `Failed - Agent crashed` | 1 | yes | |
| `Failed - Simulation crashed` | — | yes | |
| `Failed - Agent's sensors were invalid` | — | **never; FATAL** | the agent's sensor set is rejected before the route runs. It will fail identically on all 475 routes. Abort the whole sweep immediately rather than burn hours proving it |
| anything else | — | yes, and warn | schema drift must be loud, not absorbed |

The retry set is exactly the four statuses the internal orchestrator resubmits, plus explicit
handling for TickRuntime, the sensors-invalid fatal case, and unknown strings.

### Resume modes

| Mode | Skips a route when |
|---|---|
| `skip_terminal` **(default)** | its record is final **and** its status is not in the retry set |
| `skip_any_final` | its record is final, whatever the status — **exactly** the internal `--skip_if_final` |
| `none` | never; requires `--force` because it overwrites results |

**Why the default differs from the internal tool, and this is flagged for the user.** Under
`skip_any_final` there is a real, silent data-corruption path: interrupt a sweep while a route
holds a `Failed - Agent crashed` checkpoint, resume, and that route is accepted forever without
ever being retried — even though the same run *would* have retried it had it not been
interrupted. Within-run and across-run retry semantics disagree. `skip_terminal` makes them
agree. The legacy behaviour is preserved verbatim as an option so anyone reproducing the
internal sweeps bit-for-bit can select it. **This choice is called out in the hand-back as
needing user sign-off; it is the one place the runner deliberately does not match the internal
tool's default.**

Attempt counts are persisted in `<output.root>/_runner/state.json` and **reloaded on resume**,
so the retry budget is a property of the route, not of the process. Three interrupted restarts
cannot buy 3 × the budget.

`state.json` is written atomically (temp file + `os.replace`) after every transition, so a
`SIGKILL` of the runner cannot leave a torn ledger.

---

## 6. Decision D5 — failure handling and the exit contract

### Two separate budgets

Consuming the same budget for both classes lets a bad GPU exhaust a route's retries and mark it
"attempted and failed" — a result-shaped artifact produced by infrastructure.

- `retry.record_budget` — attempts where the simulator produced a *retryable record*.
- `retry.infra_budget` — attempts that produced **no record at all**: wall-clock timeout, the
  process died with a fault pattern in stderr, non-zero exit with no checkpoint written,
  pre-launch port/GPU verification failure, **and any attempt that never launched**.
- `retry.tickruntime_budget` — its own axis, default 0.

The report states which budget each incomplete route exhausted. An infra-exhausted route is
never presented as a benchmark result.

**A failed launch is infrastructure, whatever is on disk.** A route holding a retryable record
with budget left is planned as RUN, so it reaches the backend with a *valid benchmark result*
already on disk. Both backends take that record aside and restore it if the launch fails (busy
ports, a refused `sbatch`), which means the accounting step is then looking at an **earlier**
attempt's output. Reading it as this attempt's result was a two-part silent failure: it charged
the *record* budget for an infrastructure fault, and once that budget ran out it marked the
route finished with the stale record frozen in as the answer — having never retried it. So the
outcome of the launch, not the presence of a file, decides which budget is charged. When the
infra budget is exhausted this way the report says explicitly that the record on disk is from an
earlier attempt and was preserved, not refreshed.

**An operator interrupt charges nothing — in either budget.** Ctrl-C kills whatever is in
flight; those routes did not fail, we stopped them. Charging the infra budget would mean three
interrupted sweeps permanently abandon a route that never actually ran, and abandon it *as*
"this route has NOT produced a benchmark result" — precisely the silent loss the budget exists
to prevent.

The record budget needs the same protection, and this is not the edge case it sounds like:
killing the worker's process group takes CARLA down with it, and the evaluator's crash handler
writes a *final* `Failed - Simulation crashed` record on its way out. That record is an artefact
of the interrupt, not an outcome the model produced — so the commonest shape of an interrupted
route is a route with a retryable final record. Charging it let a handful of Ctrl-Cs spend a
route's real retries and then freeze the interrupt artefact in as its benchmark result. An
attempt settled with `interrupted=True` therefore records a reason, spends no budget of any
kind, and does not count toward worker quarantine. The record is preserved — the runner never
deletes one — and the next run re-plans the route with its budget intact and starts it from a
clean slate.

The one exception is an interrupted attempt whose record is *accepted* (`Completed`,
`Perfect`, …): that route genuinely finished before anything was killed, so it stays finished.

### Worker quarantine

Consecutive infra failures **on the same worker** while other workers make progress is the
signature of one wedged GPU. After `retry.worker_quarantine_after` consecutive infra failures a
worker is removed from the pool and its routes are returned to the queue. If every worker
quarantines, the run aborts with exit 4 rather than grinding the whole route set into infra
failures. (A wedged GPU on the internal cluster once absorbed 64 % of a sweep's submissions,
because fail-fast without quarantine feeds a retry loop.)

### Exit codes

| Code | Meaning |
|---|---|
| **0** | every planned route has a **final** record on disk |
| **1** | partial sweep — at least one planned route has no final record |
| **2** | configuration / preflight error (bad config, busy port block, missing CARLA, empty route set, manifest mismatch under `--strict-manifest`) |
| **3** | interrupted by signal; children reaped and state written |
| **4** | all workers quarantined / no usable GPU |
| **5** | fatal agent misconfiguration (`Failed - Agent's sensors were invalid`) |

The load-bearing distinction: **a model failing routes is not a runner failure.** A model that
scores `Failed - TickRuntime` on all 475 routes has produced a valid — if unflattering —
benchmark result, and that run exits **0**. Exit 1 means *we do not know* the answer for some
route. Conflating "the model did badly" with "the sweep did not finish" would either make
honest results look like infrastructure failures or, far worse, let an unfinished sweep pass as
a result.

There is no path where a route without a final record yields exit 0. `--dry-run` prints the
plan and exits 0 without running anything, which is the only "success without results" case and
it produces no result files to mistake for one.

---

## 7. Decision D6 — process supervision and orphan reaping

Children are launched with `start_new_session=True` so each occupies its own process group.
The evaluator deliberately does *not* `setsid` the CARLA server it spawns, so CARLA inherits the
evaluator's group and a `killpg` on the worker takes the simulator with it. That property is
load-bearing and the generated job script must not break it (no `setsid`, no `nohup`, no
backgrounding of the python call).

If the evaluator dies uncleanly, CARLA is orphaned and holds VRAM and a port. The reaper is:

> scan `/proc` for processes owned by this uid whose cmdline contains
> `-carla-rpc-port=<P>` for a `P` in **this worker's** reserved window; `SIGTERM`, then
> `SIGKILL` after a grace period.

This is deliberately narrower than the internal reaper, which kills any CARLA whose parent is
PID 1. That heuristic is fine on a dedicated node and destructive on a shared workstation where
another user's CARLA can be reparented for entirely benign reasons. Reaping by *our own
allocated port* touches only processes the runner is responsible for — which is only possible
because ports are deterministic (§3).

Additional per-attempt hygiene, all inherited from the internal runners:

- delete the checkpoint file before every attempt (a stale checkpoint plus resume semantics
  makes a route "finish" instantly against old data) — but **hold its bytes until the launch
  actually succeeds, and put them back if it does not**. A route carrying a retryable record
  such as `Failed - Agent crashed` is planned as RUN with budget remaining, so it reaches
  `submit()` with a *valid benchmark result* on disk. Deleting before the launch is confirmed
  means a busy port or a refused `sbatch` silently converts a route that had a result into one
  the report calls "NO benchmark result was produced". Delete-then-restore keeps both
  properties: every attempt starts clean, and no launch that never happened destroys data.
  (`backends.base.take_checkpoint_aside` / `restore_checkpoint`.)
- scan the tail of stderr for `Segmentation fault`, `Aborted (core dumped)`, `Illegal
  instruction`, `Bus error` and treat a match as an infra failure even if the process has not
  exited yet
- enforce `execution.route_timeout_s` as wall clock, kill on breach, cool down before reusing
  the slot

### The `--resume` argparse trap (found while reading the vendored evaluator)

`leaderboard_evaluator.py` declares `parser.add_argument('--resume', type=bool, default=False)`.
With `type=bool`, argparse applies `bool()` to the *string*, so `--resume=0` evaluates
`bool("0")` → **True**. Every internal per-route script passes `--resume=0` and has therefore
been running with the evaluator's in-file resume **enabled**. It is harmless there only because
each XML holds a single route and the orchestrator unlinks the checkpoint before every
resubmit.

The runner **omits `--resume` entirely** (the default is a real `False`) and unlinks the
checkpoint before every attempt. Behaviourally identical to the internal runs, minus the trap.

---

## 8. Decision D7 — determinism

The requirement is that seed 42 reaches the agent and the simulator identically regardless of
worker index, GPU, port, or scheduling order.

- `seed = benchmark.seed + repetition_index`. A function of `(route, repetition)` **only**.
  Nothing in the seed path reads the worker index, the GPU, the port, or the attempt number.
- It is delivered three ways: `--traffic-manager-seed=<seed>`, `SEED=<seed>` in the environment
  (several agents read it directly), and `PYTHONHASHSEED=<seed>`.
- The seed appears in the result filename, so a mismatch is visible rather than merged.
- At report time the runner re-derives the expected seed for every result file it counts and
  fails the report if a file's embedded seed disagrees with the configured one.
- The plan is ordered by relative POSIX path, so the *plan* is byte-identical across runs.
  Execution order across workers is not deterministic and cannot be; nothing seed-bearing
  depends on it.
- Overriding `--seed` off the protocol value is allowed but prints a prominent warning and sets
  `protocol_seed_deviation: true` in the report.

**What is explicitly not claimed:** CARLA is not bit-reproducible across GPU models, driver
versions or CARLA builds. The runner guarantees identical *inputs*, not identical *outputs*.
Saying otherwise would be a false reproducibility claim, and this benchmark publishes collision
rates.

---

## 9. Decision D8 — route-set integrity

The route set *is* the benchmark definition. The runner does not pattern-match directory names
to guess which routes are real — the scaffolding trees that must never be evaluated
(`*_debug`, `*_missing`, `*_smoke`, `*_fix`, `*_revision`, `family_*`) live outside the released
tree, and hardcoding those patterns would also reject the smoke split the release intends to
ship.

Instead: `routes.manifest` optionally points at the frozen `routes/MANIFEST.tsv`. When set, the
runner checks that the discovered set matches the manifest by path **and sha256**, and reports
extras/missing/modified. `routes.strict_manifest: true` makes any mismatch a startup error
(exit 2). This consumes the route-freeze deliverable directly, and it is a far stronger check
than any name heuristic: it catches an edited XML, which no pattern ever would.

Without a manifest the runner still prints the discovered count and per-directory breakdown so
that "70 / 162 / 243 = 475" is verifiable at a glance before ~58 GPU-hours are spent.

---

## 10. Decision D9 — SLURM backend

Same config object, `execution.backend: slurm`. One SLURM job per route.

**Concurrency is `slurm.max_parallel`, and it is the *backend* that says so.** The supervision
loop opens one slot per unit of backend concurrency and indexes the backend's per-slot
resources — ports here, ports and a GPU locally — by the slot number. Reading
`execution.workers` in the loop instead meant `slurm.max_parallel` gated nothing at all (it
sizes the *local* pool and defaults to 1), and a slot index could run past the reserved port
block, where `pairs[worker % len(pairs)]` wrapped it and handed two concurrently running jobs
the same RPC and traffic-manager ports. Backends therefore expose `concurrency`, the loop reads
only that, the SLURM backend reserves exactly that many port pairs, and an out-of-range slot is
**refused** rather than wrapped. `execution.workers` is ignored under this backend, and the
config warns if it was set to something that suggests otherwise.

Dropped, as cluster-specific and not generalisable:

- `ssh <submit-host>` — the runner is submitted from wherever `sbatch` works.
- the **cap-gate file on disk** our internal orchestrators polled for a job limit — concurrency
  here is `slurm.max_parallel`, an integer in the config. The file-on-disk protocol existed to
  be editable mid-run; that is a niche need that does not justify a filesystem side channel in
  a public tool.
- per-pool `run_files_<prefix>/` namespacing — needed because two pools sharing an experiment
  name overwrote each other's job scripts and silently ran the wrong route. Job scripts here are
  under the mirrored per-route path, so two pools writing to distinct output roots cannot
  collide, and two pools sharing an output root are the same sweep.

Kept, because each was a real incident:

- **Submission rate limiting** (`slurm.submit_interval_s`) — caps a runaway submit loop.
- **Concurrency gating by our own job IDs, not by grepping `squeue` for a name prefix.** The
  internal gate counted jobs whose name matched a prefix, which also matched the orchestrator's
  own job — so the pool ran one slot short, and if you renamed the orchestrator to dodge it, a
  second pool sharing the prefix would collide with the first. Tracking the IDs we submitted
  removes the whole class.
- **Bounded resubmission** with the same two-budget accounting as local.
- **Finalized-result skipping** on resume, same predicate.

**The SLURM backend is the least-tested part of this first cut.** It is written against the
same interface and shares all the planning, resume, retry and reporting logic, but it has not
been run.

---

## 11. Module map

```
run_benchmark.py            CLI, orchestration, exit codes
oodbench/
  config.py                 schema, loader (yaml|toml|json), strict validation
  plan.py                   route discovery, manifest check, route -> paths mirroring
  results.py                finalization predicate + status taxonomy + classification
  ports.py                  deterministic allocator, invariants, probing
  gpus.py                   cuda/vulkan pair resolution, --check-gpus preflight
  jobscript.py              per-route bash generation (the inverted template)
  state.py                  attempt ledger, atomic persistence, resume
  report.py                 report.json / report.md, exit-code derivation
  reap.py                   port-scoped CARLA reaping, fault-pattern detection
  backends/
    base.py                 Backend interface
    local.py                worker pool
    slurm.py                sbatch backend (first cut, unrun)
reference_agent/
  constant_velocity_agent.py   stock AutonomousAgent, no ML dependency
configs/
  example.yaml, example.toml, reference_agent.yaml
tests/                      stdlib unittest; no pytest, no GPU, no CARLA
```

---

## 12. Deliberately out of scope for the first cut

- Anything that needs a running CARLA. Every acceptance criterion that mentions
  concurrent CARLA instances, a real interrupt/resume cycle, or a deliberately failing route is
  hardware validation, and is listed in `STATUS.md` rather than claimed here.
- Multi-node local pools (SLURM covers that case).
- A results database or leaderboard submission path — deferred to v1.0 governance.
- Automatic recovery from a mid-route CARLA hang that neither exits nor trips the wall clock.
  The wall-clock timeout is the backstop; a tighter liveness probe (checkpoint mtime stall
  detection) is designed for but not implemented.
