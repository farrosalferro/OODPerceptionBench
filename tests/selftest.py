#!/usr/bin/env python3
"""Self-tests for the acceptance harness itself.

Bundle version : v0.9
Binds to       : arXiv v1

The harness is the thing that decides whether a benchmark install is trustworthy. If it rots,
it rots silently -- a harness that has stopped detecting a missing asset looks exactly like a
harness reporting good news. So the harness gets its own tests, and they run in CI on every
push, where nothing else in ``tests/`` can.

These build synthetic leaderboard checkpoints on disk and drive the real scripts as
subprocesses, asserting on exit codes and on the JSON report. No CARLA, no GPU, no network, no
third-party packages.

    python3 selftest.py            # or: python3 -m unittest selftest -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
ROUTES_ROOT = os.path.join(REPO_ROOT, "routes")
SPLIT = os.path.join(HERE, "smoke", "SMOKE_SPLIT.tsv")
CHECK = os.path.join(HERE, "check_acceptance.py")
MAKE_GOLDEN = os.path.join(HERE, "make_golden.py")
MATERIALIZE = os.path.join(HERE, "smoke", "materialize.py")

PY = sys.executable or "python3"

EXIT_PASS, EXIT_FAIL, EXIT_ERROR, EXIT_INCONCLUSIVE = 0, 1, 2, 3


# ---------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------
def read_split(tier: str = "all") -> list:
    rows, header = [], None
    with open(SPLIT, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            f = line.split("\t")
            if header is None:
                header = f
                continue
            row = dict(zip(header, f))
            if tier == "core" and row["tier"] != "core":
                continue
            rows.append(row)
    return rows


def make_checkpoint(stem: str, agent_type, status: str = "Completed", ds: float = 100.0,
                    with_ttr: bool = True, final: bool = True) -> dict:
    """A leaderboard checkpoint shaped like the real ones (see records/SCHEMA.md)."""
    record = {
        "index": 0,
        "route_id": f"RouteScenario_{stem}_rep0",
        "scenario_name": "SyntheticScenario_1",
        "weather_id": "ClearNoon",
        "save_name": stem,
        "status": status,
        "num_infractions": 0,
        "infractions": {
            "collisions_layout": [], "collisions_pedestrian": [], "collisions_vehicle": [],
            "red_light": [], "stop_infraction": [], "outside_route_lanes": [],
            "min_speed_infractions": [], "yield_emergency_vehicle_infractions": [],
            "scenario_timeouts": [], "route_dev": [], "vehicle_blocked": [],
            "route_timeout": [],
        },
        "scores": {"score_route": 100, "score_penalty": round(ds / 100.0, 6),
                   "score_composed": ds},
        "meta": {"route_length": 132.1, "duration_game": 13.0, "duration_system": 12.0},
        "town_name": "Town02",
    }
    if with_ttr:
        record["ttr_dar"] = {
            "ttr": 12.3, "dar": 4.5, "reaction_detected": True,
            "agent_type": agent_type,
            "t_obs_frame": 100, "t_react_frame": 200,
        }
        record["infractions"]["ttr_dar"] = ["TTR/DAR measurement recorded"]
    return {
        "_checkpoint": {
            "global_record": {},
            "progress": [1, 1] if final else [0, 1],
            "records": [record],
        },
        "entry_status": "Finished",
        "eligible": True,
        "sensors": [],
        "values": [],
        "labels": [],
    }


def build_results(out_root: str, tier: str = "all", *, mutate=None) -> str:
    """Materialise a synthetic result tree for the whole split.

    ``mutate(row) -> dict | None`` may return keyword overrides for ``make_checkpoint``, or
    the sentinel ``"OMIT"`` to leave the route's result file out entirely.
    """
    for row in read_split(tier):
        stem = os.path.splitext(os.path.basename(row["path"]))[0]
        kwargs = {"agent_type": row["prop_blueprint_id"]}
        if mutate is not None:
            over = mutate(row)
            if over == "OMIT":
                continue
            if over:
                kwargs.update(over)
        rel_dir = os.path.dirname(row["path"])
        d = os.path.join(out_root, rel_dir, "results")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{stem}_seed42.json"), "w", encoding="utf-8") as fh:
            json.dump(make_checkpoint(stem, **kwargs), fh)
    return out_root


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, script, *args], capture_output=True, text=True, cwd=HERE)


def check(results_root: str, *extra: str, tier: str = "all", report: str = None):
    args = ["--results-root", results_root, "--tier", tier, "--routes-root", ROUTES_ROOT]
    if report:
        args += ["--json", report]
    args += list(extra)
    return run(CHECK, *args)


def load_report(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def verdicts(report: dict, name_prefix: str) -> set:
    out = set()
    for r in report["routes"]:
        for a in r["assertions"]:
            if a["name"].startswith(name_prefix):
                out.add(a["verdict"])
    return out


# ---------------------------------------------------------------------------------------
class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="oodbench_acceptance_selftest_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *p):
        return os.path.join(self.tmp, *p)


# ---------------------------------------------------------------------------------------
class TestSplitIntegrity(TempCase):
    """The split must describe the frozen route tree, exactly."""

    def test_split_matches_frozen_routes(self):
        p = run(MATERIALIZE, "--verify-only", "--routes-root", ROUTES_ROOT)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_tiers_have_the_documented_sizes(self):
        self.assertEqual(len(read_split("all")), 9)
        self.assertEqual(len(read_split("core")), 6)

    def test_split_covers_all_six_shipped_assets(self):
        shipped = {r["prop_blueprint_id"] for r in read_split("all")
                   if r["asset_class"] == "shipped_v0.9"}
        self.assertEqual(shipped, {
            "static.prop.concreteroadbarrier",
            "static.prop.roadclosedbarricade",
            "walker.pedestrian.astronaut",
            "walker.pedestrian.firefighter",
            "walker.pedestrian.boar",
            "walker.pedestrian.deliveryrobot",
        }, "the split must exercise every asset shipped in v0.9; a pack missing one of them "
           "would otherwise pass")

    def test_split_names_no_unshipped_asset(self):
        """A v0.9 user cannot install the other twelve, so a route needing one is unrunnable."""
        unshippable = {
            "static.prop.trafficmessageboard", "static.prop.trafficarrowboard",
            "static.prop.europianarrowboardtrailer", "static.prop.roadclosedsign",
            "walker.pedestrian.soldier", "walker.pedestrian.wheelchair",
            "vehicle.ood.sedan", "vehicle.ood.hatchback", "vehicle.ood.suv",
            "vehicle.ood.armoredvan", "vehicle.ood.dumptruck", "vehicle.ood.roadroller",
        }
        used = {r["prop_blueprint_id"] for r in read_split("all")}
        self.assertEqual(used & unshippable, set())

    def test_split_spans_three_categories_and_three_levels(self):
        rows = read_split("all")
        self.assertEqual({r["category"] for r in rows}, {"static", "pedestrian", "vehicle"})
        self.assertEqual({r["level"] for r in rows},
                         {"base", "visual_shift", "geometric_shift"})
        core = read_split("core")
        self.assertEqual({r["category"] for r in core}, {"static", "pedestrian", "vehicle"})
        self.assertEqual({r["level"] for r in core},
                         {"base", "visual_shift", "geometric_shift"})

    def test_materialize_preserves_category_scenario_level(self):
        out = self.path("mat")
        p = run(MATERIALIZE, "--out", out, "--routes-root", ROUTES_ROOT)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for row in read_split("all"):
            self.assertTrue(os.path.isfile(os.path.join(out, row["path"])), row["path"])
        self.assertTrue(os.path.isfile(os.path.join(out, "MANIFEST.tsv")))

    def test_materialize_detects_an_edited_route(self):
        routes = self.path("routes")
        shutil.copytree(ROUTES_ROOT, routes)
        victim = os.path.join(routes, read_split("all")[0]["path"])
        with open(victim, "a", encoding="utf-8") as fh:
            fh.write("<!-- tampered -->\n")
        p = run(MATERIALIZE, "--verify-only", "--routes-root", routes)
        self.assertEqual(p.returncode, 1)
        self.assertIn("MODIFIED", p.stdout)


# ---------------------------------------------------------------------------------------
class TestHarnessWithoutGoldens(TempCase):
    def test_healthy_install_is_inconclusive_not_a_pass(self):
        out = build_results(self.path("run"))
        rep = self.path("report.json")
        p = check(out, "--golden-dir", self.path("no_goldens"), report=rep)
        self.assertEqual(p.returncode, EXIT_INCONCLUSIVE,
                         "a run with no goldens must never exit 0\n" + p.stdout)
        self.assertIn("INCONCLUSIVE", p.stdout)
        r = load_report(rep)
        self.assertEqual(r["verdict"], "INCONCLUSIVE")
        self.assertEqual(verdicts(r, "A1"), {"PASS"})
        self.assertEqual(verdicts(r, "A2"), {"PASS"})
        self.assertEqual(verdicts(r, "A3"), {"PASS"})
        self.assertEqual(verdicts(r, "A4"), {"SKIP"})

    def test_example_golden_is_not_picked_up(self):
        """The shipped EXAMPLE must never be mistaken for a real bundle."""
        out = build_results(self.path("run"))
        golden_dir = self.path("example_only_goldens")
        os.makedirs(golden_dir)
        shutil.copy(os.path.join(HERE, "goldens", "EXAMPLE.golden.json"), golden_dir)
        p = check(out, "--golden-dir", golden_dir)
        self.assertEqual(p.returncode, EXIT_INCONCLUSIVE, p.stdout + p.stderr)


# ---------------------------------------------------------------------------------------
class TestA1SilentFallback(TempCase):
    """A1 is the assertion the whole harness exists for."""

    def test_missing_asset_reported_as_unknown_actor_fails(self):
        """The literal signature of a missing content pack: route completes, no actor."""
        def mutate(row):
            if row["asset_class"] == "shipped_v0.9":
                return {"agent_type": "unknown"}
            return None
        out = build_results(self.path("run"), mutate=mutate)
        rep = self.path("r.json")
        p = check(out, "--golden-dir", self.path("none"), report=rep)
        self.assertEqual(p.returncode, EXIT_FAIL, p.stdout)
        r = load_report(rep)
        bad = [x for x in r["routes"]
               if any(a["name"].startswith("A1") and a["verdict"] == "FAIL"
                      for a in x["assertions"])]
        self.assertEqual(len(bad), 6, "every shipped-asset route must go red")
        # ...and A3/A4-style symptoms are absent: the route "completed" perfectly.
        self.assertEqual(verdicts(r, "A3"), {"PASS"})

    def test_tesla_fallback_fails(self):
        """A registered vehicle blueprint resolving to a different vehicle."""
        def mutate(row):
            if row["category"] == "vehicle":
                return {"agent_type": "vehicle.tesla.model3"}
            return None
        out = build_results(self.path("run"), mutate=mutate)
        rep = self.path("r.json")
        p = check(out, "--golden-dir", self.path("none"), report=rep)
        self.assertEqual(p.returncode, EXIT_FAIL, p.stdout)
        self.assertIn("vehicle.tesla.model3", p.stdout)
        self.assertIn("attribute_filter", p.stdout)

    def test_walker_replaced_by_a_vehicle_fails_with_a_pointed_message(self):
        def mutate(row):
            if row["category"] == "pedestrian":
                return {"agent_type": "vehicle.tesla.model3"}
            return None
        out = build_results(self.path("run"), mutate=mutate)
        p = check(out, "--golden-dir", self.path("none"))
        self.assertEqual(p.returncode, EXIT_FAIL)
        self.assertIn("content pack is not installed", p.stdout)

    def test_absence_of_evidence_is_a_failure_not_a_skip(self):
        """No ttr_dar block => A1 cannot be checked => A1 FAILS. Never SKIP, never PASS."""
        out = build_results(self.path("run"), mutate=lambda row: {"with_ttr": False})
        rep = self.path("r.json")
        p = check(out, "--golden-dir", self.path("none"), report=rep)
        self.assertEqual(p.returncode, EXIT_FAIL, p.stdout)
        r = load_report(rep)
        self.assertEqual(verdicts(r, "A1"), {"FAIL"})
        self.assertEqual(verdicts(r, "A2"), {"FAIL"})
        self.assertIn("UNVERIFIABLE", p.stdout)

    def test_expectation_comes_from_the_xml_not_from_the_split_column(self):
        """Doctoring the split's blueprint column must not lower the bar."""
        split2 = self.path("doctored.tsv")
        with open(SPLIT, encoding="utf-8") as fh:
            text = fh.read()
        text = text.replace("walker.pedestrian.astronaut\tcore",
                            "walker.pedestrian.WRONG\tcore")
        with open(split2, "w", encoding="utf-8") as fh:
            fh.write(text)
        out = build_results(self.path("run"))
        p = run(CHECK, "--results-root", out, "--routes-root", ROUTES_ROOT,
                "--split", split2, "--golden-dir", self.path("none"))
        # verify_split notices the XML and the split disagree, before any assertion runs
        self.assertEqual(p.returncode, EXIT_ERROR, p.stdout + p.stderr)
        self.assertIn("DISAGREE", p.stdout + p.stderr)


