#!/usr/bin/env python3
"""OOD-PerceptionBench portable evaluation runner.

    run_benchmark.py --config <file> [--routes DIR] [--out DIR] [--agent PATH] [--workers N]

FIRST CUT. See DESIGN.md for the locked decisions and STATUS.md for what is unvalidated.

Exit codes (DESIGN.md section 6; "settled" is defined in section 6A):
    0  every planned route has a settled result
    1  partial sweep -- at least one planned route has no settled result
    2  configuration / preflight error
    3  interrupted by signal
    4  all workers quarantined / no usable GPU
    5  fatal agent misconfiguration (sensor configuration rejected)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from oodbench import (ARXIV_VERSION, BENCHMARK_RELEASE, EXIT_CONFIG, EXIT_INTERRUPTED,  # noqa: E402
                      EXIT_NO_WORKERS, __version__)
from oodbench import backends, config as config_mod, gpus as gpus_mod, plan as plan_mod  # noqa: E402
from oodbench import report as report_mod, results as results_mod  # noqa: E402
from oodbench.backends.base import Attempt, AttemptOutcome  # noqa: E402
from oodbench.plan import Decision, RouteTask  # noqa: E402
from oodbench.results import Disposition  # noqa: E402
from oodbench import state as state_mod  # noqa: E402
from oodbench.state import RunState  # noqa: E402

log = logging.getLogger("oodbench")


# ---------------------------------------------------------------------------------------
class Interrupted(Exception):
    pass


_interrupted = {"flag": False}


def _handle_signal(signum, _frame):
    if _interrupted["flag"]:
        log.error("second signal received; exiting immediately")
        os._exit(EXIT_INTERRUPTED)
    _interrupted["flag"] = True
    log.warning("signal %s received -- finishing the current poll, reaping children, then "
                "writing state and exiting non-zero", signum)


# ---------------------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_benchmark.py",
        description=f"OOD-PerceptionBench runner {__version__} (release {BENCHMARK_RELEASE}, "
                    f"arXiv {ARXIV_VERSION}) -- FIRST CUT",
    )
    p.add_argument("--config", help="path to the run configuration (.yaml/.yml/.toml/.json)")
    p.add_argument("--routes", help="override routes.root")
    p.add_argument("--out", help="override output.root")
    p.add_argument("--agent", help="override agent.entrypoint")
    p.add_argument("--workers", type=int, help="override execution.workers")
    p.add_argument("--backend", choices=("local", "slurm"), help="override execution.backend")
    p.add_argument("--resume-mode", choices=results_mod.VALID_RESUME_MODES,
                   help="override resume.mode")
    p.add_argument("--seed", type=int,
                   help="override benchmark.seed (the published protocol is 42; deviating is "
                        "flagged in the report)")
    p.add_argument("--limit", type=int, help="run at most N routes (smoke testing)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and exit 0 without running anything. Never writes "
                        "the ledger, so previewing cannot change what a later real run does")
    p.add_argument("--force", action="store_true",
                   help="required with --resume-mode none, which overwrites existing results")
    p.add_argument("--retry-infra-exhausted", action="store_true",
                   help="give routes whose INFRASTRUCTURE retry budget is spent a fresh one, "
                        "for a resume after the machine has been repaired. Clears only that "
                        "counter: no result file is touched and no other budget moves, so it "
                        "buys attempts, never answers. Deliberately not --force: it destroys "
                        "nothing")
    p.add_argument("--check-gpus", action="store_true",
                   help="print the CUDA and Vulkan device lists side by side and exit")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def build_config(args: argparse.Namespace) -> config_mod.Config:
    overrides: Dict[str, object] = {}
    if args.routes:
        overrides["routes.root"] = args.routes
    if args.out:
        overrides["output.root"] = args.out
    if args.agent:
        overrides["agent.entrypoint"] = args.agent
    if args.workers is not None:
        overrides["execution.workers"] = args.workers
    if args.backend:
        overrides["execution.backend"] = args.backend
    if args.resume_mode:
        overrides["resume.mode"] = args.resume_mode
    if args.seed is not None:
        overrides["benchmark.seed"] = args.seed
    return config_mod.load(args.config, overrides=overrides)


# ---------------------------------------------------------------------------------------
class Runner:
    """The supervision loop. One place decides what runs, what retries, and what the run means."""

    def __init__(self, cfg: config_mod.Config, args: argparse.Namespace) -> None:
        self.cfg = cfg
        self.args = args
        self.out_root = Path(cfg.output["root"])
        self.started_at = time.time()
        self.warnings: List[str] = list(cfg.warnings)
        self.skipped_done = 0
        self.skipped_exhausted = 0
        self.fatal_agent = False
        self.all_workers_quarantined = False
        self.quarantined: List[int] = []
        self.consecutive_infra: Dict[int, int] = {}

    # -- planning ----------------------------------------------------------------------
    def plan(self) -> List[RouteTask]:
        routes_root = Path(self.cfg.routes["root"])
        xmls = plan_mod.discover(routes_root)

        for w in plan_mod.scaffolding_warnings(xmls, routes_root):
            log.warning(w)
            self.warnings.append(w)

        log.info("discovered %d route XML(s) under %s", len(xmls), routes_root)
        for group, n in plan_mod.breakdown(xmls, routes_root):
            log.info("    %-48s %4d", group, n)

        manifest = self.cfg.routes["manifest"]
        if manifest:
            check = plan_mod.check_manifest(xmls, routes_root, Path(manifest))
            log.info("%s", check.summary())
            if not check.ok:
                self.warnings.append(check.summary())
                if self.cfg.routes["strict_manifest"]:
                    raise plan_mod.PlanError(
                        "routes.strict_manifest is set and the route set does not match the "
                        "manifest. The route set IS the benchmark definition; refusing to run."
                    )
        else:
            self.warnings.append(
                "no routes.manifest was configured, so the route set was not integrity-checked. "
                "Point routes.manifest at the frozen routes/MANIFEST.tsv to catch an edited or "
                "missing route XML."
            )

        tasks = plan_mod.build_tasks(
            xmls, routes_root, self.out_root,
            base_seed=self.cfg.seed, repetitions=int(self.cfg.benchmark["repetitions"]))
        plan_mod.assert_seed_consistency(tasks, self.cfg.seed,
                                         int(self.cfg.benchmark["repetitions"]))
        if self.args.limit:
            tasks = tasks[: self.args.limit]
            log.warning("--limit %d: running a subset. Subset results are NOT comparable to the "
                        "published baselines, which are over the full 475-route set.",
                        self.args.limit)
            self.warnings.append(f"--limit {self.args.limit} was used: this is a partial route set")
        return tasks

    # -- the loop ----------------------------------------------------------------------
    def run(self, tasks: List[RouteTask], state: RunState, backend) -> report_mod.Report:
        retry = self.cfg.retry
        queue: List[RouteTask] = []

        for task in tasks:
            st = state.get(task.key)
            decision = plan_mod.decide(
                task, st.budgets(), self.cfg.resume["mode"],
                record_budget=int(retry["record_budget"]),
                tickruntime_budget=int(retry["tickruntime_budget"]),
                infra_budget=int(retry["infra_budget"]),
                killed_budget=int(retry["killed_budget"]),
            )
            if decision.decision is Decision.FATAL:
                log.error("%s: %s", task.key, decision.reason)
                self.fatal_agent = True
                self.warnings.append(f"{task.key}: {decision.reason}")
                break
            # Settlement comes from decide(), never re-derived here: the caller re-deriving it
            # from "a record exists" is how a preserved record came to mean "route complete".
            if decision.decision is Decision.SKIP_DONE:
                self.skipped_done += 1
                st.finished = decision.settled
                st.last_status = decision.record.status
                continue
            if decision.decision is Decision.SKIP_EXHAUSTED:
                self.skipped_exhausted += 1
                st.finished = decision.settled
                st.last_reason = decision.reason
                log.warning("%s: %s", task.key, decision.reason)
                continue
            # This run owes the route an attempt, so whatever settled it before does not settle
            # it now. Without this, a route re-planned under `resume.mode: none` stays settled
            # on the strength of a record this run intends to replace and then never produces.
            st.finished = False
            queue.append(task)

        # A dry run must not write the ledger AT ALL, rather than write it and put it back:
        # a snapshot-and-restore leaves a window in which a SIGKILL freezes the preview's
        # settlement bits and config digest on disk, and the restoring write is itself
        # non-atomic. Not writing is crash-safe by construction. The loop above still mutates
        # the in-memory ledger, which it must -- otherwise the plan printed below would not be
        # the plan a real run would make. (Cross-review 2026-08-07, round 4, codex finding 2.)
        if not self.args.dry_run:
            state.save()

        if self.fatal_agent:
            return self._report(tasks, state, backend)

        log.info("plan: %d route(s) to run, %d already done, %d skipped with budget spent",
                 len(queue), self.skipped_done, self.skipped_exhausted)

        if self.args.dry_run:
            for task in queue[:50]:
                log.info("  would run %s -> %s", task.key, task.result_path)
            if len(queue) > 50:
                log.info("  ... and %d more", len(queue) - 50)
            return self._report(tasks, state, backend, dry_run=True)

        if not queue:
            return self._report(tasks, state, backend)

        # The BACKEND decides how many slots exist, not execution.workers. For the local pool
        # they are the same number; for SLURM the concurrency is slurm.max_parallel, and reading
        # execution.workers here is what made that setting gate nothing at all -- while also
        # letting a slot index run past the backend's reserved port block.
        n_workers = int(getattr(backend, "concurrency", self.cfg.workers))
        log.info("supervising %d concurrent slot(s) on the %s backend", n_workers, backend.name)
        slots: Dict[int, Optional[Attempt]] = {i: None for i in range(n_workers)}
        poll_s = int(self.cfg.execution["poll_interval_s"])
        pending = list(queue)

        while True:
            # 1. harvest finished attempts
            for worker, attempt in list(slots.items()):
                if attempt is None:
                    continue
                if not backend.poll(attempt):
                    continue
                slots[worker] = None
                requeue = self._settle(attempt, state, backend)
                if self.fatal_agent:
                    pending.clear()
                    break
                if requeue:
                    # Append, do not push to the front. A route that fails fast would otherwise
                    # be retried immediately in a tight loop, starving the rest of the sweep and
                    # spending its whole budget against one transient condition. Tail placement
                    # also matches how the internal orchestrator's resubmit loop behaves.
                    pending.append(attempt.task)
                state.save()

            if self.fatal_agent:
                self._drain(slots, state, backend, "aborting: agent configuration is fatal")
                break

            # 2. shut down cleanly on signal
            if _interrupted["flag"]:
                self._drain(slots, state, backend, "runner interrupted")
                raise Interrupted()

            # 3. fill idle slots
            for worker in range(n_workers):
                if worker in self.quarantined or slots[worker] is not None or not pending:
                    continue
                task = pending.pop(0)
                slots[worker] = backend.submit(task, worker)

            # 4. termination
            if not pending and all(a is None for a in slots.values()):
                break

            usable = [w for w in range(n_workers) if w not in self.quarantined]
            if not usable:
                log.error("every worker has been quarantined; aborting")
                self._drain(slots, state, backend, "aborting: no usable worker remains")
                self.all_workers_quarantined = True
                break

            time.sleep(poll_s)

        return self._report(tasks, state, backend)

    def _drain(self, slots: Dict[int, Optional[Attempt]], state: RunState, backend,
               reason: str) -> None:
        """Kill and account for every in-flight attempt, then persist. Never leaves orphans."""
        for worker, attempt in list(slots.items()):
            if attempt is None:
                continue
            backend.kill(attempt, reason)
            self._settle(attempt, state, backend, interrupted=True)
            slots[worker] = None
        state.save()

    # -- attempt bookkeeping -----------------------------------------------------------
    def _settle(self, attempt: Attempt, state: RunState, backend,
                interrupted: bool = False) -> bool:
        """Account for one finished attempt. Returns True if the task should be re-queued.

        **DESIGN.md section 6A is normative for this method.** One rule, from which every case
        below follows:

            a record on disk counts as this attempt's output only if the way the attempt ended
            could not have manufactured that record.

        The three things we know are the outcome class (how the attempt stopped), the on-disk
        case (six of them: no final record, plus the five dispositions -- and ``disposition``
        returns UNKNOWN for a non-final record too, so ``record.final`` MUST be tested first),
        and the teardown flag. ``interrupted`` is that flag; read it as *torn_down*, since the
        runner also raises it on a fatal-agent abort and when every worker is quarantined.

        Class precedence is a strict order: NEVER_STARTED > TORN_DOWN > ABNORMAL_END >
        CLEAN_EXIT. NEVER_STARTED outranking the teardown flag is what stops a drained failed
        launch from reading a *restored* earlier-attempt record as its own verdict.
        """
        task = attempt.task
        st = state.get(task.key)
        st.last_worker = attempt.worker
        st.last_duration_s = round(attempt.duration_s, 1)
        st.total_runtime_s += attempt.duration_s

        record = results_mod.read(task.result_path)
        st.last_status = record.status
        reason = attempt.detail or (attempt.outcome.value if attempt.outcome else "unknown")

        # -- class NEVER_STARTED ---------------------------------------------------------
        # Nothing ran, so nothing on disk can be read as this attempt's output. Both backends
        # put the pre-existing record back on every failed-launch path (see
        # backends.base.take_checkpoint_aside), which means `record` here belongs to an EARLIER
        # attempt. Judging it as if this attempt had produced it is a three-part silent failure:
        # it charges the route's *record* budget for an infrastructure fault; once that budget
        # runs out it freezes the stale record in as the final answer having never retried it;
        # and -- the part the report got wrong -- it lets that preserved record make the route
        # count COMPLETE at exit 0. Under `resume.mode: none` a stale *accepted* record would
        # even be re-adopted as this run's result with nothing having been driven.
        #
        # This outranks the teardown flag: a launch that failed and was then drained during a
        # shutdown still never ran, and the record still is not its.
        if attempt.outcome is AttemptOutcome.LAUNCH_FAILED or attempt.outcome is None:
            if attempt.outcome is None:
                self._warn_once(
                    f"{task.key}: the backend reported an attempt finished without saying how "
                    f"(outcome is None). Treated as a launch failure, which is the fail-safe "
                    f"reading: nothing on disk is credited to it.")
            return self._charge_infra(attempt, st, backend, reason, record,
                                      never_started=True)

        # -- class TORN_DOWN -------------------------------------------------------------
        # We stopped a route that was not failing: an operator signal, a fatal-agent abort, or
        # every worker quarantined. The machine did nothing wrong, so NOTHING is charged -- in
        # any budget. Charging would let three interrupted sweeps permanently abandon a route
        # that never actually failed, and abandon it *as* "this route has NOT produced a
        # benchmark result", the exact silent loss the budgets exist to prevent.
        #
        # This matters most for the record budget: killing the worker takes its process group
        # down, CARLA included, and a crash handler can write a *final* retryable record on the
        # way out. That record is an artefact of the kill, not something the model produced.
        if interrupted:
            if record.final and record.disposition is Disposition.FATAL:
                return self._abort_fatal(task, st, record)
            if record.final and record.disposition is Disposition.ACCEPT:
                # It genuinely finished before we killed anything.
                st.finished = True
                st.last_reason = None
                return False
            st.last_reason = (f"{record.status} (interrupted; no budget charged)"
                              if record.final
                              else f"interrupted before a record was written ({reason})")
            log.info("%s: interrupted; no retry budget charged", task.key)
            return False

        abnormal = attempt.outcome in (AttemptOutcome.TIMEOUT, AttemptOutcome.FAULT,
                                       AttemptOutcome.KILLED)

        # -- no final record: infrastructure, for both remaining classes ------------------
        if not record.final:
            return self._charge_infra(attempt, st, backend, reason, record,
                                      never_started=False)

        # -- FATAL and ACCEPT are taken first, for CLEAN_EXIT and ABNORMAL_END alike ------
        disposition = record.disposition
        if disposition is Disposition.FATAL:
            # Not fabricable by a kill: the sensor configuration is validated before the route
            # runs. Aborts the sweep either way.
            self._clear_infra_debt(st, attempt.worker)
            return self._abort_fatal(task, st, record)

        if disposition is Disposition.ACCEPT:
            # A kill cannot fabricate an ACCEPT status -- those come from the criteria
            # evaluation of a route that ended normally, and the checkpoint is written when the
            # ROUTE ends, not when the process does. A final ACCEPT plus a wall-clock kill is
            # the signature of a route that finished and hung in teardown. Re-running it would
            # overwrite a real result.
            self._clear_infra_debt(st, attempt.worker)
            st.finished = True
            st.last_reason = None
            log.info("%s: %s (score_composed=%s) in %.0fs on worker %d",
                     task.key, record.status, record.score_composed, attempt.duration_s,
                     attempt.worker)
            return False

        if disposition is Disposition.UNKNOWN:
            self._warn_once(
                f"{task.key}: unrecognised route status {record.status!r}. The runner's status "
                f"taxonomy may be out of date with this leaderboard build; treating it as "
                f"retryable. Please report it.")

        # -- retryable: which axis? ------------------------------------------------------
        if disposition is Disposition.RETRY_TICKRUNTIME:
            # Raised only by the scenario manager's own `tick_count > 4000` guard, so a kill
            # cannot manufacture it -- and it is, by definition, the outcome of a route that ran
            # a very long time, i.e. the population most likely to breach route_timeout_s during
            # teardown. Charging those to infrastructure would report a degenerate model's
            # entire row as incomplete at exit 1: a legitimate benchmark result silently
            # converted into an infrastructure failure.
            self._clear_infra_debt(st, attempt.worker)
            label, budget = "tickruntime", int(self.cfg.retry["tickruntime_budget"])
            st.attempts_tickruntime += 1
            spent = st.attempts_tickruntime
        elif abnormal:
            # THE AMBIGUOUS CELL. `Failed`, `Agent crashed`, `Simulation crashed` and
            # `Agent couldn't be set up` are exactly the statuses the evaluator's except-blocks
            # write -- which is also what a dying simulator under a surviving evaluator
            # produces. We cannot tell the two apart, so this charges neither the model's record
            # budget (it may not be the model's verdict) nor the infra budget (which never
            # settles, and would make legitimate published rows such as vad's four
            # `Agent couldn't be set up` unreproducible: exit 1 forever, on every resume).
            # Its own bounded axis: retry to resolve the ambiguity, then accept the record.
            #
            # The quarantine counter -- and the route's infra debt, which is cleared by exactly
            # the same event -- are deliberately left ALONE here: a worker that ran a route all
            # the way to record registration is demonstrably not wedged, but neither has it
            # proved itself. Resetting them let a worker that kills every route it touches
            # evade detection forever. This is the one cell where a final record is on disk and
            # nothing is cleared, because it is the one cell where the record is not evidence.
            label, budget = "killed", int(self.cfg.retry["killed_budget"])
            st.attempts_killed += 1
            spent = st.attempts_killed
            log.warning("%s: attempt ended abnormally (%s) with a %r record on disk. That "
                        "record may have been written by the route or by the kill; charging "
                        "the ambiguous-kill budget (%d/%d), not the model's record budget.",
                        task.key, reason, record.status, spent, budget)
        else:
            self._clear_infra_debt(st, attempt.worker)
            label, budget = "record", int(self.cfg.retry["record_budget"])
            st.attempts_record += 1
            spent = st.attempts_record

        st.last_reason = f"{record.status} ({label} attempts {spent}/{budget})"
        if spent < budget:
            if label == "killed":
                self._copy_record_aside(task, spent)
            log.warning("%s: %s -- retrying (%s %d/%d)", task.key, record.status, label,
                        spent, budget)
            return True

        # Budget spent. The record on disk IS the benchmark result; the route is settled.
        # Every cell that holds a final record must reach settlement in a finite number of
        # attempts -- a route that can never settle is a permanent exit 1 with no recovery
        # short of editing the ledger by hand.
        st.finished = True
        if label == "killed":
            self._warn_once(
                f"{task.key}: settled on status {record.status!r} after {spent} attempt(s) that "
                f"ended abnormally while that record was on disk. It may have been written by "
                f"the kill rather than by the model; earlier copies are under "
                f"_runner/killed_records/.")
        log.info("%s: %s -- %s retry budget spent (%d/%d); accepting the record as the result",
                 task.key, record.status, label, spent, budget)
        return False

    # -- _settle helpers ----------------------------------------------------------------
    def _warn_once(self, msg: str) -> None:
        log.warning("%s", msg)
        if msg not in self.warnings:
            self.warnings.append(msg)

    def _clear_infra_debt(self, st, worker: int) -> None:
        """One event clears both consecutive-failure counters, because they ask one question.

        The event is *an attempt produced a final record the way it ended could not have
        manufactured*. It is evidence that this worker is not wedged (so the quarantine counter
        resets) and that this route can run here (so its infra debt resets). What both budgets
        bound is a machine that is broken **now**; a lifetime tally would let unrelated hiccups
        scattered across a long sweep gate a route that has been running fine all along, and
        that gate is persisted, so the route would still be gated on the next run.

        Deliberately NOT called for an abnormal end holding a crash-shaped record: that is the
        one cell whose record we have just declared unreliable, and clearing on it is how a
        worker that kills every route it touches evaded the detector (DESIGN.md 6A.5).
        """
        self.consecutive_infra[worker] = 0
        st.attempts_infra = 0

    def _abort_fatal(self, task: RouteTask, st, record) -> bool:
        log.error("%s: status %r means the agent's sensor configuration is rejected; it will "
                  "fail identically on every route. Aborting the sweep.",
                  task.key, record.status)
        self.fatal_agent = True
        st.last_reason = f"fatal: {record.status}"
        return False

    def _charge_infra(self, attempt: Attempt, st, backend, reason: str, record,
                      never_started: bool) -> bool:
        """Charge the infrastructure budget for an attempt that produced no record of its own.

        The quarantine counter tracks exactly this population -- *nothing ran at all* is the
        signature of a wedged GPU or a permanently squatted port block -- which is why a failed
        launch advances it even though there is a perfectly good record on disk.
        """
        task = attempt.task
        infra_budget = int(self.cfg.retry["infra_budget"])
        # The quarantine streak tracks ONE population: attempts where *nothing ran at all*.
        # An attempt that launched, loaded the map and drove before breaching route_timeout_s
        # produced no record -- so the ROUTE's budget is charged, because it still has to settle
        # in a finite number of attempts -- but it is not evidence against the WORKER.
        #
        # This is the call 6A.5 already makes one cell over, for an abnormal end holding a
        # crash-shaped record: a worker that ran a route is not demonstrably wedged, and has not
        # proved itself either, so the streak is left ALONE rather than advanced or cleared.
        # Advancing it here let three slow routes in a row quarantine a healthy worker and abort
        # the sweep -- reporting a wedged GPU, on a machine whose GPU was idle and fine.
        #
        # The test is the TIMEOUT outcome specifically, not `never_started`. An attempt that
        # launched and then died without a record is a *fast* failure: it looks far more like a
        # sick worker than like a route that drove for a full route_timeout_s, so it still
        # advances the streak. Only breaching the wall clock proves the worker was working.
        if attempt.outcome is not AttemptOutcome.TIMEOUT:
            self.consecutive_infra[attempt.worker] = self.consecutive_infra.get(
                attempt.worker, 0) + 1
        # The streak is the gate; the total is the audit trail and is never cleared.
        st.attempts_infra += 1
        st.attempts_infra_total += 1
        if never_started:
            st.last_reason = f"launch failed, nothing ran ({reason})"
            log.warning("%s: launch failed on attempt %d [%s]",
                        task.key, st.attempts_infra, reason)
        else:
            st.last_reason = f"no final record ({reason})"
            log.warning("%s: no final record after attempt %d [%s]",
                        task.key, st.attempts_infra, reason)
        self._maybe_quarantine(attempt.worker, backend)
        if st.attempts_infra < infra_budget:
            return True

        if record.final and never_started:
            # A record an EARLIER attempt really did produce, preserved by the failed-launch
            # path. It must never be destroyed -- and it must never be mistaken for an answer
            # this run produced. `finished` stays false, so the report counts the route
            # incomplete and the run exits non-zero: we do not know this route's answer.
            self._warn_once(
                f"{task.key}: infrastructure retry budget exhausted "
                f"({st.attempts_infra}/{infra_budget}) without a single attempt ever starting. "
                f"The record on disk (status {record.status!r}) is from an EARLIER attempt; it "
                f"was preserved, not refreshed, and this route is reported as having no settled "
                f"result. {plan_mod.INFRA_RECOVERY_HINT}")
            return False

        log.error("%s: infrastructure retry budget exhausted (%d/%d) -- this route has NOT "
                  "produced a benchmark result. %s",
                  task.key, st.attempts_infra, infra_budget, plan_mod.INFRA_RECOVERY_HINT)
        return False

    def _copy_record_aside(self, task: RouteTask, n: int) -> None:
        """Keep a copy of an ambiguous record before the retry deletes it.

        ``take_checkpoint_aside`` clears the checkpoint on the next *successful* launch, so a
        route re-queued off the ambiguous-kill axis can end with no record at all where the old
        behaviour would have frozen the crash-shaped one in. The copy lives under ``_runner/``,
        never beside the results, so no downstream aggregator can glob it up as one.
        """
        try:
            dest = (self.out_root / "_runner" / "killed_records" / str(task.rel_dir)
                    / f"{task.stem}_seed{task.seed}.{n}.json")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(task.result_path.read_bytes())
        except OSError as exc:  # pragma: no cover - best effort
            log.warning("%s: could not copy the ambiguous record aside: %s", task.key, exc)

    def _maybe_quarantine(self, worker: int, backend) -> None:
        limit = int(self.cfg.retry["worker_quarantine_after"])
        if self.consecutive_infra.get(worker, 0) < limit or worker in self.quarantined:
            return
        self.quarantined.append(worker)
        # Say only what the counter actually counted. The old text asserted "one worker failing
        # while others progress", which is not a pattern that can occur at workers: 1 -- the
        # configuration a single-GPU machine is required to use -- and named a wedged GPU as the
        # sole cause, sending operators to re-probe a device that was healthy every time. A
        # squatted port block was always the other cause; _charge_infra says so, the message did
        # not.
        msg = (f"worker {worker} quarantined after {limit} consecutive launch failures -- "
               f"attempts where nothing ran at all. The two causes are a wedged GPU and a port "
               f"block that stays occupied; check both before reusing this worker. Re-probe the "
               f"device, and confirm the worker's ports are free in EVERY TCP state, not just "
               f"LISTEN -- a socket in TIME_WAIT refuses the bind exactly like a live server.")
        log.error(msg)
        self.warnings.append(msg)
        try:
            backend.cleanup_worker(worker)
        except Exception as exc:  # pragma: no cover
            log.warning("cleanup of quarantined worker %d failed: %s", worker, exc)

    # -- reporting ---------------------------------------------------------------------
    def _report(self, tasks: List[RouteTask], state: RunState, backend,
                dry_run: bool = False, interrupted: bool = False) -> report_mod.Report:
        rep = report_mod.build(
            tasks, state,
            started_at=self.started_at,
            config_digest=self.cfg.digest(),
            config_path=self.cfg.source_path,
            seed=self.cfg.seed,
            backend=backend.name if backend else self.cfg.execution["backend"],
            skipped_done=self.skipped_done,
            skipped_exhausted=self.skipped_exhausted,
            quarantined=self.quarantined,
            warnings=self.warnings,
            interrupted=interrupted or _interrupted["flag"],
            fatal_agent=self.fatal_agent,
            all_workers_quarantined=self.all_workers_quarantined,
        )
        if dry_run:
            rep.warnings.append("--dry-run: nothing was executed")
        return rep


# ---------------------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    if args.check_gpus:
        print(gpus_mod.report())
        return 0

    if not args.config:
        log.error("--config is required (there are no site-specific defaults, by design)")
        return EXIT_CONFIG

    try:
        cfg = build_config(args)
    except config_mod.ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_CONFIG

    if cfg.resume["mode"] == "none" and not args.force:
        log.error("resume.mode is 'none', which re-runs and overwrites existing results. "
                  "Pass --force to confirm.")
        return EXIT_CONFIG

    for w in cfg.warnings:
        log.warning(w)

    log.info("OOD-PerceptionBench runner %s -- FIRST CUT", __version__)
    log.info("release %s (binds to arXiv %s), seed %d, backend %s, %d worker(s)",
             cfg.benchmark["release"], cfg.benchmark["arxiv_version"], cfg.seed,
             cfg.execution["backend"], cfg.workers)
    log.info("config digest %s", cfg.digest()[:16])

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    runner = Runner(cfg, args)
    out_root = Path(cfg.output["root"])
    out_root.mkdir(parents=True, exist_ok=True)

    state = RunState.load_or_create(out_root / "_runner" / "state.json", cfg.digest())
    if state.note:
        log.warning("%s", state.note)
        runner.warnings.append(state.note)
    if state.config_changed():
        msg = ("this output root was produced by a DIFFERENT configuration. Resuming will mix "
               "two sets of settings in one result tree; if the agent or the CARLA build "
               "changed, use a fresh output root.")
        log.warning("%s", msg)
        runner.warnings.append(msg)
    if state.accounting_model_changed():
        msg = (f"this output root's ledger was written under attempt-accounting epoch "
               f"{state.accounting_epoch_on_disk}, and this runner uses epoch "
               f"{state_mod.ACCOUNTING_EPOCH} (DESIGN.md section 6A). The counters are carried "
               f"over unchanged, but they were charged under different rules -- before epoch 2 "
               f"every final retryable record charged the `record` axis, including attempts "
               f"that ended abnormally, which now charge the separate bounded `killed` axis. "
               f"Routes still in flight may therefore settle after a different number of "
               f"attempts, and on a different status, than the run that started this tree would "
               f"have produced. Finished routes are unaffected. The config digest deliberately "
               f"does NOT cover this -- it answers whether you changed a setting, not whether "
               f"the runner changed what one means.")
        log.warning("%s", msg)
        runner.warnings.append(msg)

    # Applied before planning, because it changes what `decide()` sees: the plan printed by a
    # dry run would otherwise be a plan for a ledger the real run will not have.
    #
    # `--dry-run` is documented, in --help and in DESIGN.md, as "print the plan and exit 0
    # without running anything". It was not true of the ledger: planning materialises a
    # TaskState for every route, sets or clears `finished` on each, and `load_or_create` has
    # already replaced the stored config digest -- then it all got saved. So previewing a run
    # could create task entries, move settlement bits, and (the sharp one) overwrite the digest,
    # which ERASES the `config_changed()` warning the operator was about to be shown on the real
    # run.
    #
    # The rule is now simply that **a dry run never calls `state.save()`** -- not here, not in
    # `run()`. Mutating the in-memory ledger is fine and necessary, since the printed plan has
    # to be the plan a real run would make; it is the write that must not happen. An earlier
    # revision snapshotted the file and restored it afterwards, which is strictly worse: it
    # leaves a window where a SIGKILL freezes the preview on disk, and the restoring write is
    # itself non-atomic. Every save on the dry-run path is guarded at its call site, so there is
    # no window to be crash-safe about.
    #
    # Applied before planning, because it changes what `decide()` sees. Note the clear is over
    # the WHOLE ledger, not the planned subset: --limit and a narrowed --routes do not scope it.
    if getattr(args, "retry_infra_exhausted", False):
        cleared = state.clear_infra_exhaustion(int(cfg.retry["infra_budget"]))
        if not args.dry_run:
            state.save()
        if cleared:
            verb = "would clear (--dry-run: nothing was written)" if args.dry_run else "cleared"
            shown = ", ".join(cleared[:5]) + (" ..." if len(cleared) > 5 else "")
            msg = (f"--retry-infra-exhausted: {verb} the infrastructure retry counter of "
                   f"{len(cleared)} route(s) that had hit the gate ({shown}). Nothing else was "
                   f"touched -- every result file, settlement bit and other budget is as it "
                   f"was, and the lifetime infra count still records what happened. If the "
                   f"machine is still broken these routes will simply exhaust the budget again "
                   f"and be reported unsettled, as before.")
            log.warning("%s", msg)
            runner.warnings.append(msg)
        else:
            log.info("--retry-infra-exhausted: no route had an exhausted infrastructure budget")
            runner.warnings.append(
                "--retry-infra-exhausted was passed but no route had an exhausted "
                "infrastructure budget, so it changed nothing. Recorded because a flag that "
                "silently does nothing is worse than one that says so.")

    backend = None
    interrupted = False
    tasks: List[RouteTask] = []
    try:
        tasks = runner.plan()
        backend = backends.make(cfg, log)
        if not args.dry_run:
            backend.preflight()
        rep = runner.run(tasks, state, backend)
    except plan_mod.PlanError as exc:
        log.error("planning error: %s", exc)
        return EXIT_CONFIG
    except Interrupted:
        interrupted = True
        log.error("interrupted")
        rep = runner._report(tasks, state, backend, interrupted=True)
    except Exception as exc:  # backend preflight and anything unforeseen
        log.error("%s", exc)
        if args.verbose:
            log.exception("traceback")
        return EXIT_CONFIG
    finally:
        if backend is not None:
            try:
                backend.shutdown()
            except Exception as exc:  # pragma: no cover
                log.warning("backend shutdown failed: %s", exc)
        # The last of the three guarded saves. A dry run has now touched nothing on disk: no
        # ledger written, so none to restore and none left behind on a tree that had none.
        if not args.dry_run:
            state.save()

    if args.dry_run:
        # The only "success without results" case, and it produces no result files that could
        # be mistaken for one.
        log.info("--dry-run: nothing was executed")
        return 0

    if interrupted:
        rep.interrupted = True
    paths = rep.write(out_root)
    # Single source of truth: Report.exit_code(). Nothing here second-guesses it.
    code = rep.exit_code()

    print()
    print(rep.to_markdown())
    log.info("report written to %s and %s", paths["json"], paths["markdown"])
    log.info("exit %d -- %s", code, report_mod.EXIT_MEANING.get(code, "unknown"))
    return code


if __name__ == "__main__":
    sys.exit(main())
