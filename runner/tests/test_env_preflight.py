"""The interpreter a route will run under is checked before anything is launched.

The first hardware validation launched CARLA, waited out its start-up, and only then had the
evaluator die on ``import py_trees``: ``environment.python`` was a bare ``python3`` that the
job's activation resolved to a different interpreter than the one the operator had in mind.
Every route paid a simulator start-up to discover a configuration error.

The preflight runs ``environment.python`` once, under the job's own activation, ``agent.env``,
``PYTHONPATH`` and working directory, imports what the evaluator imports, and aborts the sweep
with ``EXIT_CONFIG`` before a single route is attempted if any import fails. On success it
records what it found in ``<output.root>/_runner/env_provenance.json``.

These tests run it for real, against stub packages. The interpreter is ``sys.executable -S``
behind a wrapper, so the host's own site-packages cannot satisfy (or shadow) an import.
"""

import json
import os
import stat
import sys
import unittest
from pathlib import Path

from oodbench import EXIT_CONFIG, EXIT_OK, envcheck

from tests.test_integration_local import IntegrationBase

PROVENANCE_KEYS = {"schema", "checked_at", "python_executable", "python_version", "packages",
                   "agent_entrypoint", "agent_import_ok"}


class PreflightBase(IntegrationBase):

    def _isolated_env(self, missing=(), agent_source=""):
        """A ``-S`` interpreter wrapper plus a stub tree holding everything but ``missing``."""
        root = self.site.root
        stubs = root / "preflight_stubs"
        stubs.mkdir()
        sources = {
            # metadata present and different from __version__: metadata must win.
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
        wrapper = root / "py-nosite"
        wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} -S "$@"\n', encoding="utf-8")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        (root / "agent.py").write_text(agent_source, encoding="utf-8")
        return self.site.config(environment={"python": str(wrapper)},
                                agent={"pythonpath": [str(stubs)]})

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
        self.assertEqual(prov["schema"], 1)
        self.assertTrue(prov["checked_at"].endswith("Z"), prov["checked_at"])
        self.assertEqual(os.path.realpath(prov["python_executable"]),
                         os.path.realpath(sys.executable))
        self.assertEqual(prov["python_version"],
                         "%d.%d.%d" % sys.version_info[:3])
        self.assertEqual(prov["packages"], {"numpy": "1.0+stub", "scipy": None,
                                            "carla": "9.9.9", "py_trees": "2.1+stub"})
        self.assertEqual(prov["agent_entrypoint"], str(self.site.root / "agent.py"))
        self.assertIs(prov["agent_import_ok"], True)

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