# ---------------------------------------------------------------------------------------
class TestA2A3(TempCase):
    def test_status_not_completed_fails_a3(self):
        out = build_results(self.path("run"),
                            mutate=lambda row: {"status": "Failed - Agent got blocked"}
                            if row["category"] == "static" else None)
        rep = self.path("r.json")
        p = check(out, "--golden-dir", self.path("none"), report=rep)
        self.assertEqual(p.returncode, EXIT_FAIL)
        r = load_report(rep)
        self.assertEqual(verdicts(r, "A1"), {"PASS"})
        self.assertIn("FAIL", verdicts(r, "A3"))

    def test_missing_result_file_fails(self):
        out = build_results(self.path("run"),
                            mutate=lambda row: "OMIT" if row["level"] == "base" else None)
        p = check(out, "--golden-dir", self.path("none"))
        self.assertEqual(p.returncode, EXIT_FAIL)
        self.assertIn("no result found", p.stdout)

    def test_unfinalised_checkpoint_fails(self):
        out = build_results(self.path("run"), mutate=lambda row: {"final": False})
        p = check(out, "--golden-dir", self.path("none"))
        self.assertEqual(p.returncode, EXIT_FAIL)
        self.assertIn("not finalised", p.stdout)

    def test_result_from_another_route_fails(self):
        """A checkpoint whose record names a different route must not be accepted."""
        out = self.path("run")
        row = read_split("all")[0]
        stem = os.path.splitext(os.path.basename(row["path"]))[0]
        d = os.path.join(out, os.path.dirname(row["path"]), "results")
        os.makedirs(d)
        doc = make_checkpoint("route_99999_somethingelse", row["prop_blueprint_id"])
        with open(os.path.join(d, f"{stem}_seed42.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        p = check(out, "--tier", "core", "--golden-dir", self.path("none"))
        self.assertEqual(p.returncode, EXIT_FAIL)
        self.assertIn("different route", p.stdout)


# ---------------------------------------------------------------------------------------
def write_golden(path: str, split_sha: str, tier: str = "all", ds: float = 100.0,
                 tolerance: float = 1.0, drop=None, bundle_version: str = "v0.9") -> str:
    rows = read_split(tier)
    routes = {}
    for row in rows:
        if drop and row["path"] == drop:
            continue
        routes[row["path"]] = {
            "route_sha256": row["sha256"],
            "expected_blueprint_id": row["prop_blueprint_id"],
            "observed_agent_type": row["prop_blueprint_id"],
            "status": "Completed",
            "driving_score": ds,
            "route_completion": 100,
            "infraction_penalty": 1.0,
            "replicates": [{"replicate": "a", "status": "Completed", "driving_score": ds}],
            "ds_spread": 0.0,
        }
    doc = {
        "schema": "ood-perceptionbench/golden/1",
        "bundle_version": bundle_version,
        "binds_to": "arXiv v1",
        "reportable": False,
        "split": {"name": "smoke", "tier": tier, "sha256": split_sha, "n_routes": len(rows)},
        "reference_agent": {"name": "synthetic", "version": "selftest"},
        "environment": {"carla_version": "0.9.15", "content_pack_version": "v0.9"},
        "protocol": {"seed": 42, "repetitions": 1, "n_replicates": 1},
        "tolerance": {"driving_score_abs": tolerance, "policy": "selftest fixture"},
        "generated": {"utc": "1970-01-01T00:00:00Z", "by": "selftest"},
        "routes": routes,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


class TestGoldens(TempCase):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, os.path.join(HERE, "smoke"))
        from materialize import sha256_of  # noqa: E402
        self.split_sha = sha256_of(SPLIT)

    def test_healthy_install_with_golden_passes(self):
        out = build_results(self.path("run"))
        g = write_golden(self.path("g", "x.golden.json"), self.split_sha)
        p = check(out, "--goldens", g)
        self.assertEqual(p.returncode, EXIT_PASS, p.stdout + p.stderr)
        self.assertIn("PASSED", p.stdout)

    def test_ds_outside_tolerance_fails(self):
        out = build_results(self.path("run"),
                            mutate=lambda row: {"ds": 42.0} if row["level"] == "base" else None)
        g = write_golden(self.path("g", "x.golden.json"), self.split_sha, tolerance=1.0)
        rep = self.path("r.json")
        p = check(out, "--goldens", g, report=rep)
        self.assertEqual(p.returncode, EXIT_FAIL)
        r = load_report(rep)
        self.assertIn("FAIL", verdicts(r, "A4"))
        self.assertEqual(verdicts(r, "A1"), {"PASS"})

    def test_ds_inside_tolerance_passes(self):
        out = build_results(self.path("run"), mutate=lambda row: {"ds": 99.5})
        g = write_golden(self.path("g", "x.golden.json"), self.split_sha, ds=100.0,
                         tolerance=1.0)
        p = check(out, "--goldens", g)
        self.assertEqual(p.returncode, EXIT_PASS, p.stdout)

    def test_golden_for_a_different_split_is_rejected(self):
        out = build_results(self.path("run"))
        g = write_golden(self.path("g", "x.golden.json"), "0" * 64)
        p = check(out, "--goldens", g)
        self.assertEqual(p.returncode, EXIT_ERROR)
        self.assertIn("DIFFERENT smoke split", p.stdout + p.stderr)

    def test_golden_for_a_different_bundle_version_is_rejected(self):
        out = build_results(self.path("run"))
        g = write_golden(self.path("g", "x.golden.json"), self.split_sha,
                         bundle_version="v1.0")
        p = check(out, "--goldens", g)
        self.assertEqual(p.returncode, EXIT_ERROR)
        self.assertIn("content-pack version", p.stdout + p.stderr)

    def test_partial_golden_is_rejected_rather_than_silently_downgrading(self):
        out = build_results(self.path("run"))
        drop = read_split("all")[0]["path"]
        g = write_golden(self.path("g", "x.golden.json"), self.split_sha, drop=drop)
        p = check(out, "--goldens", g)
        self.assertEqual(p.returncode, EXIT_ERROR)
        self.assertIn("no entry for", p.stdout + p.stderr)

    def test_validate_only_accepts_a_good_bundle_without_any_results(self):
        """The part CI can do: no GPU, no CARLA, no run output."""
        g = write_golden(self.path("g", "x.golden.json"), self.split_sha)
        p = run(CHECK, "--validate-goldens-only", "--routes-root", ROUTES_ROOT, "--goldens", g)
        self.assertEqual(p.returncode, EXIT_PASS, p.stdout + p.stderr)
        self.assertIn("GOLDEN BUNDLE OK", p.stdout)

    def test_validate_only_rejects_a_mismatched_bundle(self):
        g = write_golden(self.path("g", "x.golden.json"), "0" * 64)
        p = run(CHECK, "--validate-goldens-only", "--routes-root", ROUTES_ROOT, "--goldens", g)
        self.assertEqual(p.returncode, EXIT_ERROR)

    def test_validate_only_without_a_bundle_is_not_a_pass(self):
        p = run(CHECK, "--validate-goldens-only", "--routes-root", ROUTES_ROOT,
                "--golden-dir", self.path("empty"))
        self.assertEqual(p.returncode, EXIT_INCONCLUSIVE)

    def test_results_root_is_required_unless_validating_goldens(self):
        p = run(CHECK, "--routes-root", ROUTES_ROOT)
        self.assertEqual(p.returncode, EXIT_ERROR)
        self.assertIn("--results-root is required", p.stderr)

    def test_two_bundles_stop_rather_than_guess(self):
        out = build_results(self.path("run"))
        write_golden(self.path("g", "a.golden.json"), self.split_sha)
        write_golden(self.path("g", "b.golden.json"), self.split_sha)
        p = check(out, "--golden-dir", self.path("g"))
        self.assertEqual(p.returncode, EXIT_ERROR)
        self.assertIn("golden bundles", p.stdout + p.stderr)


# ---------------------------------------------------------------------------------------
class TestMakeGolden(TempCase):
    def _args(self, out):
        return ["--reference-agent", "synthetic", "--reference-agent-version", "selftest",
                "--carla-version", "0.9.15", "--content-pack-version", "v0.9",
                "--routes-root", ROUTES_ROOT, "--out", out]

    def test_builds_a_bundle_the_harness_then_accepts(self):
        r1 = build_results(self.path("rep1"))
        r2 = build_results(self.path("rep2"), mutate=lambda row: {"ds": 99.6})
        out = self.path("g", "x.golden.json")
        p = run(MAKE_GOLDEN, "--replicate", r1, "--replicate", r2, *self._args(out))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(len(doc["routes"]), 9)
        self.assertAlmostEqual(doc["tolerance"]["max_observed_spread"], 0.4, places=3)
        self.assertAlmostEqual(doc["tolerance"]["driving_score_abs"], 1.0, places=3)
        self.assertEqual(doc["protocol"]["n_replicates"], 2)
        # The bundle it wrote must be usable by the harness against either replicate.
        p2 = check(r1, "--goldens", out)
        self.assertEqual(p2.returncode, EXIT_PASS, p2.stdout + p2.stderr)

    def test_tolerance_is_derived_from_the_measured_spread(self):
        r1 = build_results(self.path("rep1"), mutate=lambda row: {"ds": 100.0})
        r2 = build_results(self.path("rep2"), mutate=lambda row: {"ds": 96.0})
        out = self.path("g", "x.golden.json")
        p = run(MAKE_GOLDEN, "--replicate", r1, "--replicate", r2, *self._args(out))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertAlmostEqual(doc["tolerance"]["max_observed_spread"], 4.0, places=3)
        self.assertAlmostEqual(doc["tolerance"]["driving_score_abs"], 8.0, places=3)

    def test_refuses_to_mint_a_golden_on_a_broken_install(self):
        r1 = build_results(self.path("rep1"))
        r2 = build_results(self.path("rep2"),
                           mutate=lambda row: {"agent_type": "unknown"}
                           if row["asset_class"] == "shipped_v0.9" else None)
        out = self.path("g", "x.golden.json")
        p = run(MAKE_GOLDEN, "--replicate", r1, "--replicate", r2, *self._args(out))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("REFUSING TO WRITE", p.stdout)
        self.assertFalse(os.path.exists(out), "nothing may be written on refusal")

    def test_refuses_a_single_replicate_by_default(self):
        r1 = build_results(self.path("rep1"))
        out = self.path("g", "x.golden.json")
        p = run(MAKE_GOLDEN, "--replicate", r1, *self._args(out))
        self.assertEqual(p.returncode, 2)
        self.assertIn("At least 2", p.stdout + p.stderr)

    def test_refuses_when_replicates_disagree_on_status(self):
        r1 = build_results(self.path("rep1"))
        r2 = build_results(self.path("rep2"),
                           mutate=lambda row: {"status": "Perfect"}
                           if row["category"] == "static" else None)
        out = self.path("g", "x.golden.json")
        p = run(MAKE_GOLDEN, "--replicate", r1, "--replicate", r2, *self._args(out))
        self.assertEqual(p.returncode, 1)
        self.assertIn("disagree on status", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
