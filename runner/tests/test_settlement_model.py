"""Regression tests for the attempt-accounting model (DESIGN.md section 6A).

Four cross-review findings were four symptoms of one missing definition: the code treated
"a final record exists on disk" as both "this attempt produced a benchmark result" and "this
route is complete". Both identities are false, in different directions.

``TestFindings`` holds one test per finding. ``TestModelAmendments`` holds the guards for the
parts of the model that were amended during review -- above all the guard that a killed
attempt holding a crash-shaped record still reaches a settled answer in a bounded number of
attempts, because the alternative ("never settle") permanently converts legitimate model
verdicts -- six of them exist in the published records -- into exit 1.
"""

import argparse
import json
import logging
import subprocess
import time
import unittest
from pathlib import Path

import run_benchmark
from oodbench import EXIT_OK, config as config_mod, plan as plan_mod, report as report_mod
from oodbench.backends.base import Attempt, AttemptOutcome
from oodbench.backends.local import LocalBackend
from oodbench.state import RunState

from tests.test_integration_local import Site

SIM_CRASHED = {"_checkpoint": {"progress": [1, 1],
                               "records": [{"status": "Failed - Simulation crashed",
                                            "scores": {"score_composed": 3.0}}]}}

AGENT_CRASHED = {"_checkpoint": {"progress": [1, 1],
                                 "records": [{"status": "Failed - Agent crashed",
                                              "scores": {"score_composed": 12.5}}]}}

COMPLETED = {"_checkpoint": {"progress": [1, 1],
                             "records": [{"status": "Completed",
                                          "scores": {"score_composed": 88.0}}]}}

TICKRUNTIME = {"_checkpoint": {"progress": [1, 1],
                               "records": [{"status": "Failed - TickRuntime",
                                            "scores": {"score_composed": 0.0}}]}}


class _DummyBackend:
    name = "local"
    concurrency = 1

    def cleanup_worker(self, worker):
        pass


class ModelBase(unittest.TestCase):

    def setUp(self):
        self.site = Site()
        self.site.add_route("static/s1/base/route_1_a.xml")
        run_benchmark._interrupted["flag"] = False
        self.log = logging.getLogger("test-settlement")
        self.log.addHandler(logging.NullHandler())
        self.log.propagate = False

    def tearDown(self):
        self.site.cleanup()

    def _task(self, cfg):
        xmls = plan_mod.discover(Path(cfg.routes["root"]))
        task = plan_mod.build_tasks(xmls, Path(cfg.routes["root"]), Path(cfg.output["root"]),
                                    base_seed=cfg.seed, repetitions=1)[0]
        task.mkdirs()
        return task

    def _runner_and_state(self, cfg):
        args = argparse.Namespace(limit=None, dry_run=False, force=False)
        runner = run_benchmark.Runner(cfg, args)
        state = RunState(path=Path(cfg.output["root"]) / "_runner" / "state.json")
        return runner, state

    def _settle(self, runner, state, task, outcome, detail, interrupted=False, worker=0):
        attempt = Attempt(task=task, worker=worker, stdout_path=task.stdout_path,
                          stderr_path=task.stderr_path)
        attempt.outcome = outcome
        attempt.detail = detail
        attempt.finished_at = time.time()
        return runner._settle(attempt, state, _DummyBackend(), interrupted=interrupted)

    def _report(self, runner, tasks, state, cfg):
        return report_mod.build(
            tasks, state, started_at=0.0, config_digest=cfg.digest(),
            config_path=cfg.source_path, seed=cfg.seed, backend="local",
            skipped_done=runner.skipped_done, skipped_exhausted=runner.skipped_exhausted,
            quarantined=runner.quarantined, warnings=runner.warnings,
            interrupted=False, fatal_agent=runner.fatal_agent)


