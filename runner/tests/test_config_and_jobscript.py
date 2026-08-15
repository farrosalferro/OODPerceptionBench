"""Config validation and the generated per-route script.

The job-script tests are the closest thing to a correctness check we can run without CARLA:
they assert the properties that, if broken, fail silently on real hardware.
"""

import json
import tempfile
import unittest
from pathlib import Path

from oodbench import config as config_mod
from oodbench import jobscript, ports
from oodbench.config import ConfigError
from oodbench.plan import RouteTask


def make_site(tmp: Path):
    """A plausible-looking installation so existence checks pass."""
    (tmp / "carla").mkdir(parents=True)
    (tmp / "carla" / "CarlaUE4.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (tmp / "b2d" / "leaderboard" / "leaderboard").mkdir(parents=True)
    (tmp / "b2d" / "leaderboard" / "leaderboard" / "leaderboard_evaluator.py").write_text(
        "", encoding="utf-8")
    (tmp / "b2d" / "scenario_runner").mkdir(parents=True)
    (tmp / "agent.py").write_text("", encoding="utf-8")
    rt = tmp / "routes" / "vehicle" / "accident_two_ways" / "visual_shift"
    rt.mkdir(parents=True)
    (rt / "route_1_x.xml").write_text("<routes/>", encoding="utf-8")
    (tmp / "out").mkdir()
    return {
        "version": 1,
        "carla": {"root": str(tmp / "carla")},
        "leaderboard": {
            "work_dir": str(tmp / "b2d"),
            "root": str(tmp / "b2d" / "leaderboard"),
            "scenario_runner_root": str(tmp / "b2d" / "scenario_runner"),
        },
        "agent": {"entrypoint": str(tmp / "agent.py")},
        "output": {"root": str(tmp / "out")},
        "routes": {"root": str(tmp / "routes")},
        "gpus": [{"cuda": 0, "vulkan": 0}],
    }


class TestConfig(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.raw = make_site(self.tmp)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_minimal_valid_config_builds(self):
        cfg = config_mod.build(self.raw)
        self.assertEqual(cfg.seed, 42)
        self.assertEqual(cfg.workers, 1)
        self.assertEqual(cfg.benchmark["release"], "v0.9")
        self.assertEqual(cfg.benchmark["arxiv_version"], "v1")

    def test_every_required_field_is_actually_required(self):
        for section, key in (("carla", "root"), ("leaderboard", "work_dir"),
                             ("leaderboard", "root"), ("leaderboard", "scenario_runner_root"),
                             ("agent", "entrypoint"), ("output", "root"), ("routes", "root")):
            raw = json.loads(json.dumps(self.raw))
            del raw[section][key]
            with self.assertRaises(ConfigError, msg=f"{section}.{key}") as ctx:
                config_mod.build(raw)
            self.assertIn(f"{section}.{key}", str(ctx.exception))

    def test_gpus_is_required(self):
        raw = json.loads(json.dumps(self.raw))
        del raw["gpus"]
        with self.assertRaises(ConfigError):
            config_mod.build(raw)

    def test_unknown_section_is_an_error(self):
        raw = dict(self.raw, cluster={"host": "somewhere"})
        with self.assertRaises(ConfigError):
            config_mod.build(raw)

    def test_typo_in_a_key_is_an_error_not_a_silent_default(self):
        raw = json.loads(json.dumps(self.raw))
        raw["execution"] = {"worker": 4}     # missing 's'
        with self.assertRaises(ConfigError) as ctx:
            config_mod.build(raw)
        self.assertIn("worker", str(ctx.exception))

    def test_missing_carla_binary_is_caught(self):
        (self.tmp / "carla" / "CarlaUE4.sh").unlink()
        with self.assertRaises(ConfigError) as ctx:
            config_mod.build(self.raw)
        self.assertIn("CarlaUE4.sh", str(ctx.exception))

    def test_workers_above_gpu_count_needs_explicit_opt_in(self):
        raw = json.loads(json.dumps(self.raw))
        raw["execution"] = {"workers": 4}
        with self.assertRaises(ConfigError) as ctx:
            config_mod.build(raw)
        self.assertIn("allow_gpu_stacking", str(ctx.exception))
        raw["execution"]["allow_gpu_stacking"] = True
        cfg = config_mod.build(raw)
        self.assertEqual(cfg.workers, 4)
        self.assertTrue(any("stacking" in w for w in cfg.warnings))

    def test_omitted_vulkan_index_warns(self):
        raw = json.loads(json.dumps(self.raw))
        raw["gpus"] = [{"cuda": 0}]
        cfg = config_mod.build(raw)
        self.assertTrue(cfg.gpus[0].vulkan_assumed)
        self.assertTrue(any("vulkan" in w.lower() for w in cfg.warnings))

    def test_off_protocol_seed_warns(self):
        raw = json.loads(json.dumps(self.raw))
        raw["benchmark"] = {"seed": 7}
        cfg = config_mod.build(raw)
        self.assertTrue(any("protocol seed" in w for w in cfg.warnings))

    def test_bad_enum_values_are_rejected(self):
        for section, key, bad in (("execution", "backend", "kubernetes"),
                                  ("agent", "track", "LIDAR"),
                                  ("resume", "mode", "always"),
                                  ("slurm", "vulkan_index_scope", "process")):
            raw = json.loads(json.dumps(self.raw))
            raw[section] = {key: bad}
            with self.assertRaises(ConfigError, msg=f"{section}.{key}"):
                config_mod.build(raw)

    def test_port_release_timeout_covers_term_grace_and_post_kill_cooldown(self):
        raw = json.loads(json.dumps(self.raw))
        raw["execution"] = {"post_kill_cooldown_s": 10, "port_release_timeout_s": 19}
        with self.assertRaises(ConfigError) as ctx:
            config_mod.build(raw)
        self.assertIn("port_release_timeout_s", str(ctx.exception))

        raw["execution"]["port_release_timeout_s"] = 20
        config_mod.build(raw)

    def test_term_grace_minimum_only_applies_when_local_port_probing_is_active(self):
        for backend, probe in (("slurm", True), ("local", False)):
            with self.subTest(backend=backend, probe=probe):
                raw = json.loads(json.dumps(self.raw))
                raw["execution"] = {
                    "backend": backend,
                    "post_kill_cooldown_s": 10,
                    "port_release_timeout_s": 19,
                }
                raw["ports"] = {"probe": probe}
                config_mod.build(raw)

    def test_timeout_still_covers_cooldown_for_every_backend(self):
        for backend, probe in (("slurm", True), ("local", False)):
            with self.subTest(backend=backend, probe=probe):
                raw = json.loads(json.dumps(self.raw))
                raw["execution"] = {
                    "backend": backend,
                    "post_kill_cooldown_s": 20,
                    "port_release_timeout_s": 19,
                }
                raw["ports"] = {"probe": probe}
                with self.assertRaises(ConfigError):
                    config_mod.build(raw)

    def test_duplicate_cuda_index_is_rejected(self):
        raw = json.loads(json.dumps(self.raw))
        raw["gpus"] = [{"cuda": 0}, {"cuda": 0}]
        with self.assertRaises(ConfigError):
            config_mod.build(raw)

    # -- GPU uniqueness must cover BOTH enumerations --------------------------------------
    # Deduping only `cuda` left the Vulkan adapter free to repeat, which puts two CARLA
    # *servers* on one physical GPU while their agents sit on different ones. Vulkan does not
    # honour CUDA_VISIBLE_DEVICES, so nothing errors: one device saturates, throughput
    # collapses, and results are still produced.

    def test_duplicate_vulkan_adapter_is_rejected(self):
        raw = json.loads(json.dumps(self.raw))
        raw["gpus"] = [{"cuda": 0, "vulkan": 0}, {"cuda": 1, "vulkan": 0}]
        with self.assertRaises(ConfigError) as ctx:
            config_mod.build(raw)
        self.assertIn("vulkan", str(ctx.exception).lower())

    def test_duplicate_vulkan_adapter_is_rejected_when_one_side_was_defaulted(self):
        # gpus[0] omits `vulkan`, so it defaults to cuda=0 -- and collides with the explicit
        # vulkan: 0 on gpus[1]. The likeliest way a real config grows this bug.
        raw = json.loads(json.dumps(self.raw))
        raw["gpus"] = [{"cuda": 0}, {"cuda": 1, "vulkan": 0}]
        with self.assertRaises(ConfigError) as ctx:
            config_mod.build(raw)
        self.assertIn("vulkan", str(ctx.exception).lower())

    def test_distinct_vulkan_adapters_are_accepted(self):
        raw = json.loads(json.dumps(self.raw))
        raw["gpus"] = [{"cuda": 0, "vulkan": 1}, {"cuda": 1, "vulkan": 0}]
        cfg = config_mod.build(raw)          # a crossed but valid mapping
        self.assertEqual([(g.cuda, g.vulkan) for g in cfg.gpus], [(0, 1), (1, 0)])

    def test_allocation_scoped_vulkan_indices_are_slurm_only(self):
        raw = json.loads(json.dumps(self.raw))
        raw["slurm"] = {"vulkan_index_scope": "allocation"}
        with self.assertRaises(ConfigError) as ctx:
            config_mod.build(raw)
        self.assertIn("execution.backend='slurm'", str(ctx.exception))

    # -- agent.env may not fight the runner for a variable the runner owns -----------------

    def test_agent_env_cannot_set_a_runner_owned_variable(self):
        for name in sorted(jobscript.RESERVED_ENV):
            raw = json.loads(json.dumps(self.raw))
            raw["agent"]["env"] = {name: "1"}
            with self.assertRaises(ConfigError, msg=name) as ctx:
                config_mod.build(raw)
            msg = str(ctx.exception)
            self.assertIn(name, msg)
            self.assertIn(jobscript.RESERVED_ENV[name], msg,
                          "the error must name the config field to use instead")

    def test_the_reserved_set_covers_the_isolation_and_determinism_controls(self):
        # Spelled out so that dropping one from RESERVED_ENV is a test failure, not a silent
        # regression: ports are worker isolation, CUDA/GPU_RANK are GPU isolation, and
        # SEED/PYTHONHASHSEED are the protocol seed the result filename claims.
        for name in ("PORT", "TM_PORT", "SEED", "PYTHONHASHSEED", "CUDA_VISIBLE_DEVICES",
                     "GPU_RANK"):
            self.assertIn(name, jobscript.RESERVED_ENV, name)

    def test_composable_path_variables_are_not_reserved(self):
        # PYTHONPATH and LD_LIBRARY_PATH are appended to, not overwritten, so an agent.env entry
        # for either is additive rather than a fight for control. Rejecting them would break
        # real model configs for no safety gain.
        for name in ("PYTHONPATH", "LD_LIBRARY_PATH"):
            self.assertNotIn(name, jobscript.RESERVED_ENV, name)
        raw = json.loads(json.dumps(self.raw))
        raw["agent"]["env"] = {"LD_LIBRARY_PATH": "/opt/whatever/lib"}
        config_mod.build(raw)

    def test_ordinary_agent_env_entries_are_still_accepted(self):
        raw = json.loads(json.dumps(self.raw))
        raw["agent"]["env"] = {"PLANNER_TYPE": "traj", "OODBENCH_TARGET_SPEED": "4.0"}
        cfg = config_mod.build(raw)
        self.assertEqual(cfg.agent["env"]["PLANNER_TYPE"], "traj")

    def test_cli_overrides_can_satisfy_a_required_field(self):
        raw = json.loads(json.dumps(self.raw))
        del raw["output"]["root"]
        cfg = config_mod.build(raw, overrides={"output.root": str(self.tmp / "out")})
        self.assertTrue(cfg.output["root"].endswith("out"))

    def test_digest_is_stable_and_sensitive(self):
        a = config_mod.build(json.loads(json.dumps(self.raw))).digest()
        b = config_mod.build(json.loads(json.dumps(self.raw))).digest()
        self.assertEqual(a, b)
        raw = json.loads(json.dumps(self.raw))
        raw["benchmark"] = {"seed": 43}
        self.assertNotEqual(a, config_mod.build(raw).digest())

    def test_default_host_vulkan_scope_preserves_existing_config_digest(self):
        omitted = config_mod.build(json.loads(json.dumps(self.raw))).digest()
        raw = json.loads(json.dumps(self.raw))
        raw["slurm"] = {"vulkan_index_scope": "host"}
        explicit_default = config_mod.build(raw).digest()
        self.assertEqual(omitted, explicit_default)

    def test_strict_manifest_without_manifest_is_rejected(self):
        raw = json.loads(json.dumps(self.raw))
        raw["routes"]["strict_manifest"] = True
        with self.assertRaises(ConfigError):
            config_mod.build(raw)


class TestJobScript(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        raw = make_site(self.tmp)
        raw["agent"]["env"] = {"PLANNER_TYPE": "traj", "TEAM_EXTRA": "1"}
        raw["gpus"] = [{"cuda": 2, "vulkan": 5}]
        self.cfg = config_mod.build(raw)
        xml = self.tmp / "routes/vehicle/accident_two_ways/visual_shift/route_1_x.xml"
        self.task = RouteTask.from_xml(xml, self.tmp / "routes", self.tmp / "out",
                                       seed=42, repetition=0)
        self.pair = ports.allocate(4, 20000, 30000, 10)[2]
        self.script = jobscript.render(self.task, self.cfg, self.cfg.gpus[0], self.pair)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_pins_both_cuda_and_the_vulkan_adapter(self):
        # Setting only CUDA_VISIBLE_DEVICES puts every simulator on adapter 0 while the agents
        # spread out -- no error, just a collapsed throughput and a saturated GPU.
        self.assertIn("export CUDA_VISIBLE_DEVICES=2", self.script)
        self.assertIn("export GPU_RANK=5", self.script)
        self.assertIn('--gpu-rank="${GPU_RANK}"', self.script)

    def test_uses_the_allocated_ports_verbatim(self):
        self.assertIn(f"export PORT={self.pair.rpc}", self.script)
        self.assertIn(f"export TM_PORT={self.pair.tm}", self.script)
        self.assertIn('--port="${PORT}"', self.script)
        self.assertIn('--traffic-manager-port="${TM_PORT}"', self.script)

    def test_does_not_pass_resume(self):
        # The vendored evaluator declares --resume as type=bool, so argparse evaluates
        # bool("0") -> True. Passing it at all is the trap; we omit it and unlink instead.
        self.assertNotIn("--resume", self.script)

    def test_does_not_detach_the_child(self):
        # The evaluator deliberately does not setsid its CARLA subprocess so that a killpg on
        # the worker takes the simulator with it. Detaching here reintroduces orphans.
        for bad in ("setsid", "nohup", "disown", "&\n"):
            self.assertNotIn(bad, self.script, bad)

    def test_seed_reaches_both_the_agent_and_the_traffic_manager(self):
        self.assertIn("export SEED=42", self.script)
        self.assertIn("export PYTHONHASHSEED=42", self.script)
        self.assertIn('--traffic-manager-seed="${SEED}"', self.script)

    def test_seed_does_not_depend_on_worker_index_or_port(self):
        other = ports.allocate(4, 20000, 30000, 10)[3]
        alt = jobscript.render(self.task, self.cfg, self.cfg.gpus[0], other)
        self.assertIn("export SEED=42", alt)
        self.assertIn("export PYTHONHASHSEED=42", alt)

    def test_agent_env_is_written_verbatim(self):
        self.assertIn("export PLANNER_TYPE=traj", self.script)
        self.assertIn("export TEAM_EXTRA=1", self.script)

    def _line_of(self, script, prefix):
        for i, ln in enumerate(script.splitlines()):
            if ln.startswith(prefix):
                return i
        self.fail(f"no line starting with {prefix!r} in the generated script")

    def test_agent_env_is_emitted_before_every_runner_owned_export(self):
        # Ordering is the second defence behind the config-load rejection. Emitting agent.env
        # last let a model config silently win control of the ports, the GPU pinning or the
        # seed -- an isolation AND determinism hole in which nothing errors.
        env_line = self._line_of(self.script, "export PLANNER_TYPE=")
        for owned in ("export PORT=", "export TM_PORT=", "export SEED=",
                      "export PYTHONHASHSEED=", "export CUDA_VISIBLE_DEVICES=",
                      "export GPU_RANK=", "export CHECKPOINT_ENDPOINT="):
            self.assertLess(env_line, self._line_of(self.script, owned),
                            f"agent.env is emitted after {owned!r}, so a model config can "
                            f"override it")

    def test_a_reserved_name_that_reached_the_script_anyway_loses_to_the_runner(self):
        # Belt and braces: bypass the config-load rejection entirely and prove the emission
        # order alone is enough. If this ever inverts, the last writer wins -- and the last
        # writer must be the runner.
        cfg = config_mod.build(make_site(Path(tempfile.mkdtemp())))
        cfg.agent["env"] = {"PORT": "59999", "SEED": "7", "CUDA_VISIBLE_DEVICES": "9"}
        script = jobscript.render(self.task, cfg, self.cfg.gpus[0], self.pair)
        for name, runner_value in (("PORT", str(self.pair.rpc)), ("SEED", "42"),
                                   ("CUDA_VISIBLE_DEVICES", "2")):
            hostile = self._line_of(script, f"export {name}=")
            lines = script.splitlines()
            last = max(i for i, ln in enumerate(lines) if ln.startswith(f"export {name}="))
            self.assertGreater(last, hostile,
                               f"agent.env's {name} was the last writer")
            self.assertEqual(lines[last], f"export {name}={runner_value}")

    def test_agent_env_still_precedes_nothing_it_needs_to_override(self):
        # environment.activate runs first (it may reset the whole environment), agent.env after
        # it. Losing that ordering would let a conda activation clobber model settings.
        raw = make_site(Path(tempfile.mkdtemp()))
        raw["environment"] = {"activate": ["export MARKER_FROM_ACTIVATE=1"]}
        raw["agent"]["env"] = {"PLANNER_TYPE": "traj"}
        cfg = config_mod.build(raw)
        xml = Path(raw["routes"]["root"]) / "vehicle/accident_two_ways/visual_shift/route_1_x.xml"
        task = RouteTask.from_xml(xml, Path(raw["routes"]["root"]),
                                  Path(raw["output"]["root"]), seed=42, repetition=0)
        script = jobscript.render(task, cfg, cfg.gpus[0], self.pair)
        self.assertLess(self._line_of(script, "export MARKER_FROM_ACTIVATE="),
                        self._line_of(script, "export PLANNER_TYPE="))

    def test_checkpoint_path_keeps_the_scenario_and_level_components(self):
        self.assertIn("accident_two_ways/visual_shift/results/route_1_x_seed42.json",
                      self.script)

    def test_agent_pythonpath_comes_first(self):
        raw = make_site(Path(tempfile.mkdtemp()))
        bundled = Path(raw["leaderboard"]["work_dir"]) / "bundled"
        bundled.mkdir()
        raw["agent"]["pythonpath"] = [str(bundled)]
        cfg = config_mod.build(raw)
        xml = Path(raw["routes"]["root"]) / "vehicle/accident_two_ways/visual_shift/route_1_x.xml"
        task = RouteTask.from_xml(xml, Path(raw["routes"]["root"]),
                                  Path(raw["output"]["root"]), seed=42, repetition=0)
        script = jobscript.render(task, cfg, cfg.gpus[0], self.pair)
        line = next(ln for ln in script.splitlines() if ln.startswith("export PYTHONPATH="))
        self.assertLess(line.index(str(bundled)), line.index(raw["leaderboard"]["root"]))

    def test_working_dir_is_honoured_when_set(self):
        raw = make_site(Path(tempfile.mkdtemp()))
        repo = Path(raw["leaderboard"]["work_dir"]) / "model_repo"
        repo.mkdir()
        raw["agent"]["working_dir"] = str(repo)
        cfg = config_mod.build(raw)
        xml = Path(raw["routes"]["root"]) / "vehicle/accident_two_ways/visual_shift/route_1_x.xml"
        task = RouteTask.from_xml(xml, Path(raw["routes"]["root"]),
                                  Path(raw["output"]["root"]), seed=42, repetition=0)
        script = jobscript.render(task, cfg, cfg.gpus[0], self.pair)
        import shlex
        self.assertIn(f"cd {shlex.quote(str(repo))} || exit 2", script)

    def test_no_cd_is_emitted_when_working_dir_is_unset(self):
        self.assertNotIn("\ncd ", self.script)

    def test_generated_script_is_syntactically_valid_bash(self):
        import subprocess
        path = self.tmp / "check.sh"
        path.write_text(self.script, encoding="utf-8")
        self.assertEqual(subprocess.call(["bash", "-n", str(path)]), 0)

    def test_paths_with_spaces_are_quoted(self):
        tmp = Path(tempfile.mkdtemp()) / "a dir with spaces"
        tmp.mkdir(parents=True)
        raw = make_site(tmp)
        cfg = config_mod.build(raw)
        xml = Path(raw["routes"]["root"]) / "vehicle/accident_two_ways/visual_shift/route_1_x.xml"
        task = RouteTask.from_xml(xml, Path(raw["routes"]["root"]),
                                  Path(raw["output"]["root"]), seed=42, repetition=0)
        script = jobscript.render(task, cfg, cfg.gpus[0], self.pair)
        import subprocess
        p = tmp / "check.sh"
        p.write_text(script, encoding="utf-8")
        self.assertEqual(subprocess.call(["bash", "-n", str(p)]), 0)
        # Every path carrying the space must appear single-quoted, never bare.
        self.assertIn(f"export CARLA_ROOT='{raw['carla']['root']}'", script)
        self.assertIn(f"export ROUTES='{xml}'", script)
        for line in script.splitlines():
            if "a dir with spaces" in line and line.startswith("export "):
                self.assertIn("'", line, line)


if __name__ == "__main__":
    unittest.main()
