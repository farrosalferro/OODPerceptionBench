"""Local worker-pool backend.

Each worker slot owns one GPU pair and one port pair for the entire sweep, both pure functions
of the worker index. At most one route runs in a slot at a time, so two routes can never
contend for a port or a GPU under any ordering, restart or crash-recovery path.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from .. import jobscript, ports as ports_mod, reap, results as results_mod
from ..config import Config, GpuSpec
from ..plan import RouteTask
from ..ports import PortPair
from .base import (Attempt, AttemptOutcome, Backend, restore_checkpoint,
                   take_checkpoint_aside)


class LocalBackendError(Exception):
    pass


def _hard_death_phrase(fault: Optional[str], signalled: Optional[str]) -> str:
    """How an attempt died, for a human, naming every source that said so.

    Both can be present (a segfaulting evaluator writes the pattern *and* exits 139) and either
    can be alone: a signal whose shell message we do not match, or a pattern from the shared
    CARLA stream under a wrapper that then exited non-zero for its own reasons. The exit status
    is stated first because it is the one an operator can trust without asking whose output the
    stream was.
    """
    parts = []
    if signalled:
        parts.append(signalled)
    if fault:
        parts.append(f'"{fault}" in stderr')
    return " and ".join(parts) if parts else "ended abnormally"


class LocalBackend(Backend):
    name = "local"

    def __init__(self, cfg: Config, log) -> None:
        self.cfg = cfg
        self.log = log
        # For the local pool, concurrency IS execution.workers: one process per slot.
        self.concurrency: int = cfg.workers
        self.pairs: List[PortPair] = ports_mod.allocate(
            workers=cfg.workers,
            rpc_base=int(cfg.ports["rpc_base"]),
            tm_base=int(cfg.ports["tm_base"]),
            stride=int(cfg.ports["stride"]),
        )
        self.gpu_for: Dict[int, GpuSpec] = {
            i: cfg.gpus[i % len(cfg.gpus)] for i in range(cfg.workers)
        }
        self._open_files: Dict[int, List] = {}

    # -- lifecycle ----------------------------------------------------------------------
    def preflight(self) -> None:
        self.log.info("port allocation:\n%s", ports_mod.describe(self.pairs))
        for i, pair in enumerate(self.pairs):
            gpu = self.gpu_for[i]
            self.log.info("worker %d -> cuda:%d vulkan-adapter:%d", i, gpu.cuda, gpu.vulkan)

        if not self.cfg.ports["probe"]:
            self.log.warning(
                "ports.probe is disabled. The vendored evaluator scans upward from the port it "
                "is given, so an occupied port can put two workers on one simulator without "
                "erroring. Only disable this if you know the block is yours."
            )
            return

        busy = ports_mod.probe_pairs(self.pairs)
        if busy:
            detail = ", ".join(f"worker {w} port {p}" for w, p in busy[:10])
            raise LocalBackendError(
                f"{len(busy)} reserved port(s) already in use ({detail}). The runner will not "
                f"relocate its block automatically -- silently shifting it is how two concurrent "
                f"runs end up sharing a simulator. Free those ports, or move ports.rpc_base / "
                f"ports.tm_base in the config."
            )
        self.log.info("port preflight OK: %d ports free across %d worker(s)",
                      sum(len(p.all_ports) for p in self.pairs), len(self.pairs))

    # -- submit -------------------------------------------------------------------------
    def submit(self, task: RouteTask, worker: int) -> Attempt:
        pair = self.pairs[worker]
        gpu = self.gpu_for[worker]
        attempt = Attempt(task=task, worker=worker,
                          stdout_path=task.stdout_path, stderr_path=task.stderr_path)

        # Start from a clean slate, but keep the old record in hand: every failure path below
        # must put it back. See backends.base.take_checkpoint_aside.
        try:
            stale = take_checkpoint_aside(task)
        except OSError as exc:
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.detail = f"could not remove stale checkpoint {task.result_path}: {exc}"
            attempt.finished_at = time.time()
            return attempt

        # Reap anything still bound to this worker's own window, then re-verify. If it is still
        # busy we refuse to launch: the evaluator would scan upward into the next worker's
        # window and quietly share a simulator.
        reaped = reap.reap_ports(pair.all_ports)
        if reaped:
            self.log.warning("worker %d: reaped orphaned CARLA pid(s) %s on its own ports",
                             worker, reaped)
            time.sleep(max(1, int(self.cfg.execution["post_kill_cooldown_s"])))
        if self.cfg.ports["probe"]:
            still_busy = ports_mod.probe(pair.all_ports)
            if still_busy:
                restore_checkpoint(task, stale)
                attempt.outcome = AttemptOutcome.LAUNCH_FAILED
                attempt.detail = (f"worker {worker} ports {still_busy} still occupied after "
                                  f"reaping; refusing to launch")
                attempt.finished_at = time.time()
                return attempt

        script = jobscript.write(task, self.cfg, gpu, pair)
        task.stdout_path.parent.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        # Belt and braces: the script exports these too, but a broken `environment.activate`
        # line that resets the environment must not silently unpin the GPU.
        env["CUDA_VISIBLE_DEVICES"] = str(gpu.cuda)
        env["PYTHONHASHSEED"] = str(task.seed)

        out_fh = open(task.stdout_path, "w", encoding="utf-8")
        err_fh = open(task.stderr_path, "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                ["bash", str(script)],
                stdout=out_fh,
                stderr=err_fh,
                env=env,
                # Own process group: the evaluator does NOT detach its CARLA child, so CARLA
                # joins this group and one killpg takes the whole route down.
                start_new_session=True,
                # The working directory is the agent's business, not ours: the script `cd`s to
                # agent.working_dir when one is configured. Inheriting here keeps relative
                # paths in a user's config resolving the way they expect.
            )
        except OSError as exc:
            out_fh.close()
            err_fh.close()
            restore_checkpoint(task, stale)
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.detail = f"failed to launch: {exc}"
            attempt.finished_at = time.time()
            return attempt

        attempt.handle = proc
        self._open_files[id(attempt)] = [out_fh, err_fh]
        self.log.info("worker %d launched %s (pid %d, rpc %d, tm %d, cuda %d, vulkan %d)",
                      worker, task.key, proc.pid, pair.rpc, pair.tm, gpu.cuda, gpu.vulkan)
        return attempt

    # -- poll ---------------------------------------------------------------------------
    def poll(self, attempt: Attempt) -> bool:
        if attempt.outcome is not None:
            return True
        proc: Optional[subprocess.Popen] = attempt.handle  # type: ignore[assignment]
        if proc is None:
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.finished_at = time.time()
            return True

        rc = proc.poll()
        if rc is None:
            fault = reap.detect_fault(attempt.stderr_path)
            timeout = attempt.duration_s > float(self.cfg.execution["route_timeout_s"])
            if fault or timeout:
                reason = (f'fault pattern "{fault}"' if fault
                          else f"wall-clock timeout after {attempt.duration_s:.0f}s")
                self.log.warning("worker %d: killing %s -- %s",
                                 attempt.worker, attempt.task.key, reason)
                self.kill(attempt, reason)
                attempt.outcome = AttemptOutcome.FAULT if fault else AttemptOutcome.TIMEOUT
                attempt.detail = reason
                attempt.finished_at = time.time()
                self._close(attempt)
                return True
            return False

        attempt.exit_code = rc
        attempt.finished_at = time.time()
        self._close(attempt)
        fault = reap.detect_fault(attempt.stderr_path)
        # Death by signal is read from the exit status, NOT from the stream, and it is checked
        # here rather than inside the fault branch below -- which is the bug this line closes.
        # The rc gate used to live only inside `if fault:`, so the whole classification hung on
        # a stderr substring; and two of those substrings did not match what a shell actually
        # writes ("Aborted (core dumped)" was column-padded, SIGKILL says only "Killed"). An
        # evaluator that died of SIGABRT or was taken by the OOM killer therefore arrived here
        # as "the process decided to stop", and its crash-shaped record was charged to the
        # MODEL's record budget and settled as the model's verdict, at exit 0, silently. That is
        # cross-review finding 2 for the fourth time. A signal is not a verdict; see
        # reap.describe_exit_signal for why 255 (`sys.exit(-1)`) is deliberately NOT one.
        signalled = reap.describe_exit_signal(rc)
        hard = fault or signalled
        if not hard:
            attempt.outcome = AttemptOutcome.EXITED
            attempt.detail = f"exit {rc}"
            return True

        # Something says this attempt died hard -- a pattern in the stream, or the exit status
        # itself. Whether it is evidence about *this* attempt is the whole question; it takes
        # both of the facts below to answer it, and the record is read exactly once.
        #
        # The two sources are not equally trustworthy and the difference decides the case
        # below. The STREAM is shared with the CARLA server, so a pattern in it may be about a
        # different process. The EXIT STATUS is ours alone: nothing but this attempt's own
        # wrapper can set it, so `signalled` is never a shared-stderr artefact and never
        # reaches the demotion (a signal death cannot have rc == 0).
        has_record = results_mod.read(attempt.task.result_path).final
        # The question the demotion actually asks is "did OUR process die hard?", and there is
        # now an exact test for it. This was `rc == 0`, which is a strictly narrower proxy: the
        # vendored evaluator ends its own crash paths with `sys.exit(-1)` -> status 255, a
        # SELF-TERMINATED verdict that `describe_exit_signal` correctly declines to call a
        # signal. Under the old proxy those verdicts could never be demoted, so a UE4 abort in
        # the shared stream sent `Failed - Simulation crashed` and `Failed - Agent couldn't be
        # set up` -- the status family of four published v0.9 rows -- to the ambiguity budget
        # instead of the model's. Cross-review 2026-08-07, round 3, cursor's finding 1.
        clean_exit = signalled is None

        if clean_exit and has_record:
            # `FAULT` is inferred from a log file the SIMULATOR also writes into: the evaluator
            # starts CARLA with `Popen(..., shell=True)` and no redirection, so it inherits this
            # attempt's single stderr handle. A UE4 crash during shutdown therefore stamps
            # "the attempt died hard" on a process that exited on its own having already written
            # its verdict -- and downstream that reclassifies a genuine model result as an
            # ambiguous kill. When the process exited by itself, CLEANLY, AND a final record is
            # on disk, the pattern is reported but not believed.
            #
            # Both conditions are load-bearing, and the death test is the one that was missing.
            # The demotion's whole justification is "this stream carries a second process's
            # output" -- but that argument only reaches the case where *our* process is fine,
            # and the only evidence we have of that is how it ended. Without that test the
            # demotion also swallowed an evaluator that died of SIGSEGV / SIGABRT / the OOM
            # killer with a final record already on disk, and handed that ambiguous record to
            # the model's own record budget as a clean verdict -- which is finding 2 again, by a
            # third door. See DESIGN.md 6A.2.
            #
            # Note what the test does NOT do: a non-zero exit is still a self-terminated exit,
            # because the vendored evaluator exits non-zero for its own crash paths
            # (`sys.exit(-1)` when `_load_and_run_scenario` reports crashed). Only a hard death
            # -- which by definition means a signal -- refuses the demotion.
            #
            # Reachable only from the stream: this branch is where `fault` is set and
            # `signalled` is not. That is the point -- it exists to forgive the CARLA server's
            # output, and a signal on our own wrapper is never the server's output.
            attempt.outcome = AttemptOutcome.EXITED
            attempt.detail = (f'exit {rc}; a "{fault}" pattern appeared in stderr, but this '
                              f'process terminated itself rather than dying by signal, with a '
                              f'final record written, and stderr is shared with the CARLA '
                              f'server, so the pattern is not evidence about this attempt')
            self.log.warning("worker %d: %s finished with a %r pattern in its (shared) stderr; "
                             "judging it by its record, not by the log",
                             attempt.worker, attempt.task.key, fault)
        elif has_record:
            # Not clean, and a record is on disk: the case the missing rc test used to hide.
            # Say it out loud, because that record now goes to the bounded ambiguity axis
            # instead of being read as the model's verdict, and an operator should know why.
            attempt.outcome = AttemptOutcome.FAULT
            attempt.detail = (f'{_hard_death_phrase(fault, signalled)} (exit {rc}): a final '
                              f'record is on disk but this process did not end cleanly, so the '
                              f'record cannot be credited to it')
            self.log.warning("worker %d: %s ended hard (%s) while a final record was on disk; "
                             "the wrapper itself did not exit cleanly, so this is an abnormal "
                             "end, not a shared-stderr artefact",
                             attempt.worker, attempt.task.key,
                             _hard_death_phrase(fault, signalled))
        else:
            attempt.outcome = AttemptOutcome.FAULT
            attempt.detail = f"{_hard_death_phrase(fault, signalled)} (exit {rc})"
        return True

    def kill(self, attempt: Attempt, reason: str) -> None:
        proc = attempt.handle
        if isinstance(proc, subprocess.Popen):
            reap.terminate_process_tree(proc, grace_s=30.0)
        self.cleanup_worker(attempt.worker)
        if attempt.outcome is None:
            attempt.outcome = AttemptOutcome.KILLED
            attempt.detail = reason
            attempt.finished_at = time.time()
        self._close(attempt)

    def cleanup_worker(self, worker: int) -> None:
        pair = self.pairs[worker]
        pids = reap.reap_ports(pair.all_ports)
        if pids:
            self.log.warning("worker %d: reaped leftover CARLA pid(s) %s", worker, pids)
        cooldown = int(self.cfg.execution["post_kill_cooldown_s"])
        if pids and cooldown > 0:
            time.sleep(cooldown)

    def shutdown(self) -> None:
        for worker in range(self.concurrency):
            try:
                self.cleanup_worker(worker)
            except Exception as exc:  # pragma: no cover - best effort
                self.log.warning("cleanup of worker %d failed: %s", worker, exc)

    # -- helpers ------------------------------------------------------------------------
    def _close(self, attempt: Attempt) -> None:
        for fh in self._open_files.pop(id(attempt), []):
            try:
                fh.close()
            except OSError:
                pass
