"""SLURM backend behavior against scheduler executables faked on ``PATH``.

These tests exercise the backend at its public seam: route attempts go through real generated
job wrappers and subprocess calls to stand-in ``sbatch``/``squeue``/``sacct``/``scancel``
commands.  No scheduler or GPU is required.
"""

import argparse
import errno
import json
import logging
import os
import pwd
import re
import shlex
import shutil
import stat
import sys
import textwrap
import time
import unittest
import unittest.mock
from pathlib import Path

import run_benchmark
from oodbench import EXIT_PARTIAL, config as config_mod, plan as plan_mod
from oodbench.backends.base import AttemptOutcome
from oodbench.backends.slurm import SlurmBackend, SlurmBackendError
from oodbench.state import RunState

from tests import slurm_replay
from tests.test_integration_local import Site

# The scheduler records jobs under the effective user, whatever LOGNAME or USER say.
EFFECTIVE_USER = pwd.getpwuid(os.geteuid()).pw_name

COMPLETED = {"_checkpoint": {"progress": [1, 1],
                              "records": [{"status": "Completed",
                                           "scores": {"score_composed": 77.0}}]}}


def _quiet_log(name):
    log = logging.getLogger(name)
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


class SlurmBackendBase(unittest.TestCase):

    def setUp(self):
        self.site = Site()
        self.site.add_route("static/s1/base/route_1_a.xml")
        self.bin = self.site.root / "fake-slurm-bin"
        self.bin.mkdir()
        self.path_patch = unittest.mock.patch.dict(
            os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")})
        self.path_patch.start()
        self.log = _quiet_log("test-slurm-backend")

    def tearDown(self):
        self.path_patch.stop()
        self.site.cleanup()

    def tool(self, name, source):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(source),
                        encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def replay(self, name, *answers, when=None, replace=None):
        """Fake ``name`` with captured scheduler output; see ``tests.slurm_replay``."""
        return slurm_replay.install(self.bin, name, *answers, when=when, replace=replace)

    def calls(self, name):
        return slurm_replay.calls(self.bin, name)

    def backend_and_task(self, **sections):
        execution = {"backend": "slurm"}
        execution.update(sections.pop("execution", {}))
        cfg = config_mod.load(self.site.config(execution=execution, **sections))
        task = plan_mod.build_tasks(
            plan_mod.discover(Path(cfg.routes["root"])),
            Path(cfg.routes["root"]), Path(cfg.output["root"]),
            base_seed=cfg.seed, repetitions=1,
        )[0]
        return cfg, SlurmBackend(cfg, self.log), task


class TestFreshOutputRoot(SlurmBackendBase):

    def test_submitted_job_can_write_its_first_checkpoint(self):
        """RED BEFORE THE FIX:

            AssertionError: False is not true : the job could not create its first checkpoint
            because results/ did not exist

        The real statistics manager opens its checkpoint path without creating the parent.
        A successful ``sbatch`` is therefore not enough: the submitter must materialise the
        same per-route directory contract as the local backend before the job can run.
        """
        evaluator = Path(self.site.root / "b2d" / "leaderboard" / "leaderboard"
                         / "leaderboard_evaluator.py")
        evaluator.write_text(textwrap.dedent('''\
            import argparse, json
            from pathlib import Path

            parser = argparse.ArgumentParser()
            parser.add_argument("--checkpoint")
            args, _ = parser.parse_known_args()
            Path(args.checkpoint).write_text(json.dumps({
                "_checkpoint": {"progress": [1, 1], "records": [{
                    "status": "Completed", "scores": {"score_composed": 77.0}
                }]}
            }), encoding="utf-8")
        '''), encoding="utf-8")
        self.tool("sbatch", '''
            import os, subprocess, sys
            env = dict(os.environ)
            env["CUDA_VISIBLE_DEVICES"] = "0"
            env["SLURM_JOB_GPUS"] = "0"
            subprocess.run(["bash", sys.argv[-1]], env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
            print("101")
        ''')
        _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})

        attempt = backend.submit(task, worker=0)

        self.assertEqual(attempt.handle, "101")
        self.assertTrue(task.result_path.is_file(),
                        "the job could not create its first checkpoint because results/ did "
                        "not exist")
        self.assertEqual(json.loads(task.result_path.read_text()), COMPLETED)


class TestSignalDeathSettlement(SlurmBackendBase):

    def test_signal_death_charges_the_bounded_axis_not_the_models(self):
        """RED BEFORE THE FIX:

            AssertionError: <AttemptOutcome.EXITED: 'exited'> is not
            <AttemptOutcome.FAULT: 'fault'> : a scheduler-reported SIGKILL was treated as a
            clean model exit

        The signal component of SLURM's ``ExitCode`` is attempt-owned evidence, unlike the
        shared stderr stream. It must reach the same bounded ambiguity cell as a signal death
        observed by the local backend.
        """
        self.tool("sbatch", 'print("202")')
        self.tool("squeue", 'pass')
        self.tool("sacct", 'print("FAILED|0:9")')
        cfg, backend, task = self.backend_and_task(
            slurm={"submit_interval_s": 0},
            retry={"record_budget": 3, "infra_budget": 3, "tickruntime_budget": 0,
                   "killed_budget": 2, "worker_quarantine_after": 99},
        )
        attempt = backend.submit(task, worker=0)
        task.result_path.write_text(json.dumps({
            "_checkpoint": {"progress": [1, 1], "records": [{
                "status": "Failed - Simulation crashed",
                "scores": {"score_composed": 3.0},
            }]}
        }), encoding="utf-8")

        self.assertTrue(backend.poll(attempt))
        self.assertIs(attempt.outcome, AttemptOutcome.FAULT,
                      "a scheduler-reported SIGKILL was treated as a clean model exit")

        runner = run_benchmark.Runner(
            cfg, argparse.Namespace(limit=None, dry_run=False, force=False))
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")
        self.assertTrue(runner._settle(attempt, state, backend))
        settled = state.get(task.key)
        self.assertEqual(settled.attempts_killed, 1)
        self.assertEqual(settled.attempts_record, 0,
                         "signal death spent the model's record retry budget")

    def test_scheduler_deadline_charges_the_bounded_axis_not_the_models(self):
        """RED BEFORE THE S2 REVIEW FIX:

            AssertionError: <AttemptOutcome.EXITED: 'exited'> is not
            <AttemptOutcome.FAULT: 'fault'> : scheduler DEADLINE was treated as a self-exit

        ``DEADLINE`` is a terminal scheduler termination, not an evaluator verdict. A
        crash-shaped checkpoint must therefore reach the same bounded ambiguity axis as the
        other scheduler-owned terminal failures, never the model's record budget.
        """
        self.tool("sbatch", 'print("203")')
        self.tool("squeue", 'pass')
        self.tool("sacct", 'print("DEADLINE|0:0")')
        cfg, backend, task = self.backend_and_task(
            slurm={"submit_interval_s": 0},
            retry={"record_budget": 3, "infra_budget": 3, "tickruntime_budget": 0,
                   "killed_budget": 2, "worker_quarantine_after": 99},
        )
        attempt = backend.submit(task, worker=0)
        task.result_path.write_text(json.dumps({
            "_checkpoint": {"progress": [1, 1], "records": [{
                "status": "Failed - Simulation crashed",
                "scores": {"score_composed": 3.0},
            }]}
        }), encoding="utf-8")

        self.assertTrue(backend.poll(attempt))
        self.assertIs(attempt.outcome, AttemptOutcome.FAULT,
                      "scheduler DEADLINE was treated as a self-exit")

        runner = run_benchmark.Runner(
            cfg, argparse.Namespace(limit=None, dry_run=False, force=False))
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")
        self.assertTrue(runner._settle(attempt, state, backend))
        settled = state.get(task.key)
        self.assertEqual(settled.attempts_killed, 1)
        self.assertEqual(settled.attempts_record, 0)

    def test_scheduler_revocation_charges_the_bounded_axis_not_the_models(self):
        """RED BEFORE THE SECOND S2 REVIEW FIX:

            AssertionError: <AttemptOutcome.EXITED: 'exited'> is not
            <AttemptOutcome.FAULT: 'fault'> : scheduler REVOKED was treated as a self-exit

        ``REVOKED`` is a scheduler-owned federated sibling termination. A crash-shaped
        checkpoint must therefore reach the same bounded ambiguity axis as the other
        scheduler-owned terminal failures, never the model's record budget.
        """
        self.tool("sbatch", 'print("204;alpha")')
        self.tool("squeue", 'pass')
        self.tool("sacct", 'print("REVOKED|0:0")')
        cfg, backend, task = self.backend_and_task(
            slurm={"submit_interval_s": 0},
            retry={"record_budget": 3, "infra_budget": 3, "tickruntime_budget": 0,
                   "killed_budget": 2, "worker_quarantine_after": 99},
        )
        attempt = backend.submit(task, worker=0)
        task.result_path.write_text(json.dumps({
            "_checkpoint": {"progress": [1, 1], "records": [{
                "status": "Failed - Simulation crashed",
                "scores": {"score_composed": 3.0},
            }]}
        }), encoding="utf-8")

        self.assertTrue(backend.poll(attempt))
        self.assertIs(attempt.outcome, AttemptOutcome.FAULT,
                      "scheduler REVOKED was treated as a self-exit")

        runner = run_benchmark.Runner(
            cfg, argparse.Namespace(limit=None, dry_run=False, force=False))
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")
        self.assertTrue(runner._settle(attempt, state, backend))
        settled = state.get(task.key)
        self.assertEqual(settled.attempts_killed, 1)
        self.assertEqual(settled.attempts_record, 0)


