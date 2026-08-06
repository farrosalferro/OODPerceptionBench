#!/usr/bin/env python3
"""OOD-PerceptionBench portable evaluation runner.

    run_benchmark.py --config <file> [--routes DIR] [--out DIR] [--agent PATH] [--workers N]

FIRST CUT. See DESIGN.md for the locked decisions and STATUS.md for what is unvalidated.

Exit codes (DESIGN.md section 6):
    0  every planned route has a final record on disk
    1  partial sweep -- at least one planned route has no final record
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
                   help="print the plan and exit 0 without running anything")
    p.add_argument("--force", action="store_true",
                   help="required with --resume-mode none, which overwrites existing results")
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
            )
            if decision.decision is Decision.FATAL:
                log.error("%s: %s", task.key, decision.reason)
                self.fatal_agent = True
                self.warnings.append(f"{task.key}: {decision.reason}")
                break
            if decision.decision is Decision.SKIP_DONE:
                self.skipped_done += 1
                st.finished = True
                st.last_status = decision.record.status
                continue
            if decision.decision is Decision.SKIP_EXHAUSTED:
                self.skipped_exhausted += 1
                st.last_reason = decision.reason
                log.warning("%s: %s", task.key, decision.reason)
                continue
            queue.append(task)

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

        Two independent budgets, so a bad GPU cannot consume a route's *record* retries and
        leave behind a result-shaped artifact produced by infrastructure.
        """
        task = attempt.task
        st = state.get(task.key)
        st.last_worker = attempt.worker
        st.last_duration_s = round(attempt.duration_s, 1)
        st.total_runtime_s += attempt.duration_s
        retry = self.cfg.retry

        record = results_mod.read(task.result_path)
        st.last_status = record.status

        # A launch that never happened produced nothing, so nothing on disk can be read as this
        # attempt's output. Both backends put the pre-existing record back on every failed-launch
        # path (see backends.base.take_checkpoint_aside), which means `record` here belongs to an
        # EARLIER attempt. Judging it as if this attempt had produced it is a two-part silent
        # failure: it charges the route's *record* budget for an infrastructure fault -- busy
        # ports, a refused sbatch -- and then, once that budget runs out, freezes the stale
        # record as the final answer while the route's real retries were never spent. Worse
        # still under `resume.mode: none`, where a stale *accepted* record would be re-adopted
        # as this run's result without a single route having been driven.
        #
        # A failed launch is infrastructure, unconditionally, whatever is on disk.
        launch_failed = attempt.outcome is AttemptOutcome.LAUNCH_FAILED
        produced_record = record.final and not launch_failed

        if produced_record:
            self.consecutive_infra[attempt.worker] = 0
        elif not interrupted:
            self.consecutive_infra[attempt.worker] = self.consecutive_infra.get(
                attempt.worker, 0) + 1

        # -- this attempt produced no record: infrastructure -----------------------------
        if not produced_record:
            reason = attempt.detail or (attempt.outcome.value if attempt.outcome else "unknown")

            # An operator interrupt is not an infrastructure failure. We killed a healthy route
            # mid-flight; the machine did nothing wrong. Charging the infra budget here would
            # let three Ctrl-Cs permanently abandon a route that never actually failed -- and
            # abandon it *as* "this route has NOT produced a benchmark result", which is the
            # exact silent-loss the budget exists to prevent. Charge nothing, report nothing,
            # and let the next run start this route with its budget intact.
            if interrupted:
                st.last_reason = f"interrupted before a record was written ({reason})"
                log.info("%s: interrupted mid-route; no retry budget charged", task.key)
                return False

            st.attempts_infra += 1
            if launch_failed:
                st.last_reason = f"launch failed, nothing ran ({reason})"
                log.warning("%s: launch failed on attempt %d [%s]",
                            task.key, st.attempts_infra, reason)
            else:
                st.last_reason = f"no final record ({reason})"
                log.warning("%s: no final record after attempt %d [%s]",
                            task.key, st.attempts_infra, reason)
            self._maybe_quarantine(attempt.worker, backend)
            if st.attempts_infra < int(retry["infra_budget"]):
                return True

            if record.final:
                # Reachable only via a failed launch, which preserved a record an earlier
                # attempt really did produce. The route is not "incomplete" -- it holds a valid
                # benchmark result -- but the retries this run planned never happened, and the
                # report must not imply otherwise by staying silent.
                msg = (f"{task.key}: infrastructure retry budget exhausted "
                       f"({st.attempts_infra}/{int(retry['infra_budget'])}) without a single "
                       f"attempt ever starting. The record on disk (status {record.status!r}) "
                       f"is from an EARLIER attempt; it was preserved, not refreshed.")
                log.error("%s", msg)
                if msg not in self.warnings:
                    self.warnings.append(msg)
                return False

            log.error("%s: infrastructure retry budget exhausted (%d/%d) -- this route has NOT "
                      "produced a benchmark result", task.key, st.attempts_infra,
                      int(retry["infra_budget"]))
            return False

        # -- this attempt produced a final record: judge it -------------------------------
        disposition = record.disposition
        if disposition is Disposition.FATAL:
            log.error("%s: status %r means the agent's sensor configuration is rejected; it will "
                      "fail identically on every route. Aborting the sweep.",
                      task.key, record.status)
            self.fatal_agent = True
            st.last_reason = f"fatal: {record.status}"
            return False

        if disposition is Disposition.ACCEPT:
            st.finished = True
            st.last_reason = None
            log.info("%s: %s (score_composed=%s) in %.0fs on worker %d",
                     task.key, record.status, record.score_composed, attempt.duration_s,
                     attempt.worker)
            return False

        if disposition is Disposition.UNKNOWN:
            msg = (f"{task.key}: unrecognised route status {record.status!r}. The runner's status "
                   f"taxonomy may be out of date with this leaderboard build; treating it as "
                   f"retryable. Please report it.")
            log.warning(msg)
            if msg not in self.warnings:
                self.warnings.append(msg)

        if disposition is Disposition.RETRY_TICKRUNTIME:
            budget, label = int(retry["tickruntime_budget"]), "tickruntime"
        else:  # RETRY_RECORD or UNKNOWN
            budget, label = int(retry["record_budget"]), "record"

        # An operator interrupt charges NOTHING, in this budget as well as the infra one.
        # Killing the worker takes its whole process group down, CARLA included, and the
        # evaluator's crash handler writes a *final* `Failed - Simulation crashed` record on the
        # way out -- a record manufactured by the Ctrl-C, not produced by the model. Charging it
        # let a few interrupted sweeps spend a route's real retries and then, on the last one,
        # mark the route finished with that interrupt artefact frozen in as its benchmark
        # result. The record is preserved (the runner never deletes one), no budget moves, and
        # the next run re-plans this route with its budget intact and starts it from a clean
        # slate. An accepted record is still accepted above: that route genuinely completed
        # before we killed anything.
        if interrupted:
            spent = st.budgets()[label]
            st.last_reason = (f"{record.status} (interrupted; no {label} budget charged, "
                              f"still {spent}/{budget})")
            log.info("%s: interrupted with a %r record on disk; no retry budget charged",
                     task.key, record.status)
            return False

        if label == "tickruntime":
            st.attempts_tickruntime += 1
            spent = st.attempts_tickruntime
        else:
            st.attempts_record += 1
            spent = st.attempts_record

        st.last_reason = f"{record.status} ({label} attempts {spent}/{budget})"
        if spent < budget:
            log.warning("%s: %s -- retrying (%s %d/%d)", task.key, record.status, label,
                        spent, budget)
            return True

        # Budget spent. The record on disk IS the benchmark result; the route is complete.
        st.finished = True
        log.info("%s: %s -- %s retry budget spent (%d/%d); accepting the record as the result",
                 task.key, record.status, label, spent, budget)
        return False

    def _maybe_quarantine(self, worker: int, backend) -> None:
        limit = int(self.cfg.retry["worker_quarantine_after"])
        if self.consecutive_infra.get(worker, 0) < limit or worker in self.quarantined:
            return
        self.quarantined.append(worker)
        msg = (f"worker {worker} quarantined after {limit} consecutive infrastructure failures. "
               f"That pattern -- one worker failing while others progress -- is the signature of "
               f"a wedged GPU. Re-probe that device before reusing it.")
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
