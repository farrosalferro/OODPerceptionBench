"""Exit contract and the persistent attempt ledger."""

import json
import tempfile
import unittest
from pathlib import Path

from oodbench import EXIT_AGENT_FATAL, EXIT_INTERRUPTED, EXIT_OK, EXIT_PARTIAL
from oodbench.report import Report, RouteOutcome
from oodbench.state import RunState


def outcome(key, final, status=None, settled=None):
    """A route outcome for the exit-contract tests.

    Completeness has two halves since DESIGN.md 6A.8 -- the record is final *on disk* AND the
    ledger settled it -- so the helper states the second one. It defaults to ``final``, which
    is the shape every test in this file is about: a route whose record this run produced and
    settled. The case where the two halves disagree (a record preserved by a failed launch that
    this run never settled) is the subject of ``tests/test_settlement_model.py``.
    """
    return RouteOutcome(key=key, route=f"/x/{key}.xml", seed=42,
                        result_path=f"/out/{key}.json", final=final, status=status,
                        settled=final if settled is None else settled,
                        score_composed=1.0 if final else None)


class TestExitContract(unittest.TestCase):

    def _report(self, outcomes, **kw):
        r = Report(started_at=0.0, finished_at=1.0, planned=len(outcomes), **kw)
        r.outcomes = outcomes
        return r

    def test_all_complete_exits_zero(self):
        r = self._report([outcome("a", True, "Completed"), outcome("b", True, "Perfect")])
        self.assertEqual(r.exit_code(), EXIT_OK)

    def test_model_side_failures_are_not_runner_failures(self):
        # A model that scores Failed - TickRuntime everywhere has produced a valid, if
        # unflattering, benchmark result. That must exit 0.
        r = self._report([outcome("a", True, "Failed - TickRuntime")] * 5)
        self.assertEqual(r.exit_code(), EXIT_OK)

    def test_legitimate_route_failures_exit_zero(self):
        r = self._report([outcome("a", True, "Failed - Agent got blocked"),
                          outcome("b", True, "Failed")])
        self.assertEqual(r.exit_code(), EXIT_OK)

    def test_one_missing_record_exits_nonzero(self):
        r = self._report([outcome("a", True, "Completed"), outcome("b", False)])
        self.assertEqual(r.exit_code(), EXIT_PARTIAL)

    def test_empty_plan_exits_zero(self):
        self.assertEqual(self._report([]).exit_code(), EXIT_OK)

    def test_interrupt_is_never_zero_even_if_everything_landed(self):
        r = self._report([outcome("a", True, "Completed")], interrupted=True)
        self.assertEqual(r.exit_code(), EXIT_INTERRUPTED)

    def test_fatal_agent_wins_over_everything(self):
        r = self._report([outcome("a", True, "Completed")], fatal_agent=True)
        self.assertEqual(r.exit_code(), EXIT_AGENT_FATAL)

    def test_all_workers_quarantined_has_its_own_code(self):
        r = self._report([outcome("a", False)], all_workers_quarantined=True,
                         quarantined_workers=[0, 1])
        self.assertEqual(r.exit_code(), 4)

    def test_a_final_record_this_run_never_settled_does_not_exit_zero(self):
        """The OFF-DIAGONAL case, and the reason ``settled`` is a parameter of the helper.

        Completeness has two halves since DESIGN.md 6A.8 -- ``final`` on disk AND settled in the
        ledger -- and every other test in this file runs on the diagonal where they agree, which
        is what a route this run produced looks like. The half that carries the exit contract is
        the one where they DISAGREE: a record preserved by a failed launch is final on disk and
        was never refreshed, and reading it as a result is cross-review finding 6. A file titled
        for the exit contract has to state that itself rather than delegate it.
        """
        r = self._report([outcome("a", True, "Completed"),
                          outcome("b", True, "Failed - Agent crashed", settled=False)])
        self.assertEqual(r.exit_code(), EXIT_PARTIAL,
                         "a record this run preserved but never settled was counted as a result")
        self.assertEqual(r.status_counts, {"Completed": 1})
        self.assertEqual(r.unsettled_counts, {"Failed - Agent crashed": 1})

    def test_no_incomplete_route_can_ever_yield_zero(self):
        # Exhaustive over the flag combinations the runner can produce, and over BOTH shapes an
        # incomplete route can have: nothing on disk, and a final record the ledger never
        # settled. The second was previously unreachable through this helper.
        for incomplete in (outcome("b", False),
                           outcome("b", True, "Failed - Agent crashed", settled=False)):
            for interrupted in (False, True):
                for fatal in (False, True):
                    for allq in (False, True):
                        r = self._report([outcome("a", True, "Completed"), incomplete],
                                         interrupted=interrupted, fatal_agent=fatal,
                                         all_workers_quarantined=allq)
                        self.assertNotEqual(
                            r.exit_code(), EXIT_OK,
                            f"final={incomplete.final} settled={incomplete.settled} "
                            f"interrupted={interrupted} fatal={fatal} all_quarantined={allq}")

    def test_report_serialises_and_carries_the_version_stamp(self):
        r = self._report([outcome("a", True, "Completed")])
        d = r.to_dict()
        self.assertEqual(d["benchmark"]["release"], "v0.9")
        self.assertEqual(d["benchmark"]["arxiv_version"], "v1")
        self.assertIn("exit", d)
        json.dumps(d)  # must be serialisable
        self.assertIn("OOD-PerceptionBench run report", r.to_markdown())

    def test_off_protocol_seed_is_flagged(self):
        r = Report(started_at=0.0, finished_at=1.0, seed=7, protocol_seed_deviation=True)
        self.assertTrue(r.to_dict()["benchmark"]["protocol_seed_deviation"])
        self.assertIn("protocol seed", r.to_markdown())