class TestQueueTimeIsNotRouteTime(SlurmBackendBase):

    def test_pending_job_is_not_cancelled_at_the_route_runtime_limit(self):
        """RED BEFORE THE FIX:

            AssertionError: True is not false : a queued job was settled even though it has
            not started

        ``route_timeout_s`` bounds evaluator runtime. A scheduler may hold a valid submission
        in PENDING longer than that without ever allocating a node, GPU, ports, or checkpoint.
        """
        cancel_log = self.site.root / "scancel.log"
        self.tool("sbatch", 'print("303")')
        self.tool("squeue", 'print("PENDING")')
        self.tool("scancel", f'''
            from pathlib import Path
            Path({str(cancel_log)!r}).write_text("called", encoding="utf-8")
        ''')
        _, backend, task = self.backend_and_task(
            execution={"route_timeout_s": 10}, slurm={"submit_interval_s": 0})
        attempt = backend.submit(task, worker=0)
        attempt.started_at = time.time() - 120

        self.assertFalse(backend.poll(attempt),
                         "a queued job was settled even though it has not started")
        self.assertIsNone(attempt.outcome)
        self.assertFalse(cancel_log.exists(),
                         "queue wait was counted as route runtime and the pending job was "
                         "cancelled")

    def test_suspended_or_requeued_residence_pauses_the_route_clock(self):
        """RED BEFORE THE S2 REVIEW FIX:

            AssertionError: True is not false : suspended scheduler residence was counted as
            evaluator runtime

        Once a job has run, SLURM may suspend or requeue it. The route clock must retain earlier
        RUNNING time but exclude that scheduler-owned residence when the job resumes.
        """
        state_calls = self.site.root / "squeue.states"
        self.tool("sbatch", 'print("304")')
        self.tool("squeue", f'''
            from pathlib import Path
            calls = Path({str(state_calls)!r})
            n = int(calls.read_text()) if calls.exists() else 0
            calls.write_text(str(n + 1), encoding="utf-8")
            print(("RUNNING", "SUSPENDED", "RUNNING")[n])
        ''')
        _, backend, task = self.backend_and_task(
            execution={"route_timeout_s": 10}, slurm={"submit_interval_s": 0})
        attempt = backend.submit(task, worker=0)

        with unittest.mock.patch("oodbench.backends.slurm.time.time", return_value=100.0):
            self.assertFalse(backend.poll(attempt))
        with unittest.mock.patch("oodbench.backends.slurm.time.time", return_value=105.0):
            self.assertFalse(backend.poll(attempt))
        with unittest.mock.patch("oodbench.backends.slurm.time.time", return_value=125.0), \
                unittest.mock.patch.object(backend, "kill") as kill:
            self.assertFalse(
                backend.poll(attempt),
                "suspended scheduler residence was counted as evaluator runtime",
            )
            kill.assert_not_called()

        self.assertEqual(attempt.started_at, 120.0,
                         "the 20 suspended seconds were not removed from the route clock")

    def test_direct_kill_of_pending_job_records_zero_route_runtime(self):
        """RED BEFORE THE SECOND S2 REVIEW FIX:

            AssertionError: 150.0 != 0.0 : queued residence was persisted as route runtime

        ``Runner._drain`` kills in-flight attempts directly. A job that never reached RUNNING
        consumed no evaluator runtime, even if it spent a long time waiting in PENDING.
        """
        self.tool("sbatch", 'print("305")')
        self.tool("squeue", 'print("PENDING")')
        self.tool("scancel", 'pass')
        self.tool("sacct", 'print("CANCELLED|0:15")')
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        attempt = backend.submit(task, worker=0)
        attempt.started_at = 50.0

        with unittest.mock.patch("oodbench.backends.slurm.time.time", return_value=100.0):
            self.assertFalse(backend.poll(attempt))
        self.tool("squeue", 'pass')
        with unittest.mock.patch("oodbench.backends.slurm.time.time", return_value=200.0):
            backend.kill(attempt, "test drain")

        self.assertEqual(attempt.duration_s, 0.0,
                         "queued residence was persisted as route runtime")
        runner = run_benchmark.Runner(
            cfg, argparse.Namespace(limit=None, dry_run=False, force=False))
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")
        self.assertFalse(runner._settle(attempt, state, backend, interrupted=True))
        self.assertEqual(state.get(task.key).last_duration_s, 0.0)
        self.assertEqual(state.get(task.key).total_runtime_s, 0.0)

    def test_direct_kill_of_suspended_job_records_only_running_time(self):
        """RED BEFORE THE SECOND S2 REVIEW FIX:

            AssertionError: 100.0 != 20.0 : suspended residence was persisted as route runtime

        A direct drain after RUNNING then SUSPENDED must keep the completed RUNNING interval
        while excluding the open scheduler-owned pause that cancellation closes.
        """
        state_calls = self.site.root / "squeue.kill-states"
        self.tool("sbatch", 'print("306")')
        self.tool("squeue", f'''
            from pathlib import Path
            calls = Path({str(state_calls)!r})
            n = int(calls.read_text()) if calls.exists() else 0
            calls.write_text(str(n + 1), encoding="utf-8")
            print(("RUNNING", "SUSPENDED")[n])
        ''')
        self.tool("scancel", 'pass')
        self.tool("sacct", 'print("CANCELLED|0:15")')
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        attempt = backend.submit(task, worker=0)

        with unittest.mock.patch("oodbench.backends.slurm.time.time", return_value=100.0):
            self.assertFalse(backend.poll(attempt))
        with unittest.mock.patch("oodbench.backends.slurm.time.time", return_value=120.0):
            self.assertFalse(backend.poll(attempt))
        self.tool("squeue", 'pass')
        with unittest.mock.patch("oodbench.backends.slurm.time.time", return_value=200.0):
            backend.kill(attempt, "test drain")

        self.assertEqual(attempt.duration_s, 20.0,
                         "suspended residence was persisted as route runtime")
        runner = run_benchmark.Runner(
            cfg, argparse.Namespace(limit=None, dry_run=False, force=False))
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")
        self.assertFalse(runner._settle(attempt, state, backend, interrupted=True))
        self.assertEqual(state.get(task.key).last_duration_s, 20.0)
        self.assertEqual(state.get(task.key).total_runtime_s, 20.0)


class TestAccountingStates(SlurmBackendBase):

    def test_nonterminal_sacct_states_remain_in_flight(self):
        """RED BEFORE THE FIX:

            AssertionError: True is not false : sacct state RUNNING was treated as terminal

        ``sacct`` is the fallback after an empty or failed ``squeue`` query, but it also reports
        live states. Settling one frees the slot and submits a duplicate route against the same
        ports and checkpoint.
        """
        self.tool("sbatch", 'print("404")')
        self.tool("squeue", 'pass')
        for state in ("RUNNING", "PENDING", "COMPLETING", "REQUEUED", "SUSPENDED"):
            with self.subTest(state=state):
                self.tool("sacct", f'print({state + "|0:0"!r})')
                _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
                attempt = backend.submit(task, worker=0)

                self.assertFalse(backend.poll(attempt),
                                 f"sacct state {state} was treated as terminal")
                self.assertIsNone(attempt.outcome)
                self.assertIsNone(attempt.finished_at)


class TestCancellationWaits(SlurmBackendBase):

    def test_kill_waits_for_asynchronous_scancel_to_finish(self):
        """RED BEFORE THE FIX:

            AssertionError: False is not true : kill returned without asking whether
            asynchronous scancel finished

        ``scancel`` acknowledges a request; it does not synchronously reap the cgroup. Reading
        or settling the checkpoint before the job leaves the queue races its final writes.
        """
        queue_calls = self.site.root / "squeue.calls"
        cancel_log = self.site.root / "scancel.log"
        self.tool("sbatch", 'print("505")')
        self.tool("scancel", f'''
            from pathlib import Path
            Path({str(cancel_log)!r}).write_text("cancel requested", encoding="utf-8")
        ''')
        self.tool("squeue", f'''
            from pathlib import Path
            calls = Path({str(queue_calls)!r})
            n = int(calls.read_text()) if calls.exists() else 0
            calls.write_text(str(n + 1), encoding="utf-8")
            if n == 0:
                print("RUNNING")
        ''')
        self.tool("sacct", 'print("CANCELLED|0:15")')
        _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        attempt = backend.submit(task, worker=0)

        backend.kill(attempt, "test cancellation")

        self.assertTrue(cancel_log.is_file())
        self.assertTrue(queue_calls.is_file(),
                        "kill returned without asking whether asynchronous scancel finished")
        self.assertGreaterEqual(int(queue_calls.read_text()), 2,
                                "kill returned while squeue still reported the job")
        self.assertIs(attempt.outcome, AttemptOutcome.KILLED)

    def test_cancel_wait_requires_positive_terminal_accounting(self):
        """RED BEFORE THE S2 REVIEW FIX:

            AssertionError: 1 not greater than or equal to 2 : missing accounting was treated
            as proof that cancellation had finished

        Empty ``squeue`` is not enough: the controller/accounting transition can lag while the
        cgroup still writes. Missing ``sacct`` data must be retried until a terminal state is
        observed or the bounded cancel wait fails closed.
        """
        accounting_calls = self.site.root / "sacct.calls"
        self.tool("sbatch", 'print("506")')
        self.tool("scancel", 'pass')
        self.tool("squeue", 'pass')
        self.tool("sacct", f'''
            from pathlib import Path
            calls = Path({str(accounting_calls)!r})
            n = int(calls.read_text()) if calls.exists() else 0
            calls.write_text(str(n + 1), encoding="utf-8")
            if n:
                print("CANCELLED|0:15")
        ''')
        _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        attempt = backend.submit(task, worker=0)

        with unittest.mock.patch("oodbench.backends.slurm._CANCEL_POLL_S", 0):
            backend.kill(attempt, "test cancellation")

        self.assertGreaterEqual(
            int(accounting_calls.read_text()), 2,
            "missing accounting was treated as proof that cancellation had finished",
        )
        self.assertIs(attempt.outcome, AttemptOutcome.KILLED)


class _Clock:
    """A settable stand-in for ``time.time`` so a 180 s grace runs instantly."""

    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


CRASH_SHAPED = {"_checkpoint": {"progress": [1, 1], "records": [{
    "status": "Failed - Simulation crashed", "scores": {"score_composed": 3.0}}]}}


