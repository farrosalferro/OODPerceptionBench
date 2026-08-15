"""SLURM backend -- one job per route.

**Validated 2026-08-15 (STATUS.md H9).** It shares the planning, resume, retry and reporting
logic with the local backend and is written against the same interface. It has been run against a
real scheduler at two-way concurrency on one node (one full route category, seed 42), matching the
local backend's status and section-6A axes. Not yet measured at the full 475-route scale or larger
multi-node fan-out, and it has no early liveness probe (a transient simulator freeze is caught only
by ``execution.route_timeout_s``).

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

SLURM owns ``CUDA_VISIBLE_DEVICES`` and may remap the allocated global device to a logical index
inside the job cgroup. The wrapper never overwrites it. ``SLURM_JOB_GPUS`` remains a global ID,
so the wrapper uses it only to select the matching site-validated ``gpus: {cuda, vulkan}`` pair
for CARLA's independent Vulkan adapter. ``slurm.vulkan_index_scope`` states whether those Vulkan
indices address the host or each isolated allocation; allocation scope requires exactly one
visible GPU/job and may legitimately reuse adapter 0 across different physical allocations.
Ports still come from the deterministic allocator so that two jobs landing on one node cannot
collide.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from .. import jobscript, ports as ports_mod, reap
from ..config import Config
from ..plan import RouteTask
from .base import (Attempt, AttemptOutcome, Backend, restore_checkpoint,
                   take_checkpoint_aside)

_JOBID_RE = re.compile(
    r"\s*(?P<job>\d+)(?:;(?P<cluster>[A-Za-z0-9_.-]+))?\s*\Z"
)
_JOBID_PREFIX_RE = re.compile(
    r"\A\s*(?P<job>\d+)(?:;(?P<cluster>[A-Za-z0-9_.-]+))?(?=\s|\Z)"
)

_NONTERMINAL_STATES = frozenset({
    "CONFIGURING", "COMPLETING", "PENDING", "REQUEUED", "REQUEUE_FED", "REQUEUE_HOLD",
    "RESIZING", "RUNNING", "SIGNALING", "STAGE_OUT", "STOPPED", "SUSPENDED",
})

_FAULT_STATES = frozenset({
    "BOOT_FAIL", "CANCELLED", "DEADLINE", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED",
    "REVOKED", "TIMEOUT",
})

_CANCEL_WAIT_S = 60.0
_CANCEL_POLL_S = 0.2


class SlurmBackendError(Exception):
    pass


class SlurmBackend(Backend):
    name = "slurm"
    stable_worker_slots = False

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
        self._running: set[str] = set()
        self._started: set[str] = set()
        self._paused_at: Dict[str, float] = {}

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
            "the SLURM backend is validated at two-way concurrency on one node, not at the full "
            "475-route scale or larger multi-node fan-out. Submit a single route first and read "
            "the generated job script before launching a large sweep."
        )

    # -- submit -------------------------------------------------------------------------
    def _sbatch_header(self, task: RouteTask) -> str:
        s = self.cfg.slurm
        job_name = f"oodbench-{task.stem}"[:60]

        def directive(flag: str, value: object) -> str:
            return f"#SBATCH {flag}={shlex.quote(str(value))}"

        lines = [
            "#!/bin/bash",
            directive("--job-name", job_name),
            directive("--output", task.stdout_path),
            directive("--error", task.stderr_path),
            "#SBATCH --nodes=1",
            "#SBATCH --ntasks=1",
            f"#SBATCH --cpus-per-task={int(s['cpus_per_task'])}",
            directive("--mem", s["mem"]),
            directive("--time", s["time"]),
        ]
        if s["gres"]:
            lines.append(directive("--gres", s["gres"]))
        for key, flag in (("partition", "--partition"), ("account", "--account"),
                          ("qos", "--qos"), ("nodelist", "--nodelist"), ("exclude", "--exclude")):
            if s[key]:
                lines.append(directive(flag, s[key]))
        lines.extend(s["extra_directives"])
        return "\n".join(lines) + "\n"

    def _job_body(self, task: RouteTask, worker: int) -> str:
        """Render the shared job body while leaving CUDA placement owned by SLURM."""
        inner = jobscript.render(task, self.cfg, self.cfg.gpus[0], self.pairs[worker])
        generated_gpu_exports = (
            "export CUDA_VISIBLE_DEVICES=", "export NVIDIA_VISIBLE_DEVICES=", "export GPU_RANK=",
        )
        lines: List[str] = []
        for line in inner.splitlines()[1:]:  # drop the inner ``#!/bin/bash``
            if line.startswith(generated_gpu_exports):
                continue
            lines.append(line)
            if line == "set -o pipefail":
                lines.extend(self._scheduler_gpu_capture())
            if line.startswith("# GPU pinning:"):
                lines.extend(self._scheduler_gpu_exports())
        return "\n".join(lines) + "\n"

    @staticmethod
    def _scheduler_gpu_capture() -> List[str]:
        """Capture allocation-owned values before caller-controlled activation runs."""
        return [
            "# Preserve SLURM's job-entry allocation across environment.activate.",
            'if [ -z "${CUDA_VISIBLE_DEVICES:-}" ]; then',
            '  echo "[runner] SLURM did not set CUDA_VISIBLE_DEVICES for this GPU job" >&2',
            "  exit 2",
            "fi",
            'if [ -z "${SLURM_JOB_GPUS:-}" ]; then',
            '  echo "[runner] SLURM did not identify the global allocation in '
            'SLURM_JOB_GPUS" >&2',
            "  exit 2",
            "fi",
            'readonly OODBENCH_SLURM_CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES}"',
            'readonly OODBENCH_SLURM_JOB_GPUS="${SLURM_JOB_GPUS}"',
            "",
        ]

    def _scheduler_gpu_exports(self) -> List[str]:
        """Fail-closed CUDA-allocation to site-validated Vulkan-adapter mapping."""
        lines = [
            'export CUDA_VISIBLE_DEVICES="${OODBENCH_SLURM_CUDA_VISIBLE_DEVICES}"',
            'export NVIDIA_VISIBLE_DEVICES="${OODBENCH_SLURM_CUDA_VISIBLE_DEVICES}"',
            'export SLURM_JOB_GPUS="${OODBENCH_SLURM_JOB_GPUS}"',
        ]
        if self.cfg.slurm["vulkan_index_scope"] == "allocation":
            lines.extend([
                '# Allocation-local Vulkan indices are safe only for one physical GPU/job.',
                'case "${CUDA_VISIBLE_DEVICES}" in',
                '  *,*) echo "[runner] allocation-scoped Vulkan requires exactly one visible '
                'CUDA device, got ${CUDA_VISIBLE_DEVICES}" >&2; exit 2 ;;',
                'esac',
                'case "${SLURM_JOB_GPUS}" in',
                '  *,*) echo "[runner] allocation-scoped Vulkan requires exactly one global '
                'SLURM GPU, got ${SLURM_JOB_GPUS}" >&2; exit 2 ;;',
                'esac',
            ])
        lines.append('case "${SLURM_JOB_GPUS}" in')
        for gpu in self.cfg.gpus:
            lines.append(f"  {gpu.cuda}) export GPU_RANK={gpu.vulkan} ;;")
        lines.extend([
            '  *) echo "[runner] allocated global GPU ${SLURM_JOB_GPUS} has no matching cuda '
            'entry in gpus:" >&2; exit 2 ;;',
            "esac",
            'echo "[runner] SLURM gpu global=${SLURM_JOB_GPUS} '
            'cuda_visible=${CUDA_VISIBLE_DEVICES} vulkan_adapter=${GPU_RANK}"',
        ])
        return lines

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

        # ``jobscript.render`` is deliberately pure; unlike ``jobscript.write`` it does not
        # materialise the paths named by the script. The real statistics manager opens its
        # checkpoint directly and creates no parent directory, so a fresh SLURM output root
        # otherwise loses every route at its first write.
        task.mkdirs()

        # Keep the old record in hand: sbatch refusing a submission (full queue, QOS limit) is
        # routine, and must not destroy a valid result. See backends.base.take_checkpoint_aside.
        try:
            stale = take_checkpoint_aside(task)
        except OSError as exc:
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.detail = f"could not remove stale checkpoint: {exc}"
            attempt.finished_at = time.time()
            return attempt

        wrapper = task.job_script.with_suffix(".sbatch")
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        task.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        body = self._job_body(task, worker)
        wrapper.write_text(self._sbatch_header(task) + body, encoding="utf-8")
        wrapper.chmod(0o750)

        # Submission rate limiting: caps a runaway submit loop regardless of the gate above it.
        gap = float(self.cfg.slurm["submit_interval_s"]) - (time.time() - self._last_submit)
        if gap > 0:
            time.sleep(gap)
        self._last_submit = time.time()

        try:
            # Keep diagnostics off stdout: ``--parsable`` gives stdout a machine-readable
            # contract, while site wrappers and SLURM itself may still warn on stderr.
            out = subprocess.check_output(["sbatch", "--parsable", str(wrapper)], text=True,
                                          stderr=subprocess.PIPE).strip()
        except (subprocess.CalledProcessError, OSError) as exc:
            restore_checkpoint(task, stale)
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.detail = f"sbatch failed: {exc}"
            attempt.finished_at = time.time()
            return attempt

        m = _JOBID_RE.fullmatch(out)
        if not m:
            # Exit zero means the scheduler accepted a job. Trailer noise violates the strict
            # parsable contract, but a leading identifier remains safe to recover. Supervise
            # that accepted job through positive terminal evidence before restoring the old
            # checkpoint or returning an outcome that the runner may requeue.
            recovered = _JOBID_PREFIX_RE.match(out)
            if recovered is None:
                raise SlurmBackendError(
                    "sbatch accepted a job but returned no recoverable job identity; "
                    f"output was {out!r}. Refusing to restore the prior checkpoint or requeue "
                    "while an unidentified scheduler job may still write it; operator "
                    "intervention is required"
                )
            job_ref = recovered.group(0).strip()
            attempt.handle = job_ref
            self._submitted.append(job_ref)
            self.log.error(
                "%s: sbatch accepted SLURM job %s with malformed parsable output %r; "
                "cancelling it before retry is allowed", task.key, job_ref, out,
            )
            self._cancel_and_wait(job_ref)
            restore_checkpoint(task, stale)
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.detail = (
                f"sbatch returned malformed parsable output {out!r}; recovered and "
                f"cancelled job {job_ref} before allowing retry"
            )
            attempt.finished_at = time.time()
            return attempt
        job_ref = m.group(0).strip()
        attempt.handle = job_ref
        self._submitted.append(job_ref)
        self.log.info("submitted %s as SLURM job %s", task.key, job_ref)
        return attempt

    # -- poll ---------------------------------------------------------------------------
    def poll(self, attempt: Attempt) -> bool:
        if attempt.outcome is not None:
            return True
        job_ref = attempt.handle
        if not isinstance(job_ref, str):
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.finished_at = time.time()
            return True
        job_id, cluster_args = self._job_id_and_cluster(job_ref)

        # Gate on *our* job ids, never on a name grep -- see the module docstring.
        try:
            out = subprocess.check_output(
                ["squeue", "-h", "-j", job_id, "-o", "%T"] + cluster_args, text=True,
                stderr=subprocess.DEVNULL).strip()
        except (subprocess.CalledProcessError, OSError):
            out = ""
        if out:
            queue_state = self._base_state(out.splitlines()[0])
            # A state still visible in squeue is active scheduler ownership. PENDING,
            # COMPLETING, REQUEUED and SUSPENDED pause the evaluator clock; only RUNNING
            # advances it. Terminal accounting below remains the source of truth.
            return self._poll_active_state(attempt, job_ref, queue_state)

        accounting = self._sacct_state(job_ref)
        state, exit_code = accounting if accounting is not None else (None, None)
        base_state = self._base_state(state or "")
        if base_state in _NONTERMINAL_STATES:
            return self._poll_active_state(attempt, job_ref, base_state)

        finished_at = time.time()
        self._close_route_clock(attempt, job_ref, finished_at)
        attempt.finished_at = finished_at
        if exit_code:
            attempt.exit_code = self._exit_code(exit_code)
        signalled = (reap.describe_exit_signal(attempt.exit_code)
                     if attempt.exit_code is not None else None)
        if signalled:
            attempt.outcome = AttemptOutcome.FAULT
            attempt.detail = f"SLURM state {state or 'unknown'}; {signalled}"
            self._forget(job_ref)
            return True
        if base_state in _FAULT_STATES:
            attempt.outcome = AttemptOutcome.FAULT
            attempt.detail = f"SLURM state {state}"
        else:
            attempt.outcome = AttemptOutcome.EXITED
            attempt.detail = f"SLURM state {state or 'unknown'}"
        self._forget(job_ref)
        return True

    def _poll_active_state(self, attempt: Attempt, job_ref: str, state: str) -> bool:
        """Advance only RUNNING time; scheduler-owned residence pauses the route clock."""
        now = time.time()
        if state != "RUNNING":
            if job_ref in self._running:
                self._running.remove(job_ref)
                self._paused_at[job_ref] = now
            return False

        entered_running = job_ref not in self._running
        if job_ref not in self._started:
            # Attempt.started_at was created before sbatch. The first RUNNING observation
            # excludes initial queue residence, conservatively by at most one poll interval.
            self._started.add(job_ref)
            attempt.started_at = now
        elif entered_running:
            # Preserve earlier RUNNING time while removing the complete suspended/requeued
            # interval from Attempt.duration_s and the persisted duration audit.
            paused_at = self._paused_at.pop(job_ref, now)
            attempt.started_at += max(0.0, now - paused_at)
        self._running.add(job_ref)

        # As on the original first-RUNNING path, start/resume observations do not immediately
        # consume a poll interval from the route's budget.
        if entered_running:
            return False
        duration = attempt.duration_s
        if duration > float(self.cfg.execution["route_timeout_s"]):
            self.kill(attempt, "wall-clock timeout")
            attempt.outcome = AttemptOutcome.TIMEOUT
            attempt.detail = f"scancelled after {duration:.0f}s RUNNING"
            return True
        return False

    def _close_route_clock(self, attempt: Attempt, job_ref: str, now: float) -> None:
        """Exclude a final scheduler-owned paused interval from the duration audit."""
        paused_at = self._paused_at.pop(job_ref, None)
        if paused_at is not None:
            attempt.started_at += max(0.0, now - paused_at)

    def _route_runtime_at(self, attempt: Attempt, job_ref: str, now: float) -> float:
        """Return observed RUNNING time without mutating state needed by cancellation."""
        if job_ref not in self._started:
            return 0.0
        running_until = self._paused_at.get(job_ref, now)
        return max(0.0, running_until - attempt.started_at)

    @staticmethod
    def _base_state(state: str) -> str:
        """The stable state token, without a reason suffix or SLURM truncation marker."""
        return state.strip().split(None, 1)[0].rstrip("+").upper() if state.strip() else ""

    @staticmethod
    def _job_id_and_cluster(job_ref: str) -> Tuple[str, List[str]]:
        """Return the numeric ID and explicit cluster-routing arguments."""
        match = _JOBID_RE.fullmatch(job_ref)
        if match is None:  # handles are admitted only through this regex in submit()
            raise SlurmBackendError(f"invalid supervised SLURM job reference {job_ref!r}")
        cluster = match.group("cluster")
        return match.group("job"), (["-M", cluster] if cluster else [])

    @staticmethod
    def _sacct_state(job_ref: str) -> Optional[Tuple[str, str]]:
        job_id, cluster_args = SlurmBackend._job_id_and_cluster(job_ref)
        try:
            out = subprocess.check_output(
                ["sacct", "-X", "-j", job_id, "--format=State,ExitCode", "-n", "-P"]
                + cluster_args,
                text=True,
                stderr=subprocess.DEVNULL).strip()
        except (subprocess.CalledProcessError, OSError):
            return None
        if not out:
            return None
        fields = out.splitlines()[0].strip().split("|")
        state = fields[0].strip()
        exit_code = fields[1].strip() if len(fields) > 1 else ""
        return state, exit_code

    @staticmethod
    def _exit_code(value: str) -> Optional[int]:
        """Translate SLURM's ``status:signal`` into the shape used by local ``Popen``."""
        try:
            status_text, signal_text = value.split(":", 1)
            status, signum = int(status_text), int(signal_text)
        except (TypeError, ValueError):
            return None
        return -signum if signum else status

    def kill(self, attempt: Attempt, reason: str) -> None:
        job_ref = attempt.handle
        finished_at = time.time()
        route_runtime = None
        if isinstance(job_ref, str):
            # Capture the route-only clock before positive-terminal cancellation calls
            # ``_forget`` and discards the RUNNING/paused markers. The scheduler's asynchronous
            # teardown latency is not evaluator runtime either.
            route_runtime = self._route_runtime_at(attempt, job_ref, finished_at)
            self._cancel_and_wait(job_ref)
        if attempt.outcome is None:
            attempt.outcome = AttemptOutcome.KILLED
            attempt.detail = reason
            attempt.finished_at = finished_at
            if route_runtime is not None:
                attempt.started_at = finished_at - route_runtime

    def shutdown(self) -> None:
        if not self._submitted:
            return
        self.log.info("cancelling %d submitted SLURM job(s)", len(self._submitted))
        for job_ref in list(self._submitted):
            try:
                self._cancel_and_wait(job_ref)
            except SlurmBackendError as exc:  # best effort, but never silently
                self.log.warning("%s", exc)

    def _cancel_and_wait(self, job_ref: str) -> None:
        job_id, cluster_args = self._job_id_and_cluster(job_ref)
        subprocess.call(["scancel"] + cluster_args + [job_id])
        deadline = time.time() + _CANCEL_WAIT_S
        while True:
            try:
                queued = subprocess.check_output(
                    ["squeue", "-h", "-j", job_id, "-o", "%T"] + cluster_args, text=True,
                    stderr=subprocess.DEVNULL).strip()
            except (subprocess.CalledProcessError, OSError):
                queued = ""

            accounting = self._sacct_state(job_ref) if not queued else None
            state = self._base_state(accounting[0]) if accounting is not None else ""
            # An empty/failed status query is absence of evidence, not evidence that the job's
            # cgroup has finished. Require a positive terminal accounting state before any
            # caller may read and settle a checkpoint after asynchronous scancel.
            if not queued and state and state not in _NONTERMINAL_STATES:
                self._forget(job_ref)
                return
            if time.time() >= deadline:
                raise SlurmBackendError(
                    f"SLURM job {job_ref} has no confirmed terminal state "
                    f"{_CANCEL_WAIT_S:.0f}s after scancel; "
                    f"refusing to settle it while it may still write its checkpoint"
                )
            time.sleep(_CANCEL_POLL_S)

    def _forget(self, job_ref: str) -> None:
        self._running.discard(job_ref)
        self._started.discard(job_ref)
        self._paused_at.pop(job_ref, None)
        try:
            self._submitted.remove(job_ref)
        except ValueError:
            pass
