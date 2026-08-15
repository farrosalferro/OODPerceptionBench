"""SLURM backend behavior against scheduler executables faked on ``PATH``.

These tests exercise the backend at its public seam: route attempts go through real generated
job wrappers and subprocess calls to stand-in ``sbatch``/``squeue``/``sacct``/``scancel``
commands.  No scheduler or GPU is required.
"""

import argparse
import json
import logging
import os
import shlex
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

from tests.test_integration_local import Site


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
        self.tool("sbatch", 'print("accepted but identity unavailable")')
        _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})
        task.mkdirs()
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")

        with self.assertRaisesRegex(SlurmBackendError, "accepted.*no recoverable job"):
            backend.submit(task, worker=0)

        self.assertFalse(task.result_path.exists(),
                         "checkpoint was restored while an unidentified job may write it")


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