class TestAccountingUnavailable(SlurmBackendBase):
    """Neither ``squeue`` nor ``sacct`` knows the job: absence of evidence, not a clean exit.

    Every answer here is captured scheduler output (``fixtures/slurm``). A real ``squeue`` exits
    1 with "Invalid job id" once a finished job is purged, so a ``squeue`` error must still fall
    through to ``sacct``; only when ``sacct`` also has nothing does the grace start.
    """

    def submitted(self, sacct, *, squeue=("squeue_purged",), **sections):
        self.replay("sbatch", "sbatch_parsable")
        self.replay("squeue", *squeue)
        self.replay("sacct", *sacct)
        self.replay("scancel", "scancel_ok")
        sections.setdefault("slurm", {"submit_interval_s": 0})
        cfg, backend, task = self.backend_and_task(**sections)
        return cfg, backend, task, backend.submit(task, worker=0)

    def test_a_job_unknown_to_squeue_and_sacct_is_kept(self):
        """RED BEFORE THE FIX:

            AssertionError: True is not false : a job with no accounting record was settled
            as finished

        Today an empty ``sacct`` reply settles as EXITED "SLURM state unknown" and charges the
        model's record budget for an end nobody observed.
        """
        for squeue in ("squeue_purged", "squeue_gone_recent"):
            with self.subTest(squeue=squeue):
                _, backend, _, attempt = self.submitted(["sacct_never_issued"],
                                                        squeue=(squeue,))
                self.assertFalse(backend.poll(attempt),
                                 "a job with no accounting record was settled as finished")
                self.assertIsNone(attempt.outcome)
                self.assertIsNone(attempt.finished_at)

    def test_silent_accounting_faults_on_the_bounded_axis_after_the_grace(self):
        """RED BEFORE THE FIX:

            AssertionError: True is not false : the first silent poll settled the job

        After the grace the job settles as FAULT, never EXITED, so a crash-shaped checkpoint
        is charged to the bounded ``killed`` axis and not to the model's record budget. A
        best-effort ``scancel`` goes out first in case the job is still alive somewhere.
        """
        clock = _Clock()
        with unittest.mock.patch("time.time", clock):
            cfg, backend, task, attempt = self.submitted(
                ["sacct_never_issued"],
                retry={"record_budget": 3, "infra_budget": 3, "tickruntime_budget": 0,
                       "killed_budget": 2, "worker_quarantine_after": 99})
            task.result_path.write_text(json.dumps(CRASH_SHAPED), encoding="utf-8")
            start = clock.now
            self.assertFalse(backend.poll(attempt), "the first silent poll settled the job")
            clock.now = start + 179.0
            self.assertFalse(backend.poll(attempt), "settled before the 180 s grace ran out")
            self.assertEqual(self.calls("scancel"), [])
            clock.now = start + 180.0
            self.assertTrue(backend.poll(attempt))

        self.assertIs(attempt.outcome, AttemptOutcome.FAULT)
        # Never seen RUNNING and accounting is down: the runtime is unknown too (see F).
        self.assertEqual(attempt.detail, "accounting unavailable after 180s: "
                                         "sacct returned no record; runtime unknown (never observed RUNNING)")
        self.assertEqual(attempt.finished_at, start,
                         "the grace itself was counted as route time")
        self.assertEqual(self.calls("scancel"), [["141229"]])

        runner = run_benchmark.Runner(
            cfg, argparse.Namespace(limit=None, dry_run=False, force=False))
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")
        self.assertTrue(runner._settle(attempt, state, backend))
        settled = state.get(task.key)
        self.assertEqual(settled.attempts_killed, 1)
        self.assertEqual(settled.attempts_record, 0,
                         "missing accounting spent the model's record retry budget")

    def test_a_failing_sacct_is_told_apart_from_an_empty_reply(self):
        """RED BEFORE THE FIX:

            AssertionError: <AttemptOutcome.EXITED: 'exited'> is not
            <AttemptOutcome.FAULT: 'fault'>

        An operator reading the report must know whether accounting was down or merely had
        no record of the job. ``sacct_command_error`` is real ``sacct`` error text, captured
        for a different ``--format`` than this query's.
        """
        clock = _Clock()
        with unittest.mock.patch("time.time", clock):
            _, backend, _, attempt = self.submitted(["sacct_command_error"])
            backend.poll(attempt)
            clock.now += 180.0
            backend.poll(attempt)

        self.assertIs(attempt.outcome, AttemptOutcome.FAULT)
        self.assertEqual(
            attempt.detail,
            'accounting unavailable after 180s: sacct failed (exit 1): '
            'sacct: error: Invalid field requested: "NoSuchField"; runtime unknown (never observed RUNNING)')

    def test_a_job_that_reappears_restarts_the_grace(self):
        """RED BEFORE THE FIX:

            AssertionError: True is not false : the first silent poll settled the job

        A job visible again in ``squeue`` is owned by the scheduler. The next silence starts a
        fresh grace rather than inheriting the old one.
        """
        clock = _Clock()
        with unittest.mock.patch("time.time", clock):
            _, backend, _, attempt = self.submitted(
                ["sacct_never_issued"],
                squeue=("squeue_purged", "squeue_running", "squeue_purged"))
            start = clock.now
            self.assertFalse(backend.poll(attempt), "the first silent poll settled the job")
            clock.now = start + 170.0
            self.assertFalse(backend.poll(attempt))  # RUNNING again
            clock.now = start + 200.0
            self.assertFalse(backend.poll(attempt))  # silent again: a new grace starts
            clock.now = start + 379.0
            self.assertFalse(backend.poll(attempt), "the old grace was carried over")
            clock.now = start + 380.0
            self.assertTrue(backend.poll(attempt))
        self.assertIs(attempt.outcome, AttemptOutcome.FAULT)

    def test_a_real_terminal_state_inside_the_grace_wins(self):
        """RED BEFORE THE FIX:

            AssertionError: True is not false : settled before sacct reported a state
        """
        clock = _Clock()
        with unittest.mock.patch("time.time", clock):
            _, backend, _, attempt = self.submitted(
                ["sacct_never_issued", "sacct_never_issued", "sacct_failed_exit3"])
            start = clock.now
            backend.poll(attempt)
            clock.now = start + 100.0
            self.assertFalse(backend.poll(attempt), "settled before sacct reported a state")
            clock.now = start + 179.0
            self.assertTrue(backend.poll(attempt))

        self.assertTrue(attempt.detail.startswith("SLURM state FAILED"), attempt.detail)
        self.assertIs(attempt.outcome, AttemptOutcome.EXITED)
        self.assertEqual(attempt.exit_code, 3)

    def test_the_route_clock_stops_when_the_job_leaves_the_queue(self):
        """RED BEFORE THE FIX:

            AssertionError: 170.0 != 10.0 : the accounting grace was counted as route runtime

        Seen RUNNING, gone from ``squeue`` 10 s later, and only 160 s after that does
        accounting report the end. The job ran for at most those 10 s; the wait for
        accounting is not route time, just as the grace's own FAULT stops the clock there.
        """
        clock = _Clock()
        with unittest.mock.patch("time.time", clock):
            _, backend, _, attempt = self.submitted(
                ["sacct_never_issued", "sacct_failed_exit3"],
                squeue=("squeue_running", "squeue_gone_recent"))
            start = clock.now
            self.assertFalse(backend.poll(attempt))  # RUNNING: the route clock starts
            clock.now = start + 10.0
            self.assertFalse(backend.poll(attempt))  # gone, no accounting yet
            clock.now = start + 170.0
            self.assertTrue(backend.poll(attempt))

        self.assertIs(attempt.outcome, AttemptOutcome.EXITED)
        self.assertEqual(attempt.duration_s, 10.0,
                         "the accounting grace was counted as route runtime")
        self.assertEqual(attempt.detail, "SLURM state FAILED")

    def test_kill_during_the_grace_settles_on_confirmed_cancellation(self):
        """RED BEFORE THE FIX:

            AssertionError: True is not false : the first silent poll settled the job
        """
        _, backend, _, attempt = self.submitted(
            ["sacct_never_issued", "sacct_cancelled_running"])
        self.assertFalse(backend.poll(attempt), "the first silent poll settled the job")

        with unittest.mock.patch("oodbench.backends.slurm._CANCEL_POLL_S", 0):
            backend.kill(attempt, "test cancellation")

        self.assertIs(attempt.outcome, AttemptOutcome.KILLED)
        self.assertEqual(self.calls("scancel"), [["141229"]])
        self.assertTrue(backend.poll(attempt))

    def test_kill_during_the_grace_still_refuses_to_settle_without_accounting(self):
        """RED BEFORE THE FIX:

            AssertionError: <AttemptOutcome.EXITED: 'exited'> is not None

        With accounting still silent, cancellation cannot be confirmed, so ``kill`` keeps its
        fail-closed refusal instead of letting the checkpoint be read under a live job.
        """
        _, backend, _, attempt = self.submitted(["sacct_never_issued"])
        backend.poll(attempt)
        self.assertIsNone(attempt.outcome)

        with unittest.mock.patch("oodbench.backends.slurm._CANCEL_WAIT_S", 0), \
                unittest.mock.patch("oodbench.backends.slurm._CANCEL_POLL_S", 0), \
                self.assertRaises(SlurmBackendError):
            backend.kill(attempt, "test cancellation")
        self.assertIsNone(attempt.outcome)

    def test_shutdown_during_the_grace_cancels_the_job(self):
        """RED BEFORE THE FIX:

            AssertionError: no logs of level WARNING or higher triggered on
            test-slurm-backend

        The first silent poll had already settled and forgotten the job, so ``shutdown``
        never cancelled it.
        """
        _, backend, _, attempt = self.submitted(["sacct_never_issued"])
        backend.poll(attempt)

        with unittest.mock.patch("oodbench.backends.slurm._CANCEL_WAIT_S", 0), \
                unittest.mock.patch("oodbench.backends.slurm._CANCEL_POLL_S", 0), \
                self.assertLogs("test-slurm-backend", level="WARNING") as logs:
            backend.shutdown()

        self.assertEqual(self.calls("scancel"), [["141229"]],
                         "shutdown forgot a job whose end nobody observed")
        self.assertIn("no confirmed terminal state", "\n".join(logs.output))

    def test_the_grace_adds_no_config_key(self):
        """RED BEFORE THE FIX: AttributeError: ... has no attribute '_ACCOUNTING_GRACE_S'

        The grace is a module constant. A new schema key would change the config digest of
        every existing output root (see ``config.DIGEST_COMPAT_DEFAULTS``); this pins the
        schema to the keys it held before the fix.
        """
        from oodbench.backends import slurm as slurm_mod
        self.assertEqual(slurm_mod._ACCOUNTING_GRACE_S, 180.0)
        self.assertEqual({section: sorted(keys) for section, keys in config_mod.SCHEMA.items()}, {
            "agent": ["config", "entrypoint", "env", "pythonpath", "track", "working_dir"],
            "benchmark": ["arxiv_version", "release", "repetitions", "seed"],
            "carla": ["client_timeout_s", "root"],
            "environment": ["activate", "ld_library_path_prepend", "python"],
            "execution": ["allow_gpu_stacking", "backend", "poll_interval_s",
                          "port_release_timeout_s", "post_kill_cooldown_s", "route_timeout_s",
                          "workers"],
            "leaderboard": ["evaluator", "root", "scenario_runner_root", "work_dir"],
            "output": ["record_carla", "root"],
            "ports": ["probe", "rpc_base", "stride", "tm_base"],
            "resume": ["mode"],
            "retry": ["infra_budget", "killed_budget", "record_budget", "tickruntime_budget",
                      "worker_quarantine_after"],
            "routes": ["manifest", "root", "strict_manifest"],
            "slurm": ["account", "cpus_per_task", "exclude", "extra_directives", "gres",
                      "max_parallel", "mem", "nodelist", "partition", "qos",
                      "submit_interval_s", "time", "vulkan_index_scope"],
        })


