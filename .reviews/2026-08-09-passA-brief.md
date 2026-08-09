## Original request (verbatim)

> okay, thanks.  It seems that you have fixed the errors from the previous session. To be more
> sure about this, let's do one more round of cross-review. Make the review exhaustive.

Earlier in the same session, establishing the standing context:

> Continue the OOD-PerceptionBench v0.9 release. Read the (unshipped) internal resume point first — it's the
> resume point.

`the (unshipped) internal resume point`, the user's own checklist: *"Then run `/cross-review` on the result.
**Non-negotiable**: round 1's finding 6 existed because a repair pass was never reviewed."* And:
*"The runner has **zero hardware validation**. None of this makes it trustworthy — it closes
correctness holes."*

**This is a final acceptance review, and the user has asked for it to be exhaustive.** Do not
grade on a curve. A finding you are 60% sure of is worth reporting with that confidence stated.

## What the implementing agent says it did

You are reviewing the **committed** change set `c67a955..3a7a0c9` (2 commits) of the portable
evaluation runner for a public closed-loop CARLA benchmark. It supervises N worker processes,
each running a vendored `leaderboard_evaluator.py` over one route, and decides — from how each
attempt ended and what is on disk — whether that route has a benchmark result. **It has ZERO
hardware validation: all 219 tests drive a stand-in evaluator; no CARLA process has ever run
under it.**

`runner/DESIGN.md` §6A is normative: a 24-cell table over (outcome class × on-disk disposition)
saying which of four retry budgets an attempt charges and whether the route settles. The suite
parses that table out of DESIGN.md and drives the real `Runner._settle` for every cell.

**This code has now been through five review rounds, and every round found real defects in the
one before it, without exception.** You (or your counterpart) produced rounds 0, 4 and 5. What
survived, in order, so you can aim at what is left rather than at what is closed:

- r0 cross-review: 9 findings, both reviewers BLOCKING.
- r1: pinned the model. Its own verification found 3 majors + 3 minors.
- r2: closed those six. Its verification found 2 more majors.
- r3: the hard-death gate hung on stderr substrings, two of which never matched what a shell
  actually writes (`"Aborted (core dumped)"` is column-padded to seventeen spaces; SIGKILL prints
  only `Killed`) — so SIGABRT and OOM deaths were charged to the **model's** retry budget and
  settled as its verdict at exit 0, silently. And `retry.infra_budget: 0` gated every route
  before a single attempt, unrecoverably, on a healthy machine.
- r4 cross-review: 4 findings. The demotion's discriminator was `rc == 0` where it should be
  "was this process signalled" (the evaluator's own crash path is `sys.exit(-1)` → 255, a
  self-terminated verdict); the accounting model was not versioned in the ledger; `--dry-run` was
  never inert; the `128+N` inference is an assumption.
- r5 cross-review of that repair: a crash window in the dry-run snapshot. Fixed by never saving.

**The newest and least-validated code, in the order I would attack it:**

1. `reap.describe_exit_signal` + `local.poll` — signal classification from exit status.
   `rc < 0`, or `128 < rc <= 128+NSIG-1`, means signalled; `clean_exit = signalled is None`.
2. `plan.decide` — both infra gates now read `infra_spent and infra_spent >= infra_budget`.
3. `state.ACCOUNTING_EPOCH` + `RunState.accounting_model_changed()`.
4. `run_benchmark.main` — three guarded `state.save()` call sites for dry-run inertness.

## Deliberately out of scope

- **Zero hardware validation.** Documented at the top of `STATUS.md` and in §2 (H1–H10). Do not
  spend a finding on it.
- **Five findings deferred by explicit user decision**, each pending separately: resume-cache
  identity is only `rel_dir/stem_seedN`; SLURM `squeue` exceptions read as completed jobs; SLURM
  has no node-local reserved-port probe; the records generator accepts non-42 seeds; the runner
  reports hard-coded release metadata. **Do not re-report these as new.** A finding about how one
  of them *interacts with the new accounting model* is in scope and welcome.
- The `trap` side channel in `jobscript.render` that would remove the `128+N` inference: deferred
  to first hardware validation by user decision.
- Two pre-existing unused imports. Commit hygiene — it is already committed and pushed.

## The claim this code supports

**None directly — infrastructure only.** This runner produced no published number; the paper's
results came from earlier cluster orchestrators, and `records/verify.sh` still regenerates
Table 1 exactly from frozen CSVs.

What it supports is the release's reproducibility claim: a third party points this at their own
agent and gets a number comparable to our 17 baselines. So the failure that matters is never a
crash — it is **silent** wrongness, in either direction:

- **False result.** A record written by a dying simulator, or preserved from an earlier attempt,
  counted as the model's verdict. Four of the five rounds found an instance of this.
- **False gap (THE TRAP).** A model that legitimately scores `Failed - TickRuntime` or bare
  `Failed` on every route — one of the 17 baselines, ADMLP, is exactly this — reported as an
  infrastructure failure at exit 1.

**The single most valuable thing you can produce is a concrete (status, outcome, budget, resume
mode) tuple where the runner reports the wrong thing and no test notices.** Second most valuable:
a test that passes vacuously, or asserts something weaker than its name and docstring claim —
this suite's discipline is that every regression test was demonstrated red first, and a test that
was never red is exactly how three of the rounds above got their defect through.
