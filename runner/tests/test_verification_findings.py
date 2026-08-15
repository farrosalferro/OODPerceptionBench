"""Regression tests for the six defects found by the *verification* pass over round one.

Round one pinned the attempt-accounting model (DESIGN.md section 6A) and closed cross-review
findings 2/5/6/9. An independent verification pass then read the result and found six more:
one hole in the FAULT demotion, one doc/code drift, one dead end that violates the model's own
invariant, and three smaller ones.

**Every test here that claims a defect was demonstrated RED against the pre-fix tree before the
fix was written** -- first as a whole file against the untouched tree, then again one hunk at a
time, reverting only that fix and confirming only its own test failed. The recorded failure is
quoted verbatim in each such test's docstring, under ``RED BEFORE THE FIX``. That is spelled out
because four guards shipped in the previous round were never seen red, and a test that has only
ever been green does not distinguish "the code is right" from "the test and the code were
written by the same hand on the same day".

The eight defect tests, each red first:

======================================  ========================================================
``..._NONZERO_exit_is_still_a_fault``   MAJOR 1
``TestDocAndCodeAgree`` (both)          MAJOR 2 -- red twice: once for the missing table, once
                                        with the *pinned* rows pasted in, to show it has teeth
``..._gated_by_a_spent_infra_budget``   MAJOR 3 (unit) + ``TestDeadEndRecovery...`` (CLI)
``..._infra_debt_is_cleared_by_...``    MAJOR 3, within-run half
``..._without_the_killed_budget``       MINOR 4
``TestDigestCompatibility`` (2 of 4)    MINOR 5
``..._is_labelled_not_reached``         MINOR 6
======================================  ========================================================

The rest are **guards, not regressions, and were green before the change**: the two other
directions of the FAULT gate, the two positive cases of the killed gate, the two
digest-sensitivity checks, and the other two ``unsettled_reason`` values. They are labelled as
such where they appear; none of them is presented as evidence that a defect was fixed.

**Correction, round three.** An earlier revision of this docstring put the *whole* of
``TestTheTrapEndToEndUnderTheStricterGate`` in that list. That was wrong, in the conservative
direction: reverting only ``clean_exit = rc == 0`` gave ``2 failed, 21 passed``, and the second
failure was the end-to-end proof that an ambiguous record reaches the bounded ``killed`` axis
rather than the model's record budget. Only
``test_a_degenerate_tickruntime_sweep_with_shutdown_segfaults_still_exits_zero`` is a true guard.

**Correction, round four — and this one changes an assertion, not a label.** A cross-review
(2026-08-07, `cursor-grok-4.5-high`) found that the rule MAJOR 1 encoded was itself wrong.
``rc == 0`` is a proxy for *"our process did not die hard"*, and a narrower one than the exact
test that now exists: the vendored evaluator's own crash path is ``sys.exit(-1)`` → status 255,
a **self-terminated verdict**. The discriminator is now ``signalled is None``. Two consequences
for this file: the regression test named above was **inverted** and renamed
``..._selfterminated_crash_record_charges_the_MODEL_budget_not_the_kill_axis`` (its docstring
carries the argument), and ``TestPatternIndependence`` was added to assert the property that
would have caught round three's mistake — after a process has exited, a fault pattern in the
*shared* stream cannot move any counter.

Ordering note: the table check (`TestDocAndCodeAgree`) is the load-bearing one. The others are
point defects; that one makes the *class* of defect -- "the document says one thing and the code
does another" -- detectable by the suite instead of by the next reviewer.
"""

import argparse
import copy
import hashlib
import json
import logging
import re
import subprocess
import time
import unittest
from pathlib import Path

import run_benchmark
from oodbench import EXIT_OK, config as config_mod, plan as plan_mod, report as report_mod
from oodbench.backends.base import Attempt, AttemptOutcome
from oodbench.backends.local import LocalBackend
from oodbench.state import RunState

from tests.test_integration_local import IntegrationBase, Site
from tests.test_settlement_model import (AGENT_CRASHED, COMPLETED, ModelBase, SIM_CRASHED,
                                         TICKRUNTIME)

DESIGN_MD = Path(__file__).resolve().parent.parent / "DESIGN.md"

FAILED_BARE = {"_checkpoint": {"progress": [1, 1],
                               "records": [{"status": "Failed",
                                            "scores": {"score_composed": 0.0}}]}}

SENSORS_INVALID = {"_checkpoint": {"progress": [1, 1],
                                   "records": [{"status": "Failed - Agent's sensors were invalid",
                                                "scores": {"score_composed": 0.0}}]}}

UNRECOGNISED = {"_checkpoint": {"progress": [1, 1],
                                "records": [{"status": "Failed - Brand New Status",
                                             "scores": {"score_composed": 0.0}}]}}

UNFINALIZED = {"_checkpoint": {"progress": [0, 1], "records": []}}

#: The six on-disk cases of 6A.5, as the record bytes that produce them.
ON_DISK = {
    "NO_FINAL_RECORD": UNFINALIZED,
    "ACCEPT": COMPLETED,
    "RETRY_RECORD": AGENT_CRASHED,
    "RETRY_TICKRUNTIME": TICKRUNTIME,
    "FATAL": SENSORS_INVALID,
    "UNKNOWN": UNRECOGNISED,
}