class TestRuntimeWithoutRunning(SlurmBackendBase):
    """A job that ends before any poll sees it RUNNING: queue time is not route runtime.

    ``Attempt.started_at`` is stamped before ``sbatch``, so without a RUNNING observation
    "now - started_at" is mostly queue wait. Every scheduler answer here is captured output.
    """

    UNKNOWN = "runtime unknown (never observed RUNNING)"
    RETRY = {"record_budget": 3, "infra_budget": 3, "tickruntime_budget": 0,
             "killed_budget": 2, "worker_quarantine_after": 99}

    def submitted(self, clock, *, squeue, sacct, times):
        self.replay("sbatch", "sbatch_parsable")
        self.replay("squeue", *squeue)
        self.replay("sacct", *sacct)
        self.replay("sacct", *times, when="Start,End,Elapsed")
        self.replay("scancel", "scancel_ok")
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0},
                                                   retry=self.RETRY)
        attempt = backend.submit(task, worker=0)
        # Attempt's default factory bound the real time.time; restamp it on the test clock.
        attempt.started_at = clock.now
        return cfg, backend, task, attempt

    def settled(self, cfg, attempt, backend, task):
        runner = run_benchmark.Runner(
            cfg, argparse.Namespace(limit=None, dry_run=False, force=False))
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")
        runner._settle(attempt, state, backend)
        return state.get(task.key)

    def test_an_unobserved_run_takes_its_runtime_from_accounting(self):
        """RED BEFORE THE FIX:

            AssertionError: 3600.0 != 5.0 : queue wait was persisted as route runtime

        The job sat PENDING, ran 5 s between two polls, and was gone by the next one. Its
        runtime comes from sacct ``Elapsed``, not from wall-time arithmetic across hosts.
        """
        clock = _Clock()
        with unittest.mock.patch("time.time", clock):
            cfg, backend, task, attempt = self.submitted(
                clock, squeue=("squeue_pending", "squeue_gone_recent"),
                sacct=("sacct_failed_exit3",), times=("sacct_times_failed",))
            start = clock.now
            clock.now = start + 10.0
            self.assertFalse(backend.poll(attempt))
            clock.now = start + 3600.0
            self.assertTrue(backend.poll(attempt))

        self.assertEqual(attempt.duration_s, 5.0, "queue wait was persisted as route runtime")
        self.assertEqual(attempt.detail, "SLURM state FAILED")
        self.assertEqual(attempt.finished_at, start + 3600.0)
        settled = self.settled(cfg, attempt, backend, task)
        self.assertEqual(settled.last_duration_s, 5.0)
        self.assertEqual(settled.total_runtime_s, 5.0)

    def test_a_job_cancelled_while_pending_records_zero_runtime(self):
        """RED BEFORE THE FIX:

            AssertionError: 600.0 != 0.0 : a job that never started was given route runtime

        A real job cancelled while PENDING reports ``Start=None`` and ``Elapsed=00:00:00``.
        """
        clock = _Clock()
        with unittest.mock.patch("time.time", clock):
            cfg, backend, task, attempt = self.submitted(
                clock, squeue=("squeue_pending", "squeue_purged"),
                sacct=("sacct_cancelled_pending",), times=("sacct_times_cancelled_pending",))
            start = clock.now
            self.assertFalse(backend.poll(attempt))
            clock.now = start + 600.0
            self.assertTrue(backend.poll(attempt))

        self.assertEqual(attempt.duration_s, 0.0,
                         "a job that never started was given route runtime")
        self.assertIs(attempt.outcome, AttemptOutcome.FAULT)
        self.assertEqual(attempt.detail, f"SLURM state CANCELLED by 1000; {self.UNKNOWN}")
        self.assertEqual(self.settled(cfg, attempt, backend, task).total_runtime_s, 0.0)

    def test_unavailable_accounted_runtime_records_zero_and_says_so(self):
        """RED BEFORE THE FIX:

            AssertionError: 900.0 != 0.0 : queue wait was persisted as route runtime

        The timing query fails, so the runtime is unknown: record 0, and tag the detail so
        nobody reads it as a measured 0. ``sacct_command_error`` is real ``sacct`` error text,
        captured for a different ``--format`` than this query's.
        """
        clock = _Clock()
        with unittest.mock.patch("time.time", clock):
            _, backend, _, attempt = self.submitted(
                clock, squeue=("squeue_gone_recent",),
                sacct=("sacct_completed",), times=("sacct_command_error",))
            clock.now += 900.0
            self.assertTrue(backend.poll(attempt))

        self.assertEqual(attempt.duration_s, 0.0, "queue wait was persisted as route runtime")
        self.assertEqual(attempt.detail, f"SLURM state COMPLETED; {self.UNKNOWN}")

    def test_missing_accounting_after_the_grace_records_zero_runtime(self):
        """RED BEFORE THE FIX (with the missing-accounting grace already in place):

            AssertionError: 60.0 != 0.0 : queue wait was persisted as route runtime

        Without that grace it fails one step earlier: the first silent poll settles the job.
        Never seen RUNNING and then lost to both squeue and sacct: nothing measured the run.
        """
        clock = _Clock()
        with unittest.mock.patch("time.time", clock):
            _, backend, _, attempt = self.submitted(
                clock, squeue=("squeue_pending", "squeue_purged"),
                sacct=("sacct_never_issued",), times=("sacct_never_issued",))
            start = clock.now
            self.assertFalse(backend.poll(attempt))
            clock.now = start + 60.0
            self.assertFalse(backend.poll(attempt))
            clock.now = start + 240.0
            self.assertTrue(backend.poll(attempt))

        self.assertEqual(attempt.duration_s, 0.0, "queue wait was persisted as route runtime")
        self.assertEqual(attempt.detail, "accounting unavailable after 180s: "
                                         f"sacct returned no record; {self.UNKNOWN}")

    def test_kill_and_settlement_agree_on_the_same_timeline(self):
        """RED BEFORE THE FIX (the never-RUNNING timeline):

            AssertionError: 0.0 != 100.0 : kill and settlement disagree on the route runtime

        Both paths compute the runtime one way. A job seen RUNNING 90 s before it ends counts
        90 s either way; a job never seen RUNNING asks accounting either way.
        """
        timelines = {
            "observed_running": dict(
                polls=(("squeue_running", 10.0), ("squeue_running", 70.0)),
                sacct_settle="sacct_completed", sacct_kill="sacct_cancelled_running",
                times="sacct_times_failed", runtime=90.0, tag=""),
            "never_running": dict(
                polls=(("squeue_pending", 10.0),),
                sacct_settle="sacct_cancelled_pending", sacct_kill="sacct_cancelled_pending",
                times="sacct_times_cancelled_pending", runtime=0.0,
                tag=f"; {self.UNKNOWN}"),
        }
        for name, t in timelines.items():
            with self.subTest(timeline=name):
                got = {}
                for path in ("settle", "kill"):
                    clock = _Clock()
                    with unittest.mock.patch("time.time", clock), \
                            unittest.mock.patch("oodbench.backends.slurm._CANCEL_POLL_S", 0):
                        squeue = [answer for answer, _ in t["polls"]] + ["squeue_gone_recent"]
                        _, backend, _, attempt = self.submitted(
                            clock, squeue=squeue, sacct=(t["sacct_" + path],),
                            times=(t["times"],))
                        start = clock.now
                        for _, at in t["polls"]:
                            clock.now = start + at
                            self.assertFalse(backend.poll(attempt))
                        clock.now = start + 100.0
                        if path == "settle":
                            self.assertTrue(backend.poll(attempt))
                        else:
                            backend.kill(attempt, "test cancellation")
                        got[path] = (attempt.duration_s, attempt.detail)
                self.assertEqual(got["kill"][0], got["settle"][0],
                                 "kill and settlement disagree on the route runtime")
                self.assertEqual(got["settle"][0], t["runtime"])
                self.assertEqual(got["kill"][1], "test cancellation" + t["tag"])
                self.assertTrue(got["settle"][1].endswith(t["tag"]), got["settle"][1])


class TestDurableAside(SlurmBackendBase):
    """The checkpoint a retry replaces is kept on disk, not only in process memory.

    Until sbatch's answer is understood, the old checkpoint is the route's only record. Held in
    memory, it died with any exception or crash between set-aside and restore.
    """

    OLD = json.dumps(COMPLETED).encode("utf-8")

    def asides(self, task):
        return sorted(task.result_path.parent.glob(task.result_path.name + ".aside-*"))

    def ledger(self, cfg):
        path = Path(cfg.output["root"]) / "_runner" / "aside.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def sbatch_seeing(self, task, answer):
        """An sbatch that records which aside files existed when it ran, then replays
        ``answer`` (a captured case)."""
        seen = self.site.root / "sbatch.saw"
        captured = slurm_replay.case(answer)
        pattern = task.result_path.name + ".aside-*"
        self.tool("sbatch", f'''
            import sys
            from pathlib import Path
            found = sorted(p.name for p in Path({str(task.result_path.parent)!r}).glob({pattern!r}))
            Path({str(seen)!r}).write_text("\\n".join(found), encoding="utf-8")
            sys.stdout.write({captured["stdout"]!r})
            sys.stderr.write({captured["stderr"]!r})
            sys.exit({captured["rc"]!r})
        ''')
        return seen

    def test_the_old_checkpoint_is_on_disk_and_synced_before_sbatch(self):
        """RED BEFORE THE FIX:

            AssertionError: '' is not true : sbatch ran while the old checkpoint existed only
            in memory
        """
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        task.mkdirs()
        task.result_path.write_bytes(self.OLD)
        seen = self.sbatch_seeing(task, "sbatch_parsable")

        synced = []
        real_fsync = os.fsync

        def recording_fsync(fd):
            synced.append(os.fstat(fd).st_ino)
            return real_fsync(fd)

        with unittest.mock.patch("os.fsync", recording_fsync):
            attempt = backend.submit(task, worker=0)

        self.assertEqual(attempt.handle, "141229")
        self.assertTrue(seen.read_text(),
                        "sbatch ran while the old checkpoint existed only in memory")
        [aside] = self.asides(task)
        self.assertEqual(seen.read_text(), aside.name)
        self.assertEqual(aside.read_bytes(), self.OLD)
        self.assertFalse(task.result_path.exists(), "the attempt does not start clean")
        self.assertIn(aside.stat().st_ino, synced, "the aside file was not fsynced")
        self.assertIn(aside.parent.stat().st_ino, synced, "its directory was not fsynced")
        [line] = self.ledger(cfg)
        self.assertEqual((line["event"], line["key"], line["path"]),
                         ("set_aside", task.key, str(aside)))

    def test_a_full_disk_while_setting_aside_aborts_before_sbatch(self):
        """RED BEFORE THE FIX:

            AssertionError: Lists differ: [['--parsable', ...]] != [] : sbatch ran although the
            old checkpoint could not be kept

        The ``ENOSPC`` is synthetic (injected at ``os.write``); no scheduler output is faked.
        """
        self.replay("sbatch", "sbatch_parsable")
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        task.mkdirs()
        task.result_path.write_bytes(self.OLD)

        with unittest.mock.patch("os.write",
                                 side_effect=OSError(errno.ENOSPC, "No space left on device")):
            attempt = backend.submit(task, worker=0)

        self.assertEqual(self.calls("sbatch"), [],
                         "sbatch ran although the old checkpoint could not be kept")
        self.assertIs(attempt.outcome, AttemptOutcome.LAUNCH_FAILED)
        self.assertIn("No space left on device", attempt.detail)
        self.assertEqual(task.result_path.read_bytes(), self.OLD)
        self.assertEqual(self.asides(task), [], "a partial aside file was left behind")

    def test_a_refused_submission_restores_from_the_file_and_deletes_it(self):
        """RED BEFORE THE FIX:

            AssertionError: '' is not true : the restore could not have come from disk
        """
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        task.mkdirs()
        task.result_path.write_bytes(self.OLD)
        seen = self.sbatch_seeing(task, "sbatch_invalid_partition")

        attempt = backend.submit(task, worker=0)

        self.assertIs(attempt.outcome, AttemptOutcome.LAUNCH_FAILED)
        self.assertTrue(seen.read_text(), "the restore could not have come from disk")
        self.assertEqual(task.result_path.read_bytes(), self.OLD)
        self.assertEqual(self.asides(task), [])
        self.assertEqual([line["event"] for line in self.ledger(cfg)],
                         ["set_aside", "restored"])

    def test_settlement_deletes_the_aside_file(self):
        """RED BEFORE THE FIX:

            ValueError: not enough values to unpack (expected 1, got 0)
        """
        for path in ("poll", "kill"):
            with self.subTest(path=path):
                self.replay("sbatch", "sbatch_parsable")
                self.replay("squeue", "squeue_gone_recent")
                self.replay("sacct", "sacct_completed" if path == "poll"
                            else "sacct_cancelled_running")
                self.replay("scancel", "scancel_ok")
                cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
                task.mkdirs()
                task.result_path.write_bytes(self.OLD)

                attempt = backend.submit(task, worker=0)
                [aside] = self.asides(task)
                if path == "poll":
                    self.assertTrue(backend.poll(attempt))
                else:
                    with unittest.mock.patch("oodbench.backends.slurm._CANCEL_POLL_S", 0):
                        backend.kill(attempt, "test cancellation")

                self.assertFalse(aside.exists(), "the aside file outlived the settled attempt")
                self.assertEqual([line["event"] for line in self.ledger(cfg)][-2:],
                                 ["set_aside", "discarded"])

    def test_two_retries_of_one_route_get_separate_files(self):
        """RED BEFORE THE FIX:

            AssertionError: 0 != 2 : a second retry overwrote the first aside file
        """
        self.replay("sbatch", "sbatch_parsable")
        _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        task.mkdirs()
        task.result_path.write_bytes(self.OLD)
        backend.submit(task, worker=0)
        task.result_path.write_bytes(b'{"second": true}')
        backend.submit(task, worker=0)

        asides = self.asides(task)
        self.assertEqual(len(asides), 2, "a second retry overwrote the first aside file")
        self.assertEqual(sorted(a.read_bytes() for a in asides),
                         sorted([self.OLD, b'{"second": true}']))

    def test_no_aside_file_when_there_was_no_checkpoint(self):
        """A marker holds the route instead (``TestUnsettledJobMarker``)."""
        self.replay("sbatch", "sbatch_parsable")
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        backend.submit(task, worker=0)
        self.assertEqual(self.asides(task), [])
        self.assertEqual([line["event"] for line in self.ledger(cfg)], ["marked"])


