"""Backend interface shared by the local worker pool and the SLURM submitter.

Both backends consume the *same* config object and the same plan, resume, retry and reporting
logic. A backend is responsible only for: turn this task into a running thing, tell me when it
stopped, and kill it if I ask.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from ..plan import RouteTask


class AttemptOutcome(Enum):
    """Why an attempt stopped. Distinct from the *record* status on disk."""

    EXITED = "exited"            # the process finished on its own; read the record to judge
    TIMEOUT = "timeout"          # breached execution.route_timeout_s
    FAULT = "fault"              # a fault pattern appeared in stderr
    KILLED = "killed"            # we killed it (shutdown, quarantine)
    LAUNCH_FAILED = "launch_failed"  # never started: busy ports, sbatch error, ...


@dataclass
class Attempt:
    """One in-flight or finished execution of a task."""

    task: RouteTask
    worker: int
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    outcome: Optional[AttemptOutcome] = None
    exit_code: Optional[int] = None
    detail: Optional[str] = None
    handle: object = None        # backend-private (Popen, SLURM job id, ...)
    stdout_path: Optional[Path] = None
    stderr_path: Optional[Path] = None

    @property
    def duration_s(self) -> float:
        return (self.finished_at or time.time()) - self.started_at


def take_checkpoint_aside(task: RouteTask) -> Optional[bytes]:
    """Remove any existing checkpoint, returning its bytes so a failed launch can put it back.

    An attempt must start from a clean slate: a stale checkpoint plus the evaluator's in-file
    resume makes a route "finish" instantly against old data.

    But a launch that never happened must not destroy the record it was going to replace.
    A route holding a retryable record (say ``Failed - Agent crashed``) with budget left is
    planned as RUN, so it reaches here with a *valid benchmark result* on disk. If the launch
    then fails -- busy ports, a full scheduler queue -- deleting first would turn a route with
    a real, if unflattering, result into one reported as "NO benchmark result was produced".
    Returning the bytes lets the caller restore them on every failure path.
    """
    try:
        if task.result_path.exists():
            blob = task.result_path.read_bytes()
            task.result_path.unlink()
            return blob
    except OSError:
        raise
    return None


def restore_checkpoint(task: RouteTask, blob: Optional[bytes]) -> None:
    """Put back a checkpoint taken aside by :func:`take_checkpoint_aside`. Best effort."""
    if blob is None:
        return
    try:
        task.result_path.parent.mkdir(parents=True, exist_ok=True)
        task.result_path.write_bytes(blob)
    except OSError:
        pass


class Backend:
    name = "base"

    #: How many attempts this backend supervises at once. The supervision loop opens exactly
    #: this many worker slots and indexes the backend's per-slot resources (ports, GPUs) with
    #: the slot number, so it is the backend -- not ``execution.workers`` -- that decides.
    #: They are not the same thing: ``execution.workers`` sizes the *local* pool, while the
    #: SLURM backend's concurrency is ``slurm.max_parallel``. Reading ``execution.workers`` in
    #: the loop meant ``slurm.max_parallel`` gated nothing at all and slot indices could run
    #: past the reserved port block. The conservative default of 1 means a backend that forgets
    #: to set it under-parallelises rather than handing two workers the same resources.
    concurrency: int = 1

    def preflight(self) -> None:
        """Raise on anything that would make the whole run pointless. Called once."""

    def submit(self, task: RouteTask, worker: int) -> Attempt:
        raise NotImplementedError

    def poll(self, attempt: Attempt) -> bool:
        """Return True when the attempt has finished; fill in outcome/exit_code/detail."""
        raise NotImplementedError

    def kill(self, attempt: Attempt, reason: str) -> None:
        raise NotImplementedError

    def cleanup_worker(self, worker: int) -> None:
        """Release anything the worker slot still holds (orphaned simulators, ports)."""

    def shutdown(self) -> None:
        """Called once on the way out, including on signal."""
