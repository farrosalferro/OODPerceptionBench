"""SLURM backend -- one job per route.

**FIRST CUT, NEVER EXECUTED.** It shares the planning, resume, retry and reporting logic with
the local backend and is written against the same interface, but no part of it has been run
against a real scheduler. Treat every line as unverified.

Design notes (DESIGN.md section 10):

Dropped from the internal orchestrators, as cluster-specific:

* ``ssh <submit-host>`` -- submit from wherever ``sbatch`` works.
* the on-disk **cap-gate file** the internal orchestrators polled for a job limit -- concurrency
  here is ``slurm.max_parallel``, an integer in the config. A file-on-disk side channel does not
  belong in a public tool.
* per-pool ``run_files_<prefix>/`` namespacing -- job scripts live under the mirrored per-route
  path, so two pools writing to distinct output roots cannot collide.

Kept, because each was a real incident:

* submission rate limiting (``slurm.submit_interval_s``);
* **concurrency gating on our own job IDs, not on a ``squeue`` name-prefix grep.** The internal
  gate counted jobs whose *name* matched a prefix, which also matched the orchestrator's own
  job -- so the pool ran one slot short, and renaming the orchestrator to dodge that let a
  second pool collide with the first. Tracking submitted IDs removes the class of bug;
* bounded resubmission with the same two-budget accounting as local;
* finalized-result skipping on resume, same predicate.

Under SLURM the per-job cgroup normally exposes exactly one GPU, so the in-job CUDA index is 0
and CARLA's Vulkan enumeration is likewise restricted to that device. The worker's `cuda`/
`vulkan` values are therefore *not* applied here; ``--gres`` does the pinning. Ports still come
from the deterministic allocator so that two jobs landing on one node cannot collide.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from .. import jobscript, ports as ports_mod
from ..config import Config, GpuSpec
from ..plan import RouteTask
from .base import (Attempt, AttemptOutcome, Backend, restore_checkpoint,
                   take_checkpoint_aside)

_JOBID_RE = re.compile(r"(\d+)\s*$")


class SlurmBackendError(Exception):
    pass


class SlurmBackend(Backend):
    name = "slurm"

    def __init__(self, cfg: Config, log) -> None:
        self.cfg = cfg
        self.log = log
        # Concurrency for this backend is slurm.max_parallel -- NOT execution.workers, which
        # sizes the local pool and has a default of 1. The supervision loop opens exactly
        # `concurrency` slots and indexes `self.pairs` with the slot number, so allocating the
        # port block from the same number is what makes slot -> ports a bijection. The previous
        # `min(max_parallel, workers)` was computed and then never read by anything: the loop
        # used execution.workers directly, so max_parallel gated nothing, and a slot index past
        # the end of the block was wrapped with `% len(pairs)` -- which hands two concurrently
        # running jobs the same RPC and traffic-manager ports.
        self.concurrency: int = max(1, int(cfg.slurm["max_parallel"]))
        self.pairs = ports_mod.allocate(
            workers=self.concurrency,
            rpc_base=int(cfg.ports["rpc_base"]),
            tm_base=int(cfg.ports["tm_base"]),
            stride=int(cfg.ports["stride"]),
        )
        self._last_submit = 0.0
        self._submitted: List[str] = []

    def preflight(self) -> None:
        for tool in ("sbatch", "squeue"):
            if subprocess.call(["bash", "-lc", f"command -v {tool} >/dev/null"]) != 0:
                raise SlurmBackendError(
                    f"execution.backend is 'slurm' but `{tool}` is not on PATH"
                )
        if not self.cfg.slurm["partition"]:
            self.log.warning("slurm.partition is unset; relying on the cluster default")
        self.log.info("concurrency %d job(s) in flight (slurm.max_parallel); "
                      "execution.workers=%d is not used by this backend",
                      self.concurrency, self.cfg.workers)
        self.log.warning(
            "the SLURM backend is a FIRST CUT and has never been run against a real scheduler. "
            "Submit a single route first and read the generated job script before launching a "
            "full sweep."
        )

    # -- submit -------------------------------------------------------------------------
    def _sbatch_header(self, task: RouteTask) -> str:
        s = self.cfg.slurm
        job_name = f"oodbench-{task.stem}"[:60]
        lines = [
            "#!/bin/bash",
            f"#SBATCH --job-name={job_name}",
            f"#SBATCH --output={task.stdout_path}",
            f"#SBATCH --error={task.stderr_path}",
            "#SBATCH --nodes=1",
            "#SBATCH --ntasks=1",
            f"#SBATCH --cpus-per-task={int(s['cpus_per_task'])}",
            f"#SBATCH --mem={s['mem']}",
            f"#SBATCH --time={s['time']}",
        ]
        if s["gres"]:
            lines.append(f"#SBATCH --gres={s['gres']}")
        for key, flag in (("partition", "--partition"), ("account", "--account"),
                          ("qos", "--qos"), ("nodelist", "--nodelist"), ("exclude", "--exclude")):
            if s[key]:
                lines.append(f"#SBATCH {flag}={s[key]}")
        lines.extend(s["extra_directives"])
        return "\n".join(lines) + "\n"

    def submit(self, task: RouteTask, worker: int) -> Attempt:
        attempt = Attempt(task=task, worker=worker,
                          stdout_path=task.stdout_path, stderr_path=task.stderr_path)

        # Refuse an out-of-range slot rather than wrapping it. `self.pairs[worker % len(pairs)]`
        # silently gave two *concurrently running* jobs the same RPC and traffic-manager ports;
        # if they landed on one node, the second CARLA would find the port taken, scan upward
        # and the two routes would quietly share a simulator. Checked before the checkpoint is
        # touched, so a refusal here cannot disturb an existing record either.
        if not 0 <= worker < len(self.pairs):
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.detail = (
                f"worker slot {worker} has no reserved port pair ({len(self.pairs)} allocated "
                f"from slurm.max_parallel={self.concurrency}); refusing to reuse another slot's "
                f"ports"
            )
            attempt.finished_at = time.time()
            self.log.error("%s: %s", task.key, attempt.detail)
            return attempt

        # Keep the old record in hand: sbatch refusing a submission (full queue, QOS limit) is
        # routine, and must not destroy a valid result. See backends.base.take_checkpoint_aside.
        try:
            stale = take_checkpoint_aside(task)
        except OSError as exc:
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.detail = f"could not remove stale checkpoint: {exc}"
            attempt.finished_at = time.time()
            return attempt

        pair = self.pairs[worker]
        # Under a per-job cgroup the visible device is index 0 for both CUDA and Vulkan.
        gpu = GpuSpec(cuda=0, vulkan=0, vulkan_assumed=False)
        inner = jobscript.render(task, self.cfg, gpu, pair)
        wrapper = task.job_script.with_suffix(".sbatch")
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        task.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        body = inner.split("\n", 1)[1]  # drop the inner "#!/bin/bash"
        wrapper.write_text(self._sbatch_header(task) + body, encoding="utf-8")
        wrapper.chmod(0o750)

        # Submission rate limiting: caps a runaway submit loop regardless of the gate above it.
        gap = float(self.cfg.slurm["submit_interval_s"]) - (time.time() - self._last_submit)
        if gap > 0:
            time.sleep(gap)
        self._last_submit = time.time()

        try:
            out = subprocess.check_output(["sbatch", str(wrapper)], text=True,
                                          stderr=subprocess.STDOUT).strip()
        except (subprocess.CalledProcessError, OSError) as exc:
            restore_checkpoint(task, stale)
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.detail = f"sbatch failed: {exc}"
            attempt.finished_at = time.time()
            return attempt

        m = _JOBID_RE.search(out)
        if not m:
            restore_checkpoint(task, stale)
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.detail = f"could not parse a job id from sbatch output: {out!r}"
            attempt.finished_at = time.time()
            return attempt
        job_id = m.group(1)
        attempt.handle = job_id
        self._submitted.append(job_id)
        self.log.info("submitted %s as SLURM job %s", task.key, job_id)
        return attempt

    # -- poll ---------------------------------------------------------------------------
    def poll(self, attempt: Attempt) -> bool:
        if attempt.outcome is not None:
            return True
        job_id = attempt.handle
        if not isinstance(job_id, str):
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.finished_at = time.time()
            return True

        # Gate on *our* job ids, never on a name grep -- see the module docstring.
        try:
            out = subprocess.check_output(
                ["squeue", "-h", "-j", job_id, "-o", "%T"], text=True,
                stderr=subprocess.DEVNULL).strip()
        except (subprocess.CalledProcessError, OSError):
            out = ""
        if out:
            if attempt.duration_s > float(self.cfg.execution["route_timeout_s"]):
                self.kill(attempt, "wall-clock timeout")
                attempt.outcome = AttemptOutcome.TIMEOUT
                attempt.detail = f"scancelled after {attempt.duration_s:.0f}s"
                attempt.finished_at = time.time()
                return True
            return False

        attempt.finished_at = time.time()
        state = self._sacct_state(job_id)
        if state and state.startswith(("CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY",
                                       "PREEMPTED", "BOOT_FAIL")):
            attempt.outcome = AttemptOutcome.FAULT
            attempt.detail = f"SLURM state {state}"
        else:
            attempt.outcome = AttemptOutcome.EXITED
            attempt.detail = f"SLURM state {state or 'unknown'}"
        return True

    @staticmethod
    def _sacct_state(job_id: str) -> Optional[str]:
        try:
            out = subprocess.check_output(
                ["sacct", "-j", job_id, "--format=State", "-n", "-P"], text=True,
                stderr=subprocess.DEVNULL).strip()
        except (subprocess.CalledProcessError, OSError):
            return None
        return out.splitlines()[0].strip() if out else None

    def kill(self, attempt: Attempt, reason: str) -> None:
        job_id = attempt.handle
        if isinstance(job_id, str):
            subprocess.call(["scancel", job_id])
        if attempt.outcome is None:
            attempt.outcome = AttemptOutcome.KILLED
            attempt.detail = reason
            attempt.finished_at = time.time()

    def shutdown(self) -> None:
        if not self._submitted:
            return
        self.log.info("cancelling %d submitted SLURM job(s)", len(self._submitted))
        subprocess.call(["scancel"] + self._submitted)
