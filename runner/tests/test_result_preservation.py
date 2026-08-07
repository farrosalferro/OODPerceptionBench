"""A launch that never happened must not destroy *or be mistaken for* the record it was going
to replace, and an operator interrupt must not spend a route's retry budget.

Every defect covered here was found by reading the code, reproduced with a failing probe, then
fixed. They all fail in the same direction and for the same reason: **something that is not a
benchmark result gets accounted as one**, so a route either loses its fair attempts or freezes
a stale/manufactured record as its final answer. None of them violates the exit contract, so
nothing else in the suite notices them coming back.
"""

import argparse
import json
import logging
import socket
import time
import unittest
from pathlib import Path

import run_benchmark
from oodbench import config as config_mod, plan as plan_mod
from oodbench.backends.base import Attempt, AttemptOutcome
from oodbench.backends.local import LocalBackend
from oodbench.state import RunState

from tests.test_integration_local import Site

CRASHED = {"_checkpoint": {"progress": [1, 1],
                           "records": [{"status": "Failed - Agent crashed",
                                        "scores": {"score_composed": 12.5}}]}}

#: What the evaluator's crash handler writes when a Ctrl-C kills the worker's process group and
#: takes CARLA down with it. A *final* record, manufactured by the interrupt.
SIM_CRASHED = {"_checkpoint": {"progress": [1, 1],
                               "records": [{"status": "Failed - Simulation crashed",
                                            "scores": {"score_composed": 3.0}}]}}

COMPLETED = {"_checkpoint": {"progress": [1, 1],
                             "records": [{"status": "Completed",
                                          "scores": {"score_composed": 88.0}}]}}


class _DummyBackend:
    """Enough backend for ``Runner._settle``: it only ever calls ``cleanup_worker``."""

    name = "local"
    concurrency = 1

    def cleanup_worker(self, worker):
        pass


class PreservationBase(unittest.TestCase):

    def setUp(self):
        self.site = Site()
        self.site.add_route("static/s1/base/route_1.xml")
        run_benchmark._interrupted["flag"] = False
        self.log = logging.getLogger("test-preservation")
        self.log.addHandler(logging.NullHandler())
        self.log.propagate = False

    def tearDown(self):
        self.site.cleanup()

    def _task(self, cfg):
        xmls = plan_mod.discover(Path(cfg.routes["root"]))
        task = plan_mod.build_tasks(xmls, Path(cfg.routes["root"]), Path(cfg.output["root"]),
                                    base_seed=cfg.seed, repetitions=1)[0]
        task.mkdirs()
        return task

    def _runner_and_state(self, cfg):
        args = argparse.Namespace(limit=None, dry_run=False, force=False)
        runner = run_benchmark.Runner(cfg, args)
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")
        return runner, state

    def _settle(self, runner, state, task, outcome, detail, interrupted=False, worker=0):
        attempt = Attempt(task=task, worker=worker, stdout_path=task.stdout_path,
                          stderr_path=task.stderr_path)
        attempt.outcome = outcome
        attempt.detail = detail
        attempt.finished_at = time.time()
        return runner._settle(attempt, state, _DummyBackend(), interrupted=interrupted)


class TestFailedLaunchPreservesRecord(PreservationBase):
    """A route holding a retryable record is planned as RUN, so it reaches submit() with a
    valid benchmark result on disk. If the launch then fails, that result must survive."""

    def test_busy_port_launch_failure_preserves_the_existing_record(self):
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(CRASHED), encoding="utf-8")

        backend = LocalBackend(cfg, self.log)
        pair = backend.pairs[0]

        squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            squatter.bind(("localhost", pair.rpc))
            squatter.listen(1)
            attempt = backend.submit(task, worker=0)
        finally:
            squatter.close()

        self.assertEqual(attempt.outcome, AttemptOutcome.LAUNCH_FAILED)
        self.assertTrue(task.result_path.is_file(),
                        "a launch that never started deleted a valid benchmark record")
        self.assertEqual(json.loads(task.result_path.read_text()), CRASHED)

    def test_successful_launch_still_starts_from_a_clean_slate(self):
        # The other half of the contract: when the launch DOES happen, the stale checkpoint
        # must be gone, or the evaluator's in-file resume finishes the route against old data.
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(CRASHED), encoding="utf-8")

        backend = LocalBackend(cfg, self.log)
        attempt = backend.submit(task, worker=0)
        self.assertNotEqual(attempt.outcome, AttemptOutcome.LAUNCH_FAILED)
        try:
            deadline = time.time() + 60
            while not backend.poll(attempt) and time.time() < deadline:
                time.sleep(0.1)
            self.assertTrue(backend.poll(attempt), "the stand-in evaluator never finished")
            # Whatever is on disk now came from this attempt, not from the stale checkpoint.
            record = json.loads(task.result_path.read_text())
            self.assertNotEqual(record["_checkpoint"]["records"][0]["status"],
                                "Failed - Agent crashed",
                                "the stale checkpoint survived a successful launch")
        finally:
            backend.kill(attempt, "test teardown")
            backend.shutdown()