class TestFindings(ModelBase):
    """One test per cross-review finding. Each fails on the pre-fix code."""

    # -- finding 2 ---------------------------------------------------------------------
    def test_a_timeout_kill_does_not_charge_the_record_budget(self):
        """The runner kills a route on the wall clock; CARLA's crash handler can write a
        *final* ``Failed - Simulation crashed`` on the way down. That record is an artefact of
        the kill, not a verdict the model produced -- exactly what the interrupt path already
        refuses to charge. Charging it spent the route's real retries and, once they were gone,
        froze the manufactured record in as the benchmark result at exit 0.

        The kill path for TIMEOUT/FAULT is the *same* ``killpg`` as Ctrl-C; only the
        ``interrupted`` flag differed.
        """
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 3, "record_budget": 1, "tickruntime_budget": 0,
                   "worker_quarantine_after": 99}))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(SIM_CRASHED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)
        # One earlier infra failure on this worker, so a wrongly-reset counter is visible.
        runner.consecutive_infra[0] = 1

        requeue = self._settle(runner, state, task, AttemptOutcome.TIMEOUT,
                               "wall-clock timeout after 3600s")

        st = state.get(task.key)
        self.assertEqual(st.attempts_record, 0,
                         "a wall-clock kill spent the route's RECORD retry budget on a record "
                         "the kill itself may have manufactured")
        self.assertFalse(st.finished,
                         "a kill-manufactured crash record was frozen in as the benchmark "
                         "result")
        self.assertEqual(runner.consecutive_infra[0], 1,
                         "a killed attempt reset the wedged-GPU detector, so a worker that "
                         "kills every route it touches can never be quarantined")
        self.assertTrue(requeue, "the route must be retried, not abandoned")

    # -- finding 5 ---------------------------------------------------------------------
    def test_decide_honours_the_infra_budget_when_a_final_retryable_record_exists(self):
        """``decide()`` gated RUN vs SKIP_EXHAUSTED on the record/tickruntime budgets whenever a
        final record existed, and consulted the infra budget only on the no-record branch. A
        route holding an old retryable record therefore retried past its infra limit forever.
        """
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(AGENT_CRASHED), encoding="utf-8")

        decision = plan_mod.decide(
            task, {"record": 0, "tickruntime": 0, "infra": 3, "killed": 0}, "skip_terminal",
            record_budget=3, tickruntime_budget=0, infra_budget=3, killed_budget=2)

        self.assertIs(decision.decision, plan_mod.Decision.SKIP_EXHAUSTED,
                      "a route whose infrastructure budget is spent was planned to RUN again "
                      "because it happened to hold a final retryable record")
        self.assertFalse(decision.settled,
                         "an infra-exhausted route has no settled answer: this run cannot "
                         "produce the retry it owes")

    # -- finding 6 ---------------------------------------------------------------------
    def test_infra_exhausted_failed_launch_is_reported_incomplete_and_exits_nonzero(self):
        """The previous repair pass correctly stopped a failed launch from destroying the record
        it was about to replace, and correctly left ``finished`` False. But the report derived
        completeness from ``rec.final`` read off disk, and the *preserved* record is final -- so
        a route that never ran, with infrastructure exhausted, reported complete at exit 0.
        """
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 1, "record_budget": 3, "tickruntime_budget": 0,
                   "worker_quarantine_after": 99}))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(AGENT_CRASHED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        requeue = self._settle(runner, state, task, AttemptOutcome.LAUNCH_FAILED,
                               "worker 0 ports [20000] still occupied after reaping")
        self.assertFalse(requeue, "infra budget 1/1 is spent")

        rep = self._report(runner, [task], state, cfg)

        self.assertNotEqual(rep.exit_code(), EXIT_OK,
                            "a sweep that never ran a route, with infrastructure exhausted, "
                            "exited 0 on the strength of a record it preserved but never "
                            "refreshed")
        self.assertEqual([o.key for o in rep.incomplete], [task.key])
        self.assertEqual(rep.to_dict()["totals"]["incomplete"], 1)
        self.assertEqual(json.loads(task.result_path.read_text()), AGENT_CRASHED,
                         "the preserved record must still survive byte-identical")

    # -- finding 9 ---------------------------------------------------------------------
    def test_report_flags_result_files_whose_embedded_seed_is_not_the_configured_one(self):
        """DESIGN.md claimed the runner re-derives the expected seed for every result file it
        counts and fails the report on a disagreement. It was never implemented, and as written
        it was vacuous: the report reads planned paths, which carry the seed by construction.
        The check with content is over the *tree*: a stray ``_seed7.json`` sitting beside the
        counted results means two protocols are mixed in one output root.
        """
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")
        foreign = task.result_path.with_name("route_1_a_seed7.json")
        foreign.write_text(json.dumps(COMPLETED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)
        state.get(task.key).finished = True

        rep = self._report(runner, [task], state, cfg)

        hits = [w for w in rep.warnings if "seed" in w.lower() and "7" in w]
        self.assertTrue(hits,
                        f"a result file carrying a foreign seed was neither counted nor "
                        f"mentioned; the report is silent about a mixed-protocol output root. "
                        f"warnings={rep.warnings}")
        self.assertIn(str(foreign.name), " ".join(hits),
                      "the warning must name the offending file so it can be found")
        self.assertEqual([o.result_path for o in rep.outcomes], [str(task.result_path)],
                         "only planned paths may ever be counted")


class TestModelAmendments(ModelBase):
    """Guards for the parts of the model that adversarial review amended.

    Each of these is a way the *fix* could destroy something the fix exists to protect.
    """

    def test_a_killed_route_holding_a_crash_record_still_settles_in_finite_attempts(self):
        """THE ANTI-RATCHET GUARD. ``Failed - Agent crashed`` / ``Simulation crashed`` /
        ``Agent couldn't be set up`` are real published results (6 rows in the v0.9 records).
        ``Agent couldn't be set up`` in particular comes from the agent-init watchdog -- a hang,
        i.e. precisely the population that also trips ``route_timeout_s``. If an abnormal end
        holding such a record charged an unrecoverable budget, those rows could never be
        reproduced: every resume would re-plan, re-kill and re-report them as unsettled, exit 1
        forever. The ambiguity is bounded instead: retry it, then settle on the record.
        """
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 3, "record_budget": 3, "tickruntime_budget": 0,
                   "worker_quarantine_after": 99}))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(AGENT_CRASHED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)
        budget = int(cfg.retry["killed_budget"])
        self.assertGreaterEqual(budget, 2, "the default must buy at least one clean re-run")

        # Drive it the way the supervision loop does: re-queue only while _settle asks for it.
        attempts = 0
        while self._settle(runner, state, task, AttemptOutcome.TIMEOUT, "wall-clock timeout"):
            attempts += 1
            self.assertLess(attempts, 20,
                            "the route was re-queued forever: an ambiguous kill must not be "
                            "able to spend an unbounded number of attempts")

        st = state.get(task.key)
        self.assertEqual(st.attempts_record, 0)
        self.assertTrue(st.finished,
                        "a route killed while holding a crash-shaped record never reached a "
                        "settled answer -- a legitimate, published model verdict converted "
                        "into a permanent exit 1")
        self.assertEqual(st.attempts_killed, budget,
                         "the ambiguous-kill budget must be bounded, not spent forever")
        rep = self._report(runner, [task], state, cfg)
        self.assertEqual(rep.exit_code(), EXIT_OK)

    def test_never_started_outranks_teardown_and_never_reads_the_record(self):
        """Class precedence. A launch that failed and was then drained during a shutdown is
        both NEVER_STARTED and TORN_DOWN. Nothing ran, so the restored earlier-attempt record
        must not be read as this attempt's verdict under either flag -- an ACCEPT must not be
        re-adopted (the ``resume.mode: none`` hazard) and a FATAL must not abort the sweep.
        """
        cfg = config_mod.load(self.site.config(resume={"mode": "none"}))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        self._settle(runner, state, task, AttemptOutcome.LAUNCH_FAILED,
                     "sbatch refused the submission", interrupted=True)

        st = state.get(task.key)
        self.assertFalse(st.finished,
                         "a drained failed launch re-adopted a stale accepted record as this "
                         "run's result")
        self.assertEqual(st.attempts_record, 0)

    def test_a_fatal_record_left_by_an_earlier_attempt_never_aborts_from_a_failed_launch(self):
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        task.result_path.write_text(json.dumps({"_checkpoint": {
            "progress": [1, 1],
            "records": [{"status": "Failed - Agent's sensors were invalid",
                         "scores": {"score_composed": 0.0}}]}}), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        self._settle(runner, state, task, AttemptOutcome.LAUNCH_FAILED, "ports busy")

        self.assertFalse(runner.fatal_agent,
                         "a record belonging to an EARLIER attempt aborted the whole sweep from "
                         "a launch that never ran")

    def test_a_kill_cannot_fabricate_an_accepted_record_so_it_stays_accepted(self):
        """The converse of finding 2, and the reason the fix cannot be a blanket
        'killed => infrastructure': the checkpoint is written when the *route* ends, not when
        the *process* ends, so a final record plus a wall-clock kill is the signature of a route
        that finished and hung in teardown. Re-running it would overwrite a real result.
        """
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(COMPLETED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        requeue = self._settle(runner, state, task, AttemptOutcome.TIMEOUT, "wall-clock timeout")

        self.assertFalse(requeue)
        self.assertTrue(state.get(task.key).finished)
        self.assertEqual(state.get(task.key).attempts_infra, 0)

    def test_a_kill_cannot_fabricate_tickruntime_so_the_degenerate_row_survives(self):
        """THE TRAP. ``Failed - TickRuntime`` is raised only by the scenario manager's own
        ``tick_count > 4000`` guard, so a kill cannot manufacture it -- and it is by definition
        the outcome of a route that ran a very long time, i.e. the population most likely to
        breach ``route_timeout_s`` during teardown. A model that scores it everywhere has
        produced a valid, if unflattering, benchmark result and must still exit 0.
        """
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 3, "record_budget": 3, "tickruntime_budget": 0,
                   "worker_quarantine_after": 99}))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(TICKRUNTIME), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        requeue = self._settle(runner, state, task, AttemptOutcome.TIMEOUT, "wall-clock timeout")

        st = state.get(task.key)
        self.assertFalse(requeue)
        self.assertEqual(st.attempts_tickruntime, 1)
        self.assertEqual(st.attempts_infra, 0)
        self.assertTrue(st.finished)
        self.assertEqual(self._report(runner, [task], state, cfg).exit_code(), EXIT_OK)

    def test_a_degenerate_model_row_replanned_without_a_ledger_is_still_complete(self):
        """The sharpest form of the trap. A result tree produced by a model that scores
        ``Failed - TickRuntime`` on every route, re-planned after its ledger was lost, yields
        SKIP_EXHAUSTED for every route with ``tickruntime`` spent 0 of budget 0. If the ledger's
        settlement bit is not set there, a complete and valid model row reports as N incomplete
        routes at exit 1.
        """
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(TICKRUNTIME), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)

        decision = plan_mod.decide(task, state.get(task.key).budgets(), "skip_terminal",
                                   record_budget=3, tickruntime_budget=0, infra_budget=3,
                                   killed_budget=2)
        self.assertIs(decision.decision, plan_mod.Decision.SKIP_EXHAUSTED)
        self.assertTrue(decision.settled,
                        "the record IS the answer once its own retry budget is gone")

        state.get(task.key).finished = decision.settled
        rep = self._report(runner, [task], state, cfg)
        self.assertEqual(rep.exit_code(), EXIT_OK)

    def test_a_fault_pattern_from_the_shared_stderr_does_not_condemn_a_finished_route(self):
        """``AttemptOutcome.FAULT`` is inferred from a stderr file the *simulator also writes
        into*: the evaluator launches CARLA with ``Popen(..., shell=True)`` and no redirection,
        and the runner gives the whole wrapper one stderr handle. A UE4 shutdown crash therefore
        stamps FAULT on a process that exited on its own having already written its verdict.
        """
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        backend = LocalBackend(cfg, self.log)

        task.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        task.stderr_path.write_text("Signal 11 caught.\nSegmentation fault (core dumped)\n",
                                    encoding="utf-8")
        task.result_path.write_text(json.dumps(AGENT_CRASHED), encoding="utf-8")

        done = subprocess.Popen(["true"])
        done.wait()
        attempt = Attempt(task=task, worker=0, stdout_path=task.stdout_path,
                          stderr_path=task.stderr_path, handle=done)

        self.assertTrue(backend.poll(attempt))
        self.assertIs(attempt.outcome, AttemptOutcome.EXITED,
                      "a fault pattern written by the CARLA server was read as evidence about "
                      "an evaluator that exited on its own with a final record on disk")
        self.assertIn("stderr", (attempt.detail or ""),
                      "the pattern must still be reported, just not believed")

    def test_a_fault_with_no_record_is_still_a_fault(self):
        cfg = config_mod.load(self.site.config())
        task = self._task(cfg)
        backend = LocalBackend(cfg, self.log)
        task.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        task.stderr_path.write_text("Segmentation fault (core dumped)\n", encoding="utf-8")

        done = subprocess.Popen(["true"])
        done.wait()
        attempt = Attempt(task=task, worker=0, stdout_path=task.stdout_path,
                          stderr_path=task.stderr_path, handle=done)

        self.assertTrue(backend.poll(attempt))
        self.assertIs(attempt.outcome, AttemptOutcome.FAULT)

    def test_the_status_breakdown_and_the_totals_cannot_contradict_each_other(self):
        """A route that is reported incomplete must not also be counted in the status
        breakdown as though its record were a result: an operator reads those two numbers side
        by side, and a downstream aggregator sums the second one.
        """
        cfg = config_mod.load(self.site.config(
            retry={"infra_budget": 1, "record_budget": 3, "tickruntime_budget": 0,
                   "worker_quarantine_after": 99}))
        task = self._task(cfg)
        task.result_path.write_text(json.dumps(AGENT_CRASHED), encoding="utf-8")
        runner, state = self._runner_and_state(cfg)
        self._settle(runner, state, task, AttemptOutcome.LAUNCH_FAILED, "ports busy")

        d = self._report(runner, [task], state, cfg).to_dict()

        self.assertEqual(sum(d["totals"]["by_status"].values()), d["totals"]["complete"],
                         "the status breakdown counted a route the totals call incomplete")
        self.assertEqual(sum(d["totals"]["by_status_unsettled"].values()),
                         d["totals"]["incomplete"])
        self.assertEqual(d["incomplete_routes"][0]["unsettled_reason"],
                         "unrefreshed_record")
        self.assertTrue(d["incomplete_routes"][0]["final"],
                        "'no result was produced' is the wrong story for this row: a record "
                        "exists, it was just never refreshed")


if __name__ == "__main__":
    unittest.main()
