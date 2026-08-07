"""Regression tests for the two defects found by the *third* review round.

Round two closed six defects and was itself verified by three independent agents, two of which
re-derived the same two holes from different directions and then failed to refute them. Both are
the same shape as everything before them: a rule that was stated correctly and implemented over
the wrong quantity.

**MAJOR A -- the hard-death gate hung on a stderr substring, and two of the substrings were
wrong.** Round two's fix gated the FAULT *demotion* on ``rc == 0``, which is right; but the
demotion is only ever reached inside ``if fault:``, and ``fault`` comes from matching literal
text against a stream. ``"Aborted (core dumped)"`` was written with a single space and had never
matched anything, because a shell pads the signal name into a fixed column; SIGKILL's message is
the bare word ``Killed``, which is in no pattern at all. So an evaluator that died of SIGABRT or
was taken by the OOM killer reached ``if not fault:`` and was recorded as "the process decided to
stop" -- its crash-shaped record charged to the MODEL's record budget and settled as the model's
verdict, at exit 0, with no warning. Under ``configs/reference_agent.yaml``, which ships
``record_budget: 1``, that settles on the FIRST attempt with no retry at all. The fix reads death
by signal from the exit status, where it is unambiguous and needs no text.

**MAJOR B -- ``retry.infra_budget: 0`` was a total dead end.** ``decide()`` gated on
``infra_spent >= infra_budget`` and, unlike every other axis, that gate is evaluated during
*planning*, before the first attempt. On a virgin ledger ``0 >= 0`` is true, so every route was
SKIP_EXHAUSTED before anything ran: exit 1, zero executions, on a completely healthy machine.
Nothing could release it -- ``--retry-infra-exhausted`` clears a counter that was never charged,
and deleting the ledger does not help because the gate never consulted it. The value is legal
(validation rejects only ``< 0``), ``tickruntime_budget`` ships at ``0`` so it reads as the
idiomatic "no retries", and ``configs/reference_agent.yaml`` already carries a non-default
``infra_budget: 1``, so the field is one operators edit. This is DESIGN.md 6A.5's dead-end
invariant broken by an off-by-one rather than by a rule.

**Provenance.** Against the untouched pre-fix tree: ``12 failed, 10 passed``. The failures were
then attributed to individual hunks by reverting exactly one at a time in a scratch copy:

======  ==========================================================  ==========================
hunk    what it is                                                  red on its own
======  ==========================================================  ==========================
A1      ``signalled = reap.describe_exit_signal(rc)`` in ``poll``    4 tests
A2      ``FAULT_PATTERNS`` as ``\\s+`` regexes (the dead pattern)     1 test + 1 subtest
B       ``infra_spent and infra_spent >= infra_budget``              4 tests
C       the ``--dry-run`` rollback of the infra clear                1 test
        (superseded in round four by "a dry run never saves"; see
         ``TestDryRunWritesNothingAtAll``, hunk E, 4 tests)
======  ==========================================================  ==========================

``test_a_sigabrt_crash_record_is_charged_to_the_bounded_axis...`` is deliberately not in that
table: it goes red only when A1 **and** A2 are both reverted, because either one alone catches
SIGABRT. That is worth stating rather than hiding, and it is also the argument for keeping both:
A2 alone cannot see SIGKILL (no pattern will ever match ``Killed``) and A1 alone cannot see a
simulator crashing under a still-running evaluator, where there is no exit status yet.

Tests labelled ``GUARD`` were green before and after and are **not** offered as evidence that a
defect was fixed. They exist because both fixes are exactly the kind that break something in the
other direction -- widen "died hard" too far and a degenerate model's real result becomes an
infrastructure failure; loosen the infra gate too far and the bound it enforces disappears.
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import run_benchmark
from oodbench import (EXIT_OK, EXIT_PARTIAL, config as config_mod, plan as plan_mod, reap,
                      state as state_mod)
from oodbench.backends.base import Attempt, AttemptOutcome
from oodbench.backends.local import LocalBackend
from oodbench.state import RunState

from tests.test_integration_local import IntegrationBase, Site
from tests.test_settlement_model import AGENT_CRASHED, ModelBase, SIM_CRASHED, TICKRUNTIME

#: What bash actually writes when a child dies by signal: the name is padded into a fixed
#: column. Reproduced verbatim from a real run rather than hand-written, because the single
#: space in the old pattern is the entire defect.
BASH_ABORT = ("job.sh: line 2: 2515142 Aborted                 (core dumped) "
              "python3 /x/leaderboard_evaluator.py\n")
BASH_KILLED = ("job.sh: line 2: 2515170 Killed                  "
               "python3 /x/leaderboard_evaluator.py\n")
BASH_SEGV = ("job.sh: line 2: 2515148 Segmentation fault      (core dumped) "
             "python3 /x/leaderboard_evaluator.py\n")


# =======================================================================================
# MAJOR A -- death by signal is read from the exit status, not from the stream
# =======================================================================================
class TestSignalDeathIsReadFromTheExitStatus(unittest.TestCase):
    """The pure function, first: it is what makes the classification text-independent."""

    def _tmp(self) -> Path:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Path(d.name)

    def test_a_signalled_job_script_is_recognised_from_a_negative_rc(self):
        """RED BEFORE THE FIX:

            AttributeError: module 'oodbench.reap' has no attribute 'describe_exit_signal'

        ``Popen.poll()`` returns ``-N`` when the process we launched *directly* -- the job
        script's shell -- is itself killed by signal N. A cgroup OOM reaper or a SLURM
        preemption takes the whole group, shell included. Before the fix this surfaced as
        ``AttemptOutcome.EXITED`` with ``detail='exit -9'``: a negative "exit code" read as a
        self-terminated verdict.
        """
        self.assertIn("SIGKILL", reap.describe_exit_signal(-9) or "")
        self.assertIn("SIGABRT", reap.describe_exit_signal(-6) or "")

    def test_the_shell_relay_convention_is_recognised(self):
        """``jobscript.render`` ends with ``exit ${rc}``, so the evaluator's ``128+N`` arrives
        here verbatim."""
        self.assertIn("SIGABRT", reap.describe_exit_signal(134) or "")   # 128 + 6
        self.assertIn("SIGKILL", reap.describe_exit_signal(137) or "")   # 128 + 9
        self.assertIn("SIGSEGV", reap.describe_exit_signal(139) or "")   # 128 + 11

    def test_the_evaluators_own_crash_exit_is_NOT_a_signal_death(self):
        """GUARD, and the load-bearing one for this whole fix.

        The vendored evaluator ends its own crash path with ``sys.exit(-1)``, which is exit
        status **255** -- numerically ``128 + 127``, and there is no signal 127. A blanket
        ``rc >= 128`` rule would reclassify that self-terminated verdict as a hard death and
        spend the ambiguity budget on a record the model really did write, which is the same
        error as the defect, pointing the other way. The upper bound is what stops it.
        """
        self.assertIsNone(reap.describe_exit_signal(255))
        self.assertIsNone(reap.describe_exit_signal(0))
        self.assertIsNone(reap.describe_exit_signal(1))
        self.assertIsNone(reap.describe_exit_signal(3))

    def test_the_abort_pattern_matches_what_a_shell_actually_writes(self):
        """RED BEFORE THE FIX:

            AssertionError: None is not truthy : 'Aborted (core dumped)' has never matched
            anything -- the shell pads the signal name into a column

        ``FAULT_PATTERNS`` is still consulted while an attempt is *running*, where there is no
        exit status yet, so the dead pattern had to be repaired rather than deleted.
        """
        tmp = self._tmp()
        for name, text in (("abrt", BASH_ABORT), ("segv", BASH_SEGV)):
            p = tmp / f"{name}.err"
            p.write_text(text, encoding="utf-8")
            with self.subTest(sig=name):
                self.assertTrue(reap.detect_fault(p),
                                f"the real {name} stderr line matched no fault pattern")
        # And the word an agent might legitimately log is still NOT a fault, on purpose: a
        # false positive here costs a real retry, and SIGKILL is caught by exit status instead.
        p = tmp / "killed.err"
        p.write_text(BASH_KILLED, encoding="utf-8")
        self.assertIsNone(reap.detect_fault(p),
                          "a bare 'Killed' must not be a fault pattern -- it is an ordinary "
                          "word an agent may log, and the exit status covers SIGKILL")

    def test_the_reported_text_is_readable_not_a_raw_regex(self):
        """GUARD. ``detail`` and the report show this string to an operator, so it must be the
        matched text with the column padding collapsed -- not ``Aborted\\s+\\(core dumped\\)``."""
        tmp = self._tmp()
        p = tmp / "abrt.err"
        p.write_text(BASH_ABORT, encoding="utf-8")
        self.assertEqual(reap.detect_fault(p), "Aborted (core dumped)")


class TestHardDeathClassification(ModelBase):
    """The same question at the backend boundary, with real processes that really die."""

    def _poll_real(self, script, record=None, stderr_text=""):
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        backend = LocalBackend(cfg, self.log)
        task.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        task.stderr_path.write_text(stderr_text, encoding="utf-8")
        if record is not None:
            task.result_path.write_text(json.dumps(record), encoding="utf-8")
        proc = subprocess.Popen(["bash", "-c", script],
                                stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        proc.wait()
        attempt = Attempt(task=task, worker=0, stdout_path=task.stdout_path,
                          stderr_path=task.stderr_path, handle=proc)
        self.assertTrue(backend.poll(attempt))
        return attempt

    def test_a_sigabrt_with_a_record_and_no_matching_text_is_a_fault(self):
        """RED BEFORE THE FIX:

            AssertionError: <AttemptOutcome.EXITED: 'exited'> is not <AttemptOutcome.FAULT:
            'fault'> : an evaluator that died of SIGABRT was classified as a clean exit, so its
            crash-shaped record was charged to the MODEL's record budget

        The stderr here is deliberately EMPTY: this is the case where the text tells us
        nothing, which is the case the old code got wrong. 134 = 128 + SIGABRT.
        """
        attempt = self._poll_real("kill -ABRT $$", record=AGENT_CRASHED)
        self.assertIs(attempt.outcome, AttemptOutcome.FAULT,
                      "an evaluator that died of SIGABRT was classified as a clean exit, so "
                      "its crash-shaped record was charged to the MODEL's record budget")
        self.assertIn("SIGABRT", attempt.detail or "",
                      "the detail must name the signal, since no text did")

    def test_an_oom_kill_with_a_record_is_a_fault(self):
        """RED BEFORE THE FIX: same assertion, ``SIGKILL``. This is the OOM-killer shape, and
        the one that can never be caught by a pattern -- ``Killed`` is too ordinary a word to
        match on."""
        attempt = self._poll_real("kill -KILL $$", record=AGENT_CRASHED)
        self.assertIs(attempt.outcome, AttemptOutcome.FAULT)
        self.assertIn("SIGKILL", attempt.detail or "")

    def test_a_signalled_wrapper_negative_rc_is_a_fault(self):
        """RED BEFORE THE FIX:

            AssertionError: <AttemptOutcome.EXITED: 'exited'> is not <AttemptOutcome.FAULT:
            'fault'> ; detail was 'exit -9'

        When the reaper takes the shell itself, ``Popen.poll()`` reports the signal directly as
        a negative number. ``sys.exit(-1)`` cannot produce one, so this discriminator is exact.
        """
        proc = subprocess.Popen(["bash", "-c", "sleep 30"])
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        task.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        task.stderr_path.write_text("", encoding="utf-8")
        task.result_path.write_text(json.dumps(AGENT_CRASHED), encoding="utf-8")
        attempt = Attempt(task=task, worker=0, stdout_path=task.stdout_path,
                          stderr_path=task.stderr_path, handle=proc)
        self.assertTrue(LocalBackend(cfg, self.log).poll(attempt))
        self.assertIs(attempt.outcome, AttemptOutcome.FAULT)
        self.assertLess(attempt.exit_code, 0)

    def test_a_clean_exit_with_a_shared_stderr_fault_is_still_demoted(self):
        """GUARD -- the direction the fix must not break, and the reason the demotion exists at
        all. CARLA shares this attempt's stderr, so a UE4 crash during *shutdown* must not
        condemn an evaluator that exited on its own with a verdict already written."""
        attempt = self._poll_real("exit 0", record=AGENT_CRASHED, stderr_text=BASH_SEGV)
        self.assertIs(attempt.outcome, AttemptOutcome.EXITED)
        self.assertIn("stderr", attempt.detail or "")

    def test_the_evaluators_own_nonzero_crash_exit_is_still_a_clean_exit(self):
        """GUARD. ``sys.exit(-1)`` -> 255 is a self-terminated verdict and must stay on the
        model's record budget. If this ever goes red, the signal range has been widened too
        far."""
        attempt = self._poll_real("exit 255", record=AGENT_CRASHED)
        self.assertIs(attempt.outcome, AttemptOutcome.EXITED)


class TestTheTrapUnderHardDeath(IntegrationBase):
    """THE TRAP, end to end through ``main()``, on the newly-reachable ABNORMAL_END path.

    A model that legitimately fails every route is a real benchmark result and MUST exit 0.
    Widening what counts as a hard death moves whole populations of attempts from CLEAN_EXIT to
    ABNORMAL_END, so the trap has to be re-checked *there*.
    """

    def _cfg(self, **retry):
        base = {"record_budget": 2, "infra_budget": 2, "tickruntime_budget": 0,
                "killed_budget": 2, "worker_quarantine_after": 99}
        base.update(retry)
        for rel in ("static/s1/base/route_1_a.xml", "static/s1/visual_shift/route_2_b.xml"):
            self.site.add_route(rel)
        return self.site.config(retry=base)

    def test_a_degenerate_tickruntime_row_whose_process_is_SIGNALLED_still_exits_zero(self):
        """GUARD, and the one that would have caught an over-broad fix.

        ``Failed - TickRuntime`` is raised by the scenario manager's own tick guard, so a kill
        cannot manufacture it. It is charged to the tickruntime axis in BOTH outcome classes,
        and the default budget of 0 settles it on the attempt itself. If a future change routes
        ABNORMAL_END x RETRY_TICKRUNTIME anywhere else, a degenerate model's entire published
        row turns into N incomplete routes at exit 1.
        """
        code = self.run_cli(self._cfg(), mode="abort_after_tickruntime")
        rep = self.report()
        self.assertEqual(code, EXIT_OK, "a degenerate TickRuntime row was reported incomplete")
        self.assertEqual(rep["totals"]["incomplete"], 0)
        self.assertEqual(rep["totals"]["by_status"], {"Failed - TickRuntime": 2})
        for route in rep["routes"]:
            self.assertEqual(route["attempts"]["tickruntime"], 1)
            self.assertEqual(route["attempts"]["record"], 0)
            self.assertEqual(route["attempts"]["infra"], 0)

    def test_a_sigabrt_crash_record_is_charged_to_the_bounded_axis_not_the_model_budget(self):
        """RED BEFORE THE FIX (against the whole pre-fix tree; see the module docstring -- this
        one needs BOTH hunk A1 and hunk A2 reverted, because either alone catches SIGABRT):

            AssertionError: 2 != 0 : the model's record budget was spent by a SIGABRT

        The consequence of MAJOR A, measured where it matters: the same physical event (the
        evaluator dying during teardown with an ambiguous record already on disk) was accounted
        one way for SIGSEGV and the opposite way for SIGABRT, purely because of which shell
        message was printed. It still settles at exit 0 -- the axis is bounded, which is the
        whole point of the ``killed`` budget -- but on the right counter, loudly, with the
        superseded records kept under ``_runner/killed_records/``.
        """
        code = self.run_cli(self._cfg(), mode="abort_after_record")
        rep = self.report()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(rep["totals"]["by_status"], {"Failed - Simulation crashed": 2})
        for route in rep["routes"]:
            self.assertEqual(route["attempts"]["record"], 0,
                             "the model's record budget was spent by a SIGABRT")
            self.assertEqual(route["attempts"]["killed"], 2)
            self.assertEqual(route["attempts"]["infra"], 0)
        self.assertTrue(any("may have been written by the kill" in w for w in rep["warnings"]),
                        "settling on an ambiguous record must be reported, not silent")

    def test_an_oom_killed_crash_record_is_charged_to_the_bounded_axis(self):
        """RED BEFORE THE FIX: same assertion, SIGKILL. Kept separate from SIGABRT because
        SIGKILL is the shape no stderr pattern will ever catch."""
        self.assertEqual(self.run_cli(self._cfg(), mode="oomkill_after_record"), EXIT_OK)
        for route in self.report()["routes"]:
            self.assertEqual(route["attempts"]["record"], 0)
            self.assertEqual(route["attempts"]["killed"], 2)


# =======================================================================================
# MAJOR B -- retry.infra_budget: 0 must mean "no infra retries", never "no attempts"
# =======================================================================================
class TestInfraBudgetZeroIsNotADeadEnd(ModelBase):

    def _decide(self, attempts, infra_budget):
        cfg = config_mod.load(self.site.config(
            retry={"record_budget": 3, "infra_budget": infra_budget, "tickruntime_budget": 0,
                   "killed_budget": 2, "worker_quarantine_after": 99}))
        return plan_mod.decide(self._task(cfg), attempts, "skip_terminal",
                               record_budget=3, tickruntime_budget=0,
                               infra_budget=infra_budget, killed_budget=2)

    def test_a_virgin_route_runs_even_when_the_infra_budget_is_zero(self):
        """RED BEFORE THE FIX:

            AssertionError: <Decision.SKIP_EXHAUSTED> is not <Decision.RUN> : retry.infra_budget
            0 gated every route before a single attempt, on a healthy machine, for ever

        The infra gate is the only one evaluated *before* the axis it bounds has been charged,
        so ``0 >= 0`` fired on an empty ledger. Every other budget reads as "N attempts on this
        axis, then accept"; this one silently read as "never start".
        """
        self.assertIs(self._decide({}, 0).decision, plan_mod.Decision.RUN,
                      "retry.infra_budget 0 gated every route before a single attempt, on a "
                      "healthy machine, for ever")

    def test_one_charged_infra_failure_still_gates_at_budget_zero(self):
        """GUARD. The guard clause must not disable the gate -- once the axis HAS been charged,
        ``1 >= 0`` is a real exhaustion and the route is unsettled, exactly as at any other
        budget."""
        d = self._decide({"infra": 1}, 0)
        self.assertIs(d.decision, plan_mod.Decision.SKIP_EXHAUSTED)
        self.assertFalse(d.settled, "an infra-exhausted route has no settled answer")

    def test_the_gate_is_unchanged_for_every_positive_budget(self):
        """GUARD, exhaustive over the interesting neighbourhood: the fix must change behaviour
        for ``infra_budget == 0`` and for nothing else."""
        for budget in (1, 2, 3):
            for spent in range(0, 5):
                with self.subTest(budget=budget, spent=spent):
                    expected = (plan_mod.Decision.SKIP_EXHAUSTED if spent >= budget
                                else plan_mod.Decision.RUN)
                    self.assertIs(self._decide({"infra": spent}, budget).decision, expected)

    def test_a_route_holding_a_genuine_record_is_not_gated_at_budget_zero(self):
        """RED BEFORE THE FIX: the same defect on the other branch of ``decide`` -- the one
        DESIGN.md 6A.5's dead-end invariant is written about. A real model verdict with record
        budget left to spend was refused a retry it was owed, for ever."""
        task_cfg = config_mod.load(self.site.config())
        task = self._task(task_cfg)
        task.result_path.write_text(json.dumps(AGENT_CRASHED), encoding="utf-8")
        self.assertIs(self._decide({"record": 1}, 0).decision, plan_mod.Decision.RUN)


class TestInfraBudgetZeroEndToEnd(IntegrationBase):

    def test_a_healthy_sweep_at_infra_budget_zero_completes_at_exit_zero(self):
        """RED BEFORE THE FIX:

            AssertionError: 1 != 0 : infra_budget 0 produced a zero-work sweep -- exit 1 with
            no route ever launched

        Measured where an operator would meet it: a healthy machine, a healthy model, one legal
        config value, and the runner refused to do anything at all while printing a recovery
        instruction (``--retry-infra-exhausted``) that provably could not help, because
        ``clear_infra_exhaustion`` only clears a counter that was actually charged.
        """
        for rel in ("static/s1/base/route_1_a.xml", "static/s1/visual_shift/route_2_b.xml"):
            self.site.add_route(rel)
        cfg = self.site.config(retry={"record_budget": 2, "infra_budget": 0,
                                      "tickruntime_budget": 0, "killed_budget": 2,
                                      "worker_quarantine_after": 99})
        code = self.run_cli(cfg, mode="ok")
        rep = self.report()
        self.assertEqual(code, EXIT_OK,
                         "infra_budget 0 produced a zero-work sweep -- exit 1 with no route "
                         "ever launched")
        self.assertEqual(rep["totals"]["incomplete"], 0)
        self.assertEqual(len(self.site.trace_rows()), 2,
                         "the evaluator was never invoked at all")

    def test_at_budget_zero_a_real_infra_failure_gates_and_is_then_recoverable(self):
        """GUARD over the whole loop: the guard clause must not cost the axis its meaning.

        One launch failure at budget 0 gates the route (that IS exhaustion), the run exits
        non-zero, and ``--retry-infra-exhausted`` -- which could not touch the pre-fix gate,
        because the counter was never charged -- now releases it losslessly.
        """
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self.site.config(retry={"record_budget": 2, "infra_budget": 0,
                                      "tickruntime_budget": 0, "killed_budget": 2,
                                      "worker_quarantine_after": 99})
        self.assertEqual(self.run_cli(cfg, mode="no_record"), EXIT_PARTIAL)
        ledger = json.loads((self.site.out / "_runner" / "state.json").read_text())
        st = next(iter(ledger["tasks"].values()))
        self.assertEqual(st["attempts_infra"], 1)
        self.assertFalse(st["finished"])

        # A plain resume is still gated -- the gate is real, not disabled.
        self.assertEqual(self.run_cli(cfg, mode="ok"), EXIT_PARTIAL)

        # ...and the advertised lossless escape actually works now.
        self.assertEqual(self.run_cli(cfg, mode="ok", extra=["--retry-infra-exhausted"]),
                         EXIT_OK)
        self.assertEqual(self.report()["totals"]["incomplete"], 0)


class TestRetryInfraExhaustedRespectsDryRun(IntegrationBase):

    def _gate_one_route(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self.site.config(retry={"record_budget": 2, "infra_budget": 1,
                                      "tickruntime_budget": 0, "killed_budget": 2,
                                      "worker_quarantine_after": 99})
        self.assertEqual(self.run_cli(cfg, mode="no_record"), EXIT_PARTIAL)
        return cfg

    def _infra(self):
        ledger = json.loads((self.site.out / "_runner" / "state.json").read_text())
        return next(iter(ledger["tasks"].values()))["attempts_infra"]

    def test_a_dry_run_does_not_spend_the_recovery_it_was_asked_to_preview(self):
        """RED BEFORE THE FIX:

            AssertionError: 0 != 1 : --dry-run spent the recovery it was asked to preview

        The clear ran before planning and saved unconditionally, so an operator asking what
        ``--retry-infra-exhausted`` *would* do had already done it. The earlier code argued the
        persistence was needed for the printed plan to be accurate -- true of the clear, not of
        the save; clearing in memory gives the same plan with no write.
        """
        cfg = self._gate_one_route()
        self.assertEqual(self._infra(), 1)
        code = run_benchmark.main(["--config", cfg, "--dry-run", "--retry-infra-exhausted"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self._infra(), 1,
                         "--dry-run spent the recovery it was asked to preview")

    def test_the_real_run_still_persists_it(self):
        """GUARD. The fix must not turn the flag into a no-op everywhere."""
        cfg = self._gate_one_route()
        self.assertEqual(self.run_cli(cfg, mode="ok", extra=["--retry-infra-exhausted"]),
                         EXIT_OK)
        self.assertEqual(self._infra(), 0)


# =======================================================================================
# ROUND FOUR -- from the cross-review of round three (2026-08-07)
# =======================================================================================
class TestDryRunWritesNothingAtAll(IntegrationBase):
    """`--dry-run` is documented as "print the plan and exit 0 without running anything".

    RED BEFORE THE FIX:

        AssertionError: <ledger bytes> != <ledger bytes> : --dry-run rewrote the ledger

    Round three rolled back only ``attempts_infra``. codex found the rest: planning materialises
    a ``TaskState`` for every route, sets or clears ``finished`` on each, and ``load_or_create``
    has already replaced the stored ``config_digest`` -- then it is all saved. The sharp end is
    the digest: previewing a run under a changed config **erases the `config_changed()` warning**
    the operator would have been shown on the real run.

    Round four's first attempt snapshotted the file and restored it afterwards. codex reviewed
    *that* and was right again: a snapshot leaves a window in which a ``SIGKILL`` freezes the
    preview on disk, and the restoring write is itself non-atomic. The rule is now that a dry
    run **never calls ``save()``**, which is crash-safe by construction rather than by narrowing
    a window -- and ``test_no_dry_run_path_writes_the_ledger_at_all`` asserts that structurally,
    so "which call site did we forget" cannot be asked again either.
    """

    def _run_once(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self.site.config(retry={"record_budget": 2, "infra_budget": 2,
                                      "tickruntime_budget": 0, "killed_budget": 2,
                                      "worker_quarantine_after": 99})
        self.assertEqual(self.run_cli(cfg, mode="ok"), EXIT_OK)
        return cfg

    def _ledger(self):
        return (self.site.out / "_runner" / "state.json").read_bytes()

    def test_a_dry_run_leaves_an_existing_ledger_byte_identical(self):
        cfg = self._run_once()
        before = self._ledger()
        self.assertEqual(run_benchmark.main(["--config", cfg, "--dry-run"]), EXIT_OK)
        self.assertEqual(self._ledger(), before, "--dry-run rewrote the ledger")

    def test_a_dry_run_does_not_erase_the_changed_config_warning(self):
        """The consequence that makes this more than tidiness.

        Preview under a changed config, then really resume: the operator must still be told the
        tree was produced by a different configuration. Before the fix the dry run stamped the
        new digest into the ledger, so the real run compared new against new and said nothing.
        """
        self._run_once()
        changed = self.site.config(retry={"record_budget": 9, "infra_budget": 2,
                                          "tickruntime_budget": 0, "killed_budget": 2,
                                          "worker_quarantine_after": 99})
        self.assertEqual(run_benchmark.main(["--config", changed, "--dry-run"]), EXIT_OK)
        self.assertEqual(self.run_cli(changed, mode="ok"), EXIT_OK)
        self.assertTrue(
            any("DIFFERENT configuration" in w for w in self.report()["warnings"]),
            "a dry run swallowed the configuration-changed warning by stamping its own digest")

    def test_a_dry_run_on_a_fresh_tree_leaves_no_ledger_behind(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self.site.config()
        self.assertEqual(run_benchmark.main(["--config", cfg, "--dry-run"]), EXIT_OK)
        self.assertFalse((self.site.out / "_runner" / "state.json").exists(),
                         "--dry-run created a ledger on a tree that had none")

    def test_no_dry_run_path_writes_the_ledger_at_all(self):
        """The structural version, and the one that survives refactoring.

        The tests above check the *result* on disk, which a snapshot-and-restore would also
        satisfy while still leaving a crash window. This checks the mechanism: make ``save()``
        itself fatal and assert a dry run still exits 0. If any future edit adds a save on a
        dry-run path -- or drops one of the three guards -- this goes red immediately, and it
        does so without needing to know where the call sites are.

        Both directions are asserted, because a guard that is always on is as wrong as one that
        is always off: the same fatal ``save()`` must make a REAL run fail.
        """
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self.site.config()
        boom = lambda self: (_ for _ in ()).throw(AssertionError("state.save() on a dry run"))
        with unittest.mock.patch.object(RunState, "save", boom):
            self.assertEqual(run_benchmark.main(["--config", cfg, "--dry-run"]), EXIT_OK)
            with self.assertRaises(AssertionError):
                self.run_cli(cfg, mode="ok")


class TestAccountingEpochIsAnnounced(IntegrationBase):
    """Resuming a ledger written under a different §6A model must not be silent.

    RED BEFORE THE FIX:

        AssertionError: False is not true : resuming a pre-epoch-2 ledger said nothing about
        the accounting model having changed

    Raised independently by **both** cross-review reviewers, and by round three's own
    side-effect auditor. The config digest deliberately does not cover this
    (``DIGEST_COMPAT_DEFAULTS``), and that is right for the question the digest asks -- but it
    left the accounting change that arrived with the same key invisible too. Three questions,
    three answers: the digest for settings, the runner version for the build, and
    ``accounting_epoch`` for the rules.
    """

    def _aged_ledger(self):
        """A ledger as the pre-§6A runner would have left it: no epoch, no killed axis."""
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self.site.config(retry={"record_budget": 3, "infra_budget": 2,
                                      "tickruntime_budget": 0, "killed_budget": 2,
                                      "worker_quarantine_after": 99})
        self.assertEqual(self.run_cli(cfg, mode="crash_record"), EXIT_OK)
        path = self.site.out / "_runner" / "state.json"
        raw = json.loads(path.read_text())
        raw.pop("accounting_epoch", None)
        for st in raw["tasks"].values():
            st.pop("attempts_killed", None)
            st["finished"] = False
        path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
        return cfg

    def test_resuming_a_pre_epoch_ledger_warns(self):
        cfg = self._aged_ledger()
        self.assertEqual(self.run_cli(cfg, mode="ok"), EXIT_OK)
        self.assertTrue(
            any("accounting epoch" in w for w in self.report()["warnings"]),
            "resuming a pre-epoch-2 ledger said nothing about the accounting model having "
            "changed")

    def test_a_ledger_this_runner_wrote_does_not_warn(self):
        """GUARD. The warning must fire on a real epoch change and never on an ordinary resume,
        or it becomes noise an operator learns to ignore."""
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self.site.config()
        self.assertEqual(self.run_cli(cfg, mode="crash_record"), EXIT_OK)
        self.assertEqual(self.run_cli(cfg, mode="ok"), EXIT_OK)
        self.assertFalse(any("accounting epoch" in w for w in self.report()["warnings"]))

    def test_the_epoch_is_stamped_into_every_ledger(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        self.assertEqual(self.run_cli(self.site.config(), mode="ok"), EXIT_OK)
        raw = json.loads((self.site.out / "_runner" / "state.json").read_text())
        self.assertEqual(raw["accounting_epoch"], state_mod.ACCOUNTING_EPOCH)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
