# OOD-PerceptionBench portable runner — design

> **Artifact version:** runner design v1, drafted for **OOD-PerceptionBench release v0.9**,
> which binds to **arXiv v1** of the paper. A v1.0 release (replacement assets + re-run) binds
> to arXiv v2; scores produced by the two are **not** comparable and the runner stamps which
> one it targeted into every report it writes.
>
> **Status of the accompanying code: local first cut with bounded hardware validation.** Real
> CARLA 0.9.15 runs have exercised local single/two-worker execution, ports, reaping,
> interrupt/resume, failure reporting, and the nine-route acceptance split. Multi-GPU mapping,
> the full 475-route scale, and SLURM remain unvalidated. See `STATUS.md` §2 for the exact
> evidence. This document is the durable part: decisions that are expensive to change later.

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
  port_release_timeout_s: 90  # worker cooldown; no route attempt exists until ports release
  allow_gpu_stacking: false

gpus:                    # ordered list; worker i uses gpus[i % len(gpus)].
                         # `cuda` is always unique; host-scoped `vulkan` is also unique.
  - cuda: 0
    vulkan: 0

ports:
  rpc_base: 20000
  tm_base: 30000
  stride: 10
  probe: true

retry:
  record_budget: 3       # attempts that ENDED ON THEIR OWN having written a retryable record
  infra_budget: 3        # CONSECUTIVE attempts that wrote no record, or never launched
  tickruntime_budget: 0  # 0 = match the lab: TickRuntime is model-side, do not retry
  killed_budget: 2       # attempts KILLED while a crash-shaped record was on disk (see 6A)
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
  vulkan_index_scope: host  # host | allocation
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
2. **Reap and re-verify the worker's own window before assigning another route.** A busy window
   holds only that worker slot idle; no attempt or checkpoint belongs to the pending route yet,
   so teardown cannot consume its infrastructure budget. The supervisor sends SIGTERM without
   waiting, re-probes on later ticks, escalates surviving owned CARLA processes to SIGKILL after
   10 s, and waits up to `execution.port_release_timeout_s` for the evaluator-equivalent bind
   probe to pass. That timeout must cover the 10 s TERM grace plus the configured post-kill
   cooldown, and the SIGKILL tick always retains the slot for a later probe even if supervisor
   polling reached the deadline late. The final pre-launch probe closes the readiness-to-launch
   race. Persistent occupation still fails closed and reaches the ordinary bounded
   infra/quarantine path; it is never allowed to wander upward.

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

**CUDA indices must be unique. Host-scoped Vulkan indices must also be unique**, and for an
asymmetric reason: a repeated host adapter puts two CARLA servers on one physical GPU while
their agents sit on different ones. Several local workers per GPU is expressed by listing the
GPU once and setting `execution.allow_gpu_stacking`, not by repeating a host adapter index.

SLURM device cgroups introduce a second legitimate scope. A scheduler-global GPU may be remapped
to logical CUDA 0 while the same isolated physical device is independently enumerated as Vulkan
adapter 0. Two one-GPU jobs can therefore use different physical GPUs even though both wrappers
pass `-graphicsadapter=0`. Sites that prove this by matching in-job CUDA/NVML and Vulkan UUID or
PCI identity set `slurm.vulkan_index_scope: allocation`; repeated Vulkan indices are then valid
across allocations. The generated wrapper still looks up the scheduler-global ID, preserves
scheduler CUDA, and fails closed unless exactly one GPU is visible. `host` remains the default
and preserves prior configuration digests and validation.

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
cannot buy 3 × the budget. The ledger also carries the route's **settlement** bit; what sets it,
what clears it, and the gating order inside `decide()` are normative in **§6A**.

`state.json` is written atomically (temp file + `os.replace`) after every transition, so a
`SIGKILL` of the runner cannot leave a torn ledger.

---

## 6. Decision D5 — failure handling and the exit contract

### Separate budgets, one per *way an attempt can fail*

Consuming the same budget for two classes lets a bad GPU exhaust a route's retries and mark it
"attempted and failed" — a result-shaped artifact produced by infrastructure.

- `retry.record_budget` — attempts that **ended on their own** having produced a *retryable
  record*. A self-terminated process's verdict is its own.
- `retry.infra_budget` — **consecutive** attempts that produced **no record at all**: wall-clock
  timeout, the process died with a fault pattern in stderr and nothing final on disk, non-zero
  exit with no checkpoint written, pre-launch port/GPU verification failure, **and any attempt
  that never launched**. Consecutive because what it bounds is a machine that is broken now; any
  attempt that produces a record of its own clears the count (§6A.5). A lifetime total is kept
  separately for the report and gates nothing.
- `retry.tickruntime_budget` — its own axis, default 0.
- `retry.killed_budget` — attempts the runner **killed** (wall clock, fault, quarantine) while a
  *crash-shaped* record was on disk. Neither budget above is right for that cell: the record may
  be the model's verdict or may be an artefact of the kill, and there is no evidence to
  separate them. See §6A.5/§6A.6; the axis is bounded so the route still settles.

The report states which budget each incomplete route exhausted. An infra-exhausted route is
never presented as a benchmark result.

> **§6A is normative for which budget is charged.** The two paragraphs that follow (a failed
> launch is infrastructure; an operator interrupt charges nothing) are still true, but they are
> *instances* of the general rule, not independent rules — they were written as one-off repairs
> and a third one-off would be a fifth inconsistent variant. §6A subsumes both.

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
| **0** | every planned route has a **settled** result — see §6A |
| **1** | partial sweep — at least one planned route has no settled result |
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

There is no path where a route without a settled result yields exit 0. `--dry-run` prints the
plan and exits 0 without running anything, which is the only "success without results" case and
it produces no result files to mistake for one. **It also writes no ledger** — planning mutates
one in memory, because otherwise the plan it prints would not be the plan a real run would make,
but every `state.save()` on that path is guarded, so nothing reaches disk at any point. Anything less was a live
defect: replacing the stored config digest during a preview erased the *"produced by a DIFFERENT
configuration"* warning the real run was about to give (§6A.6, item 20).

> **Amended by §6A.** "Every planned route has a *final record on disk*" was the original
> wording and it is not sufficient: a record can be on disk without this run having produced or
> settled it. §6A defines *settled* and is normative where the two disagree.

---

## 6A. NORMATIVE — the attempt-accounting model

**This section is normative.** Where §5, §6 or §8 disagree with it, this section wins. It exists
because four independent cross-review findings (a kill-manufactured record charged to the record
budget; `decide()` ignoring the infra budget on one of its two branches; an infra-exhausted
failed launch still exiting 0; a documented seed re-check that was never written) were all
symptoms of the same missing definition, and because the repair passes that produced those
symptoms each fixed one path in isolation. There must be exactly one model, and this is it.

### 6A.1 The mistake being corrected

The code treated **"a final record exists on disk"** as **"this attempt produced a benchmark
result"**, and **"a final record exists on disk"** as **"this route is complete"**. Both
identities are false, in different directions, and every one of the four findings is one of the
gaps:

| identity assumed | breaks when | finding |
|---|---|---|
| final record ⇒ this attempt produced it | the attempt never launched (record restored) | fixed earlier; generalised here |
| final record ⇒ this attempt produced it | the attempt ended abnormally and what is on disk is a crash-shaped record the ending itself could have written (see 6A.6.2 for how far that is actually reachable) | 2 |
| final record ⇒ nothing else can block a retry | the *infra* budget is spent | 5 |
| final record ⇒ route complete ⇒ exit 0 | the record is stale/unsettled and this run never ran the route | 6 |

### 6A.2 Vocabulary — the three things the runner actually knows

Only these exist. Do not invent a fourth axis.

**(a) `AttemptOutcome`** (`oodbench/backends/base.py`) — *how the attempt stopped*. The real
values, and nothing else, are:

