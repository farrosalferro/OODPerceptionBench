"""Route discovery, path mirroring, manifest integrity, and the resume/budget decision."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from oodbench import plan
from oodbench.plan import Decision, RouteTask


def make_tree(root: Path):
    """A miniature of the canonical layout: <category>/<scenario>/<level>/route_*.xml."""
    files = [
        "static/construction_obstacle/base/route_1_a.xml",
        "static/construction_obstacle/visual_shift/route_2_b.xml",
        "static/construction_obstacle/geometric_shift/route_3_c.xml",
        "vehicle/accident_two_ways/base/route_4_d.xml",
    ]
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"<routes><!-- {rel} --></routes>", encoding="utf-8")
    return files


class TestDiscovery(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "routes"
        self.root.mkdir()
        self.files = make_tree(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_all_and_orders_deterministically(self):
        a = plan.discover(self.root)
        b = plan.discover(self.root)
        self.assertEqual(len(a), 4)
        self.assertEqual([p.name for p in a], [p.name for p in b])
        rels = [p.relative_to(self.root).as_posix() for p in a]
        self.assertEqual(rels, sorted(rels))

    def test_empty_tree_is_an_error(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        with self.assertRaises(plan.PlanError):
            plan.discover(empty)

    def test_scaffolding_is_warned_about_not_silently_filtered(self):
        extra = self.root / "static_smoke" / "s" / "l" / "route_9_x.xml"
        extra.parent.mkdir(parents=True)
        extra.write_text("<routes/>", encoding="utf-8")
        xmls = plan.discover(self.root)
        self.assertEqual(len(xmls), 5, "scaffolding must not be silently dropped")
        warns = plan.scaffolding_warnings(xmls, self.root)
        self.assertTrue(any("static_smoke" in w for w in warns))


class TestPathMirroring(unittest.TestCase):
    """The {scenario}/{level}/ component must be structurally impossible to drop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "routes"
        self.root.mkdir()
        make_tree(self.root)
        self.out = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_result_path_preserves_every_intermediate_component(self):
        xml = self.root / "static/construction_obstacle/visual_shift/route_2_b.xml"
        t = RouteTask.from_xml(xml, self.root, self.out, seed=42, repetition=0)
        self.assertEqual(
            t.result_path,
            self.out.resolve() / "static/construction_obstacle/visual_shift/results"
            / "route_2_b_seed42.json")
        self.assertEqual(t.rel_dir.as_posix(), "static/construction_obstacle/visual_shift")

    def test_pointing_routes_at_a_category_still_keeps_scenario_and_level(self):
        sub = self.root / "static"
        xml = sub / "construction_obstacle/visual_shift/route_2_b.xml"
        t = RouteTask.from_xml(xml, sub, self.out, seed=42, repetition=0)
        self.assertEqual(t.rel_dir.as_posix(), "construction_obstacle/visual_shift")
        self.assertIn("construction_obstacle/visual_shift", str(t.result_path))

    def test_seed_is_in_the_result_filename(self):
        xml = self.root / "vehicle/accident_two_ways/base/route_4_d.xml"
        t42 = RouteTask.from_xml(xml, self.root, self.out, seed=42, repetition=0)
        t43 = RouteTask.from_xml(xml, self.root, self.out, seed=43, repetition=1)
        self.assertNotEqual(t42.result_path, t43.result_path)
        self.assertTrue(t42.result_path.name.endswith("_seed42.json"))

    def test_keys_are_unique_across_the_tree(self):
        xmls = plan.discover(self.root)
        tasks = plan.build_tasks(xmls, self.root, self.out, base_seed=42, repetitions=1)
        keys = [t.key for t in tasks]
        self.assertEqual(len(keys), len(set(keys)))

    def test_job_scripts_and_logs_mirror_too_so_they_cannot_collide(self):
        xmls = plan.discover(self.root)
        tasks = plan.build_tasks(xmls, self.root, self.out, base_seed=42, repetitions=1)
        scripts = [t.job_script for t in tasks]
        self.assertEqual(len(scripts), len(set(scripts)))
        logs = [t.stdout_path for t in tasks]
        self.assertEqual(len(logs), len(set(logs)))

    def test_seed_is_a_function_of_repetition_only(self):
        xmls = plan.discover(self.root)
        tasks = plan.build_tasks(xmls, self.root, self.out, base_seed=42, repetitions=3)
        self.assertEqual(len(tasks), 12)
        for t in tasks:
            self.assertEqual(t.seed, 42 + t.repetition)
        plan.assert_seed_consistency(tasks, 42, 3)