class TestSubmissionIdentity(SlurmBackendBase):

    def test_parsable_federation_job_id_remains_supervised(self):
        """RED BEFORE THE FIX:

            AssertionError: <AttemptOutcome.LAUNCH_FAILED: 'launch_failed'> is not None : a
            successfully submitted job was requeued outside supervision

        ``--parsable`` makes the identifier a scheduler contract instead of scraping human
        prose; federated clusters append ``;cluster`` and still refer to the same numeric job.
        """
        args_log = self.site.root / "sbatch.args"
        self.tool("sbatch", f'''
            import json, sys
            from pathlib import Path
            Path({str(args_log)!r}).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
            print("606;cluster")
        ''')
        _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})

        attempt = backend.submit(task, worker=0)

        self.assertIsNone(attempt.outcome,
                          "a successfully submitted job was requeued outside supervision")
        self.assertEqual(attempt.handle, "606;cluster")
        self.assertIn("606;cluster", backend._submitted)
        self.assertIn("--parsable", json.loads(args_log.read_text()))

    def test_cluster_qualifier_routes_every_supervision_command(self):
        """RED BEFORE THE S2 REVIEW FIX:

            AssertionError: '607' != '607;alpha' : the cluster qualifier was discarded after
            submission

        The optional cluster emitted by ``sbatch --parsable`` is routing information. Preserve
        it in the handle and send ``squeue``, ``sacct`` and ``scancel`` to that same cluster.
        """
        queue_calls = self.site.root / "squeue.args"
        accounting_calls = self.site.root / "sacct.args"
        cancel_calls = self.site.root / "scancel.args"
        self.tool("sbatch", 'print("607;alpha")')
        self.tool("squeue", f'''
            import json, sys
            from pathlib import Path
            path = Path({str(queue_calls)!r})
            rows = path.read_text().splitlines() if path.exists() else []
            rows.append(json.dumps(sys.argv[1:]))
            path.write_text("\\n".join(rows), encoding="utf-8")
            if len(rows) == 1:
                print("RUNNING")
        ''')
        self.tool("sacct", f'''
            import json, sys
            from pathlib import Path
            Path({str(accounting_calls)!r}).write_text(
                json.dumps(sys.argv[1:]), encoding="utf-8")
            print("CANCELLED|0:15")
        ''')
        self.tool("scancel", f'''
            import json, sys
            from pathlib import Path
            Path({str(cancel_calls)!r}).write_text(
                json.dumps(sys.argv[1:]), encoding="utf-8")
        ''')
        _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})

        attempt = backend.submit(task, worker=0)
        self.assertEqual(attempt.handle, "607;alpha",
                         "the cluster qualifier was discarded after submission")
        self.assertFalse(backend.poll(attempt))
        backend.kill(attempt, "test federated cancellation")

        for path in (queue_calls, accounting_calls, cancel_calls):
            rows = path.read_text().splitlines()
            args = json.loads(rows[-1])
            self.assertIn("-M", args)
            self.assertEqual(args[args.index("-M") + 1], "alpha")
            self.assertIn("607", args)
            self.assertNotIn("607;alpha", args)

    def test_successful_submission_with_trailer_is_cancelled_before_requeue(self):
        """RED BEFORE THE SECOND S2 REVIEW FIX:

            AssertionError: False is not true : accepted malformed submission was orphaned

        Exit zero from ``sbatch`` means the scheduler accepted a job. If trailer noise breaks
        the strict parsable contract, recover the leading identity and positively confirm its
        cancellation before restoring the stale checkpoint or allowing a retry.
        """
        cancel_calls = self.site.root / "scancel-malformed.args"
        self.tool("sbatch", 'print("608;alpha unexpected trailer")')
        self.tool("squeue", 'pass')
        self.tool("sacct", 'print("CANCELLED|0:15")')
        self.tool("scancel", f'''
            import json, sys
            from pathlib import Path
            Path({str(cancel_calls)!r}).write_text(
                json.dumps(sys.argv[1:]), encoding="utf-8")
        ''')
        _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        task.mkdirs()
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")

        attempt = backend.submit(task, worker=0)

        self.assertIs(attempt.outcome, AttemptOutcome.LAUNCH_FAILED)
        self.assertTrue(cancel_calls.is_file(),
                        "accepted malformed submission was orphaned")
        args = json.loads(cancel_calls.read_text())
        self.assertEqual(args, ["-M", "alpha", "608"])
        self.assertNotIn("608;alpha", backend._submitted)
        self.assertEqual(json.loads(task.result_path.read_text()), COMPLETED,
                         "stale checkpoint was not restored after positive termination")

    def test_successful_submission_without_an_identity_refuses_to_requeue(self):
        """RED BEFORE THE SECOND S2 REVIEW FIX:

            AssertionError: SlurmBackendError not raised

        A successful submission whose output contains no recoverable leading identity cannot
        be cancelled safely. Fail the sweep closed and keep the prior checkpoint aside instead
        of returning LAUNCH_FAILED, restoring it beside a possible writer, and requeueing.
        """
        self.replay("sbatch", "sbatch_without_parsable")
        self.replay("squeue", "squeue_name_comment_zero")
        _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        task.mkdirs()
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")

        with self.assertRaisesRegex(SlurmBackendError, "accepted.*no recoverable job"):
            backend.submit(task, worker=0)

        self.assertFalse(task.result_path.exists(),
                         "checkpoint was restored while an unidentified job may write it")
        # The old checkpoint is not lost: it stays on disk, untouched, for the operator.
        [aside] = task.result_path.parent.glob(task.result_path.name + ".aside-*")
        self.assertEqual(json.loads(aside.read_text()), COMPLETED)


class TestUnidentifiedSubmission(SlurmBackendBase):
    """sbatch accepted a job but its reply carries no job id.

    The reply is real: plain ``sbatch`` without ``--parsable`` prints "Submitted batch job N",
    standing in for a site wrapper that drops the flag. Every submission carries its own
    ``--comment`` tag, and the job is looked up among the user's jobs by name *and* that tag.
    The captured lookup lists two held jobs that share a name and a script but carry different
    tags: name and script alone would let a retry adopt an older job of the same route.
    """

    OLD = json.dumps(COMPLETED).encode("utf-8")
    TAG_NEWER = "oodbench:9b17e4c2a05d4f3e8c6b2d1a7f904e55"   # job 141565
    TAG_OLDER = "oodbench:3f2a9c0d6e4b4c1fa7d85e9b0c6a1f42"   # job 141564
    TAG_OTHER = "oodbench:00000000000000000000000000000000"   # in no captured reply
    THEIRS_B = "<jobdir>/b/route_x_seed42.sbatch"             # job 141459, by %o

    def submit_unidentified(self, tag):
        """Submit with a reply that has no id; this submission's tag is ``tag``."""
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        task.mkdirs()
        task.result_path.write_bytes(self.OLD)
        self.replay("sbatch", "sbatch_without_parsable")
        self.replay("squeue", "squeue_name_comment_two_tagged", when="%i|%k")
        self.replay("squeue", "squeue_gone_recent")
        self.replay("sacct", "sacct_cancelled_pending")
        self.replay("scancel", "scancel_ok")
        # create=True only so the test could fail on its assertion before the fix existed.
        tag_patch = unittest.mock.patch("oodbench.backends.slurm._submission_tag",
                                        return_value=tag, create=True)
        tag_patch.start()
        self.addCleanup(tag_patch.stop)
        return cfg, backend, task

    def asides(self, task):
        return sorted(task.result_path.parent.glob(task.result_path.name + ".aside-*"))

    def test_the_job_carrying_our_tag_is_supervised(self):
        """RED BEFORE THE FIX:

            SlurmBackendError: sbatch accepted a job but returned no recoverable job identity;
            ... There is no job of yours named oodbench-route_1_a running the script ...
        """
        _, backend, task = self.submit_unidentified(self.TAG_NEWER)

        attempt = backend.submit(task, worker=0)

        self.assertEqual(attempt.handle, "141565")
        self.assertIsNone(attempt.outcome)
        self.assertEqual(self.calls("squeue")[0], [
            "-h", "-u", EFFECTIVE_USER, "--name", "oodbench-route_1_a", "-o", "%i|%k"])
        wrapper = task.job_script.with_suffix(".sbatch").read_text(encoding="utf-8")
        self.assertIn(f"#SBATCH --comment={self.TAG_NEWER}\n", wrapper)
        [aside] = self.asides(task)
        with unittest.mock.patch("oodbench.backends.slurm._CANCEL_POLL_S", 0):
            backend.shutdown()
        self.assertEqual(self.calls("scancel"), [["141565"]],
                         "the identified job was not cancelled at shutdown")
        self.assertFalse(aside.exists())

    def test_an_older_job_of_the_same_route_is_never_adopted(self):
        """RED BEFORE THE FIX:

            AssertionError: SlurmBackendError not raised

        The new job is not visible yet. An older job of this route has the same name and the
        same script path (``%o``, from the capture with three held jobs), so a name-and-script
        lookup adopted it. Its tag (``%k``) is not this submission's, so it is left alone.
        """
        _, backend, task = self.submit_unidentified(self.TAG_OTHER)
        self.replay("squeue", "squeue_name_cmd_three_matches", when="%i|%o",
                    replace={self.THEIRS_B: str(task.job_script.with_suffix(".sbatch"))})

        with self.assertRaises(SlurmBackendError) as raised:
            backend.submit(task, worker=0)

        message = str(raised.exception)
        self.assertIn(".aside-", message, "the error does not say where the old checkpoint is")
        [aside] = self.asides(task)
        self.assertEqual(aside.read_bytes(), self.OLD)
        self.assertFalse(task.result_path.exists(), "restored beside a possible writer")
        for needed in (str(aside), str(task.result_path), self.TAG_OTHER, "scancel"):
            self.assertIn(needed, message)
        self.assertEqual(self.calls("scancel"), [], "cancelled a job that is not ours")
        self.assertEqual(backend._submitted, [])

    def test_every_submission_gets_its_own_tag(self):
        """RED BEFORE THE FIX:

            AssertionError: 0 != 2 : a wrapper carried no submission tag
        """
        self.replay("sbatch", "sbatch_parsable")
        _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        wrapper = task.job_script.with_suffix(".sbatch")
        tags = []
        for _ in range(2):
            backend.submit(task, worker=0)
            tags += re.findall(r"^#SBATCH --comment=(oodbench:[0-9a-f]{32})$",
                               wrapper.read_text(encoding="utf-8"), re.M)
        self.assertEqual(len(tags), 2, "a wrapper carried no submission tag")
        self.assertNotEqual(tags[0], tags[1])

    def test_the_lookup_asks_for_the_effective_user_not_the_environment(self):
        """RED BEFORE THE FIX:

            AssertionError: 'someone-else' != '<effective user>'

        ``getpass.getuser()`` reads ``LOGNAME``/``USER`` first. A stale or overridden variable
        then made the lookup search another user's jobs.
        """
        environ = {name: "someone-else" for name in ("LOGNAME", "USER", "LNAME", "USERNAME")}
        with unittest.mock.patch.dict(os.environ, environ):
            _, backend, task = self.submit_unidentified(self.TAG_OTHER)
            with self.assertRaises(SlurmBackendError):
                backend.submit(task, worker=0)
        self.assertEqual(self.calls("squeue")[0][2], EFFECTIVE_USER)

    def test_an_unconfirmed_cancel_keeps_the_old_checkpoint_aside(self):
        """RED BEFORE THE FIX:

            AssertionError: '.aside-' not found in 'SLURM job 141229 has no confirmed terminal
            state 0s after scancel; ...'

        The reply ``141229 trailing noise`` is **synthetic** (no captured reply has an id
        followed by noise). Its id is recovered, but the job never confirms an end, so the old
        checkpoint must stay aside: restoring it could race the job's own writes.
        """
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        task.mkdirs()
        task.result_path.write_bytes(self.OLD)
        self.replay("sbatch", {"rc": 0, "stdout": "141229 trailing noise\n", "stderr": ""})
        self.replay("squeue", "squeue_running")
        self.replay("scancel", "scancel_ok")

        with unittest.mock.patch("oodbench.backends.slurm._CANCEL_WAIT_S", 0), \
                unittest.mock.patch("oodbench.backends.slurm._CANCEL_POLL_S", 0), \
                self.assertRaises(SlurmBackendError) as raised:
            backend.submit(task, worker=0)

        message = str(raised.exception)
        self.assertIn(".aside-", message)
        [aside] = self.asides(task)
        self.assertEqual(aside.read_bytes(), self.OLD)
        self.assertIn("not restored", message)
        self.assertIn(str(aside), message)
        self.assertFalse(task.result_path.exists())
        self.assertIn("141229", backend._submitted, "shutdown can no longer cancel the job")