| value | set by | meaning |
|---|---|---|
| `EXITED` | both backends | the process finished **on its own**; local sets it when `Popen.poll()` returns a code and stderr has no fault pattern, SLURM when the job left the queue and `sacct` reports a non-fault state |
| `TIMEOUT` | both backends | breached `execution.route_timeout_s`; the runner killed it |
| `FAULT` | both backends | a fault pattern in stderr (`Segmentation fault`, `Aborted (core dumped)`, …) — see the warning below; or a SLURM state of `CANCELLED`/`TIMEOUT`/`NODE_FAIL`/`OUT_OF_MEMORY`/`PREEMPTED`/`BOOT_FAIL` |
| `KILLED` | `Backend.kill()` | the runner killed it and no more specific outcome was already set |
| `LAUNCH_FAILED` | both backends | it never started: busy ports after reaping, an out-of-range slot, `Popen`/`sbatch` refusal, an unparsable job id, a checkpoint that could not be moved aside |
| *(`None`)* | — | not a member of the enum, but `Attempt.outcome` is `Optional` and `_settle` must handle it. Treated as `LAUNCH_FAILED` (see 6A.4 class **NEVER_STARTED**) and warned about, because it means a backend returned "finished" without saying how |

> **`FAULT` is weaker evidence than its name suggests, and the local backend must say so.**
> On the local backend `FAULT` is inferred by scanning a **log file the simulator also writes
> into**: the vendored evaluator starts CARLA with `subprocess.Popen(cmd, shell=True)` and no
> redirection, so CARLA inherits the attempt's single stderr handle, and `reap.detect_fault`
> scans the tail of that shared file. A UE4 crash *during shutdown* therefore stamps "this
> attempt died hard" on a process that exited on its own having already written its verdict —
> and downstream, under the table in 6A.5, that reclassifies a genuine model result as an
> ambiguous kill. **Rule:** when the supervised process **terminated itself** rather than dying
> by signal, *and* a final record is on disk, a stderr fault pattern is reported in
> `Attempt.detail` and logged as a warning, but the outcome stays `EXITED`. It is a property of
> a log file containing a second process's output, not a property of the attempt. The in-flight
> branch (pattern seen while the process is still running, so the runner kills it) is
> unaffected: there the runner really did end the attempt. The proper fix — giving the CARLA
> server its own stream — is a change to the generated job script and is out of scope.
>
> **That rule was `status 0` until round four, and the difference matters.** `rc == 0` is a
> *proxy* for "did not die hard", and a narrower one than the exact test that now exists. The
> vendored evaluator ends its own crash paths with `sys.exit(-1)` → status **255**
> (`leaderboard_evaluator.py:584`), which is a self-terminated verdict; under the proxy those
> verdicts could never be demoted, so a UE4 abort during teardown sent
> `Failed - Simulation crashed` and `Failed - Agent couldn't be set up` — **the status family of
> four of the six published v0.9 rows** — to the bounded ambiguity budget instead of the model's
> own. The discriminator is `signalled is None`. Found by cross-review, `cursor-grok-4.5-high`,
> 2026-08-07; settled by the user in the reviewer's favour.
>
> **Consequence, and it is checkable rather than merely argued: after a process has exited, a
> fault pattern cannot move any counter.** With a record on disk, `signalled` alone picks the
> branch. Without one, CLEAN_EXIT and ABNORMAL_END both route `NO_FINAL_RECORD` to `infra`
> through the same `_charge_infra`, so the cell — and the quarantine counter — are identical
> either way. `FAULT_PATTERNS` therefore earns its place only in the in-flight branch, where
> there is no exit status to read yet. `tests/test_verification_findings.py::TestPatternIndependence`
> asserts this over (status × stderr × rc), and it is the test that would have caught round
> three: widening the patterns to match what a shell actually writes was correct in itself, and
> silently moved a population onto a different budget because nothing asserted independence.
>
> **Both conditions are load-bearing, and the death test is the one that was missing.** The
> demotion's justification reaches exactly as far as "*our* process is fine, and this pattern is
> the other one's" — and the only evidence the runner has that our process is fine is how it
> ended. Without that test the demotion also swallowed the evaluator itself dying hard with a
> final record already on disk, and then handed that record to the model's own `record` budget
> as a clean verdict — finding 2 again, through a third door.
>
> *(This paragraph said "all three conditions … and `rc == 0` is the one that was missing" until
> the 2026-08-09 review. Round four had already replaced `rc == 0` with `signalled is None` two
> paragraphs above, so the box both superseded the rule and re-asserted it — the exact doc/code
> drift §6A exists to end, committed inside the section that ends it. Kept visible rather than
> silently corrected.)*
>
> **And a fourth door, which is why the classification no longer depends on the stream at all.**
> The `rc` test above is inside the fault branch, so it is reached only when a pattern already
> matched — and for a whole round the patterns were wrong. `"Aborted (core dumped)"` was written
> with one space and had **never matched anything**, because a shell pads the signal name into a
> fixed column (`Aborted` + seventeen spaces + `(core dumped)`); `Segmentation fault` matched
> only by accident, as a bare prefix before the padding; and SIGKILL, which is what a cgroup OOM
> reaper sends, prints only `Killed` and was in no pattern at all. An evaluator that aborted or
> was OOM-killed therefore reached `if not fault:` and was classified CLEAN_EXIT. **Rule, added
> in round three: death by signal is read from the exit status, never from the stream.**
> `reap.describe_exit_signal` recognises `rc < 0` (the job script itself was signalled, which
> `Popen.poll` reports directly) and the shell's `128 + N` relay, bounded above by `NSIG` so that
> `sys.exit(-1)` → 255 is excluded.
>
> **Stated assumption, because the `128 + N` half is an inference and not a proof.** The shell
> writes `128 + N` for a signalled child, but nothing stops a *program* from calling
> `exit(134)` deliberately, and the runner cannot tell the two apart from the number alone.
> This is safe for the pinned evaluator, which uses only `sys.exit(-1)` → 255 and `sys.exit(0)`
> (`leaderboard_evaluator.py:584,586`); argparse exits 2 and an uncaught exception 1, both far
> below the range. It is an assumption about **the evaluator you configure**: `leaderboard.root`
> and `leaderboard.evaluator` are yours to point elsewhere, and an evaluator that intentionally
> exits in 129–192 will have that attempt read as a hard death — charging the bounded `killed`
> axis instead of `record`, which costs retries and can change the status a route settles on.
> The `rc < 0` half has no such ambiguity. Raised by cross-review (`gpt-5.6-luna`, 2026-08-07)
> and accepted as documented rather than closed: the clean fix is a `trap` in
> `jobscript.render` writing the signal to a side channel, which removes the inference
> entirely — but it changes the generated job script, which has now run against real CARLA but
> has not been measured with this signal side channel, so the change remains deferred pending a
> dedicated hard-death experiment (STATUS.md §2). That source is ours alone — nothing but this attempt's own
> wrapper sets its exit status — so it is never a shared-stderr artefact and, having a non-zero
> `rc` by construction, it can never reach the demotion. `FAULT_PATTERNS` survives as a
> *secondary* signal because the in-flight branch, where a simulator crashes under a hung
> evaluator, has no exit status to read yet; the padded-column bug in it is fixed, and a bare
> `Killed` is deliberately still **not** a pattern, since it is an ordinary word an agent may log
> and a false positive there costs a real retry.
>
> What neither test does is treat a non-zero exit as an abnormal end on its own. The vendored
> evaluator exits `-1` from its own crash paths (`main()` calls `sys.exit(-1)` whenever `run()`
> reports `crashed`), which is a *self-terminated verdict* and belongs to CLEAN_EXIT. That is
> exactly what the `NSIG` upper bound protects: 255 is numerically `128 + 127` and there is no
> signal 127, so a rule of "`rc >= 128` means signalled" would have reclassified every one of the
> evaluator's own crash verdicts — the same error as the defect, pointing the other way. Only a
> non-zero exit **together with** evidence of a hard death refuses the demotion. Two consequences
> worth stating, because they are what keeps the trap shut: `Failed - TickRuntime` is written on
> a path that returns `crashed = False`, so a degenerate model exits 0 and is not affected at
> all; and a route that *is* affected lands on `RETRY_TICKRUNTIME` or the bounded `killed` axis,
> never on `infra`.
>
> The SLURM backend has no equivalent demotion because it never infers `FAULT` from stderr at
> all — it reads `sacct` states, and a job that ends `FAILED` with a non-zero exit is `EXITED`
> there. That asymmetry is deliberate but unvalidated; it belongs to finding 3, which is out of
> scope for this pass.

