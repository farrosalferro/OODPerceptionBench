"""The evaluator's "world never became ready" exit is an infrastructure failure.

Patch 330 makes the leaderboard evaluator probe the CARLA world (``get_world().get_settings()``
every 10 s, up to ``OODPB_WORLD_READY_S``) before it builds anything, and exit with a distinct
status if the world never answers. Nothing ran: no map, no agent, no route. Whatever is on disk
afterwards cannot be this attempt's output, so the attempt is charged to the INFRASTRUCTURE
budget and advances the wedged-worker streak -- never the route's record budget.
"""

import json
import re
import time
import unittest
from pathlib import Path

from oodbench import EVALUATOR_WORLD_NOT_READY, config as config_mod, reap
from oodbench.backends.base import Attempt, AttemptOutcome

from tests.test_settlement_model import COMPLETED, SIM_CRASHED, ModelBase, _DummyBackend

PATCH_330 = (Path(__file__).resolve().parents[2] / "patches"
             / "330-Bench2Drive_leaderboard_leaderboard_leaderboard_evaluator.patch")


class TestWorldNotReadyExit(ModelBase):

    def _cfg(self, infra_budget=3, record_budget=2):
        return config_mod.load(self.site.config(
            retry={"infra_budget": infra_budget, "record_budget": record_budget,
                   "tickruntime_budget": 0, "worker_quarantine_after": 99}))

    def _settle_rc(self, runner, state, task, outcome, rc, interrupted=False):
        attempt = Attempt(task=task, worker=0, stdout_path=task.stdout_path,
                          stderr_path=task.stderr_path)
        attempt.outcome = outcome
        attempt.exit_code = rc
        attempt.detail = f"exit {rc}"
        attempt.finished_at = time.time()
        return runner._settle(attempt, state, _DummyBackend(), interrupted=interrupted)

    def test_no_record_is_charged_to_infra_and_retried(self):
        cfg = self._cfg()
        task = self._task(cfg)
        runner, state = self._runner_and_state(cfg)

        requeue = self._settle_rc(runner, state, task, AttemptOutcome.EXITED,
                                  EVALUATOR_WORLD_NOT_READY)

        st = state.get(task.key)
        self.assertTrue(requeue)
        self.assertEqual((st.attempts_infra, st.attempts_record), (1, 0))
        self.assertEqual(runner.consecutive_infra[0], 1)
        self.assertIn("world never became ready", st.last_reason)

    def test_a_crash_record_on_disk_is_not_charged_to_the_record_budget(self):
        """The evaluator exits 75 before it writes anything, so a final record on disk is not
        its own. Read as an ordinary clean exit, a crash-shaped record spent the route's record
        retries -- and once those ran out, froze it in as the benchmark result."""
        cfg = self._cfg(record_budget=1)
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(SIM_CRASHED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        for outcome in (AttemptOutcome.EXITED, AttemptOutcome.FAULT):
            requeue = self._settle_rc(runner, state, task, outcome, EVALUATOR_WORLD_NOT_READY)
            self.assertTrue(requeue, outcome)

        st = state.get(task.key)
        self.assertEqual(st.attempts_record, 0,
                         "a world-never-ready exit spent the route's RECORD budget")
        self.assertEqual(st.attempts_infra, 2)
        self.assertFalse(st.finished)
        self.assertEqual(runner.consecutive_infra[0], 2,
                         "nothing ran, which is the wedged-worker signature: the streak must "
                         "advance")

    def test_an_accepted_record_on_disk_is_not_adopted(self):
        cfg = self._cfg()
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        self._settle_rc(runner, state, task, AttemptOutcome.EXITED, EVALUATOR_WORLD_NOT_READY)

        st = state.get(task.key)
        self.assertFalse(st.finished, "a record this attempt cannot have written was adopted")
        self.assertEqual(st.attempts_infra, 1)

    def test_exhausting_the_infra_budget_leaves_the_route_unsettled(self):
        cfg = self._cfg(infra_budget=2)
        task = self._task(cfg)
        runner, state = self._runner_and_state(cfg)

        self.assertTrue(self._settle_rc(runner, state, task, AttemptOutcome.EXITED,
                                        EVALUATOR_WORLD_NOT_READY))
        self.assertFalse(self._settle_rc(runner, state, task, AttemptOutcome.EXITED,
                                         EVALUATOR_WORLD_NOT_READY))
        st = state.get(task.key)
        self.assertFalse(st.finished)
        self.assertEqual((st.attempts_infra, st.attempts_record), (2, 0))

    def test_teardown_still_outranks_it(self):
        cfg = self._cfg()
        task = self._task(cfg)
        runner, state = self._runner_and_state(cfg)

        self.assertFalse(self._settle_rc(runner, state, task, AttemptOutcome.EXITED,
                                         EVALUATOR_WORLD_NOT_READY, interrupted=True))
        st = state.get(task.key)
        self.assertEqual((st.attempts_infra, st.attempts_record), (0, 0))

    def test_other_exit_codes_are_unaffected(self):
        cfg = self._cfg(record_budget=1)
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(SIM_CRASHED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        self._settle_rc(runner, state, task, AttemptOutcome.EXITED, 1)

        self.assertEqual(state.get(task.key).attempts_record, 1)


class TestEvaluatorSync(unittest.TestCase):

    def test_patch_330_exits_with_the_code_the_runner_classifies(self):
        text = PATCH_330.read_text(encoding="utf-8")
        found = re.findall(r"^\+WORLD_NOT_READY_EXIT_CODE = (\d+)$", text, re.M)
        self.assertEqual(found, [str(EVALUATOR_WORLD_NOT_READY)])
        self.assertIn("sys.exit(WORLD_NOT_READY_EXIT_CODE)", text)

    def test_the_code_is_not_read_as_a_signal(self):
        self.assertIsNone(reap.describe_exit_signal(EVALUATOR_WORLD_NOT_READY))


if __name__ == "__main__":
    unittest.main()