class TestFailedLaunchIsChargedToInfrastructure(PreservationBase):
    """A launch that never started is infrastructure, whatever is on disk.

    The backend restores the pre-existing record on every failed-launch path, so by the time
    ``_settle`` reads the result file it is looking at an EARLIER attempt's output. Judging it
    as this attempt's result was a two-part silent failure: it charged the route's *record*
    budget for an infrastructure fault -- busy ports, a refused ``sbatch`` -- and then, once
    that budget ran out, marked the route finished with the stale record frozen in as its
    benchmark result, having never actually retried it.
    """

    RETRY_BUDGETS = {"infra_budget": 2, "record_budget": 3, "tickruntime_budget": 0,
                     "worker_quarantine_after": 99}
    DETAIL = "worker 0 ports [20000] still occupied after reaping; refusing to launch"

    def _fail_launch(self, runner, state, task, worker=0):
        return self._settle(runner, state, task, AttemptOutcome.LAUNCH_FAILED, self.DETAIL,
                            worker=worker)

    def test_failed_launch_spends_the_infra_budget_not_the_record_budget(self):
        cfg = config_mod.load(self.site.config(retry=self.RETRY_BUDGETS))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(CRASHED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        self.assertTrue(self._fail_launch(runner, state, task),
                        "a failed launch with budget left must be re-queued")
        st = state.get(task.key)
        self.assertEqual(st.attempts_record, 0,
                         "a failed launch charged the route's RECORD retry budget; "
                         "infrastructure faults must never spend the model's attempts")
        self.assertEqual(st.attempts_infra, 1)
        self.assertFalse(st.finished)

    def test_repeated_failed_launches_never_freeze_the_stale_record_as_the_result(self):
        cfg = config_mod.load(self.site.config(retry=self.RETRY_BUDGETS))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(CRASHED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        self.assertTrue(self._fail_launch(runner, state, task))
        self.assertFalse(self._fail_launch(runner, state, task),
                         "infra budget 2/2 is spent; the route must stop being re-queued")

        st = state.get(task.key)
        self.assertEqual(st.attempts_record, 0)
        self.assertEqual(st.attempts_infra, 2)
        self.assertFalse(st.finished,
                         "the route was marked finished on the strength of a record no attempt "
                         "in this run ever produced")
        self.assertEqual(json.loads(task.result_path.read_text()), CRASHED,
                         "the pre-existing record must survive byte-identical")
        self.assertTrue(any("EARLIER" in w for w in runner.warnings),
                        "the report must say the retries never happened, not stay silent")

    def test_failed_launch_does_not_adopt_a_stale_accepted_record(self):
        # The nastiest shape: under `resume.mode: none` a route with an *accepted* record is
        # deliberately re-planned. If a failed launch is then read as this attempt's output, the
        # old score is silently re-adopted as this run's result with nothing having been driven.
        cfg = config_mod.load(self.site.config(resume={"mode": "none"},
                                               retry=self.RETRY_BUDGETS))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        self._fail_launch(runner, state, task)
        st = state.get(task.key)
        self.assertFalse(st.finished,
                         "a failed launch re-adopted a stale accepted record as this run's "
                         "result")
        self.assertEqual(st.attempts_infra, 1)
        self.assertEqual(st.attempts_record, 0)

    def test_failed_launches_still_count_toward_worker_quarantine(self):
        # Consecutive infra failures on one worker are the signature of a wedged GPU or a
        # permanently squatted port block. Treating a failed launch as "produced a record" reset
        # that counter every time, so the detector could never fire.
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 9, "record_budget": 3, "tickruntime_budget": 0,
                   "worker_quarantine_after": 2}))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(CRASHED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        self._fail_launch(runner, state, task)
        self._fail_launch(runner, state, task)
        self.assertEqual(runner.quarantined, [0],
                         "repeated failed launches on one worker never quarantined it")

    def test_a_real_retryable_record_is_still_charged_to_the_record_budget(self):
        # The fix must not disarm record accounting for attempts that really did run.
        cfg = config_mod.load(self.site.config(retry=self.RETRY_BUDGETS))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(CRASHED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        self.assertTrue(self._settle(runner, state, task, AttemptOutcome.EXITED, "exit 1"))
        st = state.get(task.key)
        self.assertEqual(st.attempts_record, 1)
        self.assertEqual(st.attempts_infra, 0)


class TestInterruptDoesNotSpendBudget(PreservationBase):

    def _interrupt_once(self, runner, state, task):
        return self._settle(runner, state, task, AttemptOutcome.KILLED, "runner interrupted",
                            interrupted=True)

    def test_repeated_interrupts_never_exhaust_the_infra_budget(self):
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 3, "record_budget": 3, "tickruntime_budget": 0,
                   "worker_quarantine_after": 3}))
        task = self._task(cfg)
        runner, state = self._runner_and_state(cfg)

        for _ in range(5):   # more interrupts than the budget
            self.assertFalse(self._interrupt_once(runner, state, task),
                             "an interrupted attempt asked to be re-queued")

        st = state.get(task.key)
        self.assertEqual(st.attempts_infra, 0,
                         "an operator interrupt was charged to the infrastructure retry budget")
        self.assertIsNotNone(st.last_reason)

        decision = plan_mod.decide(task, st.budgets(), cfg.resume["mode"],
                                   record_budget=3, tickruntime_budget=0, infra_budget=3,
                                   killed_budget=int(cfg.retry["killed_budget"]))
        self.assertIs(decision.decision, plan_mod.Decision.RUN,
                      "interrupts permanently abandoned a route that never actually failed")

    def test_interrupts_do_not_quarantine_a_healthy_worker(self):
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 3, "record_budget": 3, "tickruntime_budget": 0,
                   "worker_quarantine_after": 2}))
        task = self._task(cfg)
        runner, state = self._runner_and_state(cfg)
        for _ in range(4):
            self._interrupt_once(runner, state, task)
        self.assertEqual(runner.quarantined, [],
                         "interrupting the sweep quarantined workers that never misbehaved")

    def test_a_genuine_infra_failure_is_still_charged(self):
        # The fix must not disarm the budget for real failures.
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 2, "record_budget": 3, "tickruntime_budget": 0,
                   "worker_quarantine_after": 99}))
        task = self._task(cfg)
        runner, state = self._runner_and_state(cfg)

        requeue = self._settle(runner, state, task, AttemptOutcome.EXITED, "exit 3")

        self.assertTrue(requeue)
        self.assertEqual(state.get(task.key).attempts_infra, 1)

    # -- the other half of the interrupt fix -------------------------------------------------
    # The original fix only covered the *no record* path. But killing the worker's process group
    # takes CARLA down with it, and the evaluator's crash handler writes a FINAL
    # `Failed - Simulation crashed` record on the way out -- so the common shape of an
    # interrupted route is precisely the one the fix missed.

    def test_interrupt_does_not_charge_the_record_budget(self):
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 3, "record_budget": 2, "tickruntime_budget": 0,
                   "worker_quarantine_after": 99}))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(SIM_CRASHED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        for _ in range(4):   # more interrupts than the record budget
            self.assertFalse(self._interrupt_once(runner, state, task),
                             "an interrupted attempt asked to be re-queued")

        st = state.get(task.key)
        self.assertEqual(st.attempts_record, 0,
                         "Ctrl-C spent the route's record retries on a record the Ctrl-C itself "
                         "manufactured")
        self.assertFalse(st.finished,
                         "an interrupt artefact was frozen in as the route's benchmark result")

        decision = plan_mod.decide(task, st.budgets(), cfg.resume["mode"],
                                   record_budget=2, tickruntime_budget=0, infra_budget=3,
                                   killed_budget=int(cfg.retry["killed_budget"]))
        self.assertIs(decision.decision, plan_mod.Decision.RUN,
                      "the next run must re-plan an interrupted route, budget intact")

    def test_interrupt_does_not_charge_the_tickruntime_budget(self):
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 3, "record_budget": 3, "tickruntime_budget": 1,
                   "worker_quarantine_after": 99}))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(
            {"_checkpoint": {"progress": [1, 1],
                             "records": [{"status": "Failed - TickRuntime",
                                          "scores": {"score_composed": 0.0}}]}}),
            encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        for _ in range(3):
            self._interrupt_once(runner, state, task)
        self.assertEqual(state.get(task.key).attempts_tickruntime, 0)

    def test_an_interrupted_route_that_completed_is_still_accepted(self):
        # The converse guard: a route that genuinely finished before we killed anything has a
        # real result, and must not be re-run on the next pass.
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        self.assertFalse(self._interrupt_once(runner, state, task))
        self.assertTrue(state.get(task.key).finished,
                        "an interrupt discarded a route that had genuinely completed")

    def test_a_retryable_record_is_still_charged_when_not_interrupted(self):
        # Guard against the fix disarming record accounting in the normal path.
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 3, "record_budget": 2, "tickruntime_budget": 0,
                   "worker_quarantine_after": 99}))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(SIM_CRASHED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        self.assertTrue(self._settle(runner, state, task, AttemptOutcome.EXITED, "exit 1"))
        self.assertEqual(state.get(task.key).attempts_record, 1)


if __name__ == "__main__":
    unittest.main()