**(b) `Disposition`** (`oodbench/results.py`) — *what the result file on disk says*. The real
values are `ACCEPT`, `RETRY_RECORD`, `RETRY_TICKRUNTIME`, `FATAL`, `UNKNOWN`. Plus the
zeroth case, which is not a `Disposition` value at all:

> **Trap.** `ResultRecord.disposition` returns `UNKNOWN` for a record that is **not final**, and
> also for a final record whose status string is unrecognised. These are different situations
> with different accounting. Every rule below is stated over **six** on-disk cases —
> `NO_FINAL_RECORD` first, then the five dispositions — and code MUST test `record.final`
> before reading `record.disposition`.

**(c) The teardown flag** — `_settle(..., interrupted=True)`. Orthogonal to the outcome: it is
set by `Runner._drain()`, which is reached on an operator signal (`SIGINT`/`SIGTERM`), on a fatal
agent abort, and when every worker has been quarantined. It means *the runner tore this attempt
down as part of shutting the sweep down*. The parameter name is historical; read it as
`torn_down`.

### 6A.3 The one rule

> **A record counts as this attempt's output only if the way the attempt ended could not have
> manufactured that record.**

This is a strict weakening of the orchestrator's proposed rule ("counts only if the attempt
terminated on its own") and the difference is load-bearing — see 6A.6 and 6A.8.

Two facts about the vendored evaluator make the rule decidable rather than a judgement call:

1. **The checkpoint is written immediately after the route is registered**, inside the route
   loop (`save_progress` + `write_statistics` after each `_load_and_run_scenario`), *not* at
   process exit. With one route per XML, `progress` reaches `[1, 1]` with one record the moment
   the route ends. Everything after that — world reset, global statistics, cleanup, CARLA
   shutdown — is **teardown**, and teardown is exactly where a route hangs or dies. So *a final
   record present when the runner kills an attempt was, in the normal case, written before the
   kill by the route itself*.
2. **The statuses a crash path can write are exactly the retryable ones.** The evaluator writes
   `Failed - Simulation crashed` from its bare `except Exception`, `Failed - Agent crashed` from
   `except AgentError`, `Failed - Agent couldn't be set up` from the agent-init handler — i.e.
   all of `RETRY_STATUSES`. It cannot write an `ACCEPT` status from a crash path (those come
   from the criteria evaluation of a route that ended normally), it cannot write
   `Failed - TickRuntime` from a crash path (that is raised only by the scenario manager's own
   `tick_count > 4000` guard), and it cannot write `Failed - Agent's sensors were invalid` from
   one (static validation, before the route runs).

Therefore **the fabricable set is exactly `RETRY_RECORD`** (plus `UNKNOWN`, conservatively,
because an unrecognised status cannot be reasoned about). No new status set is introduced; the
existing taxonomy already partitions this correctly.

### 6A.4 Outcome classes

| class | members | meaning |
|---|---|---|
| **CLEAN_EXIT** | `EXITED` | the process decided to stop |
| **ABNORMAL_END** | `TIMEOUT`, `FAULT`, `KILLED` | the attempt ended abnormally: the runner killed it, or it died hard |
| **NEVER_STARTED** | `LAUNCH_FAILED`, `None` | nothing ran. Anything on disk belongs to an **earlier** attempt (both backends restore the checkpoint they took aside) |
| **TORN_DOWN** | *any outcome* with the teardown flag | the runner is shutting the sweep down |

**Precedence is a strict total order, and it is checkable:**

```
NEVER_STARTED  >  TORN_DOWN  >  ABNORMAL_END  >  CLEAN_EXIT
```

`NEVER_STARTED` outranking the teardown flag is load-bearing and was got wrong once already.
A failed launch sits in its worker slot until the next poll harvests it, so a fatal-agent abort
or an operator signal in between drains it with the teardown flag set. If TORN_DOWN won there,
the attempt would reach the "take `FATAL` and `ACCEPT` first" step and honour a **restored,
earlier-attempt** record: re-adopting an `ACCEPT` as this run's result (the `resume.mode: none`
hazard) or aborting the sweep on a `FATAL` that nothing in this run ever saw. Nothing ran, so
nothing on disk is this attempt's — regardless of why the runner stopped.

The budget follows the class: a launch that failed, failed for its own reason (busy ports, a
refused `sbatch`) and that reason predates the teardown, so it charges `infra` like any other
failed launch. What the teardown flag buys such an attempt is nothing extra — it never gets to
read the record either way.

### 6A.5 The table

Every cell of (outcome class × on-disk case). `budget` is the counter incremented in
`state.json`; **an attempt charges at most one budget, never two**. `finished` is the ledger's
settlement bit (6A.7). `complete` is what the report counts (6A.8).

