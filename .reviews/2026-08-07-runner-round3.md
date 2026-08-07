# Cross-review — runner/ round 3 — 2026-08-07

**Reviewers:** `gpt-5.6-luna` @ xhigh (codex, OpenAI) · `cursor-grok-4.5-high` (cursor, xAI)
**Implementer:** Claude (Opus 5). Three labs across the loop.
**Verdicts: codex `concerns`, cursor `BLOCKING`.** Neither reviewer failed.

This is the review the previous record (`2026-08-07-runner-records.md`) said was owed on the
repair pass. It covers the **whole uncommitted stack** — rounds 1, 2 and 3 against `c67a955` —
because rounds 1 and 2 had never been reviewed by a different model family.

## Change set

19 files, 277 KB diff. **Reconstructed from `git diff HEAD -- runner/` plus three untracked test
files**, not from the snapshot hook: the change set spans three sessions and the hook only held
round 3. File list:

```
runner/DESIGN.md                     runner/oodbench/report.py
runner/README.md                     runner/oodbench/state.py
runner/STATUS.md                     runner/run_benchmark.py
runner/configs/example.yaml          runner/tests/test_hard_death_and_infra_zero.py  (new)
runner/configs/reference_agent.yaml  runner/tests/test_integration_local.py
runner/oodbench/backends/local.py    runner/tests/test_plan.py
runner/oodbench/config.py            runner/tests/test_report_and_state.py
runner/oodbench/plan.py              runner/tests/test_result_preservation.py
runner/oodbench/reap.py              runner/tests/test_settlement_model.py           (new)
                                     runner/tests/test_verification_findings.py      (new)
```

## What round 3 was

The two verification agents killed mid-run in the previous session were re-run, plus a third
auditing side effects. Two of the three independently derived the same two majors; adversarial
refuters failed to refute either. Both were fixed:

- **MAJOR A** — the hard-death gate hung on stderr substrings, two of which never matched what a
  shell writes (`"Aborted (core dumped)"` is column-padded; SIGKILL prints only `Killed`). SIGABRT
  and OOM deaths were classified CLEAN_EXIT and charged to the **model's** record budget. Death by
  signal is now read from the exit status (`reap.describe_exit_signal`).
- **MAJOR B** — `retry.infra_budget: 0` gated every route before a single attempt, on a healthy
  machine, unrecoverably. Both gates now use the guarded idiom.

Plus three minors and two false doc claims corrected. Suite 188 → **211 passed, 60 subtests**;
every round-3 test demonstrated red first (`12 failed, 10 passed` against the untouched tree).

**One empirical claim verified first-hand** (the item `the (unshipped) internal resume point` flagged for the user's
eye): classifying all 8,550 rows of the published records through the runner's own
`RETRY_STATUSES` gives **6** RETRY_RECORD rows (4× `Failed - Agent couldn't be set up`, all
`vad`/`hard_break`; 1× `Failed - Simulation crashed`, 1× `Failed - Agent crashed`, both
`hydra_next`) **plus 2 blank-status rows** that classify UNKNOWN — the other superseded cell. The
pinned model would have stranded **eight** published rows, not six. Claim confirmed and
understated; the direction of the MAJOR-2 decision is settled.

## Findings and adjudication

| # | Sev | Source | Finding | Verdict | Resolution |
|---|---|---|---|---|---|
| 1 | high | cursor | Demotion keys off `rc == 0`, so the evaluator's own `sys.exit(-1)`→255 crash verdicts with a CARLA abort in the shared stderr become FAULT→`killed` | **ACCEPT** | escalated → **user chose the reviewer's rule**; fixed in round 4 |
| 2 | med | codex | Relayed exit codes 129–192 could be an intentional evaluator exit, not a signal | **ACCEPT, narrowed** | escalated → **user chose document-for-v0.9**; §6A.2 |
| 3 | med | codex | `--dry-run` still writes settlement bits, task entries and the config digest | **ACCEPT** | escalated → **user chose fully inert**; fixed in round 4 |
| 4 | med | codex | `DIGEST_COMPAT_DEFAULTS` hides an accounting-model migration in legacy ledgers | **ACCEPT** | escalated → **user chose the ledger epoch**; fixed in round 4 |
| 5 | med | cursor | Same defect as 4, independently derived | **ACCEPT** (merged with 4) | as 4 |