class TestManifest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "routes"
        self.root.mkdir()
        self.files = make_tree(self.root)
        self.manifest = Path(self.tmp.name) / "MANIFEST.tsv"
        self._write_manifest(self.files)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_manifest(self, rels):
        rows = ["path\tsha256\tcategory"]
        for rel in rels:
            sha = hashlib.sha256((self.root / rel).read_bytes()).hexdigest()
            rows.append(f"{rel}\t{sha}\t{rel.split('/')[0]}")
        self.manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def test_matching_tree_passes(self):
        check = plan.check_manifest(plan.discover(self.root), self.root, self.manifest)
        self.assertTrue(check.ok, check.summary())
        self.assertEqual(check.total_expected, 4)

    def test_edited_route_is_detected(self):
        # No name heuristic could ever catch this, and an edited XML silently changes the
        # benchmark definition.
        target = self.root / self.files[0]
        target.write_text("<routes><!-- tampered --></routes>", encoding="utf-8")
        check = plan.check_manifest(plan.discover(self.root), self.root, self.manifest)
        self.assertFalse(check.ok)
        self.assertEqual(check.modified, [self.files[0]])

    def test_missing_and_extra_are_detected(self):
        (self.root / self.files[0]).unlink()
        extra = self.root / "vehicle/accident_two_ways/base/route_99_z.xml"
        extra.write_text("<routes/>", encoding="utf-8")
        check = plan.check_manifest(plan.discover(self.root), self.root, self.manifest)
        self.assertEqual(check.missing, [self.files[0]])
        self.assertEqual(len(check.extra), 1)


class TestDecide(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "routes"
        self.root.mkdir()
        make_tree(self.root)
        self.out = Path(self.tmp.name) / "out"
        xml = self.root / "vehicle/accident_two_ways/base/route_4_d.xml"
        self.task = RouteTask.from_xml(xml, self.root, self.out, seed=42, repetition=0)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_result(self, status, progress=(1, 1)):
        p = self.task.result_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"_checkpoint": {
            "progress": list(progress),
            "records": [{"status": status, "scores": {"score_composed": 1.0}}]}}),
            encoding="utf-8")

    def _decide(self, attempts=None, mode="skip_terminal", record=3, tick=0, infra=3, killed=2):
        # `killed_budget` is required and has no default (DESIGN.md 6A.9): there is no safe
        # value to fall back to, so the caller must say. None of the cases below charge the
        # ambiguity axis, so the gate cannot fire and every assertion means what it did.
        return plan.decide(self.task, attempts or {}, mode,
                           record_budget=record, tickruntime_budget=tick, infra_budget=infra,
                           killed_budget=killed)

    def test_no_result_runs(self):
        self.assertIs(self._decide().decision, Decision.RUN)

    def test_completed_is_skipped(self):
        self._write_result("Completed")
        self.assertIs(self._decide().decision, Decision.SKIP_DONE)

    def test_tickruntime_with_zero_budget_is_skipped_not_rerun(self):
        self._write_result("Failed - TickRuntime")
        self.assertIs(self._decide().decision, Decision.SKIP_EXHAUSTED)

    def test_tickruntime_with_budget_is_rerun(self):
        self._write_result("Failed - TickRuntime")
        self.assertIs(self._decide(tick=2).decision, Decision.RUN)

    def test_crash_is_rerun_under_skip_terminal(self):
        self._write_result("Failed - Agent crashed")
        self.assertIs(self._decide().decision, Decision.RUN)

    def test_crash_is_skipped_under_skip_any_final(self):
        self._write_result("Failed - Agent crashed")
        self.assertIs(self._decide(mode="skip_any_final").decision, Decision.SKIP_DONE)

    def test_persisted_budget_is_honoured_across_restarts(self):
        # Three interrupted restarts must not buy three times the budget.
        self._write_result("Failed - Agent crashed")
        self.assertIs(self._decide(attempts={"record": 3}).decision, Decision.SKIP_EXHAUSTED)
        self.assertIs(self._decide(attempts={"record": 2}).decision, Decision.RUN)

    def test_infra_budget_is_separate_from_record_budget(self):
        # No result on disk at all: only the infra budget applies, and a spent record budget
        # must not block it (nor vice versa).
        self.assertIs(self._decide(attempts={"record": 99}).decision, Decision.RUN)
        self.assertIs(self._decide(attempts={"infra": 3}).decision, Decision.SKIP_EXHAUSTED)

    def test_sensors_invalid_is_fatal(self):
        self._write_result("Failed - Agent's sensors were invalid")
        self.assertIs(self._decide().decision, Decision.FATAL)

    def test_unfinalized_result_runs_again(self):
        self._write_result("Completed", progress=(0, 1))
        self.assertIs(self._decide().decision, Decision.RUN)

    def test_mode_none_always_runs(self):
        self._write_result("Completed")
        self.assertIs(self._decide(mode="none").decision, Decision.RUN)


if __name__ == "__main__":
    unittest.main()