#: The outcomes that make up each class. ABNORMAL_END is checked over all three of its members,
#: because "the class decides, not the member" is exactly the property that keeps a wall-clock
#: kill and a stderr fault pattern accounted the same way.
CLASS_OUTCOMES = {
    "CLEAN_EXIT": [AttemptOutcome.EXITED],
    "ABNORMAL_END": [AttemptOutcome.TIMEOUT, AttemptOutcome.FAULT, AttemptOutcome.KILLED],
    "NEVER_STARTED": [AttemptOutcome.LAUNCH_FAILED, None],
    "TORN_DOWN": [AttemptOutcome.EXITED],
}

BUDGET_FIELD = {
    "record": "attempts_record",
    "tickruntime": "attempts_tickruntime",
    "infra": "attempts_infra",
    "killed": "attempts_killed",
}


# =======================================================================================
# MAJOR 1 -- the FAULT demotion ignored the child's exit status
# =======================================================================================
class TestFaultDemotionIsGatedOnACleanExit(ModelBase):

    def _poll(self, rc, stderr_text, record):
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        backend = LocalBackend(cfg, self.log)
        task.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        task.stderr_path.write_text(stderr_text, encoding="utf-8")
        if record is not None:
            task.result_path.write_text(json.dumps(record), encoding="utf-8")
        proc = subprocess.Popen(["bash", "-c", f"exit {rc}"])
        proc.wait()
        attempt = Attempt(task=task, worker=0, stdout_path=task.stdout_path,
                          stderr_path=task.stderr_path, handle=proc)
        self.assertTrue(backend.poll(attempt))
        return attempt

    def test_a_fault_pattern_with_a_NONZERO_exit_is_still_a_fault(self):
        """RED BEFORE THE FIX:

            AssertionError: <AttemptOutcome.EXITED: 'exited'> is not <AttemptOutcome.FAULT:
            'fault'> : the evaluator itself died hard (exit 139) and the demotion still called
            it a clean exit

        The demotion exists because the CARLA server shares this attempt's stderr, so a UE4
        crash during *shutdown* must not condemn an evaluator that exited on its own. It was
        written with no test on ``rc`` at all, so it also swallowed the case it must never
        swallow: the evaluator dying of SIGSEGV / SIGABRT / the OOM killer with a final record
        already on disk. Then the ambiguous record is credited to the model as a clean verdict
        and charged to the *record* budget -- which is finding 2, re-opened through a third
        door.

        139 = 128 + SIGSEGV, which is what ``bash`` reports for a segfaulting child. **Round
        four made that value load-bearing**, which it was not when this test was written: the
        discriminator is no longer ``rc == 0`` but ``signalled is None``, and 139 is in the
        shell's signal-relay range. The test still asserts the right thing for the right reason
        -- a process that died by signal is not a clean exit -- but it now passes because of the
        exit status rather than in spite of it.
        """
        attempt = self._poll(139, "Segmentation fault (core dumped)\n", AGENT_CRASHED)
        self.assertIs(attempt.outcome, AttemptOutcome.FAULT,
                      "the evaluator itself died hard (exit 139) and the demotion still called "
                      "it a clean exit")
        self.assertEqual(attempt.exit_code, 139)
        self.assertIn("139", attempt.detail or "",
                      "the detail must name the exit status that refused the demotion")

    def test_the_demotion_still_applies_to_a_clean_exit(self):
        """The other direction, which the fix must not break: exit 0 plus a final record means
        the pattern came from the simulator's half of the shared stream."""
        attempt = self._poll(0, "Segmentation fault (core dumped)\n", AGENT_CRASHED)
        self.assertIs(attempt.outcome, AttemptOutcome.EXITED)
        self.assertIn("stderr", attempt.detail or "")

    def test_a_nonzero_exit_without_a_fault_pattern_is_still_a_clean_exit(self):
        """The vendored evaluator exits non-zero for its own crash paths (``sys.exit(-1)`` when
        ``_load_and_run_scenario`` reports crashed), so a non-zero exit alone must NOT mean
        ABNORMAL_END -- only a non-zero exit *with* a fault pattern refuses the demotion."""
        attempt = self._poll(255, "route finished\n", AGENT_CRASHED)
        self.assertIs(attempt.outcome, AttemptOutcome.EXITED)

    # -- THE TRAP, under the stricter gate ------------------------------------------------
    def test_a_degenerate_tickruntime_row_survives_the_stricter_gate(self):
        """THE TRAP. A model that scores ``Failed - TickRuntime`` everywhere is a real result and
        must exit 0. Under the stricter gate such an attempt can now reach ABNORMAL_END (a
        shutdown segfault plus a non-zero exit), so the trap has to be re-checked *there*, not
        only on the clean-exit path: ``RETRY_TICKRUNTIME`` is charged to the tickruntime axis in
        both classes, whose default budget of 0 settles it immediately.
        """
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 3, "record_budget": 3, "tickruntime_budget": 0,
                   "killed_budget": 2, "worker_quarantine_after": 99}))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(TICKRUNTIME), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        requeue = self._settle(runner, state, task, AttemptOutcome.FAULT,
                               'fault pattern "Segmentation fault" in stderr (exit 139)')

        st = state.get(task.key)
        self.assertFalse(requeue)
        self.assertTrue(st.finished)
        self.assertEqual(st.attempts_tickruntime, 1)
        self.assertEqual(st.attempts_infra, 0)
        self.assertEqual(st.attempts_killed, 0)
        self.assertEqual(self._report(runner, [task], state, cfg).exit_code(), EXIT_OK)

    def test_a_model_that_fails_every_route_bare_still_settles_at_exit_zero(self):
        """The other half of the trap: a bare ``Failed`` is a RETRY_RECORD status, so under an
        abnormal end it lands on the bounded ambiguity axis. Bounded is the whole point -- it
        must still reach exit 0, just after spending that axis.
        """
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 3, "record_budget": 3, "tickruntime_budget": 0,
                   "killed_budget": 2, "worker_quarantine_after": 99}))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(FAILED_BARE), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        attempts = 0
        while self._settle(runner, state, task, AttemptOutcome.FAULT, "segfault, exit 139"):
            attempts += 1
            self.assertLess(attempts, 20, "unbounded retry on an abnormal end")

        st = state.get(task.key)
        self.assertTrue(st.finished)
        self.assertEqual(st.attempts_record, 0)
        self.assertEqual(self._report(runner, [task], state, cfg).exit_code(), EXIT_OK)