> **Invariant over the whole table — no cell may be a dead end.** Every cell that has a final
> record on disk MUST reach settlement in a finite number of attempts. A rule that charges such
> a cell to a budget which never settles does not merely delay the answer: because budgets are
> persisted and reloaded, the route hits the same exhausted gate on *every* future resume, and
> the only escapes are deleting `state.json` (losing every budget) or `--resume-mode none
> --force` (overwriting the whole tree). That is a denial of service on a route that has a real
> record, and it is how the first draft of this model would have made six *published* v0.9 rows
> — vad's four `Failed - Agent couldn't be set up`, hydra_next's `Failed - Simulation crashed`
> and `Failed - Agent crashed` — permanently unreproducible at exit 1 by the released runner.
> The `infra` budget is the one budget that deliberately does not settle, so **nothing that
> holds a final record of its own may be charged to it**. "In a finite number of attempts" means
> attempts that actually run: the discharge immediately below states exactly what is bounded and
> what is conditional on the machine, cell by cell, because one cell (NEVER_STARTED holding an
> earlier attempt's record) is precisely where those two things meet.

#### The termination argument for that invariant

The invariant above is a claim, and a claim about a state machine has to be discharged rather
than asserted. It is discharged here, for every cell, and the code is checked against this
table row by row by `tests/test_verification_findings.py`.

Let a route's budgets be `B_rec`, `B_tick`, `B_kill`, `B_infra`, each a finite non-negative
integer (config validation rejects negatives). Two facts about the implementation carry the
argument:

> **What this argument does not cover, learned the hard way.** Everything below reasons about
> attempts that *charge* an axis, and is therefore silent about a gate that fires before any
> attempt exists. `B_infra = 0` was exactly that: the planner's infra gate is evaluated during
> planning, `0 >= 0` held on a virgin ledger, and every route was skipped before running — a
> dead end at the boundary of the very domain this argument quantifies over, reached through a
> legal config value. Fixed in 6A.9 by guarding both infra gates on the counter having actually
> been charged. Recorded here rather than quietly patched, because the lesson generalises: an
> invariant is only as strong as the quantifier it is discharged over, and "for all finite
> non-negative budgets" was doing less work than it appeared to.

* **Every attempt charges exactly one axis, or none.** The `_settle` branches are mutually
  exclusive by construction and the table below is total over (class × on-disk case) — which
  is why the table must be total, and why the test fails if a cell is missing.
* **`_settle` re-queues only when the axis it just charged is still strictly below its
  budget.** An attempt that charges nothing (TORN_DOWN) never re-queues either; the run is
  ending.

**(1) Within one run, a route can cost at most `(B_rec + B_tick + B_kill + 1) × (B_infra + 1)`
attempts.** An attempt that produced a final record *the way it ended could not have
manufactured* charges one of the three record-shaped axes and clears the route's infra streak;
there can be at most `B_rec + B_tick + B_kill` such attempts before one of them reaches its
budget and settles the route. Between any two of them at most `B_infra` infra-charging attempts
can occur, because the one that reaches `B_infra` does not re-queue. Both factors are finite,
so the product is.

**(2) Every cell holding a final record settles inside that bound.** Cell by cell: `ACCEPT`
settles on the attempt itself, in either class. `FATAL` terminates the whole sweep at exit 5 by
definition, and is not a benchmark result. `RETRY_TICKRUNTIME` settles after `B_tick` charges
(default 0 ⇒ the first one). `RETRY_RECORD` and `UNKNOWN` settle after `B_rec` charges under
CLEAN_EXIT and after `B_kill` under ABNORMAL_END — which is precisely why that cell may not be
routed to `infra`, and is the substance of the superseded-rows note below. TORN_DOWN charges nothing and preserves
everything; the next run re-plans the route with its budgets intact.

**(3) The one remaining cell — NEVER_STARTED holding an earlier attempt's record — settles as
soon as one attempt starts, and the runner may not withhold that attempt for ever.** This is
the cell where the two requirements pull against each other: the record was not produced by
this run, so counting it complete would be finding 6, and re-planning it with the infra budget
spent would be finding 5. Neither is done. Instead:

* `infra` counts **consecutive** infra failures, not a lifetime tally, and is cleared by the
  same event that clears the worker-quarantine counter. A route that has been running fine can
  therefore no longer be gated mid-run by hiccups scattered across a long sweep.
* the gate that remains is a statement about the machine at a point in time, and it is
  released, losslessly and by name, with `--retry-infra-exhausted`: it clears that one counter
  for exactly the routes that hit the gate, touching no record, no settlement bit and no other
  budget. Every message that reports the gate names it.

So settlement is unreachable only while the machine cannot start the route at all — which is
the honest meaning of exit 1 — and it is reachable again the moment the machine is repaired,
without deleting the ledger and without overwriting the tree. The invariant's own words are
that the escapes were "deleting `state.json` (losing every budget) or `--resume-mode none
--force` (overwriting the whole tree)"; the objection is that they are destructive, and a
non-destructive one now exists. What the runner must never do is buy the appearance of
settlement — `--retry-infra-exhausted` buys attempts, never answers.

#### The machine-checked form

Prose drifts from code; this table has drifted once already. The rows below are
parsed by `tests/test_verification_findings.py`, which drives `_settle` for every cell and
fails if the budget charged or the settlement bit disagrees with what is written here — in
either direction. Editing one artifact without the other is now a test failure rather than a
finding.

```text
# 6A.5 NORMATIVE TABLE -- machine-checked; do not edit without running the suite.
# columns: outcome_class  on_disk_case  budget_charged  sets_finished
#   budget_charged: infra | record | tickruntime | killed | none
#   sets_finished : yes | no | when_spent  (when_spent = only once that budget is spent)
CLEAN_EXIT     NO_FINAL_RECORD    infra        no
CLEAN_EXIT     ACCEPT             none         yes
CLEAN_EXIT     RETRY_RECORD       record       when_spent
CLEAN_EXIT     RETRY_TICKRUNTIME  tickruntime  when_spent
CLEAN_EXIT     FATAL              none         no
CLEAN_EXIT     UNKNOWN            record       when_spent
ABNORMAL_END   NO_FINAL_RECORD    infra        no
ABNORMAL_END   ACCEPT             none         yes
ABNORMAL_END   RETRY_RECORD       killed       when_spent
ABNORMAL_END   RETRY_TICKRUNTIME  tickruntime  when_spent
ABNORMAL_END   FATAL              none         no
ABNORMAL_END   UNKNOWN            killed       when_spent
NEVER_STARTED  NO_FINAL_RECORD    infra        no
NEVER_STARTED  ACCEPT             infra        no
NEVER_STARTED  RETRY_RECORD       infra        no
NEVER_STARTED  RETRY_TICKRUNTIME  infra        no
NEVER_STARTED  FATAL              infra        no
NEVER_STARTED  UNKNOWN            infra        no
TORN_DOWN      NO_FINAL_RECORD    none         no
TORN_DOWN      ACCEPT             none         yes
TORN_DOWN      RETRY_RECORD       none         no
TORN_DOWN      RETRY_TICKRUNTIME  none         no
TORN_DOWN      FATAL              none         no
TORN_DOWN      UNKNOWN            none         no
```

> **Two rows of the model as *pinned* by the round-one brief are superseded, deliberately.** The
> pinned rows were `ABNORMAL_END × RETRY_RECORD → infra, never settles` and
> `ABNORMAL_END × UNKNOWN → infra`. The code charges `killed` and settles, and **the code is
> the artifact that stands**; this table has been moved to it rather than the other way round.
> The reason is the invariant three paragraphs up, which the pinned rows violate on their own
> terms: `infra` never settles, the gate is persisted and reloaded, and the cell holds a final
> record — so the pinned rows are the definition of a dead end. Two further facts make the
> choice not even close. `AttemptOutcome.FAULT` is inferred from a stderr stream the CARLA
> server shares (6A.2), so a *cleanly finished* evaluator is routinely classified ABNORMAL_END;
> and six rows of the published v0.9 record set hold a `RETRY_RECORD` status as their final
> result, four of them `Failed - Agent couldn't be set up`, which the agent-init watchdog writes
> on a hang — the same population that trips `route_timeout_s`. Under the pinned rows the
> released runner would report those six as unsettled, at exit 1, on every resume, for ever.
> What survives from the pinned model is its substance: a kill never spends the model's `record`
> budget, and the route never settles on the first such attempt. Full argument in 6A.6.6/6A.6.7.

**CLEAN_EXIT** — the process stopped on its own, so whatever it wrote is its own verdict.

| on disk | budget | sets `finished` | counts complete | note |
|---|---|---|---|---|
| no final record | `infra` | no | no | wrote nothing; unchanged |
| `ACCEPT` | none | **yes** | yes | the result; unchanged |
| `RETRY_RECORD` | `record` | only once `record` is spent | only once `record` is spent | unchanged |
| `RETRY_TICKRUNTIME` | `tickruntime` | only once `tickruntime` is spent (default budget 0 ⇒ immediately) | same | unchanged — **the degenerate-model row** |
| `FATAL` | none | no | **no** (was: yes) | aborts the sweep; exit 5 regardless, so this cannot change any exit code |
| `UNKNOWN` (final) | `record` + loud warning | as `RETRY_RECORD` | as `RETRY_RECORD` | unchanged |

**ABNORMAL_END** — the attempt ended abnormally, so a fabricable record is not evidence *of the
model's verdict* — but it is not evidence *against* it either.

