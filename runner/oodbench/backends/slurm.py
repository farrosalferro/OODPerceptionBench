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

import getpass
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import jobscript, ports as ports_mod, reap
from ..config import Config
from ..plan import RouteTask
from .base import Attempt, AttemptOutcome, Backend, take_checkpoint_aside

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

# How long a job may be unknown to both ``squeue`` and ``sacct`` before it settles as FAULT.
# A module constant, not a config key: a new key would move every existing config digest.
_ACCOUNTING_GRACE_S = 180.0

_ELAPSED_RE = re.compile(r"\A(?:(?P<days>\d+)-)?(?P<h>\d+):(?P<m>\d+):(?P<s>\d+)\Z")
_RUNTIME_UNKNOWN = "runtime unknown (never observed RUNNING)"


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
        self._unknown_since: Dict[str, float] = {}
        self._aside: Dict[str, Tuple[RouteTask, Path]] = {}

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
        job_name = _job_name(task)

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

        # Keep the old record: sbatch refusing a submission (full queue, QOS limit) is routine,
        # and must not destroy a valid result. See backends.base.take_checkpoint_aside. It is
        # kept on disk, synced, before the original goes: held only in memory, any exception
        # or crash before sbatch's answer was understood lost the route's only record.
        try:
            aside = self._write_aside(task)
        except OSError as exc:
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.detail = f"could not set the old checkpoint aside: {exc}"
            attempt.finished_at = time.time()
            self.log.error("%s: %s; not submitting", task.key, attempt.detail)
            return attempt
        try:
            take_checkpoint_aside(task)
        except OSError as exc:
            self._restore_aside(task, aside)
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
            self._restore_aside(task, aside)
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
                return self._identify_by_name(attempt, task, wrapper, aside, out)
            job_ref = recovered.group(0).strip()
            attempt.handle = job_ref
            self._submitted.append(job_ref)
            self.log.error(
                "%s: sbatch accepted SLURM job %s with malformed parsable output %r; "
                "cancelling it before retry is allowed", task.key, job_ref, out,
            )
            try:
                self._cancel_and_wait(job_ref)
            except SlurmBackendError as exc:
                # The job may still be writing the checkpoint, so the old one stays aside.
                # The job stays in ``_submitted``: shutdown tries the cancel again.
                if aside is not None:
                    self._aside_ledger(task, "kept", aside, job=job_ref)
                raise SlurmBackendError(
                    f"{exc}. {_reclaim_steps(task, aside, f'scancel {job_ref}')}"
                ) from exc
            self._restore_aside(task, aside)
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
        if aside is not None:
            self._aside[job_ref] = (task, aside)
        self.log.info("submitted %s as SLURM job %s", task.key, job_ref)
        return attempt

    def _identify_by_name(self, attempt: Attempt, task: RouteTask, wrapper: Path,
                          aside: Optional[Path], out: str) -> Attempt:
        """sbatch exited zero, so a job exists, but its reply named none. Find it.

        A job name alone is not enough: the same route stem recurs across seeds, levels and
        separate runs, and ``squeue --name`` lists every one of the user's jobs with that name.
        So the job must also run this submission's own script (``%o`` is always absolute).
        Exactly one match is supervised as usual. Otherwise nothing is cancelled or restored:
        an unknown job may still write the checkpoint, and the operator is told how to recover.
        """
        name = _job_name(task)
        found, failure = _own_jobs(name, wrapper)
        if found is not None and len(found) == 1:
            job_ref = found[0]
            attempt.handle = job_ref
            self._submitted.append(job_ref)
            if aside is not None:
                self._aside[job_ref] = (task, aside)
            self.log.warning(
                "%s: sbatch reply %r carried no job id; identified SLURM job %s by its name and "
                "script", task.key, out, job_ref,
            )
            return attempt

        if found is None:
            lookup = f"Looking it up by name {name} and script {wrapper} failed: {failure}"
        elif not found:
            lookup = f"There is no job of yours named {name} running the script {wrapper}"
        else:
            lookup = (f"Jobs {', '.join(found)} of yours are all named {name} and run the "
                      f"script {wrapper}, so none can be told apart")
        if aside is not None:
            self._aside_ledger(task, "kept", aside, reason="unidentified job")
        find = f"squeue -u {_user() or '$USER'} --name {name} -o '%i %o'; scancel <id>"
        raise SlurmBackendError(
            "sbatch accepted a job but returned no recoverable job identity; "
            f"output was {out!r}. {lookup}. Refusing to restore the prior checkpoint or "
            "requeue while an unidentified scheduler job may still write it. "
            + _reclaim_steps(task, aside, find)
        )

    # -- the set-aside checkpoint -------------------------------------------------------
    def _write_aside(self, task: RouteTask) -> Optional[Path]:
        """Copy the current checkpoint to ``<checkpoint>.aside-<unix ns>``, synced to disk.

        Returns None when there is no checkpoint. Raises OSError (a full disk, say) with no
        partial file left and the original untouched, so the caller can abort before sbatch.
        """
        try:
            blob = task.result_path.read_bytes()
        except FileNotFoundError:
            return None
        aside = task.result_path.with_name(f"{task.result_path.name}.aside-{time.time_ns()}")
        fd = os.open(aside, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            view = memoryview(blob)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                aside.unlink()
            except OSError:  # never mask the write error that brought us here
                pass
            raise
        os.close(fd)
        _fsync_dir(aside.parent)
        self._aside_ledger(task, "set_aside", aside)
        return aside

    def _restore_aside(self, task: RouteTask, aside: Optional[Path]) -> None:
        """Put the set-aside checkpoint back in place; the rename also removes the file."""
        if aside is None:
            return
        try:
            os.replace(aside, task.result_path)
            _fsync_dir(aside.parent)
        except OSError as exc:  # the aside file survives; startup refuses until reclaimed
            self.log.error("%s: could not restore %s: %s", task.key, aside, exc)
            return
        self._aside_ledger(task, "restored", aside)

    def _discard_aside(self, job_ref: str) -> None:
        """The job is over: the checkpoint it replaced is no longer needed."""
        entry = self._aside.pop(job_ref, None)
        if entry is None:
            return
        task, aside = entry
        try:
            aside.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.log.warning("%s: could not delete %s: %s", task.key, aside, exc)
            return
        self._aside_ledger(task, "discarded", aside, job=job_ref)

    def _aside_ledger(self, task: RouteTask, event: str, aside: Path, **extra) -> None:
        """Append one synced line to ``_runner/aside.jsonl``: what happened to which file."""
        path = Path(self.cfg.output["root"]) / "_runner" / "aside.jsonl"
        line = {"time": time.time(), "event": event, "key": task.key, "path": str(aside)}
        line.update(extra)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:  # the aside file itself is the record; the ledger explains it
            self.log.warning("could not append to %s: %s", path, exc)

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
            self._unknown_since.pop(job_ref, None)
            queue_state = self._base_state(out.splitlines()[0])
            # A state still visible in squeue is active scheduler ownership. PENDING,
            # COMPLETING, REQUEUED and SUSPENDED pause the evaluator clock; only RUNNING
            # advances it. Terminal accounting below remains the source of truth.
            return self._poll_active_state(attempt, job_ref, queue_state)

        # A failed squeue is not proof the job is gone: once a finished job is purged, a real
        # squeue exits 1 ("Invalid job id"). So both paths ask accounting.
        accounting, missing = self._sacct_query(job_ref)
        if accounting is None:
            return self._poll_without_accounting(attempt, job_ref, missing)
        self._unknown_since.pop(job_ref, None)
        state, exit_code = accounting
        base_state = self._base_state(state)
        if base_state in _NONTERMINAL_STATES:
            return self._poll_active_state(attempt, job_ref, base_state)

        finished_at = time.time()
        runtime, unknown = self._route_runtime(attempt, job_ref, finished_at)
        if exit_code:
            attempt.exit_code = self._exit_code(exit_code)
        signalled = (reap.describe_exit_signal(attempt.exit_code)
                     if attempt.exit_code is not None else None)
        if signalled:
            outcome, detail = AttemptOutcome.FAULT, f"SLURM state {state}; {signalled}"
        elif base_state in _FAULT_STATES:
            outcome, detail = AttemptOutcome.FAULT, f"SLURM state {state}"
        else:
            outcome, detail = AttemptOutcome.EXITED, f"SLURM state {state}"
        self._settle_attempt(attempt, outcome, detail, finished_at, runtime, unknown)
        self._forget(job_ref)
        return True

    def _poll_without_accounting(self, attempt: Attempt, job_ref: str, missing: str) -> bool:
        """Neither squeue nor sacct knows the job: wait out a grace, then settle as FAULT.

        An empty or failed accounting reply is absence of evidence. Settling it as a clean
        exit charged the model's record budget for an end nobody observed. After
        ``_ACCOUNTING_GRACE_S`` it settles on the bounded axis instead, with the route clock
        stopped where the job was last seen, and a best-effort ``scancel`` in case it is
        still alive somewhere.
        """
        now = time.time()
        since = self._unknown_since.setdefault(job_ref, now)
        if now - since < _ACCOUNTING_GRACE_S:
            return False
        job_id, cluster_args = self._job_id_and_cluster(job_ref)
        try:
            subprocess.call(["scancel"] + cluster_args + [job_id],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
        self.log.warning("SLURM job %s: no squeue or sacct answer for %.0fs (%s); settling it "
                         "as a fault", job_ref, now - since, missing)
        # Accounting is down, so a never-observed run cannot be measured either.
        runtime = self._observed_runtime(attempt, job_ref, since)
        self._settle_attempt(
            attempt, AttemptOutcome.FAULT,
            f"accounting unavailable after {_ACCOUNTING_GRACE_S:.0f}s: {missing}",
            since, runtime if runtime is not None else 0.0, runtime is None)
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

    def _observed_runtime(self, attempt: Attempt, job_ref: str, now: float) -> Optional[float]:
        """RUNNING time seen by polls, excluding paused residence; None if never seen RUNNING.

        Reads without mutating, so ``kill`` can call it before cancellation forgets the job.
        """
        if job_ref not in self._started:
            return None
        running_until = self._paused_at.get(job_ref, now)
        return max(0.0, running_until - attempt.started_at)

    def _route_runtime(self, attempt: Attempt, job_ref: str, now: float) -> Tuple[float, bool]:
        """The route runtime to record, and whether it is unknown (then it is 0).

        The one clock shared by settlement and ``kill``. ``Attempt.started_at`` is stamped
        before ``sbatch``, so for a job no poll saw RUNNING, "now - started_at" is mostly queue
        wait. Such a job takes its runtime from accounting ``Elapsed`` instead, which also
        sidesteps clock skew between the compute node and this host.
        """
        observed = self._observed_runtime(attempt, job_ref, now)
        if observed is not None:
            return observed, False
        accounted = self._accounted_runtime(job_ref)
        return (accounted, False) if accounted is not None else (0.0, True)

    @staticmethod
    def _accounted_runtime(job_ref: str) -> Optional[float]:
        """sacct ``Elapsed`` in seconds; None if the job never started or sacct cannot say."""
        job_id, cluster_args = SlurmBackend._job_id_and_cluster(job_ref)
        try:
            out = subprocess.check_output(
                ["sacct", "-X", "-j", job_id, "-n", "-P", "-o", "Start,End,Elapsed"]
                + cluster_args,
                text=True, stderr=subprocess.DEVNULL).strip()
        except (subprocess.CalledProcessError, OSError):
            return None
        fields = out.splitlines()[0].split("|") if out else []
        if len(fields) < 3 or fields[0].strip() in ("", "None", "Unknown"):
            return None  # a job cancelled while PENDING reports Start=None
        match = _ELAPSED_RE.match(fields[2].strip())
        if match is None:
            return None
        return float(int(match["days"] or 0) * 86400 + int(match["h"]) * 3600
                     + int(match["m"]) * 60 + int(match["s"]))

    @staticmethod
    def _settle_attempt(attempt: Attempt, outcome: AttemptOutcome, detail: str,
                        finished_at: float, runtime: float, unknown: bool) -> None:
        """Record the outcome so that ``Attempt.duration_s`` is exactly ``runtime``."""
        attempt.outcome = outcome
        attempt.detail = f"{detail}; {_RUNTIME_UNKNOWN}" if unknown else detail
        attempt.finished_at = finished_at
        attempt.started_at = finished_at - runtime

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
        return SlurmBackend._sacct_query(job_ref)[0]

    @staticmethod
    def _sacct_query(job_ref: str) -> Tuple[Optional[Tuple[str, str]], str]:
        """Return ``((state, exit_code), "")``, or ``(None, why accounting had no answer)``."""
        job_id, cluster_args = SlurmBackend._job_id_and_cluster(job_ref)
        try:
            result = subprocess.run(
                ["sacct", "-X", "-j", job_id, "--format=State,ExitCode", "-n", "-P"]
                + cluster_args,
                capture_output=True, text=True)
        except OSError as exc:
            return None, f"sacct could not run ({exc.strerror or exc})"
        if result.returncode != 0:
            lines = result.stderr.strip().splitlines()
            reason = f": {lines[0].strip()[:200]}" if lines else ""
            return None, f"sacct failed (exit {result.returncode}){reason}"
        out = result.stdout.strip()
        if not out:
            return None, "sacct returned no record"
        fields = out.splitlines()[0].strip().split("|")
        state = fields[0].strip()
        if not state:
            return None, "sacct returned no state"
        exit_code = fields[1].strip() if len(fields) > 1 else ""
        return (state, exit_code), ""

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
        if not isinstance(job_ref, str):
            if attempt.outcome is None:
                attempt.outcome = AttemptOutcome.KILLED
                attempt.detail = reason
                attempt.finished_at = finished_at
            return
        # Capture the route-only clock before positive-terminal cancellation calls ``_forget``
        # and discards the RUNNING/paused markers. The scheduler's asynchronous teardown
        # latency is not evaluator runtime either.
        observed = self._observed_runtime(attempt, job_ref, finished_at)
        self._cancel_and_wait(job_ref)
        if attempt.outcome is None:
            if observed is not None:
                runtime, unknown = observed, False
            else:  # only now is accounting final; same rule as settlement
                runtime, unknown = self._route_runtime(attempt, job_ref, finished_at)
            self._settle_attempt(attempt, AttemptOutcome.KILLED, reason, finished_at,
                                 runtime, unknown)

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
        self._discard_aside(job_ref)
        self._unknown_since.pop(job_ref, None)
        self._running.discard(job_ref)
        self._started.discard(job_ref)
        self._paused_at.pop(job_ref, None)
        try:
            self._submitted.remove(job_ref)
        except ValueError:
            pass


def _job_name(task: RouteTask) -> str:
    return f"oodbench-{task.stem}"[:60]


def _user() -> Optional[str]:
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # no login name and no passwd entry, as in some containers
        return None


def _own_jobs(name: str, wrapper: Path) -> Tuple[Optional[List[str]], str]:
    """IDs of the user's queued jobs named ``name`` that run ``wrapper``.

    Returns ``(None, reason)`` when the lookup itself failed.
    """
    user = _user()
    if user is None:
        return None, "the current user name is unknown"
    try:
        done = subprocess.run(["squeue", "-h", "-u", user, "--name", name, "-o", "%i|%o"],
                              capture_output=True, text=True)
    except OSError as exc:
        return None, f"squeue could not run ({exc})"
    if done.returncode != 0:
        first = (done.stderr.strip().splitlines() or [""])[0][:200]
        return None, f"squeue failed (exit {done.returncode}): {first}"
    target = os.path.realpath(wrapper)
    found = []
    for line in done.stdout.splitlines():
        job_id, sep, script = line.strip().partition("|")
        if sep and job_id.isdigit() and os.path.realpath(script) == target:
            found.append(job_id)
    return found, ""


def _reclaim_steps(task: RouteTask, aside: Optional[Path], stop_job: str) -> str:
    """How an operator gets a route back after the runner refused to touch it."""
    if aside is None:
        return (f"There was no previous checkpoint at {task.result_path}. Stop the job first "
                f"({stop_job}) and confirm it has ended before rerunning.")
    return (f"The previous checkpoint was set aside to {aside} and was not restored. To "
            f"recover: stop the job ({stop_job}), confirm it has ended (sacct -j <id>), then "
            f"move {aside} back to {task.result_path}.")


def _fsync_dir(directory: Path) -> None:
    """Make a create, rename or delete in ``directory`` durable."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