# =======================================================================================
# MAJOR 2 -- the document and the code must agree cell for cell
# =======================================================================================
class TestDocAndCodeAgree(ModelBase):
    """The 6A.5 table is normative, so it is machine-checked rather than merely written down.

    RED BEFORE THE FIX:

        AssertionError: DESIGN.md carries no machine-checkable 6A.5 table (looked for the
        sentinel '# 6A.5 NORMATIVE TABLE'). The normative model exists only as prose, which is
        exactly how the pinned rows and the code came to disagree.

    and, with the *pinned* rows pasted into the block to prove the check has teeth:

        AssertionError: ABNORMAL_END x RETRY_RECORD: DESIGN.md says budget 'infra', the code
        charges 'killed'

    The direction of the fix is recorded in DESIGN.md 6A.5 and 6A.6.6: the **code** stands and
    the table was moved to it, because routing that cell to ``infra`` breaks the table's own
    invariant (``infra`` never settles, and the gate is reloaded on every resume) and would make
    six *published* v0.9 rows permanently unreproducible at exit 1.
    """

    SENTINEL = "# 6A.5 NORMATIVE TABLE"

    def _rows(self):
        text = DESIGN_MD.read_text(encoding="utf-8")
        self.assertIn(self.SENTINEL, text,
                      f"DESIGN.md carries no machine-checkable 6A.5 table (looked for the "
                      f"sentinel {self.SENTINEL!r}). The normative model exists only as prose, "
                      f"which is exactly how the pinned rows and the code came to disagree.")
        # Everything after the sentinel line, up to the end of its fenced block.
        block = text.split(self.SENTINEL, 1)[1].split("\n", 1)[1].split("```", 1)[0]
        rows = []
        for line in block.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            self.assertEqual(len(parts), 4, f"malformed table row: {line!r}")
            rows.append(tuple(parts))
        return rows

    def _cfg(self):
        return config_mod.load(self.site.config(
            retry={"infra_budget": 2, "record_budget": 2, "tickruntime_budget": 2,
                   "killed_budget": 2, "worker_quarantine_after": 99}))

    def test_every_cell_of_the_normative_table_is_covered_exactly_once(self):
        rows = self._rows()
        cells = [(c, d) for c, d, _b, _f in rows]
        self.assertEqual(len(cells), len(set(cells)), "a cell is listed twice")
        expected = {(c, d) for c in CLASS_OUTCOMES for d in ON_DISK}
        self.assertEqual(set(cells), expected,
                         "the table must state all four classes x all six on-disk cases; a "
                         "missing cell is how an unstated case gets an accidental answer")

    def test_the_code_charges_the_budget_the_table_says_for_every_cell(self):
        cfg = self._cfg()
        for klass, on_disk, budget, finished in self._rows():
            for outcome in CLASS_OUTCOMES[klass]:
                with self.subTest(klass=klass, on_disk=on_disk, outcome=outcome):
                    self._check_cell(cfg, klass, on_disk, budget, finished, outcome)

    def _check_cell(self, cfg, klass, on_disk, budget, finished, outcome):
        task = self._task(cfg)
        if task.result_path.exists():
            task.result_path.unlink()
        task.result_path.write_text(json.dumps(ON_DISK[on_disk]), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)
        requeue = self._settle(runner, state, task, outcome, "table check",
                               interrupted=(klass == "TORN_DOWN"))
        st = state.get(task.key)

        charged = {name: getattr(st, field) for name, field in BUDGET_FIELD.items()}
        want = {name: (1 if name == budget else 0) for name in BUDGET_FIELD}
        self.assertEqual(charged, want,
                         f"{klass} x {on_disk}: DESIGN.md says budget {budget!r}, the code "
                         f"charged {sorted(k for k, v in charged.items() if v)!r}")

        if finished == "yes":
            self.assertTrue(st.finished, f"{klass} x {on_disk}: the table says this settles")
            self.assertFalse(requeue)
        elif finished == "no":
            self.assertFalse(st.finished, f"{klass} x {on_disk}: the table says this never "
                                          f"settles on its own")
        else:
            self.assertEqual(finished, "when_spent")
            self.assertFalse(st.finished,
                             f"{klass} x {on_disk}: settled on the FIRST attempt, but the table "
                             f"says only once the {budget} budget is spent (2 here)")
            self.assertTrue(requeue, f"{klass} x {on_disk}: must be retried while budget remains")
            guard = 0
            while self._settle(runner, state, task, outcome, "table check"):
                guard += 1
                self.assertLess(guard, 20, f"{klass} x {on_disk}: unbounded retry")
            self.assertTrue(st.finished,
                            f"{klass} x {on_disk}: the {budget} budget is spent and the route "
                            f"still has no settled answer -- a dead end")