class TestSqueueErrors(SlurmBackendBase):
    """A failing ``squeue`` is told apart from a job that has left the queue.

    A real ``squeue`` exits 1 with "Invalid job id specified" once a finished job is purged;
    that means *gone*. Any other failure says nothing about the job. The error used here,
    ``squeue_unknown_cluster``, is real ``squeue`` error text, captured for an unknown ``-M``
    cluster rather than for this exact command line.
    """

    OLD = json.dumps(COMPLETED).encode("utf-8")

    def submitted(self, sacct, *, old=True):
        self.replay("sbatch", "sbatch_parsable")
        self.replay("squeue", "squeue_unknown_cluster")
        self.replay("sacct", *sacct)
        self.replay("scancel", "scancel_ok")
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        task.mkdirs()
        if old:
            task.result_path.write_bytes(self.OLD)
        return cfg, backend, task, backend.submit(task, worker=0)

    def test_an_squeue_error_with_no_accounting_never_settles(self):
        """RED BEFORE THE FIX:

            AssertionError: SlurmBackendError not raised

        Both tools are silent and ``squeue`` failed: the job may still be running. Settling
        it as a FAULT deleted the old checkpoint and let the route be retried beside it.
        """
        clock = _Clock()
        with unittest.mock.patch("time.time", clock):
            cfg, backend, task, attempt = self.submitted(["sacct_never_issued"])
            start = clock.now
            self.assertFalse(backend.poll(attempt))
            clock.now = start + 179.0
            self.assertFalse(backend.poll(attempt))
            clock.now = start + 180.0
            with self.assertRaises(SlurmBackendError) as raised:
                backend.poll(attempt)

        self.assertIsNone(attempt.outcome)
        [aside] = task.result_path.parent.glob(task.result_path.name + ".aside-*")
        self.assertEqual(aside.read_bytes(), self.OLD)
        message = str(raised.exception)
        for needed in ("141229", "No cluster 'nosuchcluster'", str(aside), "scancel 141229"):
            self.assertIn(needed, message)
        self.assertEqual(self.calls("scancel"), [])
        self.assertIn("141229", backend._submitted, "shutdown can no longer cancel the job")

        # Shutdown later confirms the cancel: the old checkpoint still stays for the operator.
        self.replay("squeue", "squeue_purged")
        self.replay("sacct", "sacct_cancelled_running")
        with unittest.mock.patch("oodbench.backends.slurm._CANCEL_POLL_S", 0):
            backend.shutdown()
        self.assertTrue(aside.exists(), "the kept checkpoint was deleted at shutdown")

    def test_an_squeue_error_defers_to_a_terminal_accounting_state(self):
        """Green before and after: accounting's terminal state is positive evidence."""
        _, backend, _, attempt = self.submitted(["sacct_failed_exit3"])
        self.assertTrue(backend.poll(attempt))
        self.assertIs(attempt.outcome, AttemptOutcome.EXITED)

    def test_an_squeue_error_with_live_accounting_stays_in_flight(self):
        """Green before and after."""
        _, backend, _, attempt = self.submitted(["sacct_running"])
        self.assertFalse(backend.poll(attempt))
        self.assertIsNone(attempt.outcome)


class TestInheritedSqueueSettings(SlurmBackendBase):
    """``squeue`` reads ``SQUEUE_*`` variables from the environment as if they were options.

    A user with ``SQUEUE_STATES=PENDING`` in their shell profile sees only pending jobs, so a
    RUNNING job prints nothing and reads as gone. The fake ``squeue`` here is synthetic: it
    applies that one filter the way squeue(1) documents it and otherwise prints the captured
    ``RUNNING`` reply, or this submission's tagged row for the name lookup.
    """

    TAG = "oodbench:9b17e4c2a05d4f3e8c6b2d1a7f904e55"

    def setUp(self):
        super().setUp()
        env_patch = unittest.mock.patch.dict(os.environ, {"SQUEUE_STATES": "PENDING"})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.tool("squeue", f'''
            import os, sys
            if os.environ.get("SQUEUE_STATES", "RUNNING") == "RUNNING":
                print("141229|{self.TAG}" if "%i|%k" in sys.argv else "RUNNING")
        ''')

    def test_a_running_job_is_not_lost_to_an_inherited_state_filter(self):
        """RED BEFORE THE FIX:

            AssertionError: True is not false : a RUNNING job hidden by SQUEUE_STATES was
            settled as a fault
        """
        self.replay("sbatch", "sbatch_parsable")
        self.replay("sacct", "sacct_never_issued")
        self.replay("scancel", "scancel_ok")
        clock = _Clock()
        with unittest.mock.patch("time.time", clock):
            # Long enough that 180 s RUNNING is not a route timeout.
            _, backend, task = self.backend_and_task(
                execution={"route_timeout_s": 3600}, slurm={"submit_interval_s": 0})
            attempt = backend.submit(task, worker=0)
            start = clock.now
            self.assertFalse(backend.poll(attempt))
            clock.now = start + 180.0
            self.assertFalse(backend.poll(attempt),
                             "a RUNNING job hidden by SQUEUE_STATES was settled as a fault")

        self.assertIsNone(attempt.outcome)
        self.assertEqual(self.calls("scancel"), [])

    def test_the_tag_lookup_is_not_narrowed_by_an_inherited_state_filter(self):
        """RED BEFORE THE FIX:

            SlurmBackendError: sbatch accepted a job but returned no recoverable job identity;
            ... There is no job of yours named oodbench-route_1_a ...
        """
        self.replay("sbatch", "sbatch_without_parsable")
        self.replay("sacct", "sacct_running")
        self.replay("scancel", "scancel_ok")
        tag_patch = unittest.mock.patch("oodbench.backends.slurm._submission_tag",
                                        return_value=self.TAG)
        tag_patch.start()
        self.addCleanup(tag_patch.stop)
        _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})

        attempt = backend.submit(task, worker=0)

        self.assertEqual(attempt.handle, "141229")
        self.assertIsNone(attempt.outcome)

    def test_scheduler_connection_settings_still_reach_squeue(self):
        """Only the ``SQUEUE_*`` defaults are dropped; ``SLURM_CONF`` and the rest pass."""
        seen = self.site.root / "squeue.env"
        self.tool("squeue", f'''
            import json, os
            from pathlib import Path
            kept = {{k: v for k, v in os.environ.items() if k in ("SLURM_CONF", "SQUEUE_STATES")}}
            Path({str(seen)!r}).write_text(json.dumps(kept), encoding="utf-8")
            print("RUNNING")
        ''')
        self.replay("sbatch", "sbatch_parsable")
        with unittest.mock.patch.dict(os.environ, {"SLURM_CONF": "/etc/slurm/other.conf"}):
            _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
            attempt = backend.submit(task, worker=0)
            self.assertFalse(backend.poll(attempt))

        env = json.loads(seen.read_text(encoding="utf-8"))
        self.assertEqual(env.get("SLURM_CONF"), "/etc/slurm/other.conf")
        self.assertNotIn("SQUEUE_STATES", env)


