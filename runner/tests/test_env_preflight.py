"""The interpreter a route will run under is checked before anything is launched.

The first hardware validation launched CARLA, waited out its start-up, and only then had the
evaluator die on ``import py_trees``: ``environment.python`` was a bare ``python3`` that the
job's activation resolved to a different interpreter than the one the operator had in mind.
Every route paid a simulator start-up to discover a configuration error.

The preflight runs ``environment.python`` once, under the job's own activation, ``agent.env``,
``PYTHONPATH`` and working directory, imports what the evaluator imports, and aborts the sweep
with ``EXIT_CONFIG`` before a single route is attempted if any import fails. On success it
records what it found in ``<output.root>/_runner/env_provenance.json``, including a fingerprint
of the agent's git checkout (``agent_code``) that the golden builder compares with the
reference checkout.

These tests run it for real, against stub packages. The interpreter is ``sys.executable -S``
behind a wrapper, so the host's own site-packages cannot satisfy (or shadow) an import.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oodbench import EXIT_CONFIG, EXIT_OK, envcheck

from tests.test_integration_local import IntegrationBase

PROVENANCE_KEYS = {"schema", "checked_at", "python_executable", "python_version", "packages",
                   "agent_entrypoint", "agent_import_ok", "agent_env", "agent_code"}
AGENT_CODE_KEYS = {"entrypoint_sha256", "entrypoint_rel", "git_head", "scope", "scope_clean",
                   "scope_dirty", "git_error"}


class PreflightBase(IntegrationBase):

    def _isolated_env(self, missing=(), agent_source="", shadow_carla=False):
        """A ``-S`` interpreter wrapper plus a stub tree holding everything but ``missing``.

        ``shadow_carla`` puts another ``carla.py`` on the path in front of the stub tree, the way
        a CARLA egg on ``PYTHONPATH`` shadows a pip-installed wheel.
        """
        root = self.site.root
        stubs = root / "preflight_stubs"
        stubs.mkdir()
        sources = {
            # metadata present, owning the imported file, and different from __version__:
            # metadata must win.
            "carla": '__version__ = "module-attr"\n',
            # no metadata: falls back to __version__.
            "numpy": '__version__ = "1.0+stub"\n',
            # neither: null.
            "scipy": "",
            "py_trees": '__version__ = "2.1+stub"\n',
        }
        for name, src in sources.items():
            if name not in missing:
                (stubs / f"{name}.py").write_text(src, encoding="utf-8")
        if "carla" not in missing:
            info = stubs / "carla-9.9.9.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: carla\nVersion: 9.9.9\n", encoding="utf-8")
            (info / "RECORD").write_text(
                "carla.py,,\ncarla-9.9.9.dist-info/METADATA,,\ncarla-9.9.9.dist-info/RECORD,,\n",
                encoding="utf-8")
        pythonpath = [str(stubs)]
        if shadow_carla:
            shadow = root / "preflight_shadow"
            shadow.mkdir()
            (shadow / "carla.py").write_text('__version__ = "shadow-copy"\n', encoding="utf-8")
            pythonpath.insert(0, str(shadow))
        wrapper = root / "py-nosite"
        wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} -S "$@"\n', encoding="utf-8")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        (root / "agent.py").write_text(agent_source, encoding="utf-8")
        return self.site.config(environment={"python": str(wrapper)},
                                agent={"pythonpath": pythonpath})

    def provenance(self):
        return json.loads((self.site.out / "_runner" / "env_provenance.json").read_text())


class TestEnvPreflight(PreflightBase):

    def test_a_missing_import_aborts_before_any_route_is_attempted(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self._isolated_env(missing=("py_trees",))

        with self.assertLogs("oodbench", level="ERROR") as logs:
            code = self.run_cli(cfg)

        self.assertEqual(code, EXIT_CONFIG)
        self.assertEqual(self.site.trace_rows(), [], "a route was launched anyway")
        joined = "\n".join(logs.output)
        self.assertIn("py_trees", joined)
        self.assertIn("--skip-env-preflight", joined)
        self.assertFalse((self.site.out / "_runner" / "env_provenance.json").exists())

    def test_an_agent_that_fails_to_import_aborts(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self._isolated_env(agent_source="import does_not_exist_anywhere\n")

        with self.assertLogs("oodbench", level="ERROR") as logs:
            code = self.run_cli(cfg)

        self.assertEqual(code, EXIT_CONFIG)
        self.assertEqual(self.site.trace_rows(), [])
        self.assertIn("does_not_exist_anywhere", "\n".join(logs.output))

    def test_success_writes_provenance_in_the_documented_shape(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self._isolated_env()

        self.assertEqual(self.run_cli(cfg), EXIT_OK)

        prov = self.provenance()
        self.assertEqual(set(prov), PROVENANCE_KEYS)
        self.assertEqual(prov["schema"], 2)
        self.assertTrue(prov["checked_at"].endswith("Z"), prov["checked_at"])
        self.assertEqual(os.path.realpath(prov["python_executable"]),
                         os.path.realpath(sys.executable))
        self.assertEqual(prov["python_version"],
                         "%d.%d.%d" % sys.version_info[:3])
        self.assertEqual(prov["packages"], {"numpy": "1.0+stub", "scipy": None,
                                            "carla": "9.9.9", "py_trees": "2.1+stub"})
        self.assertEqual(prov["agent_entrypoint"], str(self.site.root / "agent.py"))
        self.assertIs(prov["agent_import_ok"], True)
        # What the routes got as agent.env: the golden builder checks DATAGEN against it.
        self.assertEqual(prov["agent_env"], {"FAKE_TRACE": str(self.site.trace),
                                             "FAKE_COUNTER": str(self.site.counter)})
        self.assertEqual(set(prov["agent_code"]), AGENT_CODE_KEYS)
        self.assertEqual(prov["agent_code"]["entrypoint_sha256"],
                         hashlib.sha256((self.site.root / "agent.py").read_bytes()).hexdigest())

    def test_an_agent_outside_git_still_passes_without_a_fingerprint(self):
        """Only golden building needs the fingerprint: a plain run must not fail for want of
        one. The ceiling stops git from finding a repository above the test's temp dir."""
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self._isolated_env()

        ceiling = {"GIT_CEILING_DIRECTORIES": str(self.site.root.parent)}
        with mock.patch.dict(os.environ, ceiling), \
                self.assertLogs("oodbench", level="WARNING") as logs:
            self.assertEqual(self.run_cli(cfg), EXIT_OK)

        code = self.provenance()["agent_code"]
        self.assertIsNone(code["git_head"])
        self.assertIsNone(code["scope_clean"])
        self.assertIn("not a git repository", code["git_error"])
        self.assertTrue(any("fingerprint" in line for line in logs.output), logs.output)

    def test_a_shadowed_distribution_does_not_report_its_version(self):
        """RED BEFORE THE FIX: the version came from whichever distribution was INSTALLED under
        that name, even when the module actually imported was another copy found first on the
        path -- a CARLA egg on PYTHONPATH in front of a pip-installed wheel, say. The provenance
        then named a version that never ran."""
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self._isolated_env(shadow_carla=True)

        self.assertEqual(self.run_cli(cfg), EXIT_OK)

        self.assertEqual(self.provenance()["packages"]["carla"], "shadow-copy")

    def test_skip_flag_bypasses_the_check(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self._isolated_env(missing=("py_trees",))

        code = self.run_cli(cfg, extra=["--skip-env-preflight"])

        # The fake evaluator imports none of these, so the route itself runs.
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(self.site.trace_rows()), 1)
        self.assertFalse((self.site.out / "_runner" / "env_provenance.json").exists())
        rep = json.loads((self.site.out / "_runner" / "report.json").read_text())
        self.assertTrue(any("--skip-env-preflight" in w for w in rep["warnings"]),
                        "skipping the check must be recorded in the report")

    def test_dry_run_does_not_run_it(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self._isolated_env(missing=("py_trees",))

        self.assertEqual(self.run_cli(cfg, extra=["--dry-run"]), 0)
        self.assertFalse((self.site.out / "_runner" / "env_provenance.json").exists())


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@invalid")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True).stdout


class TestAgentFingerprint(unittest.TestCase):
    """``agent_code``: which code the agent was, in the golden builder's terms.

    The golden builder used to accept any replicate whose agent path merely ENDED in the
    reference entrypoint, so a run of another clone (or of an edited copy) was stamped as the
    clean reference checkout. The fingerprint ties a run to one commit of one clean tree.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oodbench_fp_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Never let git find a repository above the temp dir.
        patcher = mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.tmp / "agent_repo"
        (self.repo / "team_code").mkdir(parents=True)
        self.entry = self.repo / "team_code" / "data_agent.py"
        self.entry.write_text("from autopilot import AutoPilot\n", encoding="utf-8")
        (self.repo / "team_code" / "autopilot.py").write_text("class AutoPilot: pass\n",
                                                             encoding="utf-8")
        (self.repo / "README.md").write_text("agent\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        _git(self.repo, "init", "-q")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "agent")
        self.head = _git(self.repo, "rev-parse", "HEAD").strip()

    def fingerprint(self):
        return envcheck.agent_fingerprint(str(self.entry))

    def test_a_clean_checkout(self):
        code = self.fingerprint()
        self.assertEqual(set(code), AGENT_CODE_KEYS)
        self.assertEqual(code["entrypoint_sha256"],
                         hashlib.sha256(self.entry.read_bytes()).hexdigest())
        self.assertEqual(code["entrypoint_rel"], "team_code/data_agent.py")
        self.assertEqual(code["git_head"], self.head)
        self.assertEqual(code["scope"], "team_code")
        self.assertIs(code["scope_clean"], True)
        self.assertEqual(code["scope_dirty"], [])
        self.assertIsNone(code["git_error"])

    def test_an_edit_to_an_imported_file_makes_the_scope_dirty(self):
        # The entrypoint's own hash cannot see this: the driving code is in autopilot.py.
        before = self.fingerprint()["entrypoint_sha256"]
        with open(self.repo / "team_code" / "autopilot.py", "a", encoding="utf-8") as fh:
            fh.write("# local edit\n")
        code = self.fingerprint()
        self.assertEqual(code["entrypoint_sha256"], before)
        self.assertIs(code["scope_clean"], False)
        self.assertEqual(code["scope_dirty"], [" M team_code/autopilot.py"])

    def test_an_untracked_file_in_scope_makes_it_dirty(self):
        (self.repo / "team_code" / "data_agent_debug.py").write_text("# copy\n", encoding="utf-8")
        code = self.fingerprint()
        self.assertIs(code["scope_clean"], False)
        self.assertEqual(code["scope_dirty"], ["?? team_code/data_agent_debug.py"])

    def test_ignored_files_do_not_count(self):
        (self.repo / "team_code" / "__pycache__").mkdir()
        (self.repo / "team_code" / "__pycache__" / "x.pyc").write_bytes(b"\0")
        self.assertIs(self.fingerprint()["scope_clean"], True)

    def test_a_change_outside_the_scope_does_not_count(self):
        # The same scope rule as the golden builder's check of the reference checkout.
        with open(self.repo / "README.md", "a", encoding="utf-8") as fh:
            fh.write("edited\n")
        (self.repo / "notes.txt").write_text("untracked\n", encoding="utf-8")
        code = self.fingerprint()
        self.assertIs(code["scope_clean"], True)
        self.assertEqual(code["git_head"], self.head)

    def test_an_entrypoint_at_the_repo_top_scopes_the_whole_tree(self):
        top = self.repo / "agent.py"
        top.write_text("# agent\n", encoding="utf-8")
        _git(self.repo, "add", "agent.py")
        _git(self.repo, "commit", "-q", "-m", "top")
        (self.repo / "notes.txt").write_text("untracked\n", encoding="utf-8")
        code = envcheck.agent_fingerprint(str(top))
        self.assertEqual(code["entrypoint_rel"], "agent.py")
        self.assertEqual(code["scope"], ".")
        self.assertIs(code["scope_clean"], False)

    def test_outside_git_records_why_instead_of_failing(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "agent.py").write_text("# agent\n", encoding="utf-8")
        code = envcheck.agent_fingerprint(str(plain / "agent.py"))
        self.assertEqual(set(code), AGENT_CODE_KEYS)
        self.assertEqual(code["entrypoint_sha256"],
                         hashlib.sha256(b"# agent\n").hexdigest())
        for key in ("git_head", "entrypoint_rel", "scope", "scope_clean", "scope_dirty"):
            self.assertIsNone(code[key], key)
        self.assertIn("not a git repository", code["git_error"])

    def test_a_missing_git_is_recorded_not_raised(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp / "no_such_bin")}):
            code = self.fingerprint()
        self.assertIsNone(code["git_head"])
        self.assertEqual(code["git_error"], "git is not installed")

    def test_no_absolute_path_is_recorded(self):
        with open(self.repo / "team_code" / "autopilot.py", "a", encoding="utf-8") as fh:
            fh.write("# local edit\n")
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "agent.py").write_text("# agent\n", encoding="utf-8")
        for code in (self.fingerprint(), envcheck.agent_fingerprint(str(plain / "agent.py"))):
            blob = json.dumps(code)
            self.assertNotIn(str(self.tmp), blob)
            self.assertNotIn(os.path.realpath(str(self.tmp)), blob)
            self.assertNotIn('"/', blob, "no value may start with an absolute path")

    def test_paths_in_git_errors_are_masked(self):
        err = subprocess.CompletedProcess(
            [], 128, stdout="",
            stderr=f"fatal: detected dubious ownership in repository at '{self.repo}'\n"
                   f"To add an exception for this directory, call:\n")
        with mock.patch.object(envcheck.subprocess, "run", return_value=err):
            code = self.fingerprint()
        self.assertIn("dubious ownership", code["git_error"])
        self.assertNotIn(str(self.tmp), code["git_error"])
        self.assertIsNone(code["scope_clean"])


class TestPreflightScript(PreflightBase):

    def test_script_uses_the_job_environment(self):
        from oodbench import config as config_mod
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = config_mod.load(self.site.config(
            environment={"python": sys.executable, "activate": ["source /opt/env/activate"]},
            agent={"working_dir": str(self.site.root)}))
        script = envcheck.render(cfg)
        # Activation, then agent.env, then paths, then cd -- the route jobscript's order.
        order = [script.index("source /opt/env/activate"), script.index("FAKE_TRACE"),
                 script.index("export PYTHONPATH="), script.index(f"cd {self.site.root}")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("CUDA_VISIBLE_DEVICES", script)
        self.assertIn(sys.executable, script)


if __name__ == "__main__":
    unittest.main()
