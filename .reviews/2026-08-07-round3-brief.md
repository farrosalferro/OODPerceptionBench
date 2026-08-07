## Original request (verbatim)

> Continue the OOD-PerceptionBench v0.9 release. Read
> <PERC>/the (unshipped) internal resume point
> first — it's the resume point. Background is in the (unshipped) internal release plan and the cross-review
> record at staging/repo/.reviews/2026-08-07-runner-records.md.

`the (unshipped) internal resume point` (the resume point the user pointed at) says, verbatim:

> **The decision round 2 made, which needs your eye**
>
> **MAJOR 2 was settled in favour of the CODE, and `DESIGN.md` was moved to match.** The pinned
> rows (`ABNORMAL_END × RETRY_RECORD → infra/never-settle`, and `× UNKNOWN → infra`) are formally
> superseded. The agent's third argument is the one worth checking yourself:
>
> > six rows of the published v0.9 record set hold a RETRY_RECORD status as their final result
> > (four `Failed - Agent couldn't be set up`, written by the agent-init watchdog on a hang — the
> > same population that trips `route_timeout_s`), so under the pinned rows the released runner
> > would report those six as unsettled at exit 1 on every resume, for ever.
>
> That is an empirical claim about your released data. If it holds, the code is right and the
> original model row was wrong. **Worth verifying against `records/` before accepting.**
>
> **Resume checklist**
> 1. **Re-run the two killed verification agents.** [...] One audits regressions and cell-for-cell
>    doc/code parity; the other enumerates the full `AttemptOutcome × disposition × teardown`
>    cross product and proves no cell can strand.
> 2. **Then run `/cross-review` on the result.** Non-negotiable: round 1's finding 6 existed
>    *because* a repair pass was never reviewed. Two rounds have each found real defects in the
>    previous one.
> 3. **Only then** commit and push.
>
> **Unchanged and still true**
>
> The runner has **zero hardware validation**. None of this makes it trustworthy — it closes
> correctness holes. A real CARLA sweep is still required before anyone should rely on it, and
> `STATUS.md` §2 lists the ten hardware-only items (H1–H10).

## What the implementing agent says it did

**This change set spans three repair rounds and is reconstructed from `git diff HEAD`, not from
the snapshot hook** — the hook only saw round three. `HEAD` is `c67a955`, pushed and untagged.
Rounds 1 and 2 were written by sub-agents in earlier sessions; round 3 is mine. **Rounds 1 and 2
have never been cross-reviewed**, which is why the whole stack is in front of you rather than
just my hunks.

Context: this is the portable evaluation runner for a public closed-loop CARLA benchmark. It
supervises N worker processes, each running a vendored `leaderboard_evaluator.py` over one route,
and decides — from how each attempt ended and what is on disk — whether that route has a
benchmark result. It has **zero hardware validation**: all 211 tests drive a stand-in evaluator,
no CARLA process has ever run under it.

The normative model is `runner/DESIGN.md` §6A: a 24-cell table over
(outcome class × on-disk disposition), where each cell says which of four retry budgets an
attempt charges and whether the route settles. The table is machine-checked — the test suite
parses the fenced block out of DESIGN.md and drives the real `Runner._settle` for every cell.

**What I did this session (round 3).** I re-ran the two verification agents the previous session
had killed, plus a third auditing the side effects. Two of the three independently derived the
same two defects; adversarial refuters failed to refute either. I fixed both myself:

**(A) `oodbench/reap.py` + `oodbench/backends/local.py` — death by signal is now read from the
exit status, not from stderr text.** Round 2 gated the FAULT *demotion* on `rc == 0`, which is
right, but the demotion sits inside `if fault:` and `fault` came from literal substring matching
against a stream. Two of those substrings were wrong: `"Aborted (core dumped)"` was written with
one space and had never matched anything (a shell pads the signal name into a fixed column), and
SIGKILL prints only `Killed`, which was in no pattern. So an evaluator that died of SIGABRT or
was OOM-killed hit `if not fault:` → `EXITED` → CLEAN_EXIT, and its crash-shaped record was
charged to the **model's** record budget and settled as the model's verdict at exit 0, silently.
New `reap.describe_exit_signal(rc)` recognises `rc < 0` (the job script itself signalled;
`Popen.poll` reports this directly) and the shell's `128 + N` relay **bounded above by
`NSIG-1`**. The bound is the load-bearing part: the vendored evaluator ends its own crash path
with `sys.exit(-1)` → exit status 255 = `128 + 127`, and there is no signal 127, so a naive
`rc >= 128` rule would have reclassified every self-terminated crash verdict — the same error
pointing the other way. `FAULT_PATTERNS` became `\s+`-tolerant regexes and kept as a *secondary*
signal, because `poll()` also consults them while an attempt is still **running**, where there is
no exit status yet. A bare `Killed` is deliberately still not a pattern (ordinary word, an agent
may log it, false positive costs a real retry).

**(B) `oodbench/plan.py` — `retry.infra_budget: 0` was a total dead end.** Both infra gates read
`infra_spent >= infra_budget`, and unlike every other axis that gate is evaluated during
*planning*, before the first attempt. On a virgin ledger `0 >= 0` held, so every route was
`SKIP_EXHAUSTED` before anything ran: exit 1, zero executions, healthy machine. Unrecoverable —
`--retry-infra-exhausted` clears a counter that was never charged, and deleting the ledger does
not help because at budget 0 the gate never consults it. Legal value (validation rejects only
`< 0`), and `tickruntime_budget` ships at `0` so `0` reads as the idiomatic "no retries". Both
gates now carry `infra_spent and infra_spent >= infra_budget` — the idiom the `killed` gate one
screen above already used. **This changes behaviour only when `infra_budget == 0`**, which I
asserted exhaustively over budget ∈ {1,2,3} × spent ∈ {0..4}.

