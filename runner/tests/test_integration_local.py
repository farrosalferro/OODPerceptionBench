"""End-to-end tests of the local supervision loop against a *fake* evaluator.

No CARLA, no GPU, no network. A stand-in ``leaderboard_evaluator.py`` writes whatever
checkpoint JSON the test asks for (or hangs, or dies without writing one), which lets the
resume, retry, quarantine, timeout and exit-code paths be exercised for real.

This is emphatically **not** a substitute for hardware validation -- it proves the runner's
logic, not that CARLA starts on the right GPU. See STATUS.md.
"""

import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import run_benchmark
from oodbench import EXIT_CONFIG, EXIT_OK, EXIT_PARTIAL, ports

FAKE_EVALUATOR = textwrap.dedent('''\
    """Stand-in for leaderboard_evaluator.py. Behaviour is driven by FAKE_MODE."""
    import argparse, json, os, signal, sys, time
    from pathlib import Path

    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint")
    p.add_argument("--routes")
    p.add_argument("--port", type=int)
    p.add_argument("--traffic-manager-port", type=int)
    p.add_argument("--traffic-manager-seed", type=int)
    p.add_argument("--gpu-rank")
    args, _ = p.parse_known_args()

    mode = os.environ.get("FAKE_MODE", "ok")
    trace = os.environ.get("FAKE_TRACE")
    if trace:
        with open(trace, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "route": args.routes,
                "checkpoint": args.checkpoint,
                "port": args.port,
                "tm_port": args.traffic_manager_port,
                "tm_seed": args.traffic_manager_seed,
                "gpu_rank": args.gpu_rank,
                "seed_env": os.environ.get("SEED"),
                "cuda": os.environ.get("CUDA_VISIBLE_DEVICES"),
                "pid": os.getpid(),
                "t": time.time(),
            }) + "\\n")

    def emit(status, progress=(1, 1)):
        Path(args.checkpoint).parent.mkdir(parents=True, exist_ok=True)
        Path(args.checkpoint).write_text(json.dumps({"_checkpoint": {
            "progress": list(progress),
            "records": [{"status": status, "scores": {"score_composed": 55.5}}],
        }}), encoding="utf-8")

    if mode == "ok":
        emit("Completed")
    elif mode == "tickruntime":
        emit("Failed - TickRuntime")
    elif mode == "crash_record":
        emit("Failed - Agent crashed")
        sys.exit(1)
    elif mode == "sensors_invalid":
        emit("Failed - Agent's sensors were invalid")
    elif mode == "no_record":
        sys.exit(3)
    elif mode == "partial_record":
        emit("Completed", progress=(0, 1))
        sys.exit(1)
    elif mode == "hang":
        time.sleep(600)
    elif mode == "shutdown_crash_tickruntime":
        # The trap's end-to-end shape: the route produced a real TickRuntime verdict, then the
        # CARLA server segfaulted during teardown into the stderr stream it SHARES with this
        # process. The real evaluator exits 0 on this path (`crashed` stays False).
        emit("Failed - TickRuntime")
        print("Signal 11 caught.", file=sys.stderr, flush=True)
        print("Segmentation fault (core dumped)", file=sys.stderr, flush=True)
    elif mode == "shutdown_crash_agent":
        # Same shared-stderr crash, but on the evaluator's own crash path, which exits non-zero
        # (`sys.exit(-1)`). Cannot be told apart from the evaluator itself dying, so the record
        # goes to the bounded ambiguity axis rather than to the model's record budget.
        emit("Failed - Agent crashed")
        print("Segmentation fault (core dumped)", file=sys.stderr, flush=True)
        sys.exit(255)
    elif mode == "abort_after_record":
        # The evaluator itself dies of SIGABRT with a crash-shaped record already on disk.
        # bash pads the signal name into a column, so the stderr line is NOT the single-spaced
        # "Aborted (core dumped)" the fault patterns used to look for -- which is exactly why
        # this shape was classified as a clean exit and charged to the model's record budget.
        emit("Failed - Simulation crashed")
        os.kill(os.getpid(), signal.SIGABRT)
    elif mode == "oomkill_after_record":
        # What a cgroup OOM kill looks like: SIGKILL, and bash prints only "Killed", which is
        # in no fault pattern at all and deliberately never will be (too ordinary a word).
        emit("Failed - Simulation crashed")
        os.kill(os.getpid(), signal.SIGKILL)
    elif mode == "abort_after_tickruntime":
        # THE TRAP, on the hard-death path: a real degenerate-model verdict, then the process
        # dies by signal. Must still settle at exit 0 -- TickRuntime is not fabricable by a
        # kill, so it is charged to the tickruntime axis in BOTH outcome classes.
        emit("Failed - TickRuntime")
        os.kill(os.getpid(), signal.SIGABRT)
    elif mode == "flaky":
        counter = Path(os.environ["FAKE_COUNTER"])
        n = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(n + 1))
        if n == 0:
            sys.exit(3)
        emit("Completed")
    sys.exit(0)
''')