| on disk | budget | sets `finished` | counts complete | note |
|---|---|---|---|---|
| no final record | `infra` | no | no | unchanged |
| `ACCEPT` | none | **yes** | yes | unchanged. A kill cannot fabricate an `ACCEPT`; the route finished and we killed its teardown |
| `RETRY_RECORD` | **`killed`** (was: `record`) | only once `killed` is spent | only once `killed` is spent | **finding 2, as amended.** Ambiguous by construction: precisely the status a dying CARLA under a surviving evaluator produces — *and* precisely what a route that crashed on its own leaves behind. Charge neither the model's budget nor the never-settling one |
| `RETRY_TICKRUNTIME` | `tickruntime` | as CLEAN_EXIT | as CLEAN_EXIT | **amendment — see 6A.6.** Not fabricable, and this is the cell the degenerate-model row lands in |
| `FATAL` | none | no | no | aborts; not fabricable by a kill |
| `UNKNOWN` (final) | **`killed`** + loud warning | as `RETRY_RECORD` | as `RETRY_RECORD` | conservative about *attribution* (do not spend the model's budget) without being unrecoverable: a status we do not recognise must never make a route unsettleable |

**NEVER_STARTED** — nothing ran; the file on disk is an earlier attempt's, restored.

| on disk | budget | sets `finished` | counts complete | note |
|---|---|---|---|---|
| *any* of the six | `infra` | **no** | **no** | the record MUST be preserved byte-identical and MUST NOT be read as a verdict — including `FATAL` (never aborts from here) and `ACCEPT` (never re-adopted, which is the `resume.mode: none` hazard). **Finding 6 lands here**: the preserved record no longer makes the route complete |

**TORN_DOWN** — we stopped a route that was not failing.

| on disk | budget | sets `finished` | counts complete | note |
|---|---|---|---|---|
| no final record | **none** | no | no | unchanged |
| `ACCEPT` | none | **yes** | yes | unchanged: it genuinely finished before we killed anything |
| `RETRY_RECORD` / `RETRY_TICKRUNTIME` / `UNKNOWN` | **none** | no | no | unchanged. Record preserved, budget intact, re-planned next run |
| `FATAL` | none | no | no | aborts; unchanged |

**The `killed` axis, stated once.** It is a *bounded ambiguity* budget, not a failure count:
retry to resolve the ambiguity, and when the budget is spent accept the record as the answer
with a loud, reported warning. Default 2, which buys exactly one clean re-run; 0 or 1 accepts a
possibly-manufactured record without ever re-running the route, which the config warns about.
Deliberately *not* the `record` budget (a kill must not spend the model's retries — that is
finding 2) and deliberately *not* `infra` (which never settles — that is the invariant above).
Before a retry the record is copied to `_runner/killed_records/…` (6A.11), because
`take_checkpoint_aside` will delete it on the next successful launch.

**Ordering requirement inside `_settle`, because this is where the implementation will slip.**
The pre-fix code computed one boolean (`produced_record = record.final and not launch_failed`)
and branched to the infrastructure path first, reading the disposition only on the other side.
That shape cannot express the tables above, because the `FATAL` and `ACCEPT` rows have to be
honoured for ABNORMAL_END — which is on the infrastructure side. The order MUST be:

1. classify the outcome (6A.4);
2. if the class is **NEVER_STARTED**: charge `infra`, never look at the disposition at all
   (it belongs to an earlier attempt), never abort, never settle. This step is reached **before**
   the teardown check, per the precedence order;
3. if the teardown flag is set (**TORN_DOWN**): take `FATAL` and `ACCEPT` from the record, charge
   nothing otherwise, settle nothing;
4. otherwise read the record; if it is not final, charge `infra`; then take `FATAL` and `ACCEPT`
   first, for CLEAN_EXIT and ABNORMAL_END alike;
5. then the retryable rows: `RETRY_TICKRUNTIME` charges `tickruntime` in both classes;
   otherwise ABNORMAL_END charges `killed` and CLEAN_EXIT charges `record`.

**The two consecutive-failure counters, stated once and separately.** There are exactly two, they
ask the same question of different subjects, and **one event clears both**:

| counter | subject | question |
|---|---|---|
| `Runner.consecutive_infra[worker]` | a worker | is this GPU wedged? |
| `TaskState.attempts_infra` | a route | is this route currently unable to start at all? |

Both MUST count attempts that produced **no final record of their own** — i.e. NEVER_STARTED
(whatever is on disk, since that record is not this attempt's) and CLEAN_EXIT/ABNORMAL_END with
no final record — MUST be cleared by any attempt that produced a final record *the way it ended
could not have manufactured*, and MUST be left untouched by TORN_DOWN. Note this deliberately
decouples them from budget attribution: an ABNORMAL_END holding a `RETRY_RECORD` charges the
`killed` budget and leaves both counters **untouched** — neither advanced nor cleared. Not
advanced, because a worker that ran a route all the way to record registration is demonstrably
not wedged, and without that a model whose teardown hangs would quarantine every worker and exit
4 ("no usable GPU"), a misdiagnosis. Not *cleared* either, because clearing on a record the kill
may itself have written is how a worker that kills every route it touches evaded the detector.

**`attempts_infra` is a streak, not a lifetime tally, and that is what makes the infra budget
honest.** `retry.infra_budget` bounds *consecutive* infrastructure failures — a machine that is
broken **now**. Accumulating unrelated hiccups over a 475-route sweep let three scattered busy
ports, hours apart, gate a route that had been running fine all along; and because the ledger is
persisted, that route stayed gated on every later run too. This is a dead end reached *inside* a
single run, which no operator action outside the run can reach, so the streak rule is required
by the termination argument above and is not merely a nicety. The cumulative count is kept
alongside it as `TaskState.attempts_infra_total`, which nothing gates on and nothing clears: it
is the audit trail, and it is what the report shows as `attempts.infra_total`.

### 6A.6 What amends the orchestrator's proposal, and why

1. **`TIMEOUT`/`FAULT`/`KILLED` do not unconditionally mean infra.** The proposed row
   "TIMEOUT / FAULT / KILLED → infra → never complete" is wrong in the shipped configuration and
   would have destroyed exactly the row the trap warns about. Reasons, in order of weight:
   - The checkpoint is written when the **route** ends, not when the **process** ends (6A.3),
     so a final record + a wall-clock kill is the signature of *a route that finished and hung
     in teardown* — `_reset_world_settings()` and CARLA shutdown are RPCs into a simulator that
     may already be sick.
   - `Failed - TickRuntime` is raised by `tick_count > 4000`: it is, by definition, the outcome
     of a route that ran for a very long time. Those are the routes most likely to breach
     `execution.route_timeout_s` (default 3600 s) *during teardown*. A degenerate model scoring
     `Failed - TickRuntime` everywhere would therefore be the population most exposed to a blunt
     "killed ⇒ infra" rule: every route charged to infra, every route re-run to the infra
     budget, and the whole model row reported incomplete with exit 1 — a legitimate benchmark
     result silently converted into an infrastructure failure. That is the trap, reached by a
     different door than the obvious one.
   - `ACCEPT` under a kill is likewise genuine and must stay accepted; the existing interrupt
     path already carved this out and the general rule must not lose it.
2. **The premise of finding 2 is narrower than stated, and the fix is kept anyway.** The
   finding says CARLA's crash handler "writes a `Simulation crashed` record on the way down"
   when the runner kills a route. With the vendored evaluator that is **not** reachable through
   the runner's own kill path: the evaluator installs a handler for `SIGINT` only
   (`leaderboard_evaluator.py`), while `reap.terminate_process_tree` and SLURM's `scancel` both
   deliver `SIGTERM` (then `SIGKILL`), which Python's default disposition turns into immediate
   termination — no `except` block, no write. The children are also started with
   `start_new_session=True`, so a terminal `Ctrl-C` never reaches them directly either. The rule
   is adopted regardless, because the accounting must not depend on that: the launched command
   is user-supplied (`agent.entrypoint`, `environment.activate`, the generated job script) and
   may trap `SIGTERM`; a `FAULT` can be a segfault *after* a partial write; and a crash-shaped
   record co-occurring with an abnormal end is ambiguous evidence whatever produced it. §6
   already classifies "died with a fault pattern in stderr" as infrastructure — the code simply
   stopped honouring that whenever a record happened to be final. **What it must not do is treat
   ambiguous evidence as evidence for the opposite conclusion**: see amendment 6.
3. **Completeness is not "disposition-based" as proposed.** "Record budget exhausted on a
   retryable status ⇒ COMPLETE" is right but insufficient: it is not decidable from the record
   alone (the same record is settled or unsettled depending on the ledger and the resume mode),
   and a purely disposition-based rule breaks `resume.mode: skip_any_final`, under which a
   retryable final record with an unspent budget is deliberately accepted as the answer.
   Completeness is therefore defined as **settlement** (6A.7), computed in one place.
4. **`LAUNCH_FAILED` is not a separate row.** It is the NEVER_STARTED class, which also absorbs
   `outcome is None`. The earlier repair pass's dedicated `launch_failed` predicate is replaced
   by the class, not supplemented.
5. **`FATAL` no longer counts complete.** A rejected sensor configuration is a verdict about the
   agent, not a benchmark result for the route. This cannot change an exit code (fatal always
   yields 5) and only affects the report's totals, where it is the honest count.

#### Amended again by adversarial review of this section

The first draft of 6A.5 sent ABNORMAL_END × `RETRY_RECORD` and × `UNKNOWN` to `infra`, never
settling. Review — checked against the code and the published records — found that this
re-opened the trap through a second door, and three further defects. All four are accepted; the
tables above are the amended version.

6. **The ambiguous cell gets its own bounded budget, not `infra`.** Two facts kill the
   `infra` routing. *(a)* `AttemptOutcome.FAULT` is not reliable evidence of an abnormal end at
   all — it is inferred from a stderr file the CARLA server also writes into (see the warning in
   6A.2), so a cleanly-exited evaluator holding a genuine model verdict is routinely classified
   ABNORMAL_END. *(b)* `infra` exhaustion is terminal on every future resume, so the routing was
   not "retry it more carefully", it was "this route can never settle again". Measured blast
   radius: six rows in `records/ood_perceptionbench_records_v0.9.csv` hold a `RETRY_RECORD`
   status as their **final published result**, four of them `Failed - Agent couldn't be set up`
   — written by the agent-init watchdog, i.e. a *hang*, which is exactly the population that
   also trips `route_timeout_s`. Under the first draft the released runner would have reported
   those rows as unsettled and exited 1, for ever. The fix keeps finding 2's substance (the
   model's `record` budget is never charged for a kill, and the route never settles on the first
   such attempt) while restoring the table-wide invariant that a final record always settles in
   finite attempts. The reviewer's alternative — charge `record`, treating the evaluator's
   `except AgentError` verdict as the model's own — is the honest reading of 6A.3 fact 1, but it
   spends the model's retries on an event that may be pure infrastructure; the separate axis is
   strictly more informative and costs one config key.
7. **`UNKNOWN` under an abnormal end is bounded too.** "Conservative" cannot mean
   "unrecoverable" on the one cell whose job is to absorb schema drift: a leaderboard build that
   renames a status makes *every* route `UNKNOWN`, and any of them ending abnormally would be
   permanently unsettleable — the whole model row incomplete at exit 1, not "a loud warning".
   The warning is the guard; permanent incompleteness is a denial of service on a status that is
   probably fine. **No rule in this table may make a route unsettleable on the basis of a status
   the runner does not recognise.**
8. **Precedence is an order, not a sentence.** "TORN_DOWN takes precedence" plus "NEVER_STARTED
   never looks at the disposition" is a contradiction on the cell where both hold, and the
   reading it invites regresses the exact defect the earlier repair pass fixed. 6A.4 now states
   a strict total order with NEVER_STARTED first, and gives the reachability argument.
9. **The report's two numbers must reconcile.** Making `complete` settlement-based without
   touching `totals.by_status` left the same route counted as a result *and* as a gap: the
   status breakdown summed over every outcome, so a preserved-but-unsettled record appeared as
   e.g. `Failed - Agent crashed` in a table an operator reads next to "incomplete: 1" and a
   downstream aggregator sums. `by_status` now covers exactly the complete routes and
   `by_status_unsettled` exactly the incomplete ones (6A.8).

#### Amended a third time, by an independent verification pass over the repair

The pass that implemented 6A.1–6A.11 was itself verified, and six defects came back. Two of
them are the same *class* of defect this section was written to end — a rule stated in one
artifact and not implemented in the other — which is why the response is not only six fixes but
one machine check.

10. **The `FAULT` demotion must test the child's exit status.** As first written it demoted on
    "a fault pattern *and* a final record", with no test on `rc`, so it also swallowed the
    evaluator dying hard with a record already on disk. `rc == 0` is now required; the reasoning
    and the two consequences that keep the trap shut are in the warning box in 6A.2.
11. **The pinned `infra` routing of the ambiguous cell is formally superseded, not merely
    amended.** The round-one brief pinned `ABNORMAL_END × RETRY_RECORD → infra, never settles`
    (and the same for `UNKNOWN`); the code charges `killed` and settles. One of the two had to
    go and the code was kept — argument at the table in 6A.5, and in items 6 and 7 above.
12. **The table is machine-checked.** A normative table that only exists as prose is a table that
    will drift again; `tests/test_verification_findings.py` parses the block in 6A.5 and drives
    `_settle` for all 24 cells (ABNORMAL_END over all three of its outcomes), failing on any
    disagreement in either direction. This is the durable half of items 10–11.
13. **The invariant is discharged, not asserted.** "No cell may be a dead end" is now backed by
    an explicit termination argument with a bound (6A.5), which in turn required two changes:
    `attempts_infra` counts *consecutive* failures, and the infra gate has a lossless,
    first-class release (`--retry-infra-exhausted`, 6A.8). Finding 6 is untouched: an unrun route
    still never reports complete, and the flag never settles anything.
14. **A parameter with no safe default gets no default** (`decide(..., killed_budget)`, 6A.9).
15. **Schema growth is not a configuration change** (`DIGEST_COMPAT_DEFAULTS`, 6A.11), and
    `unsettled_reason` is evaluated ledger-first so its three values are actually three (6A.8).

#### Amended a fourth time, by an independent verification pass over *that* verification

Three agents audited item 10–15. Two of them, working from different directions, arrived at the
same two defects and then failed to refute them. Both are this section's recurring shape once
more: a rule that is right, implemented over the wrong quantity.

16. **A hard death is read from the exit status, not from a log line.** Item 10 gated the
    demotion on `rc == 0` — correct, and unreachable, because the gate sits inside `if fault:`
    and `fault` came from matching literal text against a stream whose formatting nobody had
    checked. `"Aborted (core dumped)"` had never matched anything (the shell column-pads the
    signal name) and SIGKILL's `Killed` was in no pattern at all, so SIGABRT and OOM deaths were
    classified CLEAN_EXIT and charged to the model's `record` budget: finding 2, a fourth time.
    `reap.describe_exit_signal` now decides it from `rc` alone. Reasoning, and why the `NSIG`
    upper bound must exclude `sys.exit(-1)` → 255, in the warning box in 6A.2.
17. **`retry.infra_budget: 0` was a dead end that item 13's release could not open.** The infra
    gate is the only one evaluated before its own axis has been charged, so at budget 0 it fired
    on a virgin ledger and no route ever ran. Both gates now carry the same `spent and spent >=
    budget` guard the `killed` gate already used (6A.9). Note what this says about item 13: the
    termination argument was sound over the domain it stated — "let the budgets be finite
    non-negative integers" — and still admitted a dead end at the boundary of that domain,
    because the argument reasons about attempts that *charge* an axis and this gate fires before
    any attempt exists. An invariant is only as good as the quantifier it is discharged over.

Two smaller ones from the same round: `--dry-run --retry-infra-exhausted` persisted the ledger
edit an operator had asked only to preview, and the exit-contract tests ran only where `final`
and `settled` agree, leaving the off-diagonal — the case the whole distinction in 6A.8 exists
for — asserted nowhere.

#### Amended a fifth time, by the cross-review that round three finally ran

Round three's repair was reviewed by two models from other labs (`gpt-5.6-luna`,
`cursor-grok-4.5-high`); cursor returned BLOCKING. Record:
the maintainers' review record (kept internal). **All four surviving findings were escalated rather than
fixed in place, and the user ruled on each** — the first time in five rounds that a model change
was made by the person who owns the release rather than by the agent that found it.