**Nothing rejected on the merits.** Every finding was escalated rather than fixed by the agent
that found it; the user ruled on all four, choosing the recommended option in each case. The
resulting round-4 changes are recorded below.

### 1 — the demotion's discriminator is `rc == 0` where it should be "was not signalled"

cursor is right on the principle, and this is the sharpest finding of the review.

The demotion exists because CARLA shares the attempt's stderr, so a pattern in that stream may be
about a different process. Its justification reaches exactly as far as *"our process is fine."*
Round 2 proxied that as `rc == 0`. But the vendored evaluator ends its own crash paths with
`sys.exit(-1)` → **255** — verified: `leaderboard_evaluator.py:584,586` uses only `-1` and `0` —
which is a *self-terminated verdict*, not a hard death. `describe_exit_signal(255)` correctly
returns `None`. So the exact discriminator now exists, and `rc == 0` is a strictly narrower proxy
for it.

Consequence, and it is round 3's own doing: making the padded `Aborted` pattern match moved a
population that previously fell through to `EXITED`→`record` onto `FAULT`→`killed`. Those are
self-terminated `Simulation crashed` / `Agent couldn't be set up` verdicts — **the same status
family as four of the six published `vad` rows**.

Under cursor's rule (`demote when has_record and signalled is None`), `FAULT_PATTERNS` becomes
**accounting-irrelevant post-exit**, which is worth stating because it is checkable: with a
record, `signalled` alone decides; without a record, both CLEAN_EXIT and ABNORMAL_END route
`NO_FINAL_RECORD` to `infra` and both reach `_maybe_quarantine` through the same
`_charge_infra`, so the cell is identical either way. The patterns would then be used only by the
in-flight kill branch, where there is no exit status and where they are unambiguously right.

**Why this is escalated rather than fixed:** it inverts round 2's deliberate, documented,
red-first-tested decision (the round-2 test asserting the current behaviour was red pre-fix; it is now
`..._selfterminated_crash_record_charges_the_MODEL_budget_not_the_kill_axis`). That is a §6A model change of exactly the
class `the (unshipped) internal resume point` reserved for the user.

### 2 — exit codes 129–192 are a heuristic, not a proof

Valid in principle, **unreachable for the pinned evaluator**: `sys.exit(-1)`/`sys.exit(0)` are its
only exits, argparse uses 2, an uncaught exception 1. All outside the range. The residual is real
but narrow — `leaderboard.root`/`evaluator` are configurable, so a *third-party* evaluator could
exit 134 intentionally. codex's suggested side channel (an explicit trap in `jobscript.render`)
would remove the inference entirely. Recommend documenting the assumption for v0.9 and taking the
side channel when the runner is first exercised against real hardware.

### 3 — `--dry-run` is not inert

Confirmed and **pre-existing** (not introduced by round 3): `run()`'s plan loop mutates
`st.finished` for SKIP_DONE / SKIP_EXHAUSTED / RUN, materialises a `TaskState` for every planned
route via `state.get`, and `load_or_create` overwrites the stored `config_digest` — then saves.
Round 3's rollback restores only `attempts_infra`. codex's point that previewing under a changed
config **erases the later `config_changed()` warning** is the sharp end of it.

Escalated rather than applied because it is entangled with the open decision in the next section.
**Superseded by R4-2 below:** round 4 first implemented this as a whole-ledger snapshot/restore,
and the round-4 review showed that is still wrong — it leaves a crash window and a non-atomic
restoring write. The shipped fix is that a dry run never calls `save()` at all.

### 4 + 5 — the digest carve-out conflates two different questions