**(C) Minor: `--dry-run --retry-infra-exhausted` spent the recovery it was asked to preview.**
The clear ran before planning and saved unconditionally. It is now applied in memory (so the
printed plan is still the plan the real run would make) and rolled back in the `finally` before
the last write. The rollback lives in the `finally` specifically because `run()` also saves the
ledger mid-plan, so restoring any earlier would leave the cleared value as the last thing on disk.

**(D) Minor: the exit-contract tests only ran on the diagonal** where `final` and `settled`
agree. Added the off-diagonal (a record preserved by a failed launch: final on disk, never
settled, must not exit 0) and extended the exhaustive flag-combination test over both shapes.

**(E) Minor: `DIGEST_COMPAT_DEFAULTS` pin is now asserted equal to the `SCHEMA` default** it
stands for. Nothing coupled them; if a future default bump left the pin behind, the carve-out
would start hiding a real operator choice from the config digest.

**Test discipline.** Every round-3 test was demonstrated RED first. Against the untouched pre-fix
tree: `12 failed, 10 passed`. Attributed per hunk by reverting exactly one at a time — A1 (the
exit-status check) 4 tests, A2 (the dead patterns) 1 test + 1 subtest, B (the gate guard) 4
tests, C (dry-run rollback) 1 test. One test goes red only with A1 **and** A2 both reverted
(either alone catches SIGABRT); that is stated in the module docstring rather than hidden.
Tests green before and after are labelled `GUARD` and are not offered as evidence of a fix.

**Two doc claims I corrected because they were false.** STATUS.md said both end-to-end trap tests
in `TestTheTrapEndToEndUnderTheStricterGate` were green-before-and-after guards; reverting
round 2's `clean_exit = rc == 0` alone gives `2 failed, 21 passed` and the second failure is
`test_a_crash_record_under_a_nonzero_exit_settles_on_the_bounded_axis`, so it is a real
regression test. And DESIGN.md §6A.2 claimed the `rc` gate caught "SIGSEGV, SIGABRT, the OOM
killer" when two of those three were still swallowed.

**One empirical claim I verified first-hand** before accepting round 2's central decision: I
classified all 8,550 rows of the published `records/ood_perceptionbench_records_v0.9.csv` through
the runner's own `RETRY_STATUSES`. Six rows hold a RETRY_RECORD status as their final result (4×
`Failed - Agent couldn't be set up`, all `vad`/`hard_break`; 1× `Failed - Simulation crashed` and
1× `Failed - Agent crashed`, both `hydra_next`), plus 2 blank-status rows that classify UNKNOWN —
the other superseded row. So the pinned model would have stranded **eight** published rows. The
direction of that decision is settled; whether the code and DESIGN.md now agree is not.

Suite: **211 passed, 60 subtests** (was 188). De-hardcode grep over `runner/`
(the private-path token list) is empty. `records/verify.sh` still passes
all four stages and Table 1 regenerates exactly.

## Deliberately out of scope

- **Zero hardware validation, and nothing here changes that.** No CARLA process has ever run
  under this runner. STATUS.md §2 lists ten hardware-only items (H1–H10). Please do not report
  "this needs a real sweep" as a finding — it is the single loudest statement in STATUS.md.
- **Five cross-review findings from the 2026-08-07 review remain untouched, by explicit user
  decision, each pending separately:** (1) resume cache identity is only `rel_dir/stem_seedN`, so
  a model/checkpoint/route change does not invalidate it; (3) SLURM `squeue` exceptions read as
  completed jobs; (4) SLURM has no node-local reserved-port probe; (7) the records generator
  accepts non-42 seeds while labelling output the seed-42 release; (8) the runner reports
  hard-coded release metadata. See `.reviews/2026-08-07-runner-records.md`.
- The SLURM backend still classifies a job ending FAILED with a non-zero exit as `EXITED` and
  never infers FAULT. Known, overlaps finding 3, out of scope.
- `oodbench/reap.py` `terminate_process_tree`, `find_carla_on_ports` and the port machinery are
  unchanged from `HEAD` and are not part of this review.
- Two pre-existing unused imports (`run_benchmark.py` `EXIT_NO_WORKERS`, `local.py` `Path`).
- Nothing here is committed. Do not propose commit hygiene.

## The claim this code supports

**None directly — infrastructure only.** This runner produced **no** number in the paper; every
published result came from the earlier internal SLURM orchestrators, and `records/` is generated
from that result tree, not from this runner's reports. `records/verify.sh` confirms Table 1 still
regenerates exactly from the frozen CSVs.

What it supports indirectly is the release's central reproducibility claim: that a third party
can point this runner at their own agent and get a number comparable to our 17 baselines. The
failure mode that matters is therefore **not** a crash — it is the runner silently accepting
something that is not a benchmark result *as* one, or silently refusing to produce one. Both
directions are live here:

- **False result.** A record written by a dying simulator, or preserved from an earlier attempt,
  counted as the model's verdict. Defect (A) is exactly this, and it is the fourth recurrence of
  the same underlying finding across three rounds.
- **False gap.** A legitimate model result reported as an infrastructure failure at exit 1.
  This is **THE TRAP**: a model that scores `Failed - TickRuntime` (or bare `Failed`) on every
  route is a real, publishable result — one of the 17 baselines, ADMLP, is exactly this — and
  MUST exit 0. It has nearly been broken three times. Defect (B) is a false gap in its purest
  form: zero routes run, exit 1, healthy machine.

The most valuable finding you can give me is a case where a route strands forever, or where a
kill-manufactured record settles as a model verdict, that the 24-cell table does not cover.