# =======================================================================================
# MAJOR 3 -- no cell holding a final record may be a permanent dead end
# =======================================================================================
class TestNoPermanentDeadEnd(ModelBase):

    def _cfg(self, infra=2, record=3, killed=2):
        return config_mod.load(self.site.config(
            retry={"infra_budget": infra, "record_budget": record, "tickruntime_budget": 0,
                   "killed_budget": killed, "worker_quarantine_after": 99}))

    def _decide(self, task, state, cfg):
        return plan_mod.decide(
            task, state.get(task.key).budgets(), cfg.resume["mode"],
            record_budget=int(cfg.retry["record_budget"]),
            tickruntime_budget=int(cfg.retry["tickruntime_budget"]),
            infra_budget=int(cfg.retry["infra_budget"]),
            killed_budget=int(cfg.retry["killed_budget"]))

    def test_a_genuine_record_gated_by_a_spent_infra_budget_can_still_be_settled(self):
        """RED BEFORE THE FIX (first failing assertion; ``clear_infra_exhaustion`` did not
        exist either, so the test could not get past step 5 in any case):

            AssertionError: '--retry-infra-exhausted' not found in "final record with status
            'Failed - Agent crashed' and record budget 1/3 left to spend, but the
            infrastructure retry budget is gone (2/2): the record on disk is from an EARLIER
            attempt and this run cannot produce the retry it owes"

        The route holds a real ``Failed - Agent crashed`` this ledger's own attempt produced,
        with two of its three record retries still owed. Three failed launches then spend the
        infra budget. Finding 6 correctly makes that run exit 1 -- but because budgets are
        persisted, every future resume hit the same gate, so the only escapes were deleting the
        ledger (losing every budget) or ``--resume-mode none --force`` (overwriting the tree).
        That is the dead end DESIGN.md:616 forbids.

        The fix does not settle the route (that would re-open finding 6) and does not re-plan it
        (that would re-open finding 5). It makes the *recovery* first-class and lossless, and
        makes the report name it.
        """
        cfg = self._cfg()
        task = self._task(cfg)
        runner, state = self._runner_and_state(cfg)

        # 1. a real attempt produces a real, retryable model verdict.
        task.result_path.write_text(json.dumps(AGENT_CRASHED), encoding="utf-8")
        self.assertTrue(self._settle(runner, state, task, AttemptOutcome.EXITED, "exit 255"))
        self.assertEqual(state.get(task.key).attempts_record, 1)

        # 2. the machine then refuses to start anything.
        for _ in range(int(cfg.retry["infra_budget"])):
            self._settle(runner, state, task, AttemptOutcome.LAUNCH_FAILED, "ports busy")

        # 3. this run is honest about it: exit 1, record preserved, never re-adopted.
        rep = self._report(runner, [task], state, cfg)
        self.assertNotEqual(rep.exit_code(), EXIT_OK)
        self.assertEqual(rep.to_dict()["incomplete_routes"][0]["unsettled_reason"],
                         "unrefreshed_record")

        # 4. and so is the next run -- the gate is persisted (finding 5).
        d = self._decide(task, state, cfg)
        self.assertIs(d.decision, plan_mod.Decision.SKIP_EXHAUSTED)
        self.assertFalse(d.settled)
        self.assertIn("--retry-infra-exhausted", d.reason,
                      "an operator staring at a gated route must be told the one lossless way "
                      "out; 'edit _runner/state.json by hand' is not a recovery path")

        # 5. the recovery is lossless: only the infra gate moves.
        before = copy.deepcopy(state.get(task.key).budgets())
        cleared = state.clear_infra_exhaustion(int(cfg.retry["infra_budget"]))
        st = state.get(task.key)
        self.assertEqual(cleared, [task.key])
        self.assertEqual(st.attempts_infra, 0)
        self.assertEqual(st.attempts_record, before["record"],
                         "the recovery spent the model's retries")
        self.assertEqual(st.attempts_killed, before["killed"])
        self.assertEqual(st.attempts_infra_total, int(cfg.retry["infra_budget"]),
                         "the audit trail of what the machine did must survive the recovery")
        self.assertEqual(json.loads(task.result_path.read_text()), AGENT_CRASHED,
                         "the record must survive the recovery byte-identical")

        # 6. and settlement is then reached in a bounded number of attempts.
        self.assertIs(self._decide(task, state, cfg).decision, plan_mod.Decision.RUN)
        attempts = 0
        while self._settle(runner, state, task, AttemptOutcome.EXITED, "exit 255"):
            attempts += 1
            self.assertLess(attempts, 20, "unbounded")
        self.assertTrue(state.get(task.key).finished)
        self.assertEqual(state.get(task.key).attempts_record,
                         int(cfg.retry["record_budget"]))
        self.assertEqual(self._report(runner, [task], state, cfg).exit_code(), EXIT_OK)

    def test_infra_debt_is_cleared_by_an_attempt_that_produced_its_own_record(self):
        """RED BEFORE THE FIX:

            AssertionError: 2 != 0 : scattered infrastructure hiccups accumulated across a long
            run and gated a route that has been running fine

        The within-run half of the same dead end, which no recovery flag can reach because it
        happens while the run is in flight. ``infra_budget`` bounds *consecutive* infrastructure
        failures -- the signature of a machine that is broken now -- not a lifetime tally of
        unrelated hiccups. This is the same clearing rule the worker-quarantine counter already
        has, and for the same reason; the two are now cleared by exactly the same event.
        """
        cfg = self._cfg(infra=3)
        task = self._task(cfg)
        runner, state = self._runner_and_state(cfg)

        for _ in range(2):
            self._settle(runner, state, task, AttemptOutcome.EXITED, "no record")
        self.assertEqual(state.get(task.key).attempts_infra, 2)

        task.result_path.write_text(json.dumps(AGENT_CRASHED), encoding="utf-8")
        self._settle(runner, state, task, AttemptOutcome.EXITED, "exit 255")

        st = state.get(task.key)
        self.assertEqual(st.attempts_infra, 0,
                         "scattered infrastructure hiccups accumulated across a long run and "
                         "gated a route that has been running fine")
        self.assertEqual(st.attempts_infra_total, 2,
                         "the cumulative count is the audit trail and must NOT be cleared")
        self.assertEqual(st.attempts_record, 1)

    def test_an_ambiguous_kill_does_not_clear_the_infra_debt(self):
        """The converse, and the reason the rule is stated over *the ending could not have
        manufactured this record* rather than over *a record exists*: an abnormal end holding a
        crash-shaped record is exactly the evidence we have just declared unreliable, so it
        clears nothing -- neither the quarantine counter nor the route's infra debt.
        """
        cfg = self._cfg(infra=3)
        task = self._task(cfg)
        runner, state = self._runner_and_state(cfg)
        self._settle(runner, state, task, AttemptOutcome.EXITED, "no record")

        task.result_path.write_text(json.dumps(SIM_CRASHED), encoding="utf-8")
        self._settle(runner, state, task, AttemptOutcome.TIMEOUT, "wall clock")

        self.assertEqual(state.get(task.key).attempts_infra, 1)