class TestUnsettledJobMarker(SlurmBackendBase):
    """A route with no old checkpoint to set aside is held by a ``<checkpoint>.unsettled-<ns>``
    file from before sbatch until its job is settled. A job left unsettled keeps it, rewritten
    to name the job; the next run refuses to start until the operator has stopped the job and
    deleted the file."""

    def markers(self, task):
        return sorted(task.result_path.parent.glob(task.result_path.name + ".unsettled-*"))

    def tools_present(self):
        self.tool("sbatch", "raise SystemExit(0)")
        self.tool("squeue", "raise SystemExit(0)")

    def assert_next_run_refuses(self, cfg, marker):
        self.tools_present()
        with self.assertRaises(SlurmBackendError) as raised:
            SlurmBackend(cfg, self.log).preflight()
        self.assertIn("1 unsettled-job marker", str(raised.exception))
        self.assertIn(str(marker), str(raised.exception))

    def test_an_unidentified_first_attempt_leaves_a_marker(self):
        """RED BEFORE THE FIX:

            ValueError: not enough values to unpack (expected 1, got 0)

        No earlier checkpoint, so no aside file: nothing stopped the next run from submitting
        a second job beside one this run could not identify.
        """
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        self.replay("sbatch", "sbatch_without_parsable")
        self.replay("squeue", "squeue_name_comment_zero")
        with self.assertRaises(SlurmBackendError) as raised:
            backend.submit(task, worker=0)

        [marker] = self.markers(task)
        note = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(note["job_name"], "oodbench-route_1_a")
        self.assertRegex(note["comment"], r"\Aoodbench:[0-9a-f]{32}\Z")
        self.assertEqual(note["checkpoint"], str(task.result_path))
        self.assertIn(str(marker), str(raised.exception))
        self.assert_next_run_refuses(cfg, marker)

    def test_an_unconfirmed_kill_leaves_a_marker(self):
        """RED BEFORE THE FIX:

            ValueError: not enough values to unpack (expected 1, got 0)
        """
        self.replay("sbatch", "sbatch_parsable")
        self.replay("squeue", "squeue_running")
        self.replay("scancel", "scancel_ok")
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        attempt = backend.submit(task, worker=0)

        with unittest.mock.patch("oodbench.backends.slurm._CANCEL_WAIT_S", 0), \
                unittest.mock.patch("oodbench.backends.slurm._CANCEL_POLL_S", 0), \
                self.assertRaises(SlurmBackendError) as raised:
            backend.kill(attempt, "wall-clock timeout")

        [marker] = self.markers(task)
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["job"], "141229")
        self.assertIn(str(marker), str(raised.exception))
        self.assert_next_run_refuses(cfg, marker)

    def test_an_unconfirmed_shutdown_leaves_a_marker(self):
        """RED BEFORE THE FIX:

            ValueError: not enough values to unpack (expected 1, got 0)
        """
        self.replay("sbatch", "sbatch_parsable")
        self.replay("squeue", "squeue_running")
        self.replay("scancel", "scancel_ok")
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        backend.submit(task, worker=0)

        with unittest.mock.patch("oodbench.backends.slurm._CANCEL_WAIT_S", 0), \
                unittest.mock.patch("oodbench.backends.slurm._CANCEL_POLL_S", 0), \
                self.assertLogs("test-slurm-backend", level="WARNING"):
            backend.shutdown()

        [marker] = self.markers(task)
        self.assert_next_run_refuses(cfg, marker)

    def sbatch_seeing_markers(self, task, answer):
        """An sbatch that records which marker files existed when it ran, then replays
        ``answer`` (a captured case)."""
        seen = self.site.root / "sbatch.saw"
        captured = slurm_replay.case(answer)
        pattern = task.result_path.name + ".unsettled-*"
        self.tool("sbatch", f'''
            import sys
            from pathlib import Path
            found = sorted(p.name for p in Path({str(task.result_path.parent)!r}).glob({pattern!r}))
            Path({str(seen)!r}).write_text("\\n".join(found), encoding="utf-8")
            sys.stdout.write({captured["stdout"]!r})
            sys.stderr.write({captured["stderr"]!r})
            sys.exit({captured["rc"]!r})
        ''')
        return seen

    def test_a_first_submission_is_marked_before_sbatch(self):
        """RED BEFORE THE FIX:

            AssertionError: '' is not true : sbatch ran with nothing on disk to stop a second
            run beside its job

        With no earlier checkpoint there is nothing to set aside, so a marker stands in for
        the aside file while the job is in flight. A run killed outright (no shutdown) then
        still blocks the next start.
        """
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        seen = self.sbatch_seeing_markers(task, "sbatch_parsable")
        attempt = backend.submit(task, worker=0)

        self.assertEqual(attempt.handle, "141229")
        self.assertTrue(seen.read_text(),
                        "sbatch ran with nothing on disk to stop a second run beside its job")
        [marker] = self.markers(task)
        self.assertEqual(seen.read_text(), marker.name)
        note = json.loads(marker.read_text(encoding="utf-8"))
        wrapper = task.job_script.with_suffix(".sbatch").read_text(encoding="utf-8")
        self.assertIn(f"--comment={note['comment']}", wrapper)
        self.assertEqual(note["job_name"], "oodbench-route_1_a")
        self.assert_next_run_refuses(cfg, marker)

    def test_settling_the_job_removes_its_marker(self):
        """RED BEFORE THE FIX:

            ValueError: not enough values to unpack (expected 1, got 0)
        """
        for path in ("poll", "kill", "shutdown"):
            with self.subTest(path=path):
                self.replay("sbatch", "sbatch_parsable")
                self.replay("squeue", "squeue_gone_recent")
                self.replay("sacct", "sacct_completed" if path == "poll"
                            else "sacct_cancelled_running")
                self.replay("scancel", "scancel_ok")
                cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
                attempt = backend.submit(task, worker=0)
                [marker] = self.markers(task)
                with unittest.mock.patch("oodbench.backends.slurm._CANCEL_POLL_S", 0):
                    if path == "poll":
                        self.assertTrue(backend.poll(attempt))
                    elif path == "kill":
                        backend.kill(attempt, "test cancellation")
                    else:
                        backend.shutdown()
                self.assertEqual(self.markers(task), [])
                self.tools_present()
                SlurmBackend(cfg, self.log).preflight()

    def test_a_refused_first_submission_leaves_no_marker(self):
        """RED BEFORE THE FIX:

            AssertionError: '' is not true : the marker was not written before sbatch
        """
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        seen = self.sbatch_seeing_markers(task, "sbatch_invalid_partition")
        attempt = backend.submit(task, worker=0)

        self.assertIs(attempt.outcome, AttemptOutcome.LAUNCH_FAILED)
        self.assertTrue(seen.read_text(), "the marker was not written before sbatch")
        self.assertEqual(self.markers(task), [])
        self.tools_present()
        SlurmBackend(cfg, self.log).preflight()

    def test_a_full_disk_while_marking_aborts_before_sbatch(self):
        """RED BEFORE THE FIX:

            AssertionError: Lists differ: [['--parsable', ...]] != [] : sbatch ran with no
            marker on disk

        The ``ENOSPC`` is synthetic (injected at ``os.write``); no scheduler output is faked.
        """
        self.replay("sbatch", "sbatch_parsable")
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        with unittest.mock.patch("os.write",
                                 side_effect=OSError(errno.ENOSPC, "No space left on device")):
            attempt = backend.submit(task, worker=0)

        self.assertEqual(self.calls("sbatch"), [], "sbatch ran with no marker on disk")
        self.assertIs(attempt.outcome, AttemptOutcome.LAUNCH_FAILED)
        self.assertIn("No space left on device", attempt.detail)
        self.assertEqual(self.markers(task), [], "a partial marker was left behind")

    def test_a_failed_note_update_keeps_the_first_marker(self):
        """RED BEFORE THE FIX:

            ValueError: not enough values to unpack (expected 1, got 0)

        The job is unidentified and the note naming the reason cannot be written (a
        synthetic ``ENOSPC`` on every write after submission). The marker written before
        sbatch must still stop the next run.
        """
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        self.replay("sbatch", "sbatch_without_parsable")
        self.replay("squeue", "squeue_name_comment_zero")
        real_write = os.write

        def full_after_sbatch(fd, data):
            if self.calls("sbatch"):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        with unittest.mock.patch("os.write", full_after_sbatch), \
                self.assertRaises(SlurmBackendError) as raised:
            backend.submit(task, worker=0)

        [marker] = self.markers(task)
        self.assertIn(str(marker), str(raised.exception))
        self.assert_next_run_refuses(cfg, marker)

    def test_a_marker_look_alike_does_not_refuse(self):
        """Green before and after."""
        cfg, backend, task = self.backend_and_task()
        task.mkdirs()
        task.result_path.with_name(task.result_path.name + ".unsettled-notes").write_text("x")
        self.tools_present()
        backend.preflight()


class TestLeftoverAsideBlocksStart(SlurmBackendBase):
    """A set-aside checkpoint left on disk means an earlier run stopped without settling a
    route. Starting anyway would plan that route from whatever is (or is not) at its checkpoint
    path and could later overwrite the only good record, so the backend refuses until the
    operator has put each file back or deleted it."""

    def leave_aside(self, cfg, task, payload=b'{"old": true}'):
        task.mkdirs()
        aside = task.result_path.with_name(task.result_path.name + ".aside-1791096796277153971")
        aside.write_bytes(payload)
        return aside

    def tools_present(self):
        self.tool("sbatch", "raise SystemExit(0)")
        self.tool("squeue", "raise SystemExit(0)")

    def test_a_leftover_aside_file_refuses_the_start(self):
        """RED BEFORE THE FIX:

            AssertionError: SlurmBackendError not raised
        """
        cfg, backend, task = self.backend_and_task()
        aside = self.leave_aside(cfg, task)
        self.tools_present()

        with self.assertRaises(SlurmBackendError) as raised:
            backend.preflight()

        message = str(raised.exception)
        self.assertIn(f"{aside} -> {task.result_path}\n", message)
        for needed in ("scancel", "mv <aside> <checkpoint>", "aside.jsonl"):
            self.assertIn(needed, message)
        self.assertEqual(aside.read_bytes(), b'{"old": true}', "the refusal touched the file")
        self.assertEqual(self.calls("sbatch"), [])

    def test_every_leftover_file_is_listed(self):
        """RED BEFORE THE FIX:

            AssertionError: SlurmBackendError not raised
        """
        self.site.add_route("static/s1/base/route_2_b.xml")
        cfg, backend, _ = self.backend_and_task()
        tasks = plan_mod.build_tasks(
            plan_mod.discover(Path(cfg.routes["root"])),
            Path(cfg.routes["root"]), Path(cfg.output["root"]), base_seed=cfg.seed,
            repetitions=1)
        asides = [self.leave_aside(cfg, task) for task in tasks]
        self.assertEqual(len(asides), 2)
        self.tools_present()

        with self.assertRaises(SlurmBackendError) as raised:
            backend.preflight()

        self.assertIn("2 set-aside", str(raised.exception))
        for aside in asides:
            self.assertIn(str(aside), str(raised.exception))

    def test_a_refused_submission_blocks_the_next_run(self):
        """RED BEFORE THE FIX:

            AssertionError: SlurmBackendError not raised

        The first run is T6's: sbatch's reply carries no id and no job matches, so the old
        checkpoint stays aside. A second backend over the same output root must not start.
        """
        cfg, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        task.mkdirs()
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")
        self.replay("sbatch", "sbatch_without_parsable")
        self.replay("squeue", "squeue_name_comment_zero")
        with self.assertRaises(SlurmBackendError):
            backend.submit(task, worker=0)

        with self.assertRaisesRegex(SlurmBackendError, r"1 set-aside"):
            SlurmBackend(cfg, self.log).preflight()

    def test_a_clean_output_root_still_starts(self):
        """Green before and after: the ledger ``_runner/aside.jsonl`` and look-alike names are
        not set-aside checkpoints."""
        cfg, backend, task = self.backend_and_task()
        task.mkdirs()
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")
        ledger = Path(cfg.output["root"]) / "_runner" / "aside.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("{}\n", encoding="utf-8")
        task.result_path.with_name(task.result_path.name + ".aside-notes").write_text("x")
        self.tools_present()

        backend.preflight()

    def test_an_output_root_that_does_not_exist_yet_starts(self):
        """Green before and after: a first run has no output root to scan."""
        cfg, backend, _ = self.backend_and_task()
        shutil.rmtree(cfg.output["root"], ignore_errors=True)
        self.assertFalse(Path(cfg.output["root"]).exists())
        self.tools_present()

        backend.preflight()


