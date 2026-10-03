"""Every runner configuration template the repository ships loads in the runner as shipped.

A template the runner refuses is found by the first person who copies it, before anything else
runs. ``config/example.yaml``, the file README.md's Quick start tells a new user to copy, kept an
early draft's flat layout (``carla_root``, ``upstream_dir``, ``output_root``, ...) after the
runner had moved to sections, and the runner rejected it with "unknown config section(s)".

Each test fills one template's ``<placeholders>`` with paths in a temporary install laid out like
a real one, and loads the result the way ``run_benchmark.py`` does.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

from oodbench import config as config_mod

try:
    import yaml  # noqa: F401  -- the runner needs it to read a .yaml config
except ImportError:  # pragma: no cover
    yaml = None

REPO = Path(__file__).resolve().parents[2]

PLACEHOLDER = re.compile(r"<[^<>\n]+>")


def install(tmp: Path) -> dict:
    """Lay out a CARLA root and a patched checkout under ``tmp``; return what fills each
    placeholder the shipped templates use."""
    carla = tmp / "carla"
    checkout = tmp / "carla_garage"
    b2d = checkout / "Bench2Drive"
    smoke = tmp / "scratch" / "smoke_routes"
    for d in (carla, b2d / "leaderboard" / "data", b2d / "leaderboard" / "leaderboard",
              b2d / "scenario_runner", checkout / "team_code", tmp / "agent", smoke):
        d.mkdir(parents=True, exist_ok=True)
    (carla / "CarlaUE4.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (b2d / "leaderboard" / "data" / "weather.xml").write_text("<weather/>", encoding="utf-8")
    (b2d / "leaderboard" / "leaderboard" / "leaderboard_evaluator.py").write_text(
        "", encoding="utf-8")
    (checkout / "team_code" / "data_agent.py").write_text("", encoding="utf-8")
    (tmp / "agent" / "my_agent.py").write_text("", encoding="utf-8")
    (smoke / "MANIFEST.tsv").write_text("", encoding="utf-8")
    return {
        "<path-to-carla-0.9.15>": carla,
        "<path-to-carla-0.9.15-with-the-v0.9-content-pack-installed>": carla,
        "<path-to-bench2drive-checkout>": b2d,
        "<path-to-carla_garage-checkout>": checkout,
        "<path-to-the-patched-carla_garage-checkout>": checkout,
        "<path-to-your-agent>": tmp / "agent" / "my_agent",
        "<path-to-this-repo>": REPO,
        "<path-to-repo>": REPO,
        "<absolute-path-to-your-environment>": tmp / "env",
        "<absolute-path-to-that-environment>": tmp / "env",
        "<command to activate the environment PDM-Lite runs in>": "echo activate",
        "<path-to-output-dir>": tmp / "out",
        "<replaced per replicate on the command line with --out>": tmp / "out",
        "<path-to-scratch>": tmp / "scratch",
    }


#: The golden template's example scratch folder: not a placeholder, but a path a user replaces.
GOLDEN_SCRATCH = re.compile(r"(?m)(:\s+)/scratch/smoke_routes")


@unittest.skipIf(yaml is None, "PyYAML not installed (pip install -r requirements-test.txt)")
class TestShippedConfigs(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.fill = install(self.tmp)

    def tearDown(self):
        self.tmpdir.cleanup()

    def load(self, rel: str) -> config_mod.Config:
        text = (REPO / rel).read_text(encoding="utf-8")
        text = PLACEHOLDER.sub(lambda m: str(self.fill.get(m.group(0), m.group(0))), text)
        text = GOLDEN_SCRATCH.sub(lambda m: m.group(1) + str(self.tmp / "scratch" / "smoke_routes"),
                                  text)
        filled = self.tmp / Path(rel).name.replace(".template", "")
        filled.write_text(text, encoding="utf-8")
        raw = config_mod.load_raw(filled)
        left = sorted(set(PLACEHOLDER.findall(json.dumps(raw, default=str))))
        self.assertEqual(left, [], f"{rel}: placeholder(s) this test cannot fill; add them to "
                                   f"install()")
        return config_mod.build(raw, source_path=str(filled))

    def test_top_level_example_the_quick_start_copies(self):
        cfg = self.load("config/example.yaml")
        self.assertEqual((cfg.seed, cfg.benchmark["repetitions"]), (42, 3),
                         "the published protocol is seeds 42, 43, 44")
        self.assertEqual(Path(cfg.routes["root"]), REPO / "routes")
        self.assertTrue(cfg.routes["strict_manifest"])

    def test_runner_annotated_example(self):
        cfg = self.load("runner/configs/example.yaml")
        self.assertEqual((cfg.seed, cfg.benchmark["repetitions"]), (42, 3))

    def test_reference_agent_config(self):
        cfg = self.load("runner/configs/reference_agent.yaml")
        self.assertTrue(Path(cfg.agent["entrypoint"]).is_file())

    def test_golden_generation_template(self):
        cfg = self.load("tests/configs/golden_generation.yaml.template")
        self.assertEqual((cfg.seed, cfg.benchmark["repetitions"]), (42, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
