"""The captured-output fakes in ``tests.slurm_replay`` reproduce what the scheduler printed."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from oodbench.backends.base import AttemptOutcome
from tests import slurm_replay
from tests.test_slurm_backend import SlurmBackendBase


def _run(tool, *args):
    return subprocess.run([str(tool), *args], capture_output=True)


class TestReplayFidelity(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_every_captured_case_replays_byte_for_byte(self):
        cases = slurm_replay.load()["cases"]
        self.assertGreaterEqual(len(cases), 30)
        for name, captured in cases.items():
            with self.subTest(case=name):
                program = captured["argv"][0]
                tool = slurm_replay.install(self.bin, program, name)
                got = _run(tool, *captured["argv"][1:])
                self.assertEqual(got.returncode, captured["rc"])
                self.assertEqual(got.stdout, captured["stdout"].encode("utf-8"))
                self.assertEqual(got.stderr, captured["stderr"].encode("utf-8"))

    def test_answers_are_consumed_in_order_and_the_last_repeats(self):
        tool = slurm_replay.install(self.bin, "sacct", *slurm_replay.timeline("completed",
                                                                                "sacct"))
        seen = [_run(tool, "-X", "-j", "141229").stdout for _ in range(5)]
        self.assertEqual(seen, [b"PENDING|0:0\n", b"RUNNING|0:0\n", b"COMPLETED|0:0\n",
                                b"COMPLETED|0:0\n", b"COMPLETED|0:0\n"])

    def test_when_selects_a_sequence_and_unmatched_calls_fall_back(self):
        slurm_replay.install(self.bin, "sacct", "sacct_completed")
        tool = slurm_replay.install(self.bin, "sacct", "sacct_times_cancelled_pending",
                                    when="Start,End,Elapsed")
        times = _run(tool, "-X", "-j", "1", "-n", "-P", "-o", "Start,End,Elapsed")
        state = _run(tool, "-X", "-j", "1", "--format=State,ExitCode", "-n", "-P")
        self.assertEqual(times.stdout, b"None|2026-10-04T12:31:25|00:00:00\n")
        self.assertEqual(state.stdout, b"COMPLETED|0:0\n")
        self.assertEqual(slurm_replay.calls(self.bin, "sacct"), [
            ["-X", "-j", "1", "-n", "-P", "-o", "Start,End,Elapsed"],
            ["-X", "-j", "1", "--format=State,ExitCode", "-n", "-P"],
        ])

    def test_a_call_no_sequence_serves_fails_loudly(self):
        tool = slurm_replay.install(self.bin, "squeue", "squeue_name_one_match",
                                    when="--name")
        got = _run(tool, "-h", "-j", "1", "-o", "%T")
        self.assertEqual(got.returncode, 97)
        self.assertIn(b"no sequence matches", got.stderr)

    def test_an_explicit_answer_must_be_complete(self):
        with self.assertRaises(ValueError):
            slurm_replay.install(self.bin, "sbatch", {"rc": 0, "stdout": "1\n"})


class TestBackendAgainstCapturedTimeline(SlurmBackendBase):

    def test_a_real_completed_timeline_settles_as_the_scheduler_reported(self):
        """The harness drives the unchanged backend: PENDING, RUNNING, gone + COMPLETED."""
        self.replay("sbatch", "sbatch_parsable")
        self.replay("squeue", *slurm_replay.timeline("completed", "squeue"))
        self.replay("sacct", "sacct_completed")
        _, backend, task = self.backend_and_task(slurm={"submit_interval_s": 0})

        attempt = backend.submit(task, worker=0)
        self.assertEqual(attempt.handle, "141229")
        self.assertFalse(backend.poll(attempt))  # PENDING
        self.assertFalse(backend.poll(attempt))  # RUNNING
        self.assertTrue(backend.poll(attempt))   # gone from squeue; sacct COMPLETED
        self.assertIs(attempt.outcome, AttemptOutcome.EXITED)
        self.assertEqual(attempt.detail, "SLURM state COMPLETED")
        self.assertEqual(self.calls("sbatch")[0][0], "--parsable")


if __name__ == "__main__":
    unittest.main()