class TestTheTrapEndToEndUnderTheStricterGate(IntegrationBase):
    """THE TRAP, driven through ``main()`` with a simulator that crashes into the shared stderr.

    The unit tests above check the two halves separately. This checks the thing that actually
    matters: a run in which every route's stderr contains ``Segmentation fault`` -- written by
    the *simulator*, during teardown, after the model produced a real verdict -- still exits 0
    and still reports those verdicts as the benchmark result.

    ``..._degenerate_tickruntime_sweep...`` is a **guard**: green before and after, and not
    offered as a defect regression. ``..._selfterminated_crash_record...`` was a regression test
    for round two and was **inverted in round four** after a cross-review found the rule it
    encoded to be wrong; its docstring carries the full argument, because a test that changes
    what it asserts is worthless unless the reason is written down next to it.
    """

    def test_a_degenerate_tickruntime_sweep_with_shutdown_segfaults_still_exits_zero(self):
        for i in range(3):
            self.site.add_route(f"static/s1/base/route_{i}_a.xml")
        code = self.run_cli(self.site.config(), mode="shutdown_crash_tickruntime")
        rep = self.report()
        self.assertEqual(code, EXIT_OK,
                         "a model that scores TickRuntime everywhere, on a host whose simulator "
                         "crashes on the way down, is a real benchmark result")
        self.assertEqual(rep["totals"]["by_status"], {"Failed - TickRuntime": 3})
        self.assertEqual(rep["totals"]["incomplete"], 0)
        self.assertEqual(len(self.site.trace_rows()), 3,
                         "tickruntime_budget is 0: one attempt per route, no retries")

    def test_a_selfterminated_crash_record_charges_the_MODEL_budget_not_the_kill_axis(self):
        """**INVERTED IN ROUND FOUR, deliberately.** Read this before trusting either version.

        As written for round two this test asserted the opposite -- that ``shutdown_crash_agent``
        (a `Failed - Agent crashed` record, a segfault line in the shared stderr, then
        ``sys.exit(255)``) settles on the bounded ``killed`` axis. It was a genuine regression
        test: reverting round two's ``clean_exit = rc == 0`` alone made it red.

        A cross-review (2026-08-07, `cursor-grok-4.5-high`, finding 1) showed the underlying rule
        was wrong, and the user settled it in favour of the reviewer. The demotion asks one
        question -- *did OUR process die hard?* -- and ``rc == 0`` was only ever a proxy for it.
        The vendored evaluator ends its own crash paths with ``sys.exit(-1)`` → status **255**,
        which is a self-terminated verdict; ``describe_exit_signal`` declines to call it a
        signal, and it is now the discriminator. So this fixture is a process that *terminated
        itself* having written a verdict, next to a stream the CARLA server also writes into:
        precisely the case the demotion exists to forgive.

        The consequence that made it worth changing: `Failed - Simulation crashed` and
        `Failed - Agent couldn't be set up` are the status family of **four of the six published
        v0.9 rows**, and under the old rule a UE4 abort during teardown sent them to the
        ambiguity budget instead of the model's own.

        What did NOT change, and is asserted below: a real signal death still charges ``killed``
        (see ``tests/test_hard_death_and_infra_zero.py``), and the route still settles at exit 0
        either way -- both axes are bounded. Only the axis moved.
        """
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self.site.config(retry={"record_budget": 3, "infra_budget": 2,
                                      "tickruntime_budget": 0, "killed_budget": 2,
                                      "worker_quarantine_after": 99})
        code = self.run_cli(cfg, mode="shutdown_crash_agent")
        rep = self.report()
        self.assertEqual(code, EXIT_OK, "the route must still reach a settled answer")
        self.assertEqual(rep["totals"]["by_status"], {"Failed - Agent crashed": 1})
        route = rep["routes"][0]
        self.assertEqual(route["attempts"]["record"], 3,
                         "a self-terminated verdict is the model's own and belongs to the "
                         "model's budget; the shared stderr is not evidence against it")
        self.assertEqual(route["attempts"]["killed"], 0,
                         "the ambiguity axis is for attempts that died by SIGNAL, and this "
                         "process exited by itself")
        self.assertEqual(route["attempts"]["infra"], 0,
                         "and must never reach the budget that does not settle")