**Both labs, independently — and a third agent (round 3's own side-effect auditor) reached it
too.** Three of three.

`DIGEST_COMPAT_DEFAULTS` omits `retry.killed_budget: 2` from the digest so pre-existing output
roots resume without a false "different configuration" warning. That reasoning is sound *for the
digest*, whose question is "did the operator change a setting?" — and a key nobody set is not a
setting they changed.

But the accounting model changed alongside the key. A pre-round-1 ledger holds `attempts_record`
counts that include kill-shaped ends (which now charge `killed`), and `attempts_killed = 0`. So
resuming an old output root silently continues it under a different model with different
settlement timing. `config.py`'s docstring anticipates the objection — *"the runner version is
stamped separately into every report"* — but `state.json` stores **no** runner version, so
nothing is compared at resume time and the operator would have to diff two report files by hand.

Recommendation: keep the carve-out and **persist an accounting-model epoch in the ledger**,
warning when it changes. That separates the two questions instead of collapsing them. Dropping
the carve-out instead (codex/cursor's first option) reverts round 2's MINOR 5 and makes every
existing output root re-plan 475 routes.

## Intent drift

- **[cursor, accepted]** `the (unshipped) internal resume point` listed `--retry-infra-exhausted` persistence under
  `--dry-run` as an **open decision reserved for the user**. Round 3 changed it unilaterally
  (in-memory clear, rolled back before the final write). The direction is almost certainly right
  — it matches the documented `--dry-run` contract — but it was not round 3's call to make, and
  it is flagged here rather than defended.
- **[codex, rejected]** codex reported that the non-negotiable cross-review gate was still unmet.
  It was reviewing the change set that *is* that gate; a reviewer cannot observe its own run. The
  brief's statement that rounds 1 and 2 were never cross-reviewed is what it read, and that
  statement is true of those rounds, not of this review.

## Why nothing was fixed *before* the user ruled

Every surviving finding is a change to the §6A accounting model or to a behaviour the user
explicitly reserved, and all five are marked `changes_numbers` by their reviewers. Four rounds in,
the pattern is unambiguous: each round's confident repair contained the next round's defect, and
round 3's own `STATUS.md` entry says so. Applying a fifth blind model change — before the user had
seen any of rounds 1–3 — would have repeated the exact failure this record exists to end.

## Round 4 — what was implemented after the user ruled

All four decisions went to the reviewers' side. `STATUS.md` items 20–23 and `DESIGN.md` §6A.6
carry the full reasoning; in brief:

1. **`clean_exit = signalled is None`** (`local.py`). Consequence worth recording: after a
   process has exited, a fault pattern can no longer move any counter, and
   `TestPatternIndependence` now asserts that over (status × stderr × rc). It is the test that
   would have caught round 3's own mistake, and it did not exist before this review.
   **A round-2 regression test was inverted** — `..._selfterminated_crash_record_charges_the_
   MODEL_budget_not_the_kill_axis` — with the argument written into its docstring.
2. **`state.ACCOUNTING_EPOCH = 2`**, stamped into every ledger, with
   `RunState.accounting_model_changed()` and a warning naming both epochs. An absent field reads
   as epoch 1, since only epoch 1 ever wrote a ledger without it.
3. **`--dry-run` snapshots and restores the whole ledger** and saves nothing, replacing round 3's
   field-by-field rollback. On a fresh tree it leaves no ledger behind.
4. **§6A.2 states the `128 + N` assumption** and scopes it to the *configured* evaluator, with
   the `trap`-based fix deferred to first hardware validation.

Round-4 tests were held to the same discipline — red first, one hunk at a time: hunk D (the
discriminator) 1 test + 4 subtests, hunk E (the ledger snapshot) 4 tests, hunk F (the epoch) 2
tests. Suite **211 → 218 passed, 87 subtests**. De-hardcode grep still empty.

## State

Nothing is committed. `HEAD` is `c67a955`, pushed and untagged, so no third party has run any of
this and no published number is affected — `records/` is generated from the earlier cluster
orchestrators' result tree, and `records/verify.sh` still passes all four stages with Table 1
regenerating exactly.

The runner still has **zero hardware validation**, and finding 1 is the best argument yet that the
remaining risk is not reachable by more logic tests: a stand-in evaluator writes whatever the test
author tells it to, which is why three rounds never noticed that the real shell message has
seventeen spaces in it.

---

# Round 4, reviewed in turn — 2026-08-07

Round 4 is itself a repair pass, so it went through the same gate. Same tooling, same two
reviewers, blind and in parallel. Raw artifacts: `2026-08-07-round4-{codex,cursor,merged}.json`.

**Verdicts flipped from the previous pass: cursor `clean`, codex `BLOCKING` (2 findings).**
Neither failed. cursor specifically re-attacked the demotion from the direction I asked for and
reported: *"no path where a runner kill-manufactured record is credited to the model (kills preset
FAULT/TIMEOUT/KILLED before demotion runs), and the post-exit pattern-independence invariant holds
in `poll`/`_settle`."*

## R4-1 — "caught termination can be misclassified" · codex, high · **ACCEPT as a documented assumption**

The reviewers disagreed, so this was settled by reading the code rather than by counting votes.

- **The runner-initiated half is already closed, and codex's suggested fix is a no-op there.**
  Every runner kill presets the outcome before the demotion can run: the in-flight branch sets
  `FAULT`/`TIMEOUT` explicitly (`local.py:184-185`), `_drain` goes through `kill()` which sets
  `KILLED` (`local.py:291`), and `poll()` returns immediately when `attempt.outcome is not None`.
  cursor checked this independently and reached the same conclusion.
- **The remaining half is an *external* signal the child catches and converts to a non-signal
  exit.** The vendored evaluator installs a handler for **SIGINT only**
  (`leaderboard_evaluator.py:138`); the runner sends SIGTERM then SIGKILL, and the child is a
  session leader (`start_new_session=True`), so it never receives a terminal's Ctrl-C.
- **And the case codex worries about is, in the real evaluator, the case that must go to the
  model.** That SIGINT handler raises
  `RuntimeError("Timeout: Agent took longer than {}s to setup")` — which is the
  `Failed - Agent couldn't be set up` path, **the status of four of the six published v0.9
  rows**, produced by the evaluator's own watchdog signalling itself. Charging that to the
  model's record budget is correct: the agent was too slow. Treating it as an ambiguous kill
  would be the error.

So the residual is: an operator sends SIGINT by hand to a session-leader child, which converts it
into exit 255 plus a crash-shaped record. Narrow, and the same *class* as the `128 + N`
assumption already documented in §6A.2 — a signal the runner did not send, laundered by the
child into a non-signal status. Folded into that note rather than given a code change, and it
resolves with the same deferred fix (a `trap` side channel in the generated job script, at first
hardware validation).

## R4-2 — "dry-run rollback is not crash-safe" · codex, medium, confirmed · **ACCEPT and FIXED**

Correct, and it is a defect in *my* round-4 fix rather than in anything older. `Runner.run()`
saved the ledger after the plan loop and before the dry-run branch, so a `SIGKILL` in between
froze the preview's settlement bits and config digest on disk — and the restoring `write_bytes`
was itself non-atomic, which is a new (if tiny) corruption risk the snapshot introduced.

Fixed by deleting the snapshot entirely and guarding the write instead: **a dry run never calls
`save()`**. All five call sites audited — three now guarded, two (`run()`'s supervision loop and
`_drain`) unreachable because `run()` returns at the dry-run branch first. Crash-safe by
construction rather than by narrowing a window.

`test_no_dry_run_path_writes_the_ledger_at_all` asserts it structurally: it patches
`RunState.save` to raise and requires a dry run to still exit 0 — *and* requires the same fatal
`save` to make a real run fail, so a guard stuck on is caught as readily as one stuck off. That
test does not care where the call sites are, which is what makes it survive refactoring.

Suite **218 → 219 passed, 87 subtests**.

## Standing back

Five review rounds, and every one found something real in the round before it — including this
one, which found a defect in a fix written four hours earlier in response to the previous round.
The rate is not obviously decaying. What has changed is the *kind*: rounds 1–3 found model
errors, round 4 found a crash window in a repair. Both reviewers now agree on the settlement
model itself, and cursor's clean verdict is the first in the sequence.

The runner still has **zero hardware validation**, and R4-1 is a reminder of the shape of what
remains: every open question in this record now bottoms out in "what does a real process actually
do to a real stream", which no amount of stand-in testing answers.
