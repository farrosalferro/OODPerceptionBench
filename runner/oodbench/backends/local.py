"""Local worker-pool backend.

Each worker slot owns one GPU pair and one port pair for the entire sweep, both pure functions
of the worker index. At most one route runs in a slot at a time, so two routes can never
contend for a port or a GPU under any ordering, restart or crash-recovery path.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
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


@dataclass
class _PortRelease:
    """A worker held idle while the previous CARLA socket finishes draining."""

    started_at: float
    not_before: float
    deadline: float
    kill_after: float
    kill_sent: bool
    busy: List[int]


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
        self._port_release: Dict[int, _PortRelease] = {}
        # Whether this run has taken its port block: set once preflight holds the block's locks
        # and finds it free, or when the operator disables the probe and so asserts the block
        # is theirs. Until then a CARLA on these ports belongs to someone else, and shutdown
        # must not reap it.
        self._owns_ports = False
        # One lock per reserved port, taken before the probe and held until shutdown, so a
        # second oodbench run cannot probe the same block free while this one is still in its
        # preflight and then reap this run's simulators as orphans. See DESIGN.md section 3.
        self._port_locks = ports_mod.PortLocks(
            p for pair in self.pairs for p in pair.all_ports)
        self.preflight_warnings: List[str] = []

    # -- lifecycle ----------------------------------------------------------------------
    def preflight(self) -> None:
        self.log.info("port allocation:\n%s", ports_mod.describe(self.pairs))
        for i, pair in enumerate(self.pairs):
            gpu = self.gpu_for[i]
            self.log.info("worker %d -> cuda:%d vulkan-adapter:%d", i, gpu.cuda, gpu.vulkan)

        # Before the probe and the startup wait, and whether or not the probe is enabled: the
        # probe can only say the block is free now, the lock is what keeps it this run's.
        self._lock_ports()

        if not self.cfg.ports["probe"]:
            self.log.warning(
                "ports.probe is disabled. The vendored evaluator scans upward from the port it "
                "is given, so an occupied port can put two workers on one simulator without "
                "erroring. Only disable this if you know the block is yours."
            )
            self._owns_ports = True
            return

        busy = ports_mod.probe_pairs(self.pairs)
        if busy:
            busy = self._wait_for_startup_ports(busy)
            if busy is None:
                self.log.warning("stop requested during the startup port wait")
                return
        if busy:
            detail = ", ".join(f"worker {w} port {p}" for w, p in busy[:10])
            timeout = int(self.cfg.execution["port_release_timeout_s"])
            raise LocalBackendError(
                f"{len(busy)} reserved port(s) already in use ({detail}), and not released "
                f"within {timeout} s (execution.port_release_timeout_s). The runner will not "
                f"relocate its block automatically -- silently shifting it is how two concurrent "
                f"runs end up sharing a simulator. Free those ports, or move ports.rpc_base / "
                f"ports.tm_base in the config."
            )
        self._owns_ports = True
        self.log.info("port preflight OK: %d ports free across %d worker(s)",
                      sum(len(p.all_ports) for p in self.pairs), len(self.pairs))

    def _lock_ports(self) -> None:
        """Take this run's per-port locks, or refuse the run if another oodbench run holds any.

        A held lock is refused at once, with no wait: its holder is a whole sweep. A lock
        directory we cannot use is only a warning, because the probe below still guards the
        block; the run then simply lacks protection against a second run started during its
        own preflight.
        """
        locks = self._port_locks
        try:
            locks.acquire()
        except ports_mod.PortLockHeld as exc:
            shown = ", ".join(str(p) for p in exc.ports[:10])
            more = " ..." if len(exc.ports) > 10 else ""
            raise LocalBackendError(
                f"reserved port(s) {shown}{more} are held by another oodbench run on this "
                f"machine (lock file {exc.paths[0]}). Nothing on those ports was touched. Find "
                f"that run with `fuser -v {exc.paths[0]}` or `lsof {exc.paths[0]}` (as root if "
                f"another user started it), then let it finish, stop it, or move ports.rpc_base "
                f"/ ports.tm_base in the config so the two blocks do not overlap."
            ) from None
        except OSError as exc:
            msg = (f"could not take the port locks in {locks.directory} ({exc}); continuing "
                   f"without them. The startup probe still refuses a block that is busy now, "
                   f"but a second oodbench run started on this block during this run's "
                   f"preflight is no longer detected, and either run could then reap the "
                   f"other's simulators. Set {ports_mod.PORT_LOCK_DIR_ENV} to a writable "
                   f"directory that every run on this machine shares.")
            self.log.warning("%s", msg)
            self.preflight_warnings.append(msg)
            return
        self.log.info("port locks held: %d file(s) under %s",
                      len(locks.ports), locks.directory)

    def _wait_for_startup_ports(self, busy):
        """Give a block that is busy at startup the time :meth:`can_submit` gives it mid-run.

        A run that has just ended on this block can leave CARLA's RPC+1 streaming socket
        draining for about a minute, and that refuses the evaluator's bind like a live server
        does -- a run started straight after another one used to exit 2 here. Returns the ports
        still busy when the wait ends (empty once the block binds), or None if the operator
        asked the run to stop first. Unlike can_submit this signals nothing: at startup no
        process on the block was started by this run, so none is this run's to stop.
        """
        timeout = int(self.cfg.execution["port_release_timeout_s"])
        self.log.warning(
            "reserved port(s) %s busy at startup; waiting up to %d s for them to be released "
            "(a run that just ended on this block leaves its sockets draining). Nothing on them "
            "is stopped: no process there was started by this run.",
            ", ".join(str(p) for _, p in busy[:10]), timeout)
        started = time.monotonic()
        for _ in range(timeout):
            time.sleep(1)
            if self.stop_requested():
                return None
            busy = ports_mod.probe_pairs(self.pairs)
            if not busy:
                self.log.info("reserved ports released %.1fs into the startup wait",
                              time.monotonic() - started)
                return busy
        return busy

    def can_submit(self, worker: int) -> bool:
        """Hold an idle slot until its exact evaluator ports are bindable again.

        CARLA's RPC+1 streaming socket can remain unavailable after its processes have exited.
        That is teardown state owned by the previous attempt, not a launch failure belonging to
        the next route. Readiness is therefore checked before the runner pops a task. A bounded
        deadline preserves the fail-closed behaviour for a port block that is genuinely stuck;
        once it expires, :meth:`submit` performs the existing defensive probe and reports one
        real ``LAUNCH_FAILED`` if the block is still occupied.
        """
        if not self.cfg.ports["probe"]:
            return True

        pair = self.pairs[worker]
        release = self._port_release.get(worker)
        now = time.monotonic()

        if release is not None:
            if not release.kill_sent and now >= release.kill_after:
                killed = reap.kill_carla_on_ports(pair.all_ports)
                release.kill_sent = True
                cooldown = int(self.cfg.execution["post_kill_cooldown_s"])
                # A late supervisor tick must not collapse SIGKILL and the launch decision into
                # one call. Preserve the full quiet period from the *actual* signal time and
                # retain the slot until a later probe. Extending an already-reached deadline by
                # this cooldown is still bounded and prevents teardown from becoming the next
                # route's infrastructure debt.
                release.not_before = max(release.not_before, now + cooldown)
                release.deadline = max(release.deadline, release.not_before)
                if killed:
                    self.log.warning(
                        "worker %d: CARLA pid(s) %s ignored SIGTERM on reserved ports; "
                        "sent SIGKILL without blocking peer workers",
                        worker, killed)
                return False
            if now < release.not_before:
                return False
            busy = ports_mod.probe(pair.all_ports)
            if not busy:
                elapsed = now - release.started_at
                self._port_release.pop(worker, None)
                self.log.info("worker %d: reserved ports released after %.1fs", worker, elapsed)
                return True
            release.busy = busy
            if now < release.deadline:
                return False

            elapsed = now - release.started_at
            self._port_release.pop(worker, None)
            self.log.warning(
                "worker %d: ports %s still occupied after %.1fs release timeout; the next "
                "launch will fail closed if its final probe still finds them busy",
                worker, busy, elapsed)
            return True

        busy = ports_mod.probe(pair.all_ports)
        if not busy:
            return True

        terminated = reap.terminate_carla_on_ports(pair.all_ports)
        if terminated:
            self.log.warning(
                "worker %d: sent SIGTERM to orphaned CARLA pid(s) %s on its own ports",
                worker, terminated)
        started = time.monotonic()
        cooldown = int(self.cfg.execution["post_kill_cooldown_s"])
        timeout = int(self.cfg.execution["port_release_timeout_s"])
        self._port_release[worker] = _PortRelease(
            started_at=started,
            not_before=started + cooldown,
            deadline=started + timeout,
            kill_after=started + min(reap.PORT_REAP_TERM_GRACE_S, timeout),
            kill_sent=False,
            busy=busy,
        )
        self.log.warning(
            "worker %d: ports %s remain occupied before the next launch; holding the slot "
            "for up to %ds without charging a route attempt",
            worker, busy, timeout)
        return False

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

        # With probing enabled, Runner.can_submit has already reaped and cooled this worker
        # without assigning a task. This final probe closes the check-to-launch race. Operators
        # who disable probing retain the old synchronous best-effort cleanup path.
        if not self.cfg.ports["probe"]:
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
                attempt.detail = (f"worker {worker} ports {still_busy} occupied at launch; "
                                  f"refusing to launch")
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
        self._port_release.pop(worker, None)
        pair = self.pairs[worker]
        pids = reap.reap_ports(pair.all_ports)
        if pids:
            self.log.warning("worker %d: reaped leftover CARLA pid(s) %s", worker, pids)
        cooldown = int(self.cfg.execution["post_kill_cooldown_s"])
        if pids and cooldown > 0:
            time.sleep(cooldown)

    def shutdown(self) -> None:
        # Reaping by port is safe only for a block this run took (DESIGN.md section 7). A dry run
        # never takes it and a refused run never got it, so the CARLA found there is someone
        # else's -- typically the sweep the dry run was previewing, killed mid-route.
        try:
            if self._owns_ports:
                for worker in range(self.concurrency):
                    try:
                        self.cleanup_worker(worker)
                    except Exception as exc:  # pragma: no cover - best effort
                        self.log.warning("cleanup of worker %d failed: %s", worker, exc)
        finally:
            # Only after the reaping: released earlier, another run could take the block while
            # this one is still stopping simulators on it. Also reached when ownership was never
            # established (a refused or interrupted preflight), so no failure keeps a lock.
            self._port_locks.release()

    # -- helpers ------------------------------------------------------------------------
    def _close(self, attempt: Attempt) -> None:
        for fh in self._open_files.pop(id(attempt), []):
            try:
                fh.close()
            except OSError:
                pass