class TestPatternIndependence(ModelBase):
    """Round four's checkable invariant, kept apart from the end-to-end trap tests
    because it is a unit-level property of `_settle` rather than a CLI behaviour."""

    def test_the_stderr_pattern_cannot_change_the_accounting_of_a_finished_attempt(self):
        """The invariant that makes round four's rule checkable rather than merely argued.

        Once ``signalled`` decides the class, a fault pattern in the (shared) stream must not be
        able to move a single counter after the process has exited. With a record, ``signalled``
        alone chooses the branch; without one, CLEAN_EXIT and ABNORMAL_END both send
        ``NO_FINAL_RECORD`` to ``infra`` through the same ``_charge_infra``. So the pattern is
        accounting-*irrelevant* post-exit, and its only real job is the in-flight branch, where
        there is no exit status to read yet.

        This is the test that would have caught round three's mistake. Widening the patterns to
        match what a shell actually writes was correct, and silently moved a population of
        self-terminated verdicts onto a different budget, because nothing asserted independence.
        """
        for status_bytes in (AGENT_CRASHED, TICKRUNTIME, FAILED_BARE):
            for stderr_text in ("", "route finished\n",
                                "job.sh: line 2: 999 Segmentation fault      (core dumped) x\n"):
                for rc in (0, 1, 255):
                    with self.subTest(status=status_bytes["_checkpoint"]["records"][0]["status"],
                                      rc=rc, stderr=stderr_text[:20]):
                        cfg = config_mod.load(self.site.config(
                            retry={"record_budget": 3, "infra_budget": 3, "tickruntime_budget": 0,
                                   "killed_budget": 2, "worker_quarantine_after": 99}))
                        task = self._task(cfg)
                        task.stderr_path.parent.mkdir(parents=True, exist_ok=True)
                        task.stderr_path.write_text(stderr_text, encoding="utf-8")
                        task.result_path.write_text(json.dumps(status_bytes), encoding="utf-8")
                        proc = subprocess.Popen(["bash", "-c", f"exit {rc}"])
                        proc.wait()
                        attempt = Attempt(task=task, worker=0, stdout_path=task.stdout_path,
                                          stderr_path=task.stderr_path, handle=proc)
                        LocalBackend(cfg, self.log).poll(attempt)
                        runner, state = self._runner_and_state(cfg)
                        self._settle(runner, state, task, attempt.outcome, attempt.detail or "")
                        self.assertEqual(
                            state.get(task.key).attempts_killed, 0,
                            "a non-signalled exit charged the ambiguity axis because of text in "
                            "a stream the CARLA server shares")