18. **The demotion's discriminator is "was this process signalled", not "was `rc` zero"**
    (6A.2). Round three's own widening of `FAULT_PATTERNS` is what made the difference bite:
    it moved self-terminated `Simulation crashed` / `Agent couldn't be set up` verdicts onto the
    ambiguity axis. This **inverts** a round-two regression test, which is recorded in that
    test's docstring rather than quietly rewritten.
19. **The accounting model is versioned in the ledger** (`state.ACCOUNTING_EPOCH`), and
    resuming a tree written under a different epoch warns. Raised independently by both
    reviewers *and* by round three's own auditor — three of three. `DIGEST_COMPAT_DEFAULTS` is
    right that adding a key at its default is not a settings change, but the accounting model
    changed alongside that key and nothing compared it. Three questions now have three answers:
    the digest for settings, the runner version for the build, the epoch for the rules.
20. **`--dry-run` writes nothing at all.** It was never inert: planning materialises a
    `TaskState` per route, moves `finished` bits, and `load_or_create` replaces the stored
    digest — so previewing under a changed config **erased the `config_changed()` warning** the
    real run would have shown. A dry run now never calls `save()` at all, which subsumes
    round three's field-by-field rollback. A first attempt snapshotted and restored the file
    instead; codex reviewed *that* and was right again — a snapshot leaves a crash window and
    its restoring write is not atomic. Not writing is crash-safe by construction, and a test
    makes `save()` fatal to assert it structurally.
