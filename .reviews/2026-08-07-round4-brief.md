## Original request (verbatim)

> Continue the OOD-PerceptionBench v0.9 release. Read
> the (unshipped) internal resume point first — it's the resume point. Background is in the (unshipped) internal release plan
> and the cross-review record at staging/repo/.reviews/2026-08-07-runner-records.md.

`the (unshipped) internal resume point`: *"Then run `/cross-review` on the result. **Non-negotiable**: round 1's finding
6 existed because a repair pass was never reviewed. Two rounds have each found real defects in
the previous one."* And: *"The runner has **zero hardware validation**. None of this makes it
trustworthy — it closes correctness holes."*

**This is that rule applied to itself.** A cross-review of round 3 (same tooling, same two
reviewers, record at `.reviews/2026-08-07-runner-round3.md`) returned BLOCKING with four
findings. The user ruled on all four. Round 4 implements those four rulings — and round 4 is a
repair pass, so it gets reviewed too. **Please concentrate on the round-4 delta**; rounds 1–3
were reviewed in the immediately preceding pass and their findings are closed.

## What the implementing agent says it did

Same runner as the previous review; the whole uncommitted stack vs `c67a955` is attached because
it all lands in one commit, but only the four items below are new since you last saw it.

**(1) `local.py`: `clean_exit = rc == 0` → `clean_exit = signalled is None`.** This was YOUR
finding (cursor, high, BLOCKING) and the user chose it over keeping round 2's rule. `rc == 0` was
a proxy for "our process did not die hard"; the vendored evaluator's own crash path is
`sys.exit(-1)` → 255, a self-terminated verdict, so under the proxy those verdicts could never be
demoted and a UE4 abort in the shared stderr sent `Failed - Simulation crashed` /
`Failed - Agent couldn't be set up` to the ambiguity axis instead of the model's record budget.
**A round-2 regression test was inverted** (`..._selfterminated_crash_record_charges_the_MODEL_
budget_not_the_kill_axis`), with the argument written into its docstring rather than applied
quietly. I also added `TestPatternIndependence`, which asserts over (status × stderr × rc) that
after a process has exited a fault pattern cannot move any counter — the invariant that would
have caught round 3's mistake, and which did not exist before your review.

**(2) `state.py`: `ACCOUNTING_EPOCH = 2`,** stamped into every ledger, with
`RunState.accounting_model_changed()` and a warning naming both epochs. Your finding and codex's,
independently. An absent field reads as epoch 1 (only epoch 1 ever wrote a ledger without it).
The config digest carve-out is unchanged — the user chose to keep it and add the epoch alongside,
so that "did the operator change a setting?" and "did the runner change what one means?" are
answered separately instead of collapsed.

**(3) `run_benchmark.py`: `--dry-run` now writes nothing at all.** codex's finding. The whole
ledger is snapshotted before planning and restored in the `finally`, replacing round 3's
field-by-field rollback. Planning still mutates in memory, because otherwise the printed plan
would not be the plan a real run makes. On a tree with no ledger it leaves none behind.

**(4) `DESIGN.md` §6A.2 documents the `128 + N` inference as an assumption** about the
*configured* evaluator rather than closing it — the user chose document-for-v0.9. The pinned
evaluator uses only `sys.exit(-1)`/`sys.exit(0)` (verified at `leaderboard_evaluator.py:584,586`),
argparse 2, uncaught exception 1 — all outside 129–192. A third-party evaluator could collide.
The `trap`-based side channel you suggested is deferred to first hardware validation because it
changes the generated job script, the one artifact never run against real CARLA.

Round-4 tests were held to the same red-first discipline, attributed one hunk at a time: hunk D
(discriminator) 1 test + 4 subtests, hunk E (ledger snapshot) 4 tests, hunk F (epoch) 2 tests.
Suite **211 → 218 passed, 87 subtests**. De-hardcode grep empty. `records/verify.sh` unaffected.

## Deliberately out of scope

- **Zero hardware validation**, unchanged and loudly documented (STATUS.md §2, H1–H10). Not a
  finding.
- The five findings from the *first* cross-review that the user deferred by explicit decision:
  resume-cache identity, SLURM `squeue` exceptions read as completed, SLURM node-local port
  probe, records-generator seed guard, hard-coded release metadata.
- The SLURM backend has no FAULT inference and no equivalent of `describe_exit_signal`. Known,
  overlaps a deferred finding.
- Rounds 1–3, which you reviewed in the previous pass; their findings are closed above.
- Two pre-existing unused imports. Nothing is committed — no commit-hygiene findings.

## The claim this code supports

**None directly — infrastructure only.** This runner produced no published number; `records/`
comes from the earlier cluster orchestrators and still regenerates Table 1 exactly.

The failure modes that matter are the two directions of silent wrongness. **False result:** a
record written by a dying simulator, or preserved from an earlier attempt, counted as the model's
verdict. **False gap (THE TRAP):** a model that legitimately scores `Failed - TickRuntime` or
bare `Failed` on every route — one of the 17 baselines, ADMLP, is exactly this — reported as an
infrastructure failure at exit 1.

Change (1) moves a whole population between budgets. **The most valuable thing you can do is
attack it in the direction opposite to the one you found last time:** now that a self-terminated
exit with a fault pattern is demoted regardless of exit status, is there a case where a record
that a *kill* really did manufacture now gets credited to the model? And does `TestPatternIndependence`
actually hold, or did I assert an invariant the code does not have?