class TestDeadEndRecoveryIsWiredToTheCLI(IntegrationBase):

    def test_a_route_gated_by_a_spent_infra_budget_is_recoverable_from_the_command_line(self):
        """RED BEFORE THE FIX:

            SystemExit: 2   (run_benchmark.py: error: unrecognized arguments:
                             --retry-infra-exhausted)

        End to end, through ``main()``: a run against a broken machine gates the route; a plain
        resume (machine now fixed) still skips it, because the gate is persisted; the documented
        recovery makes the same resume run it and exit 0.
        """
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self.site.config(retry={"infra_budget": 1, "record_budget": 2,
                                      "tickruntime_budget": 0, "killed_budget": 2,
                                      "worker_quarantine_after": 99})

        self.assertNotEqual(self.run_cli(cfg, mode="no_record"), EXIT_OK)
        self.assertEqual(self.report()["totals"]["incomplete"], 1)

        # The machine is fixed, but the gate is persisted: nothing is even attempted.
        before = len(self.site.trace_rows())
        self.assertNotEqual(self.run_cli(cfg, mode="ok"), EXIT_OK)
        self.assertEqual(len(self.site.trace_rows()), before,
                         "the gated route was attempted without the recovery being asked for")

        code = self.run_cli(cfg, mode="ok", extra=["--retry-infra-exhausted"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(self.site.trace_rows()), before + 1)
        self.assertEqual(self.report()["totals"]["by_status"], {"Completed": 1})
        self.assertTrue(any("--retry-infra-exhausted" in w for w in self.report()["warnings"]),
                        "using the recovery must be recorded in the report")


# =======================================================================================
# MINOR 4 -- an unsafe default that a caller can reach by forgetting a keyword
# =======================================================================================
class TestKilledBudgetHasNoUnsafeDefault(ModelBase):

    def test_decide_refuses_to_be_called_without_the_killed_budget(self):
        """RED BEFORE THE FIX:

            AssertionError: TypeError not raised

        ``killed_budget: int = 0`` made ``killed_spent >= killed_budget`` true for any charged
        ambiguity budget, so a caller that forgot the keyword silently got SKIP_EXHAUSTED
        *settled* on a record the model has just admitted might have been manufactured by the
        kill. There is no safe default here -- 0 accepts an ambiguous record having never
        re-run it, and omitting the gate resets the bound on every resume -- so the parameter is
        required and the unsafe direction is unreachable by accident.
        """
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(AGENT_CRASHED), encoding="utf-8")

        with self.assertRaises(TypeError):
            plan_mod.decide(task, {"record": 0, "killed": 1}, "skip_terminal",
                            record_budget=3, tickruntime_budget=0, infra_budget=3)

    def test_the_killed_gate_still_fires_only_once_the_axis_has_been_charged(self):
        """The behaviour the required parameter protects, stated positively."""
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(AGENT_CRASHED), encoding="utf-8")

        never_killed = plan_mod.decide(task, {"killed": 0}, "skip_terminal", record_budget=3,
                                       tickruntime_budget=0, infra_budget=3, killed_budget=0)
        self.assertIs(never_killed.decision, plan_mod.Decision.RUN,
                      "a killed_budget of 0 on a route that was never killed must not accept a "
                      "record as the answer without ever re-running it")

        spent = plan_mod.decide(task, {"killed": 2}, "skip_terminal", record_budget=3,
                                tickruntime_budget=0, infra_budget=3, killed_budget=2)
        self.assertIs(spent.decision, plan_mod.Decision.SKIP_EXHAUSTED)
        self.assertTrue(spent.settled)