21. **The `128 + N` inference is documented as an assumption about the configured evaluator**
    (6A.2), not closed. The clean fix changes the generated job script and waits for hardware.

### 6A.7 Settlement — the `finished` bit

`TaskState.finished` MUST mean exactly:

> **the result file for this route holds an answer that this run is no longer obliged to
> improve on.**

It is the *only* place settlement lives, and it is set or cleared in exactly four places:

| where | when | `finished` |
|---|---|---|
| `plan.decide` ⇒ `SKIP_DONE` | the resume policy accepts the record (`skip_terminal`: `ACCEPT`; `skip_any_final`: any final record) | **set** |
| `plan.decide` ⇒ `SKIP_EXHAUSTED` **with a final record whose own retry budget is spent** (`record`, `tickruntime` or `killed`) | the record is the answer because the retries are gone | **set** |
| `plan.decide` ⇒ `SKIP_EXHAUSTED` **because the infra budget is spent** (with or without a record on disk) | there is no answer this run produced | left false |
| `plan.decide` ⇒ `RUN` | this run owes the route an attempt | **cleared** |
| `Runner._settle` | per the 6A.5 tables | set / left |

Two of these are new and both are required:

- **Setting it on the budget-spent flavour of `SKIP_EXHAUSTED` is the degenerate-model guard.**
  A result tree produced by a model that scores `Failed - TickRuntime` on every route, re-planned
  after its ledger was lost (`state.json` corrupt and moved aside, or a fresh checkout), yields
  `SKIP_EXHAUSTED` for all 475 routes with `tickruntime` spent 0 of budget 0. Without this line
  the ledger says nothing is finished and a complete, valid model row reports as 475 incomplete
  routes and exit 1. This is the sharpest form of the trap; a completeness rule that reads the
  ledger MUST have it.
- **Clearing it on `RUN` is what makes finding 6 hold under `resume.mode: none`.** A route
  settled by a previous run and deliberately re-planned must not stay settled on the strength of
  a record this run intends to replace and then never produces.

`decide()` therefore MUST return the settlement bit alongside the decision (a field on
`TaskDecision`), rather than the caller re-deriving it. The `Decision` enum members do not
change.

### 6A.8 Completeness and the exit contract

`RouteOutcome.complete` MUST be:

```
complete  ⟺  the result file on disk is final          (re-read at report time, as today)
             AND the ledger says the route is settled  (6A.7)
```

- The disk half is kept because the report must reflect what is actually on disk: a result file
  deleted after the fact makes the route incomplete no matter what the ledger says.
- The ledger half is what stops a record that is merely *present* from being counted as an
  *answer*. This is finding 6: an infra-exhausted failed launch preserves a real, earlier record
  (correctly — it must never be destroyed), but the route reported complete and the run exited 0
  having never run it.
- A route with `st is None` (never reached, because a fatal agent aborted the planning loop) is
  not settled.

Consequences that MUST be carried through:

