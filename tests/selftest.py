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

import hashlib
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
SCHEMA = os.path.join(HERE, "goldens", "golden_schema.json")
SHIPPED_GOLDEN = os.path.join(HERE, "goldens", "pdmlite_seed42_v0.9.golden.json")
ASSET_SUMS = os.path.join(REPO_ROOT, "assets", "SHA256SUMS")

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
def make_agent_repo(root: str, entrypoint: str = "team_code/data_agent.py") -> tuple:
    """A git checkout standing in for the reference agent's repository: one commit, clean."""
    os.makedirs(os.path.join(root, os.path.dirname(entrypoint)), exist_ok=True)
    with open(os.path.join(root, entrypoint), "w", encoding="utf-8") as fh:
        fh.write("# synthetic reference agent\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="selftest", GIT_AUTHOR_EMAIL="selftest@invalid",
               GIT_COMMITTER_NAME="selftest", GIT_COMMITTER_EMAIL="selftest@invalid")
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "agent"]):
        subprocess.run(["git", "-C", root, *cmd], check=True, env=env, capture_output=True)
    sha = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    return root, sha


def write_env_provenance(rep_root: str, python: str = "3.10.15", numpy: str = "1.23.5",
                         scipy: str = "1.14.1", carla="0.9.15",
                         entrypoint: str = "/opt/cg/team_code/data_agent.py") -> None:
    """The file the runner's preflight writes into every output root (schema 1)."""
    d = os.path.join(rep_root, "_runner")
    os.makedirs(d, exist_ok=True)
    doc = {
        "schema": 1,
        "checked_at": "2026-10-03T00:00:00Z",
        "python_executable": "/opt/envs/pdmlite/bin/python3",
        "python_version": python,
        "packages": {"numpy": numpy, "scipy": scipy, "carla": carla, "py_trees": "0.8.3"},
        "agent_entrypoint": entrypoint,
        "agent_import_ok": True,
    }
    with open(os.path.join(d, "env_provenance.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def read_asset_sums() -> list:
    """(name, sha256) pairs from assets/SHA256SUMS, the content pack's source of truth."""
    out = []
    with open(ASSET_SUMS, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                sha, name = line.split(None, 1)
                out.append((name.strip().lstrip("*"), sha))
    return out


class TestMakeGolden(TempCase):
    def setUp(self):
        super().setUp()
        self.agent_repo, self.agent_sha = make_agent_repo(self.path("agent"))

    def rep(self, name: str, mutate=None, provenance: bool = True, **prov) -> str:
        root = build_results(self.path(name), mutate=mutate)
        if provenance:
            write_env_provenance(root, **prov)
        return root

    def _args(self, out, archives=None):
        archives = read_asset_sums() if archives is None else archives
        args = ["--reference-agent", "synthetic",
                "--reference-agent-repo", self.agent_repo,
                "--reference-agent-url", "https://example.invalid/synthetic/carla_garage",
                "--reference-agent-commit", self.agent_sha,
                "--reference-agent-entrypoint", "team_code/data_agent.py",
                "--carla-version", "0.9.15", "--content-pack-version", "v0.9",
                "--routes-root", ROUTES_ROOT, "--out", out]
        for name, sha in archives:
            args += ["--content-pack-archive", f"{name}={sha}"]
        return args

    def build(self, *reps, archives=None, extra=()):
        out = self.path("g", "x.golden.json")
        argv = []
        for r in reps:
            argv += ["--replicate", r]
        p = run(MAKE_GOLDEN, *argv, *self._args(out, archives), *extra)
        doc = None
        if p.returncode == 0:
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
        return p, out, doc

    def test_builds_a_bundle_the_harness_then_accepts(self):
        r1 = self.rep("rep1")
        r2 = self.rep("rep2", mutate=lambda row: {"ds": 99.6})
        p, out, doc = self.build(r1, r2)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(doc["routes"]), 9)
        self.assertAlmostEqual(doc["tolerance"]["max_observed_spread"], 0.4, places=3)
        self.assertAlmostEqual(doc["tolerance"]["driving_score_abs"], 1.0, places=3)
        self.assertEqual(doc["protocol"]["n_replicates"], 2)
        # The bundle it wrote must be usable by the harness against either replicate.
        p2 = check(r1, "--goldens", out)
        self.assertEqual(p2.returncode, EXIT_PASS, p2.stdout + p2.stderr)

    def test_tolerance_is_derived_from_the_measured_spread(self):
        r1 = self.rep("rep1", mutate=lambda row: {"ds": 100.0})
        r2 = self.rep("rep2", mutate=lambda row: {"ds": 96.0})
        p, out, doc = self.build(r1, r2)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertAlmostEqual(doc["tolerance"]["max_observed_spread"], 4.0, places=3)
        self.assertAlmostEqual(doc["tolerance"]["driving_score_abs"], 8.0, places=3)

    def test_refuses_to_mint_a_golden_on_a_broken_install(self):
        r1 = self.rep("rep1")
        r2 = self.rep("rep2", mutate=lambda row: {"agent_type": "unknown"}
                      if row["asset_class"] == "shipped_v0.9" else None)
        p, out, _ = self.build(r1, r2)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("REFUSING TO WRITE", p.stdout)
        self.assertFalse(os.path.exists(out), "nothing may be written on refusal")

    def test_refuses_a_single_replicate_by_default(self):
        p, _, _ = self.build(self.rep("rep1"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("At least 2", p.stdout + p.stderr)

    def test_refuses_when_replicates_disagree_on_status(self):
        r1 = self.rep("rep1")
        r2 = self.rep("rep2", mutate=lambda row: {"status": "Perfect"}
                      if row["category"] == "static" else None)
        p, _, _ = self.build(r1, r2)
        self.assertEqual(p.returncode, 1)
        self.assertIn("disagree on status", p.stdout)

    # ---- HV-07: the reference agent must be retrievable ------------------------------
    def test_stamps_a_retrievable_reference_agent(self):
        p, _, doc = self.build(self.rep("rep1"), self.rep("rep2"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        ra = doc["reference_agent"]
        self.assertEqual(ra["commit"], self.agent_sha)
        self.assertEqual(ra["version"], f"carla_garage@{self.agent_sha} team_code/data_agent.py")
        self.assertEqual(ra["entrypoint"], "team_code/data_agent.py")
        self.assertEqual(ra["repo"], "https://example.invalid/synthetic/carla_garage")
        with open(os.path.join(self.agent_repo, "team_code", "data_agent.py"), "rb") as fh:
            self.assertEqual(ra["entrypoint_sha256"], hashlib.sha256(fh.read()).hexdigest())

    def test_refuses_a_dirty_agent_tree(self):
        # The v0.9 golden ran untracked *_debug.py copies sitting next to the real agent. An
        # untracked file is exactly what `git status --porcelain` catches and a commit sha does not.
        with open(os.path.join(self.agent_repo, "team_code", "data_agent_debug.py"), "w") as fh:
            fh.write("# local edit\n")
        p, out, _ = self.build(self.rep("rep1"), self.rep("rep2"))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("data_agent_debug.py", p.stdout + p.stderr)
        self.assertFalse(os.path.exists(out))

    def test_refuses_an_agent_checkout_at_another_commit(self):
        p, out, _ = self.build(self.rep("rep1"), self.rep("rep2"),
                               extra=("--reference-agent-commit", "0" * 40))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(out))

    def test_refuses_replicates_that_ran_a_different_entrypoint(self):
        r1 = self.rep("rep1", entrypoint="/opt/cg/team_code/data_agent_debug.py")
        p, out, _ = self.build(r1, self.rep("rep2"))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("entrypoint", p.stdout + p.stderr)
        self.assertFalse(os.path.exists(out))

    # ---- HV-04: stamp the interpreter that RAN the routes, not the builder's -----------
    def test_stamps_the_replicates_python_not_the_builders(self):
        p, _, doc = self.build(self.rep("rep1", python="3.10.99"),
                               self.rep("rep2", python="3.10.99"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        env = doc["environment"]
        self.assertEqual(env["python"], "3.10.99")
        self.assertEqual(env["python_packages"],
                         {"numpy": "1.23.5", "scipy": "1.14.1", "carla": "0.9.15"})
        blob = json.dumps(doc)
        self.assertNotIn("/opt/", blob, "no interpreter or agent path may enter the bundle")

    def test_refuses_replicates_with_different_pythons(self):
        p, out, _ = self.build(self.rep("rep1", python="3.10.15"),
                               self.rep("rep2", python="3.8.10"))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("3.8.10", p.stdout + p.stderr)
        self.assertFalse(os.path.exists(out))

    def test_refuses_replicates_with_different_numpy(self):
        p, out, _ = self.build(self.rep("rep1"), self.rep("rep2", numpy="1.23.0"))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(out))

    def test_missing_provenance_requires_an_explicit_python(self):
        r1 = self.rep("rep1", provenance=False)
        r2 = self.rep("rep2", provenance=False)
        p, out, _ = self.build(r1, r2)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("--replicate-python", p.stdout + p.stderr)
        p, out, doc = self.build(r1, r2, extra=("--replicate-python", "3.10.15"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(doc["environment"]["python"], "3.10.15")

    def test_explicit_python_must_agree_with_provenance(self):
        p, out, _ = self.build(self.rep("rep1"), self.rep("rep2"),
                               extra=("--replicate-python", "3.8.10"))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(out))

    # ---- HV-06: one composite digest over several archives, with a stated rule ----------
    def test_composite_is_independent_of_archive_order(self):
        sums = read_asset_sums()
        self.assertGreaterEqual(len(sums), 2)
        p1, _, d1 = self.build(self.rep("rep1"), self.rep("rep2"), archives=sums)
        self.assertEqual(p1.returncode, 0, p1.stdout + p1.stderr)
        p2, _, d2 = self.build(self.rep("rep1"), self.rep("rep2"), archives=sums[::-1])
        self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
        self.assertEqual(d1["environment"]["content_pack_sha256"],
                         d2["environment"]["content_pack_sha256"])
        self.assertEqual(list(d1["environment"]["content_pack_archives"]),
                         sorted(n for n, _ in sums))

    def test_composite_equals_sha256_of_the_shipped_sums_file(self):
        # The rule is "sha256 of the canonical SHA256SUMS text", so feeding the shipped
        # assets/SHA256SUMS must reproduce `sha256sum assets/SHA256SUMS`. If this fails, either
        # the file stopped being canonical (sorted, two spaces, LF) or the rule drifted.
        with open(ASSET_SUMS, "rb") as fh:
            want = hashlib.sha256(fh.read()).hexdigest()
        p, _, doc = self.build(self.rep("rep1"), self.rep("rep2"), archives=read_asset_sums())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(doc["environment"]["content_pack_sha256"], want)

    def test_content_pack_sha256_is_only_a_cross_check(self):
        p, out, _ = self.build(self.rep("rep1"), self.rep("rep2"),
                               extra=("--content-pack-sha256", "f" * 64))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(out))

    def test_new_bundle_validates_against_the_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed (pip install -r requirements-test.txt)")
        p, _, doc = self.build(self.rep("rep1"), self.rep("rep2"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(SCHEMA, encoding="utf-8") as fh:
            jsonschema.validate(doc, json.load(fh))


class TestGoldenSchema(unittest.TestCase):
    def test_shipped_golden_still_validates(self):
        # Provenance fields added after v0.9 are optional, so the bundle measured before they
        # existed must keep validating against the schema.
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed (pip install -r requirements-test.txt)")
        with open(SCHEMA, encoding="utf-8") as fh:
            schema = json.load(fh)
        with open(SHIPPED_GOLDEN, encoding="utf-8") as fh:
            jsonschema.validate(json.load(fh), schema)


class TestGoldenGenerationTemplate(unittest.TestCase):
    """Settings a config copied from the golden template cannot run without."""

    TEMPLATE = os.path.join(HERE, "configs", "golden_generation.yaml.template")

    def _section(self, name: str) -> list:
        """The non-comment lines indented under the top-level key ``name``."""
        lines, inside = [], False
        with open(self.TEMPLATE, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                if not line.startswith(" "):
                    inside = line.startswith(name + ":")
                    continue
                if inside:
                    lines.append(line.rstrip("\n"))
        return lines

    @staticmethod
    def _value(line: str) -> tuple:
        key, _, value = line.strip().partition(":")
        return key, value.split("#")[0].strip().strip('"')

    def test_leaderboard_paths_share_one_bench2drive_root(self):
        """It once set ``work_dir`` to the carla_garage root while ``root`` and
        ``scenario_runner_root`` pointed inside its ``Bench2Drive/`` directory. The evaluator
        reads ``<work_dir>/leaderboard/data/weather.xml`` on every route, so a config copied from
        the template crashed every route before it wrote a result."""
        p = dict(self._value(line) for line in self._section("leaderboard"))
        self.assertEqual(p["root"], p["work_dir"] + "/leaderboard")
        self.assertEqual(p["scenario_runner_root"], p["work_dir"] + "/scenario_runner")

    def test_agent_env_names_the_pdmlite_log_folder(self):
        """The runner exports SAVE_PATH for every route. With it set, setup() in the public
        team_code/autopilot.py names its log folder from TOWN and REPETITION and raises KeyError
        when either is missing, so every route ended "Failed - Agent couldn't be set up" and the
        runner still exited 0. The v0.9 golden never hit it: its agent copies had the line
        edited out."""
        env, inside = {}, False
        for line in self._section("agent"):
            if not line.startswith("    "):
                inside = line.strip() == "env:"
                continue
            if inside:
                key, value = self._value(line)
                env[key] = value
        self.assertEqual(env.get("DATAGEN"), "0")
        self.assertTrue(env.get("TOWN"), "agent.env must set TOWN")
        self.assertTrue(env.get("REPETITION"), "agent.env must set REPETITION")

if __name__ == "__main__":
    unittest.main(verbosity=2)