def free_port_base(width=200):
    """Find a contiguous free block so the tests do not fight whatever else is on this host."""
    for base in range(41000, 60000, 500):
        if not ports.probe(range(base, base + width)):
            return base
    raise unittest.SkipTest("no free port block available for the integration tests")


class Site:
    """A throwaway installation the runner will accept."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        root = Path(self.tmp)
        (root / "carla").mkdir()
        (root / "carla" / "CarlaUE4.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        ev = root / "b2d" / "leaderboard" / "leaderboard"
        ev.mkdir(parents=True)
        (ev / "leaderboard_evaluator.py").write_text(FAKE_EVALUATOR, encoding="utf-8")
        (root / "b2d" / "scenario_runner").mkdir(parents=True)
        (root / "agent.py").write_text("", encoding="utf-8")
        self.routes = root / "routes"
        self.out = root / "out"
        self.out.mkdir()
        self.trace = root / "trace.jsonl"
        self.counter = root / "counter.txt"
        self.root = root

    def add_route(self, rel):
        p = self.routes / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("<routes/>", encoding="utf-8")
        return p

    def config(self, **over):
        base = free_port_base()
        cfg = {
            "version": 1,
            "carla": {"root": str(self.root / "carla")},
            "leaderboard": {
                "work_dir": str(self.root / "b2d"),
                "root": str(self.root / "b2d" / "leaderboard"),
                "scenario_runner_root": str(self.root / "b2d" / "scenario_runner"),
            },
            "agent": {"entrypoint": str(self.root / "agent.py"),
                      "env": {"FAKE_TRACE": str(self.trace),
                              "FAKE_COUNTER": str(self.counter)}},
            "environment": {"python": sys.executable},
            "execution": {"backend": "local", "workers": 1, "poll_interval_s": 1,
                          "route_timeout_s": 60, "post_kill_cooldown_s": 0},
            "gpus": [{"cuda": 0, "vulkan": 0}],
            "ports": {"rpc_base": base, "tm_base": base + 100, "stride": 10},
            "retry": {"record_budget": 2, "infra_budget": 2, "tickruntime_budget": 0,
                      "worker_quarantine_after": 99},
            "output": {"root": str(self.out)},
            "routes": {"root": str(self.routes)},
        }
        for section, values in over.items():
            cfg.setdefault(section, {})
            if isinstance(values, dict):
                cfg[section].update(values)
            else:
                cfg[section] = values
        path = self.root / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return str(path)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def trace_rows(self):
        if not self.trace.exists():
            return []
        return [json.loads(ln) for ln in self.trace.read_text().splitlines() if ln.strip()]


class IntegrationBase(unittest.TestCase):

    def setUp(self):
        self.site = Site()
        run_benchmark._interrupted["flag"] = False
        self._old_mode = os.environ.get("FAKE_MODE")

    def tearDown(self):
        self.site.cleanup()
        if self._old_mode is None:
            os.environ.pop("FAKE_MODE", None)
        else:
            os.environ["FAKE_MODE"] = self._old_mode

    def run_cli(self, config_path, mode="ok", extra=None):
        os.environ["FAKE_MODE"] = mode
        argv = ["--config", config_path] + (extra or [])
        return run_benchmark.main(argv)

    def report(self):
        return json.loads((self.site.out / "_runner" / "report.json").read_text())


class TestHappyPathAndResume(IntegrationBase):

    def test_full_sweep_completes_and_exits_zero(self):
        for rel in ("static/s1/base/route_1_a.xml",
                    "static/s1/visual_shift/route_2_b.xml",
                    "vehicle/v1/geometric_shift/route_3_c.xml"):
            self.site.add_route(rel)
        code = self.run_cli(self.site.config())
        self.assertEqual(code, EXIT_OK)
        rep = self.report()
        self.assertEqual(rep["totals"]["planned"], 3)
        self.assertEqual(rep["totals"]["incomplete"], 0)
        self.assertEqual(rep["totals"]["by_status"], {"Completed": 3})
        # The {scenario}/{level}/ component survives into the result tree.
        self.assertTrue((self.site.out / "static/s1/visual_shift/results"
                         / "route_2_b_seed42.json").is_file())

    def test_rerun_skips_completed_routes(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        self.assertEqual(self.run_cli(self.site.config()), EXIT_OK)
        first = len(self.site.trace_rows())
        self.assertEqual(self.run_cli(self.site.config()), EXIT_OK)
        self.assertEqual(len(self.site.trace_rows()), first,
                         "a completed route must not be executed a second time")
        self.assertEqual(self.report()["totals"]["skipped_already_done"], 1)

    def test_interrupted_sweep_resumes_and_finishes_the_remainder(self):
        for i in range(4):
            self.site.add_route(f"static/s1/base/route_{i}_a.xml")
        cfg = self.site.config()
        # Simulate an interruption: run once, then delete two result files.
        self.assertEqual(self.run_cli(cfg), EXIT_OK)
        results = sorted((self.site.out / "static/s1/base/results").glob("*.json"))
        self.assertEqual(len(results), 4)
        for p in results[:2]:
            p.unlink()
        before = len(self.site.trace_rows())
        self.assertEqual(self.run_cli(cfg), EXIT_OK)
        self.assertEqual(len(self.site.trace_rows()) - before, 2,
                         "exactly the two missing routes should be re-run")

    def test_seed_reaches_the_child_identically_regardless_of_worker(self):
        for i in range(4):
            self.site.add_route(f"static/s1/base/route_{i}_a.xml")
        cfg = self.site.config(
            execution={"workers": 3, "allow_gpu_stacking": True},
            gpus=[{"cuda": 0, "vulkan": 0}])
        self.assertEqual(self.run_cli(cfg), EXIT_OK)
        rows = self.site.trace_rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["tm_seed"] for r in rows}, {42})
        self.assertEqual({r["seed_env"] for r in rows}, {"42"})

    def test_workers_get_disjoint_ports(self):
        for i in range(6):
            self.site.add_route(f"static/s1/base/route_{i}_a.xml")
        cfg = self.site.config(execution={"workers": 3, "allow_gpu_stacking": True})
        self.assertEqual(self.run_cli(cfg), EXIT_OK)
        rows = self.site.trace_rows()
        used = {(r["port"], r["tm_port"]) for r in rows}
        self.assertLessEqual(len(used), 3)
        rpc_ports = {r["port"] for r in rows}
        tm_ports = {r["tm_port"] for r in rows}
        self.assertEqual(rpc_ports & tm_ports, set())
        for a in rpc_ports:
            for b in rpc_ports:
                if a != b:
                    self.assertGreaterEqual(abs(a - b), 3,
                                            "RPC windows must not overlap")

    def test_transient_post_route_port_linger_is_worker_cooldown_not_route_infra(self):
        """One self-clearing CARLA socket must not become another route's failed launch.

        Real hardware left RPC+1 unavailable briefly after the evaluator exited and the local
        backend reaped CARLA. The old fixed-sleep/single-probe path popped the next route,
        called the transient ``LAUNCH_FAILED``, and durably charged that unrelated route's
        infrastructure counters. Model the exact sequence: route one exits, reaping finds its
        CARLA children, the streaming port remains busy across a skipped fill, then is bindable.
        """
        for i in range(2):
            self.site.add_route(f"static/s1/base/route_{i}_a.xml")
        cfg = self.site.config(
            execution={"poll_interval_s": 1, "post_kill_cooldown_s": 0})

        reaped_after_first_route = False
        busy_observations = 0

        def reap_after_first_route(_ports):
            nonlocal reaped_after_first_route
            if len(self.site.trace_rows()) == 1 and not reaped_after_first_route:
                reaped_after_first_route = True
                return [1376739, 1376746]
            return []

        def port_lingers_across_one_skipped_fill(_ports):
            nonlocal busy_observations
            # Two busy observations are load-bearing: can_submit consumes the first before it
            # returns False. If Runner ignored that False and submitted anyway, submit's own
            # defensive probe would consume the second and the test would see LAUNCH_FAILED.
            if len(self.site.trace_rows()) == 1 and busy_observations < 2:
                busy_observations += 1
                return [busy]
            return []

        busy = free_port_base() + 1
        with patch("oodbench.backends.local.ports_mod.probe_pairs", return_value=[]), \
             patch("oodbench.backends.local.reap.terminate_carla_on_ports",
                   side_effect=reap_after_first_route), \
             patch("oodbench.backends.local.ports_mod.probe",
                   side_effect=port_lingers_across_one_skipped_fill), \
             patch("run_benchmark.time.sleep", side_effect=lambda _seconds: None):
            code = self.run_cli(cfg)

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(self.site.trace_rows()), 2, "both routes should launch exactly once")
        for route in self.report()["routes"]:
            self.assertEqual(route["attempts"]["infra"], 0)
            self.assertEqual(route["attempts"]["infra_total"], 0,
                             "a transient owned by the previous route reached durable debt")


class TestFailureHandling(IntegrationBase):

    def test_route_that_never_writes_a_record_exhausts_infra_budget_and_exits_nonzero(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        code = self.run_cli(self.site.config(), mode="no_record")
        self.assertEqual(code, EXIT_PARTIAL, "a partial sweep must never exit 0")
        self.assertEqual(len(self.site.trace_rows()), 2, "infra_budget=2 attempts")
        rep = self.report()
        self.assertEqual(rep["totals"]["incomplete"], 1)
        inc = rep["incomplete_routes"][0]
        self.assertEqual(inc["attempts"]["infra"], 2)
        self.assertEqual(inc["attempts"]["record"], 0,
                         "an infra failure must not consume the record budget")

    def test_unfinalized_checkpoint_counts_as_infra_not_as_a_result(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        code = self.run_cli(self.site.config(), mode="partial_record")
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertEqual(self.report()["incomplete_routes"][0]["attempts"]["infra"], 2)

    def test_retryable_record_is_retried_then_accepted_as_the_result(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        code = self.run_cli(self.site.config(), mode="crash_record")
        self.assertEqual(len(self.site.trace_rows()), 2, "record_budget=2 attempts")
        rep = self.report()
        self.assertEqual(rep["totals"]["incomplete"], 0)
        self.assertEqual(rep["routes"][0]["attempts"]["record"], 2)
        # The record on disk IS the benchmark result, so this is not a runner failure.
        self.assertEqual(code, EXIT_OK)

    def test_tickruntime_is_not_retried_by_default(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        code = self.run_cli(self.site.config(), mode="tickruntime")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(self.site.trace_rows()), 1,
                         "tickruntime_budget=0 means one attempt, matching the reference sweeps")

    def test_flaky_route_recovers_within_budget(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        code = self.run_cli(self.site.config(), mode="flaky")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(self.site.trace_rows()), 2)
        self.assertEqual(self.report()["routes"][0]["status"], "Completed")

    def test_hanging_route_is_killed_by_the_wall_clock(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self.site.config(execution={"route_timeout_s": 3, "poll_interval_s": 1},
                               retry={"infra_budget": 1})
        code = self.run_cli(cfg, mode="hang")
        self.assertEqual(code, EXIT_PARTIAL)
        rep = self.report()
        self.assertEqual(rep["totals"]["incomplete"], 1)
        self.assertIn("timeout", (rep["incomplete_routes"][0]["reason"] or "").lower())

    def test_sensors_invalid_aborts_the_whole_sweep(self):
        for i in range(5):
            self.site.add_route(f"static/s1/base/route_{i}_a.xml")
        code = self.run_cli(self.site.config(), mode="sensors_invalid")
        self.assertEqual(code, 5)
        self.assertLessEqual(len(self.site.trace_rows()), 1,
                             "a rejected sensor configuration must not burn the whole route set")

    def test_budget_is_not_refreshed_by_restarting(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self.site.config(retry={"infra_budget": 2})
        self.assertEqual(self.run_cli(cfg, mode="no_record"), EXIT_PARTIAL)
        self.assertEqual(len(self.site.trace_rows()), 2)
        self.assertEqual(self.run_cli(cfg, mode="no_record"), EXIT_PARTIAL)
        self.assertEqual(len(self.site.trace_rows()), 2,
                         "a restart must not buy a fresh retry budget")

    def test_worker_is_quarantined_after_consecutive_infra_failures(self):
        for i in range(4):
            self.site.add_route(f"static/s1/base/route_{i}_a.xml")
        cfg = self.site.config(retry={"infra_budget": 1, "worker_quarantine_after": 2})
        code = self.run_cli(cfg, mode="no_record")
        self.assertNotEqual(code, EXIT_OK)
        rep = self.report()
        self.assertEqual(rep["quarantined_workers"], [0])

    def test_timeouts_do_not_quarantine_a_worker_that_is_demonstrably_running(self):
        """A route that drove for a full ``route_timeout_s`` proves the worker WORKS.

        Charging the wall-clock kill to the quarantine streak let slow routes in a row pull a
        healthy worker and abort the whole sweep -- reported to the operator as the signature
        of a wedged GPU, on a machine whose GPU was idle and error-free. Found on hardware 14
        routes into a real 70-route sweep: three consecutive hanging routes quarantined the
        only worker, and every remaining route went unattempted.

        The routes still do not settle -- a hang produces no result and must stay unsettled --
        so this is EXIT_PARTIAL. What must NOT happen is the no-usable-GPU abort, because that
        stops the sweep dead on routes that have nothing wrong with the machine.

        Contrast with the test above: an attempt that launches and dies *fast* without a record
        still advances the streak. Only breaching the wall clock is proof the worker ran.
        """
        for i in range(4):
            self.site.add_route(f"static/s1/base/route_{i}_a.xml")
        cfg = self.site.config(
            execution={"route_timeout_s": 3, "poll_interval_s": 1, "post_kill_cooldown_s": 0},
            retry={"infra_budget": 1, "worker_quarantine_after": 2})
        code = self.run_cli(cfg, mode="hang")
        rep = self.report()
        self.assertEqual(rep["quarantined_workers"], [],
                         "a timeout means the route ran; that is not evidence of a wedged GPU")
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertEqual(len(self.site.trace_rows()), 4,
                         "every route must still be attempted -- quarantine aborted the sweep "
                         "before reaching the later ones")


class TestPreflightAndConfig(IntegrationBase):

    def test_busy_port_block_is_a_preflight_error_not_a_silent_shift(self):
        import socket
        self.site.add_route("static/s1/base/route_1_a.xml")
        base = free_port_base()
        cfg_path = self.site.config(ports={"rpc_base": base, "tm_base": base + 100,
                                           "stride": 10})
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("localhost", base))
            s.listen(1)
            code = self.run_cli(cfg_path)
        self.assertEqual(code, EXIT_CONFIG)
        self.assertEqual(len(self.site.trace_rows()), 0)

    def test_dry_run_executes_nothing_and_exits_zero(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        code = self.run_cli(self.site.config(), extra=["--dry-run"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(self.site.trace_rows()), 0)
        self.assertFalse((self.site.out / "_runner" / "report.json").exists())

    def test_resume_mode_none_requires_force(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        cfg = self.site.config(resume={"mode": "none"})
        self.assertEqual(self.run_cli(cfg), EXIT_CONFIG)
        self.assertEqual(self.run_cli(cfg, extra=["--force"]), EXIT_OK)

    def test_strict_manifest_mismatch_refuses_to_run(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        manifest = self.site.root / "MANIFEST.tsv"
        manifest.write_text("path\tsha256\nstatic/s1/base/route_1_a.xml\tdeadbeef\n",
                            encoding="utf-8")
        cfg = self.site.config(routes={"manifest": str(manifest), "strict_manifest": True})
        self.assertEqual(self.run_cli(cfg), EXIT_CONFIG)
        self.assertEqual(len(self.site.trace_rows()), 0)

    def test_empty_route_tree_is_an_error(self):
        cfg = self.site.config()
        self.site.routes.mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.run_cli(cfg), EXIT_CONFIG)


if __name__ == "__main__":
    unittest.main()