- The report's incomplete section was headed "NO benchmark result was produced for these". That
  is not accurate for every row: a finding-6 row *has* a record, it is just not settled. The
  heading is "Routes with **NO settled** result", and each such row carries `final` plus a
  machine-readable `unsettled_reason` — `no_record` (nothing final was ever written),
  `unrefreshed_record` (a record is on disk from an earlier attempt and the planned retries
  never ran), `not_reached` (the planning loop never got to this route) — so an operator can
  tell the three apart at a glance. **They are evaluated ledger-first: `not_reached` (no ledger
  entry at all), then `unrefreshed_record`, then `no_record`.** Testing `rec.final` first made
  `not_reached` unreachable for any route with a record on disk — and a route the loop never
  reached is *precisely* a route whose record is left over from an earlier run, so the case the
  label exists for was the case it never got. The ledger question ("was this route ever looked
  at?") is strictly prior to the disk question ("is there a record?").
- **`totals.by_status` counts exactly the complete routes, and `totals.by_status_unsettled`
  exactly the incomplete ones.** Redefining `complete` without this leaves the report
  self-contradictory: the same route appears as a status row *and* as a gap, in two numbers
  printed a few lines apart and summed by anything downstream. The two dicts now sum to
  `totals.complete` and `totals.incomplete` respectively, which is a checkable invariant.
- `EXIT_MEANING[0]` / `[1]` and the module docstring of `run_benchmark.py` are reworded from
  "final record" to "settled result".
- **No currently-healthy sweep changes.** Every route of a healthy sweep ends `ACCEPT`, or
  exhausts its `record`/`tickruntime` budget, or is skipped as done on resume — all of which set
  `finished`. The exit code moves 0 → 1 only for routes that hold an unsettled record this run
  failed to refresh, which is the defect.
- **Recovery from an infra-exhausted route is operator-initiated, lossless, and named in every
  message that reports the gate.** `infra` is the one budget that does not settle, so a route
  that exhausts it stays unsettled across resumes by design (that is what "we do not know this
  route's answer" means, and it is the same rule that already stopped a restart from buying a
  fresh budget). What that must not mean is *unrecoverable*: repair the machine and re-run with
  **`--retry-infra-exhausted`**, which clears the infrastructure counter — and only that counter,
  and only for routes that hit the gate. No result file is touched, no settlement bit moves, no
  other budget moves, and `attempts_infra_total` still records what happened. It buys attempts,
  never answers, which is why it does not require `--force`: it destroys nothing. The planner's
  reason string, the settle path's warning and the report all name it verbatim, from one
  constant (`plan.INFRA_RECOVERY_HINT`), because a recovery an operator cannot find is not a
  recovery. Using it is itself recorded in the report.

### 6A.9 `decide()` — gating order (finding 5)

`decide()` MUST evaluate in this order. The infra gate is reached from **both** branches, which
is the finding; it is placed *after* the record/tickruntime exhaustion check so that a route
whose record budget is spent settles on its record rather than on the state of the machine.

1. final record with `FATAL` disposition ⇒ `FATAL`
2. `results.should_skip_on_resume(record, mode)` ⇒ `SKIP_DONE` *(settled)*
3. `resume.mode == "none"` ⇒ `RUN` *(carve-out, see below)*
4. final record ⇒ pick the axis by disposition: `tickruntime` for `RETRY_TICKRUNTIME`, otherwise
   `record` — or `killed`, if that counter has been charged at least once and is now spent,
   because `_settle` would settle on the same record and a fresh process must not reset the
   bound. If spent ≥ budget ⇒ `SKIP_EXHAUSTED` *(settled — the record is the answer)*
5. infra spent ≥ 1 **and** infra spent ≥ `infra_budget` ⇒ `SKIP_EXHAUSTED` *(**not** settled —
   this run cannot produce the answer it owes)*. Reached from **both** branches: a route holding
   an old retryable record used to skip this gate entirely and retry past its infra limit for
   ever
6. otherwise ⇒ `RUN` *(clears settlement: this run owes the route an attempt)*

The `killed` gate in step 4 fires only once that counter is non-zero. Unlike `record` and
`tickruntime`, where a budget of 0 legitimately means "never retry a self-terminated verdict", a
`killed` budget of 0 on a route that was never killed must not mean "accept a record we have
just admitted might be manufactured, having never re-run it".

**Step 5 carries the same guard, and for a sharper reason: without it, `retry.infra_budget: 0`
was a total dead end.** The `record`, `tickruntime` and `killed` gates are only ever reached
*after* an attempt has charged their axis, so a budget of 0 there means "one attempt, then
accept" — which is precisely what `tickruntime_budget: 0`, the shipped default, relies on. The
infra gate is different in kind: it is evaluated during **planning**, before the first attempt
exists, so `0 >= 0` fired on a virgin ledger and every route was `SKIP_EXHAUSTED` before
anything ran — exit 1, zero executions, on a completely healthy machine. Nothing could release
it: `--retry-infra-exhausted` clears a counter that was never charged, and deleting the ledger
does not help either, because at budget 0 the gate never consults the ledger in the first place.
That is 6A.5's dead-end invariant broken by an off-by-one rather than by a rule, and it was
reachable through a legal, plausible config value — `tickruntime_budget` ships at `0`, so "0"
reads as the idiomatic "no retries", and `configs/reference_agent.yaml` already carries a
non-default `infra_budget: 1`, so the field is one operators edit. With the guard the axis means
what the other three mean, and the value the gate reports is always one that was really spent —
which in turn makes `INFRA_RECOVERY_HINT` true whenever it is printed, since
`clear_infra_exhaustion` can only clear a charged counter.

**`killed_budget` is a required parameter of `decide()` with no default, and that is a rule
about the signature, not a style preference.** It briefly carried `= 0`, under which
`killed_spent >= killed_budget` holds for any charged ambiguity budget — so a caller who merely
*forgot the keyword* got `SKIP_EXHAUSTED` **settled**, on exactly the record the model has just
said the kill might have written. The opposite default is no better: dropping the gate resets
the bound on every resume, which is what step 4 exists to prevent. When neither direction of a
default is safe, the parameter may not have one; the caller is made to say what it means, and a
forgotten keyword is a `TypeError` at the call site rather than a wrong number in a report.

**The `resume.mode: none` carve-out is deliberate and must survive.** That mode requires
`--force`, means "re-run this route regardless of history", and runs against a ledger whose
counters are already spent from the run being replaced. Consulting the record budget there would
make `--force` a no-op on any tree it is pointed at. Retries within the run are still bounded,
because `_settle` keeps incrementing the same counters. The residual is one wasted attempt per
route per run when a previous run already exhausted infra; that is loud (it is reported) and
bounded.

### 6A.10 Amendment to §8 — the report-time seed check (finding 9)

§8 claimed: *"At report time the runner re-derives the expected seed for every result file it
counts and fails the report if a file's embedded seed disagrees with the configured one."*
It was never implemented, and as written it is close to vacuous: the report reads
`task.result_path`, which is derived from the task, which carries the seed by construction —
`plan.assert_seed_consistency` already checks that at plan time and a disagreement is
unrepresentable downstream of it.

The check that has real content is over the **tree**, not the planned paths, and that is what
the runner MUST do instead:

> At report time the runner scans `<output.root>/**/results/*.json`, extracts the `_seed<N>`
> component of each filename, and lists any file whose seed is outside the configured set
> (`benchmark.seed + r` for `r` in `range(repetitions)`) as a warning naming the count and up to
> a few examples. Such files are never counted — only planned paths are ever read — and the
> warning says so.

It is a **warning, not a hard failure**: pointing a second seed at an existing output root is a
supported (if discouraged) workflow, already flagged by the config-digest change, and a hard
failure there would refuse to write a report for a run that is otherwise fine. §8's bullet is
amended to this wording.

Implemented as `plan.foreign_seed_files(out_root, allowed_seeds)`, called once from
`report.build`. The allowed set is taken from the planned tasks themselves (`{t.seed for t in
tasks}`), which is the same set `assert_seed_consistency` validated against `benchmark.seed +
range(repetitions)` — so the two halves of the seed guard cannot drift apart. `_runner/` is
skipped: it holds the runner's own scratch, including the `killed_records/` copies, which are
deliberately not results.

### 6A.11 What this model does not fix, and what it costs

- **Not fixed here (separate decisions, out of scope):** resume identity does not hash the model
  / checkpoint / route content, so a changed agent resumes onto an old tree (finding 1); a
  `squeue` failure still reads as "job finished" (finding 3); SLURM has no node-local port probe
  (finding 4); release metadata is hard-coded (finding 8).
- **Cost — an ambiguous record is destroyed by its own retry.** Charging ABNORMAL_END +
  `RETRY_RECORD` to the `killed` axis means the route is re-queued, and `take_checkpoint_aside`
  deletes the record on the next *successful* launch. If that attempt then produces nothing, the
  route ends with no record at all where the old behaviour would have frozen the crash-shaped
  one in. The runner therefore copies the record to
  `<output.root>/_runner/killed_records/<REL>/<stem>_seed<SEED>.<n>.json` before re-queueing —
  under `_runner/`, never beside the results, so no downstream aggregator can glob it up as a
  result, and the seed scan of 6A.10 skips it for the same reason.
- **Cost — one extra config key and two extra ledger counters.** `retry.killed_budget` with
  `TaskState.attempts_killed`, and `TaskState.attempts_infra_total`. All appear in the report and
  in `state.json`. A ledger written by an older build reads `attempts_killed` as 0, which is the
  correct starting value; `attempts_infra_total` is seeded from that ledger's `attempts_infra`,
  because that field *was* the lifetime tally before the streak/total split. The gate value
  itself is never re-derived on load, so upgrading the runner cannot silently un-gate or re-gate
  a route.
- **Adding `retry.killed_budget` must not invalidate every existing output root, and does not.**
  `config.build` materialises every schema key into the resolved config and `Config.digest()`
  hashes the whole thing, so adding a key silently changed the digest of every configuration
  that predates it — and the digest is what the ledger compares on resume, which made every
  pre-existing root announce "produced by a DIFFERENT configuration ... use a fresh output root".
  That advice costs 475 routes of finished work, over a key nobody set. A key listed in
  `config.DIGEST_COMPAT_DEFAULTS` is therefore omitted from the digest **while it holds the
  pinned value, and only then**: set `retry.killed_budget: 3` and it is hashed like anything
  else, because that genuinely changes the run. The pinned value is written in that constant
  rather than read from `SCHEMA`, so changing a *default* in a later version still moves the
  digest of every config that omits the key — which is right, because their behaviour would move
  too. The digest describes the configuration, not the runner; the runner version is stamped
  into every report separately, and it is the runner version that records this accounting model.
- **Cost — a possibly-manufactured record can still become the answer**, once the `killed`
  budget is spent. That is deliberate: the alternative is a route that never settles. Every such
  settlement is reported as a warning naming the status and the attempt count, and earlier
  copies of the record are kept under `_runner/killed_records/`, so the ambiguity is visible in
  the artifact rather than silently resolved.
- **Partly observed, exact corner still unvalidated.** Real CARLA runs have exercised finalized
  `Completed`, `Failed - TickRuntime`, and agent-setup-failure records plus interrupt-without-a-
  record accounting. The specific case "an attempt killed by the wall clock while holding a
  final record" has not been observed; it remains reasoned about rather than measured, which is
  why its accounting is bounded on both sides. `STATUS.md` §2 is the evidence boundary.

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
- At report time the runner scans the result tree for files whose filename-embedded seed falls
  outside the configured seed set and lists them as a warning; such files are never counted,
  because only planned paths are read. **Amended by §6A.10** — the original wording of this
  bullet ("re-derives the expected seed for every result file it counts and fails the report")
  described a check that was never implemented and that would have been vacuous, since the paths
  the report reads carry the seed by construction and `assert_seed_consistency` checks that at
  plan time.
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
