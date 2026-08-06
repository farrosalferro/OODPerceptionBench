"""Who decides how many routes run at once, and does every slot own distinct resources?

The supervision loop opens one slot per unit of backend concurrency and indexes the backend's
per-slot resources -- ports, and on the local pool a GPU -- with the slot number. Two things
therefore have to agree, and did not:

* the loop read ``execution.workers`` regardless of backend, so ``slurm.max_parallel`` gated
  nothing at all; and
* the SLURM backend wrapped an out-of-range slot with ``pairs[worker % len(pairs)]``, which
  hands two *concurrently running* jobs the same RPC and traffic-manager ports. Two CARLA
  servers landing on one node then quietly share a simulator, because the evaluator scans
  upward from an occupied port instead of failing.

No scheduler is involved in any of this: the tests construct the backend directly and never
reach ``sbatch``.
"""

import argparse
import json
import logging
import time
import unittest
from pathlib import Path

import run_benchmark
from oodbench import config as config_mod, plan as plan_mod
from oodbench.backends.base import Attempt, AttemptOutcome, Backend
from oodbench.backends.local import LocalBackend
from oodbench.backends.slurm import SlurmBackend
from oodbench.state import RunState

from tests.test_integration_local import Site

COMPLETED = {"_checkpoint": {"progress": [1, 1],
                             "records": [{"status": "Completed",
                                          "scores": {"score_composed": 77.0}}]}}


def _quiet_log(name):
    log = logging.getLogger(name)
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


class BackendBase(unittest.TestCase):

    def setUp(self):
        self.site = Site()
        self.site.add_route("static/s1/base/route_1_a.xml")
        run_benchmark._interrupted["flag"] = False
        self.log = _quiet_log("test-concurrency")

    def tearDown(self):
        self.site.cleanup()


class TestSlurmConcurrency(BackendBase):

    def _backend(self, **slurm):
        cfg = config_mod.load(self.site.config(
            execution={"backend": "slurm"}, slurm=slurm))
        return cfg, SlurmBackend(cfg, self.log)

    def test_concurrency_comes_from_max_parallel_not_from_execution_workers(self):
        # execution.workers defaults to 1 and sizes the LOCAL pool. If the loop reads it here,
        # a cluster user who set slurm.max_parallel: 6 gets one job in flight and never learns
        # why -- or, with workers raised instead, gets more slots than reserved port pairs.
        cfg, backend = self._backend(max_parallel=6)
        self.assertEqual(cfg.workers, 1)
        self.assertEqual(backend.concurrency, 6)

    def test_one_reserved_port_pair_per_slot_and_all_disjoint(self):
        _, backend = self._backend(max_parallel=6)
        self.assertEqual(len(backend.pairs), backend.concurrency)
        flat = [p for pair in backend.pairs for p in pair.all_ports]
        self.assertEqual(len(set(flat)), len(flat), "two slots share a port")

    def test_an_out_of_range_slot_is_refused_rather_than_wrapped(self):
        cfg, backend = self._backend(max_parallel=2)
        xmls = plan_mod.discover(Path(cfg.routes["root"]))
        task = plan_mod.build_tasks(xmls, Path(cfg.routes["root"]), Path(cfg.output["root"]),
                                    base_seed=cfg.seed, repetitions=1)[0]
        task.mkdirs()
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")

        attempt = backend.submit(task, worker=2)      # one past the last reserved pair

        self.assertEqual(attempt.outcome, AttemptOutcome.LAUNCH_FAILED)
        self.assertIn("no reserved port pair", attempt.detail)
        self.assertEqual(json.loads(task.result_path.read_text()), COMPLETED,
                         "a refused slot must not disturb an existing record either")

    def test_slot_zero_through_last_all_map_to_their_own_pair(self):
        _, backend = self._backend(max_parallel=4)
        self.assertEqual([backend.pairs[i].worker for i in range(4)], [0, 1, 2, 3])

    def test_the_config_warns_when_execution_workers_looks_like_it_should_apply(self):
        cfg = config_mod.load(self.site.config(
            execution={"backend": "slurm", "workers": 4, "allow_gpu_stacking": True},
            slurm={"max_parallel": 8}))
        self.assertTrue(any("slurm.max_parallel" in w for w in cfg.warnings),
                        "a user who set execution.workers under slurm gets no signal that it "
                        "is ignored")

    def test_max_parallel_below_one_is_a_config_error(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.site.config(execution={"backend": "slurm"},
                                             slurm={"max_parallel": 0}))


class TestLocalConcurrency(BackendBase):

    def test_local_concurrency_is_execution_workers(self):
        cfg = config_mod.load(self.site.config(
            execution={"workers": 3, "allow_gpu_stacking": True}))
        backend = LocalBackend(cfg, self.log)
        self.assertEqual(backend.concurrency, 3)
        self.assertEqual(len(backend.pairs), 3)


class _CountingBackend(Backend):
    """Records how many slots the supervision loop actually opens."""

    name = "counting"

    def __init__(self, concurrency):
        self.concurrency = concurrency
        self.slots_seen = set()
        self.max_in_flight = 0
        self._live = 0

    def submit(self, task, worker):
        self.slots_seen.add(worker)
        self._live += 1
        self.max_in_flight = max(self.max_in_flight, self._live)
        return Attempt(task=task, worker=worker)

    def poll(self, attempt):
        if attempt.outcome is None:
            attempt.task.result_path.parent.mkdir(parents=True, exist_ok=True)
            attempt.task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")
            attempt.outcome = AttemptOutcome.EXITED
            attempt.detail = "exit 0"
            attempt.finished_at = time.time()
            self._live -= 1
        return True

    def kill(self, attempt, reason):
        pass


class TestTheLoopHonoursBackendConcurrency(BackendBase):
    """The loop must size itself from the backend, not from ``execution.workers``."""

    def _run(self, backend_concurrency, workers, n_routes=9):
        for i in range(n_routes):
            self.site.add_route(f"static/s1/base/route_{i}_a.xml")
        cfg = config_mod.load(self.site.config(
            execution={"workers": workers, "allow_gpu_stacking": True, "poll_interval_s": 1}))
        args = argparse.Namespace(limit=None, dry_run=False, force=False)
        runner = run_benchmark.Runner(cfg, args)
        tasks = runner.plan()
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")
        backend = _CountingBackend(backend_concurrency)
        rep = runner.run(tasks, state, backend)
        return backend, rep

    def test_the_loop_opens_exactly_the_backends_slots_not_execution_workers(self):
        backend, rep = self._run(backend_concurrency=3, workers=8)
        self.assertEqual(backend.slots_seen, {0, 1, 2},
                         "the loop drove slot indices the backend never reserved")
        self.assertLessEqual(backend.max_in_flight, 3)
        self.assertEqual(rep.exit_code(), 0)

    def test_the_loop_uses_all_of_the_backends_slots(self):
        backend, _ = self._run(backend_concurrency=4, workers=1)
        self.assertEqual(backend.slots_seen, {0, 1, 2, 3},
                         "the loop under-parallelised: it sized itself from execution.workers")


if __name__ == "__main__":
    unittest.main()
