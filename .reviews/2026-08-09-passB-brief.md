## Original request (verbatim)

> okay, thanks.  It seems that you have fixed the errors from the previous session. To be more
> sure about this, let's do one more round of cross-review. Make the review exhaustive.

## What the implementing agent says it did

**READ THIS FIRST — THIS IS NOT A CHANGE SET.** The "diff" you have been given is every line of
the runner package at commit `3a7a0c9`, presented as an add-diff so you can see all of it. There
is no "before". **Review it as code, not as a change.**

That framing is the entire point of this pass. This runner has been through **five review rounds
and every one found real defects in the one before it**, but every one of those rounds reviewed a
*diff*. A diff-based review is structurally blind to a defect in code that did not change, and to
a defect that exists only in the interaction between changed and unchanged code. Roughly two
thirds of what you are looking at has never been read by a reviewer in its own right. Your
counterpart is separately reviewing the change set; you are covering what that cannot see.

**What this is.** The portable evaluation runner for a public closed-loop CARLA benchmark
(OOD-PerceptionBench v0.9). `run_benchmark.py --config <file>` supervises N worker processes,
each launching a vendored CARLA Leaderboard 2.0 `leaderboard_evaluator.py` over one route XML,
and decides — from how each attempt ended and what is on disk — whether that route has produced a
benchmark result. It resumes, retries on a four-axis budget model, pins GPUs and ports per
worker, and emits a JSON/markdown report whose exit code is the machine-readable verdict.

**It has ZERO hardware validation.** All 219 tests drive a stand-in evaluator; no CARLA server
and no GPU has ever been involved. Assume nothing here has met reality.

**The module map**, so you can spend your attention rather than your orientation:

| file | what it owns |
|---|---|
| `run_benchmark.py` | `Runner`: planning loop, worker supervision, `_settle` (the accounting), `main` |
| `oodbench/plan.py` | route discovery, task construction, `decide()` (resume gating), seed guards |
| `oodbench/results.py` | the status vocabulary and `Disposition` — what a checkpoint on disk means |
| `oodbench/state.py` | the persisted attempt ledger; budgets, settlement bit, epoch |
| `oodbench/report.py` | completeness, the exit contract, JSON + markdown |
| `oodbench/config.py` | schema, validation, the config digest |
| `oodbench/ports.py` | deterministic per-worker port allocation and probing |
| `oodbench/gpus.py` | CUDA vs Vulkan index discovery |
| `oodbench/jobscript.py` | renders the per-route bash script that is actually executed |
| `oodbench/reap.py` | fault detection, signal-from-exit-status, port-scoped CARLA reaping |
| `oodbench/backends/base.py` | `Attempt`, `AttemptOutcome`, checkpoint take-aside/restore |
| `oodbench/backends/local.py` | subprocess pool; `poll()` classifies how an attempt ended |
| `oodbench/backends/slurm.py` | sbatch/squeue/sacct; **the least exercised file here** |

`DESIGN.md` §6A is normative for `_settle`: a 24-cell table over (outcome class × on-disk
disposition) giving the budget charged and the settlement bit. The suite parses that table out of
the markdown and drives the real `_settle` for every cell.

**Where I would look, given that the change-focused reviews have been over the settlement model
four times and the rest of this package zero times:**

- `slurm.py` — it has an accounting path of its own and has never been reviewed. Does the §6A
  model even hold there? It never infers FAULT, and has no equivalent of
  `reap.describe_exit_signal`.
- `jobscript.py` — it renders the only thing that actually runs. Quoting, ordering of exports,
  `set -e` semantics, what `$?` actually captures, what happens with paths containing spaces.
- `ports.py` / `gpus.py` — two workers silently sharing a simulator or a GPU is a *silent*
  correctness failure, not a crash. `execution.allow_gpu_stacking` widens this deliberately.
- `results.py` — the status vocabulary is the foundation everything else is built on. A status
  string the real leaderboard emits that is missing from these sets lands in `UNKNOWN`.
- `config.py` — 641 lines of validation; a hole here is a hole in every downstream assumption.
- `base.py::take_checkpoint_aside` — the record-preservation invariant that three findings
  already landed on.

## Deliberately out of scope

- Zero hardware validation. Documented at the top of `STATUS.md`. Not a finding.
- **Five findings already deferred by explicit user decision** — do not re-report as new:
  resume-cache identity is only `rel_dir/stem_seedN`; SLURM `squeue` exceptions read as completed
  jobs; SLURM has no node-local reserved-port probe; the records generator accepts non-42 seeds;
  the runner reports hard-coded release metadata. **Anything else in `slurm.py` is in scope.**
- Style, naming, comment density, and the fact that the comments are unusually long — that is
  deliberate and the user wants it.
- Test files are not in this payload; judge the source on its own terms.

## The claim this code supports

**None directly — infrastructure only.** This runner produced no published number. What it
supports is the release's reproducibility claim: a third party points it at their own agent and
gets a number comparable to our 17 published baselines.

So the failure that matters is never a crash — a crash announces itself. It is **silent**
wrongness in one of two directions:

- **False result** — something that is not a benchmark result gets reported as one (a record
  written by a dying simulator; a record preserved from an earlier attempt; a route that ran on a
  GPU shared with another worker; a route that ran against a simulator another worker was using).
- **False gap** — a legitimate model result reported as an infrastructure failure at exit 1. A
  model that scores `Failed - TickRuntime` on every route is a real, publishable result; one of
  the 17 baselines is exactly that.

**The most valuable thing you can produce is a concrete scenario in the unchanged code where the
runner reports the wrong thing and nothing errors.** Isolation and determinism holes are the
richest seam and the least examined: two workers sharing an RPC port, a Vulkan adapter, or a seed
would corrupt results while every route "succeeds".
