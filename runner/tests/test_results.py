"""Finalization predicate, status taxonomy, and the record-only half of the resume decision."""

import json
import tempfile
import unittest
from pathlib import Path

from oodbench import results


def write_checkpoint(dirpath, progress, statuses, score=42.0):
    p = Path(dirpath) / "r_seed42.json"
    p.write_text(json.dumps({
        "_checkpoint": {
            "progress": progress,
            "records": [{"status": s, "scores": {"score_composed": score}} for s in statuses],
        }
    }), encoding="utf-8")
    return p


class TestFinalization(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_is_not_final(self):
        rec = results.read(Path(self.dir) / "nope.json")
        self.assertFalse(rec.exists)
        self.assertFalse(rec.final)

    def test_malformed_json_is_not_final(self):
        p = Path(self.dir) / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        rec = results.read(p)
        self.assertTrue(rec.exists)
        self.assertFalse(rec.parsable)
        self.assertFalse(rec.final)

    def test_complete_checkpoint_is_final(self):
        p = write_checkpoint(self.dir, [1, 1], ["Completed"])
        rec = results.read(p)
        self.assertTrue(rec.final)
        self.assertEqual(rec.status, "Completed")
        self.assertEqual(rec.score_composed, 42.0)

    def test_in_progress_checkpoint_is_not_final(self):
        p = write_checkpoint(self.dir, [0, 1], ["Completed"])
        self.assertFalse(results.read(p).final)

    def test_zero_of_zero_progress_is_not_final(self):
        # Stricter than the internal result_is_final(), which accepts 0 >= 0. The difference is
        # one-directional: it can only cause a re-run, never a wrongly skipped route.
        p = write_checkpoint(self.dir, [0, 0], ["Completed"])
        self.assertFalse(results.read(p).final)

    def test_empty_records_is_not_final(self):
        p = Path(self.dir) / "empty.json"
        p.write_text(json.dumps({"_checkpoint": {"progress": [1, 1], "records": []}}),
                     encoding="utf-8")
        self.assertFalse(results.read(p).final)

    def test_integral_float_progress_is_accepted(self):
        # Some JSON writers emit 1.0 rather than 1; rejecting that would re-run every route.
        p = write_checkpoint(self.dir, [1.0, 1.0], ["Completed"])
        self.assertTrue(results.read(p).final)

    def test_boolean_progress_is_rejected(self):
        p = write_checkpoint(self.dir, [True, True], ["Completed"])
        self.assertFalse(results.read(p).final)

    def test_missing_checkpoint_key_is_not_final(self):
        p = Path(self.dir) / "nock.json"
        p.write_text(json.dumps({"something": "else"}), encoding="utf-8")
        self.assertFalse(results.read(p).final)


class TestTaxonomy(unittest.TestCase):

    def test_every_observed_status_is_known(self):
        # Statuses actually seen in the published seed-42 sweep. An unknown one here would mean
        # the runner silently classifies a real outcome as "unknown".
        observed = [
            "Completed", "Failed", "Failed - TickRuntime",
            "Failed - Agent got blocked", "Failed - Agent deviated from the route",
            "Failed - Agent crashed",
        ]
        for s in observed:
            self.assertIn(s, results.KNOWN_STATUSES, s)

    def test_accept_statuses_are_never_retried(self):
        for s in results.ACCEPT_STATUSES:
            self.assertIs(results.classify(s), results.Disposition.ACCEPT, s)

    def test_retry_set_matches_the_internal_orchestrator(self):
        self.assertEqual(results.RETRY_STATUSES, frozenset({
            "Failed",
            "Failed - Agent couldn't be set up",
            "Failed - Simulation crashed",
            "Failed - Agent crashed",
        }))

    def test_tickruntime_has_its_own_disposition(self):
        self.assertIs(results.classify("Failed - TickRuntime"),
                      results.Disposition.RETRY_TICKRUNTIME)

    def test_sensors_invalid_is_fatal(self):
        self.assertIs(results.classify("Failed - Agent's sensors were invalid"),
                      results.Disposition.FATAL)

    def test_unknown_status_is_flagged_not_absorbed(self):
        self.assertIs(results.classify("Failed - Some Future Thing"),
                      results.Disposition.UNKNOWN)

    def test_disposition_sets_are_disjoint(self):
        sets = [results.ACCEPT_STATUSES, results.RETRY_STATUSES,
                results.TICKRUNTIME_STATUSES, results.FATAL_STATUSES]
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                self.assertEqual(sets[i] & sets[j], frozenset())


class TestResumePredicate(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _rec(self, status, progress=(1, 1)):
        return results.read(write_checkpoint(self.dir, list(progress), [status]))

    def test_skip_terminal_skips_a_completed_route(self):
        self.assertTrue(results.should_skip_on_resume(self._rec("Completed"), "skip_terminal"))

    def test_skip_terminal_does_not_skip_a_crashed_route(self):
        # This is the whole point of the default: under skip_any_final an interrupted sweep
        # would accept this crash as the published result without ever retrying it.
        self.assertFalse(
            results.should_skip_on_resume(self._rec("Failed - Agent crashed"), "skip_terminal"))

    def test_skip_any_final_skips_a_crashed_route(self):
        self.assertTrue(
            results.should_skip_on_resume(self._rec("Failed - Agent crashed"), "skip_any_final"))

    def test_no_mode_skips_an_unfinalized_route(self):
        rec = self._rec("Completed", progress=(0, 1))
        for mode in results.VALID_RESUME_MODES:
            self.assertFalse(results.should_skip_on_resume(rec, mode), mode)

    def test_none_never_skips(self):
        self.assertFalse(results.should_skip_on_resume(self._rec("Completed"), "none"))

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            results.should_skip_on_resume(self._rec("Completed"), "whatever")


if __name__ == "__main__":
    unittest.main()