class TestLedger(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "_runner" / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_preserves_budgets(self):
        s = RunState.load_or_create(self.path, "digest-1")
        st = s.get("vehicle/x/base/route_1_seed42")
        st.attempts_record = 2
        st.attempts_infra = 1
        st.last_status = "Failed - Agent crashed"
        s.save()

        s2 = RunState.load_or_create(self.path, "digest-1")
        st2 = s2.get("vehicle/x/base/route_1_seed42")
        self.assertEqual(st2.attempts_record, 2)
        self.assertEqual(st2.attempts_infra, 1)
        self.assertEqual(st2.budgets(),
                         {"record": 2, "tickruntime": 0, "infra": 1, "killed": 0})

    def test_write_is_atomic_no_tmp_left_behind(self):
        s = RunState.load_or_create(self.path, "d")
        s.get("k").attempts_record = 1
        s.save()
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])
        json.loads(self.path.read_text(encoding="utf-8"))

    def test_corrupt_ledger_is_preserved_not_silently_reset(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{{{ truncated", encoding="utf-8")
        s = RunState.load_or_create(self.path, "d")
        self.assertIsNotNone(s.note)
        self.assertTrue(list(self.path.parent.glob("state.json.corrupt.*")))
        self.assertEqual(s.tasks, {})

    def test_config_change_is_detected(self):
        RunState.load_or_create(self.path, "digest-1").save()
        s2 = RunState.load_or_create(self.path, "digest-2")
        self.assertTrue(s2.config_changed())
        s3 = RunState.load_or_create(self.path, "digest-2")
        s3.save()
        self.assertFalse(RunState.load_or_create(self.path, "digest-2").config_changed())

    def test_unknown_fields_in_an_older_ledger_are_ignored(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "version": 1, "created_at": 0, "config_digest": "d",
            "tasks": {"k": {"key": "k", "attempts_record": 1, "from_the_future": True}},
        }), encoding="utf-8")
        s = RunState.load_or_create(self.path, "d")
        self.assertEqual(s.get("k").attempts_record, 1)


if __name__ == "__main__":
    unittest.main()