# =======================================================================================
# MINOR 5 -- a new schema key must not invalidate every pre-existing output root
# =======================================================================================
class TestDigestCompatibility(ModelBase):

    @staticmethod
    def _payload_before_compat_keys(cfg):
        """Resolved payload as it stood before any compatibility-pinned key existed."""
        payload = copy.deepcopy(cfg.as_dict())
        for section, key in config_mod.DIGEST_COMPAT_DEFAULTS:
            payload[section].pop(key, None)
        return payload

    def test_every_compat_pin_equals_the_schema_default_it_stands_for(self):
        """The carve-out is safe only while the pinned value IS the schema default.

        ``DIGEST_COMPAT_DEFAULTS`` omits a key from the digest while it holds a hard-coded
        value, and that value is written out rather than read from ``SCHEMA`` on purpose: a
        future change of *default* must move the digest of every config that omits the key,
        because their behaviour changes too. The hazard is the other direction. If someone bumps
        the schema default and leaves the pin behind, the pin stops meaning "nobody chose this"
        and starts silently hiding a real operator choice -- a config that explicitly sets the
        OLD value would then digest identically to one that sets nothing. Nothing couples the
        two but this assertion, so it is stated here rather than left to be noticed.
        """
        for (section, key), pinned in config_mod.DIGEST_COMPAT_DEFAULTS.items():
            with self.subTest(section=section, key=key):
                self.assertEqual(
                    config_mod.SCHEMA[section][key][1], pinned,
                    f"DIGEST_COMPAT_DEFAULTS pins retry.{key}={pinned!r} but SCHEMA now "
                    f"defaults it to {config_mod.SCHEMA[section][key][1]!r}. The pin would now "
                    f"hide an explicit, non-default operator setting from the config digest. "
                    f"Either drop the entry (accepting that pre-existing output roots re-plan) "
                    f"or move the pin -- but do not leave them disagreeing.")

    @staticmethod
    def _digest_of(payload):
        """The digest recipe as it stood *before* ``retry.killed_budget`` existed.

        Deliberately spelled out rather than called from the module under test: a compatibility
        claim checked against the same function that makes the claim is not a check.
        """
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def test_adding_killed_budget_at_its_default_does_not_move_an_existing_digest(self):
        """RED BEFORE THE FIX:

            AssertionError: '3f0c...' != 'a41e...' : every output root created before
            retry.killed_budget existed now reports as a DIFFERENT configuration

        ``config.build`` materialises the new key into every resolved config and ``digest()``
        hashes the whole retry dict, so a key nobody set changed the identity of every config
        that predates it. The consequence is a false "this output root was produced by a
        DIFFERENT configuration ... use a fresh output root" on every resume -- advice that
        throws away 475 routes of finished work for a key holding its default.
        """
        path = self.site.config()          # this config file does not mention killed_budget
        cfg = config_mod.load(path)
        self.assertEqual(cfg.retry["killed_budget"], 2, "the default under test")

        legacy_payload = self._payload_before_compat_keys(cfg)

        self.assertEqual(cfg.digest(), self._digest_of(legacy_payload),
                         "every output root created before retry.killed_budget existed now "
                         "reports as a DIFFERENT configuration")

    def test_a_ledger_written_before_the_key_existed_resumes_without_a_false_alarm(self):
        path = self.site.config()
        cfg = config_mod.load(path)
        legacy_payload = self._payload_before_compat_keys(cfg)
        legacy_digest = self._digest_of(legacy_payload)

        ledger = Path(cfg.output["root"]) / "_runner" / "state.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({"version": 1, "created_at": 0.0,
                                      "config_digest": legacy_digest, "tasks": {}}),
                          encoding="utf-8")

        state = RunState.load_or_create(ledger, cfg.digest())
        self.assertFalse(state.config_changed(),
                         "resuming a pre-existing output root told the operator the "
                         "configuration had changed, when only the schema had")

    def test_setting_the_new_key_to_a_NON_default_still_moves_the_digest(self):
        """The compatibility carve-out is per value, not per key: an operator who actually
        changes the ambiguity budget has changed the run, and the digest must say so."""
        path = self.site.config()
        base = config_mod.load(path).digest()
        changed = config_mod.load(path, overrides={"retry.killed_budget": 3}).digest()
        self.assertNotEqual(base, changed)

    def test_the_digest_is_still_sensitive_to_everything_else(self):
        path = self.site.config()
        base = config_mod.load(path).digest()
        self.assertNotEqual(base, config_mod.load(
            path, overrides={"retry.record_budget": 9}).digest())
        self.assertNotEqual(base, config_mod.load(
            path, overrides={"benchmark.seed": 43}).digest())


# =======================================================================================
# MINOR 6 -- the three-way unsettled_reason must actually be three-way
# =======================================================================================
class TestUnsettledReasonIsThreeWay(ModelBase):

    def _route_outcome(self, state, task, cfg):
        runner, _ = self._runner_and_state(cfg)
        rep = report_mod.build([task], state, started_at=0.0, config_digest=cfg.digest(),
                               config_path=cfg.source_path, seed=cfg.seed, backend="local",
                               skipped_done=0, skipped_exhausted=0, quarantined=[],
                               warnings=[], interrupted=False, fatal_agent=False)
        return rep.outcomes[0]

    def test_a_route_the_planner_never_reached_is_labelled_not_reached(self):
        """RED BEFORE THE FIX:

            AssertionError: 'unrefreshed_record' != 'not_reached' : a route the planning loop
            never reached was reported as a record that was preserved but not refreshed

        ``rec.final`` was tested before ``st is None``, so ``not_reached`` was unreachable for
        any route with a record on disk -- and a route the loop never reached is *precisely* a
        route whose record is left over from an earlier run. The field exists to tell an
        operator which of three different problems they have; two of them were collapsed.

        The reachable case is a fatal-agent abort: the planning loop breaks, so every route
        after it has no ledger entry at all.
        """
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")

        o = self._route_outcome(state, task, cfg)
        self.assertFalse(o.complete)
        self.assertEqual(o.unsettled_reason, "not_reached",
                         "a route the planning loop never reached was reported as a record "
                         "that was preserved but not refreshed")

    def test_the_other_two_reasons_are_unchanged(self):
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")
        state.get(task.key).attempts_infra = 2
        self.assertEqual(self._route_outcome(state, task, cfg).unsettled_reason, "no_record")

        task.result_path.write_text(json.dumps(AGENT_CRASHED), encoding="utf-8")
        self.assertEqual(self._route_outcome(state, task, cfg).unsettled_reason,
                         "unrefreshed_record")

    def test_a_settled_route_has_no_unsettled_reason(self):
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")
        state.get(task.key).finished = True
        o = self._route_outcome(state, task, cfg)
        self.assertTrue(o.complete)
        self.assertIsNone(o.unsettled_reason)


if __name__ == "__main__":
    unittest.main()
