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

from .. import jobscript, ports as ports_mod, reap
from ..config import Config, GpuSpec
from ..plan import RouteTask
from ..ports import PortPair
from .base import (Attempt, AttemptOutcome, Backend, restore_checkpoint,
                   take_checkpoint_aside)


class LocalBackendError(Exception):
    pass


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
        if fault:
            attempt.outcome = AttemptOutcome.FAULT
            attempt.detail = f'fault pattern "{fault}" in stderr (exit {rc})'
        else:
            attempt.outcome = AttemptOutcome.EXITED
            attempt.detail = f"exit {rc}"
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