class TestSchedulerSlotsAreNotMachines(SlurmBackendBase):

    def test_cluster_transient_does_not_quarantine_slurm_slots(self):
        """RED BEFORE THE FIX:

            AssertionError: 4 != 1 : scheduler slots were retired as machines after a
            cluster-wide submission transient

        A local slot owns one stable machine/GPU/port window, so quarantine has physical
        meaning there. A SLURM slot owns only concurrency: its next submission may land on a
        different node. Retiring slot indices turns one shared scheduler transient into exit 4
        and leaves later routes unattempted.
        """
        self.site.add_route("static/s1/base/route_2_a.xml")
        self.site.add_route("static/s1/base/route_3_a.xml")
        submissions = self.site.root / "sbatch.calls"
        self.tool("sbatch", f'''
            import sys
            from pathlib import Path
            path = Path({str(submissions)!r})
            with path.open("a", encoding="utf-8") as fh:
                fh.write("attempted\\n")
            sys.exit(1)
        ''')
        for tool in ("squeue", "sacct", "scancel"):
            self.tool(tool, "pass")
        cfg = config_mod.load(self.site.config(
            execution={"backend": "slurm", "poll_interval_s": 1},
            slurm={"max_parallel": 2, "submit_interval_s": 0},
            retry={"record_budget": 1, "infra_budget": 1, "tickruntime_budget": 0,
                   "killed_budget": 2, "worker_quarantine_after": 1},
        ))
        backend = SlurmBackend(cfg, self.log)
        runner = run_benchmark.Runner(
            cfg, argparse.Namespace(limit=None, dry_run=False, force=False))
        tasks = runner.plan()
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")

        report = runner.run(tasks, state, backend)

        self.assertEqual(report.exit_code(), EXIT_PARTIAL,
                         "scheduler slots were retired as machines after a cluster-wide "
                         "submission transient")
        self.assertEqual(runner.quarantined, [])
        self.assertEqual(len(submissions.read_text().splitlines()), 3,
                         "quarantining slot indices aborted before every route was attempted")


class TestSchedulerGpuAllocation(SlurmBackendBase):

    def test_allocation_scoped_vulkan_adapter_can_repeat_across_physical_gpus(self):
        """RED BEFORE THE S3-R1A FIX:

            ConfigError: gpus[1].vulkan=0 is already claimed by gpus[0] (cuda=5).

        Real one-GPU cgroups mapped scheduler-global GPUs 5 and 6 to different NVML UUID/PCI
        devices while independently exposing each allocated NVIDIA device as logical CUDA 0
        and Vulkan adapter 0. Reusing that job-local adapter is not GPU stacking: the scheduler
        allocations are distinct physical GPUs.
        """
        calls = self.site.root / "sbatch-allocation-scope.calls"
        self.tool("sbatch", f'''
            import os, subprocess, sys
            from pathlib import Path

            calls = Path({str(calls)!r})
            n = int(calls.read_text()) if calls.exists() else 0
            calls.write_text(str(n + 1), encoding="utf-8")
            global_gpu = ("5", "6")[n]
            env = dict(os.environ)
            env["CUDA_VISIBLE_DEVICES"] = "0"
            env["SLURM_JOB_GPUS"] = global_gpu
            subprocess.run(["bash", sys.argv[-1]], env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True)
            print(730 + n)
        ''')
        _, backend, task = self.backend_and_task(
            gpus=[{"cuda": 5, "vulkan": 0}, {"cuda": 6, "vulkan": 0}],
            slurm={
                "max_parallel": 2,
                "submit_interval_s": 0,
                "vulkan_index_scope": "allocation",
            },
        )

        attempts = [backend.submit(task, worker) for worker in (0, 1)]

        self.assertEqual([attempt.handle for attempt in attempts], ["730", "731"])
        trace = self.site.trace_rows()
        self.assertEqual(len(trace), 2)
        self.assertEqual([row["cuda"] for row in trace], ["0", "0"])
        self.assertEqual([row["gpu_rank"] for row in trace], ["0", "0"])
        self.assertNotEqual(trace[0]["port"], trace[1]["port"])

    def test_allocation_scope_rejects_multiple_scheduler_visible_cuda_devices(self):
        """An allocation-local adapter is ambiguous if the job can see multiple CUDA GPUs."""
        self.tool("sbatch", '''
            import os, subprocess, sys

            env = dict(os.environ)
            env["CUDA_VISIBLE_DEVICES"] = "0,1"
            env["SLURM_JOB_GPUS"] = "5"
            completed = subprocess.run(["bash", sys.argv[-1]], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if completed.returncode:
                raise SystemExit(completed.returncode)
            print("732")
        ''')
        _, backend, task = self.backend_and_task(
            gpus=[{"cuda": 5, "vulkan": 0}],
            slurm={"submit_interval_s": 0, "vulkan_index_scope": "allocation"},
        )

        attempt = backend.submit(task, worker=0)

        self.assertIs(attempt.outcome, AttemptOutcome.LAUNCH_FAILED)
        self.assertEqual(self.site.trace_rows(), [],
                         "a multi-GPU allocation reached the evaluator under job-local scope")

    def test_allocation_scope_rejects_multiple_scheduler_global_gpus(self):
        """One logical CUDA device cannot disambiguate a multi-GPU global allocation."""
        self.tool("sbatch", '''
            import os, subprocess, sys

            env = dict(os.environ)
            env["CUDA_VISIBLE_DEVICES"] = "0"
            env["SLURM_JOB_GPUS"] = "5,6"
            completed = subprocess.run(["bash", sys.argv[-1]], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if completed.returncode:
                raise SystemExit(completed.returncode)
            print("733")
        ''')
        _, backend, task = self.backend_and_task(
            gpus=[{"cuda": 5, "vulkan": 0}, {"cuda": 6, "vulkan": 0}],
            slurm={"submit_interval_s": 0, "vulkan_index_scope": "allocation"},
        )

        attempt = backend.submit(task, worker=0)

        self.assertIs(attempt.outcome, AttemptOutcome.LAUNCH_FAILED)
        self.assertEqual(self.site.trace_rows(), [],
                         "a multi-global-GPU allocation reached the evaluator")

    def test_job_preserves_scheduler_cuda_and_maps_its_global_gpu_to_vulkan(self):
        """RED BEFORE THE FIX:

            AssertionError: '0' != '7' : the wrapper overwrote SLURM's CUDA allocation

        SLURM owns ``CUDA_VISIBLE_DEVICES`` and may remap an allocated global GPU to a logical
        index inside its cgroup. ``SLURM_JOB_GPUS`` remains global, so it is the lookup key for
        the config's site-validated CUDA-to-Vulkan mapping; copying it into CUDA would be wrong.
        """
        self.tool("sbatch", '''
            import os, subprocess, sys
            env = dict(os.environ)
            env["CUDA_VISIBLE_DEVICES"] = "7"
            env["SLURM_JOB_GPUS"] = "2"
            subprocess.run(["bash", sys.argv[-1]], env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True)
            print("707")
        ''')
        _, backend, task = self.backend_and_task(
            gpus=[{"cuda": 2, "vulkan": 5}], slurm={"submit_interval_s": 0})

        attempt = backend.submit(task, worker=0)

        self.assertEqual(attempt.handle, "707")
        trace = self.site.trace_rows()
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["cuda"], "7",
                         "the wrapper overwrote SLURM's CUDA allocation")
        self.assertEqual(trace[0]["gpu_rank"], "5",
                         "the configured global-CUDA to Vulkan mapping was ignored")

    def test_activation_cannot_clobber_the_scheduler_allocation(self):
        """RED BEFORE THE S2 REVIEW FIX:

            AssertionError: <AttemptOutcome.LAUNCH_FAILED: 'launch_failed'> is not None :
            activation replaced SLURM's allocation before GPU mapping

        Scheduler allocation variables exist at job entry. Capture them before executing the
        caller's activation commands, then restore CUDA and perform Vulkan mapping from the
        captured global ID.
        """
        self.tool("sbatch", '''
            import os, subprocess, sys
            env = dict(os.environ)
            env["CUDA_VISIBLE_DEVICES"] = "7"
            env["SLURM_JOB_GPUS"] = "2"
            subprocess.run(["bash", sys.argv[-1]], env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True)
            print("708")
        ''')
        _, backend, task = self.backend_and_task(
            environment={
                "activate": [
                    "export CUDA_VISIBLE_DEVICES=99",
                    "export SLURM_JOB_GPUS=99",
                ],
            },
            gpus=[{"cuda": 2, "vulkan": 5}],
            slurm={"submit_interval_s": 0},
        )

        attempt = backend.submit(task, worker=0)

        self.assertIsNone(attempt.outcome,
                          "activation replaced SLURM's allocation before GPU mapping")
        self.assertEqual(attempt.handle, "708")
        trace = self.site.trace_rows()
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["cuda"], "7")
        self.assertEqual(trace[0]["gpu_rank"], "5")


class TestSbatchDirectiveQuoting(SlurmBackendBase):

    def test_log_paths_with_spaces_remain_one_sbatch_argument(self):
        """RED BEFORE THE FIX:

            AssertionError: Lists differ: ['#SBATCH', '--output=/tmp/.../output', 'root',
            'with', 'spaces/...out'] != ['#SBATCH', '--output=/tmp/.../output root with
            spaces/...out'] : --output path was tokenised at its spaces

        ``#SBATCH`` lines are parsed by SLURM rather than by the script's shell, but SLURM
        accepts quoted arbitrary strings. Every generated string value must remain one option.
        """
        self.tool("sbatch", 'print("808")')
        output_root = self.site.root / "output root with spaces"
        _, backend, task = self.backend_and_task(
            output={"root": str(output_root)}, slurm={"submit_interval_s": 0})

        attempt = backend.submit(task, worker=0)

        self.assertEqual(attempt.handle, "808")
        wrapper = task.job_script.with_suffix(".sbatch").read_text(encoding="utf-8")
        for flag, expected in (("--output", task.stdout_path), ("--error", task.stderr_path)):
            directive = next(line for line in wrapper.splitlines()
                             if line.startswith(f"#SBATCH {flag}="))
            self.assertEqual(shlex.split(directive), ["#SBATCH", f"{flag}={expected}"],
                             f"{flag} path was tokenised at its spaces")


if __name__ == "__main__":
    unittest.main()
