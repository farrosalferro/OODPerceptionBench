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

import json
import os
import pwd
import re
import shlex
import subprocess
import time
import uuid
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

# ``<checkpoint>.aside-<unix ns>`` (SlurmBackend._write_aside) and
# ``<checkpoint>.unsettled-<unix ns>`` (SlurmBackend._write_marker).
_LEFTOVER_RE = re.compile(r"\A(?P<checkpoint>.+)\.(?P<kind>aside|unsettled)-\d+\Z")
_LEFTOVERS_LISTED = 20

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
        # When squeue first stopped listing a job: the route clock stops there, however long
        # accounting then takes to report how it ended.
        self._gone_since: Dict[str, float] = {}
        # Every supervised job's route, and the file that holds the route on disk until the
        # job is settled: its set-aside checkpoint, or a marker when there was none.
        self._routes: Dict[str, Tuple[RouteTask, Path]] = {}

    def preflight(self) -> None:
        self._refuse_leftovers()
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

    def _refuse_leftovers(self) -> None:
        """Refuse to start while an earlier run left a route unsettled on disk.

        A set-aside checkpoint or an ``.unsettled-`` marker means that run stopped without
        settling the route (a crash, or a job it could not identify or confirm cancelled).
        This run would plan the route from whatever now sits at the checkpoint path, beside a
        job that may still write it. Nothing is moved automatically: whether a job still
        writes that path is the operator's call.
        """
        root = Path(self.cfg.output["root"])
        found = sorted(path for path in root.rglob("*-*")
                       if _LEFTOVER_RE.match(path.name) and path.is_file())
        if not found:
            return
        kinds = [_LEFTOVER_RE.match(path.name).group("kind") for path in found]
        listed = []
        for path, kind in list(zip(found, kinds))[:_LEFTOVERS_LISTED]:
            checkpoint = path.with_name(_LEFTOVER_RE.match(path.name).group("checkpoint"))
            if kind == "aside":
                listed.append(f"  {path} -> {checkpoint}")
            else:
                listed.append(f"  {path} (a job may still write {checkpoint})")
        if len(found) > _LEFTOVERS_LISTED:
            listed.append(f"  ... and {len(found) - _LEFTOVERS_LISTED} more "
                          f"(find {root} -name '*.aside-*' -o -name '*.unsettled-*')")
        counts = []
        if "aside" in kinds:
            counts.append(f"{kinds.count('aside')} set-aside checkpoint(s)")
        if "unsettled" in kinds:
            counts.append(f"{kinds.count('unsettled')} unsettled-job marker(s)")
        find_jobs = f"squeue -u {_user() or '$USER'} -o '%i %j %k'; scancel <id>"
        raise SlurmBackendError(
            f"{' and '.join(counts)} under {root} were left by an earlier run that stopped "
            "before settling those routes:\n" + "\n".join(listed) + "\n"
            "Refusing to start until each is resolved. First stop any job of the earlier run "
            f"that is still queued or running ({find_jobs}). Then, for each set-aside "
            "checkpoint, either put the earlier result back (mv <aside> <checkpoint>) or "
            "delete the file to rerun the route. If the checkpoint exists again, a job wrote "
            "it after the file was set aside: keep whichever you trust. Delete each "
            ".unsettled- file once the job it names (by id, or by name and comment) has ended. "
            f"{root / '_runner' / 'aside.jsonl'} records when and why each file was left."
        )

    # -- submit -------------------------------------------------------------------------
    def _sbatch_header(self, task: RouteTask, tag: str) -> str:
        s = self.cfg.slurm
        job_name = _job_name(task)

        def directive(flag: str, value: object) -> str:
            return f"#SBATCH {flag}={shlex.quote(str(value))}"

        lines = [
            "#!/bin/bash",
            directive("--job-name", job_name),
            # This submission's own identity, for when sbatch's reply names no job.
            directive("--comment", tag),
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

        wrapper = task.job_script.with_suffix(".sbatch")
        tag = _submission_tag()

        # Keep the old record: sbatch refusing a submission (full queue, QOS limit) is routine,
        # and must not destroy a valid result. See backends.base.take_checkpoint_aside. It is
        # kept on disk, synced, before the original goes: held only in memory, any exception
        # or crash before sbatch's answer was understood lost the route's only record.
        # With no old record, a marker takes its place. Either way a file on disk stops the
        # next run from starting beside this job until it is settled (``_refuse_leftovers``),
        # even if this process dies without reaching shutdown.
        what = "set the old checkpoint aside"
        try:
            hold = self._write_aside(task)
            if hold is None:
                what = "write the unsettled-job marker"
                hold = self._write_marker(task, "marked", "submitted", job_name=_job_name(task),
                                          comment=tag, script=str(wrapper))
        except OSError as exc:
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.detail = f"could not {what}: {exc}"
            attempt.finished_at = time.time()
            self.log.error("%s: %s; not submitting", task.key, attempt.detail)
            return attempt
        try:
            take_checkpoint_aside(task)
        except OSError as exc:
            self._drop_hold(task, hold)
            attempt.outcome = AttemptOutcome.LAUNCH_FAILED
            attempt.detail = f"could not remove stale checkpoint: {exc}"
            attempt.finished_at = time.time()
            return attempt

        wrapper.parent.mkdir(parents=True, exist_ok=True)
        task.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        body = self._job_body(task, worker)
        wrapper.write_text(self._sbatch_header(task, tag) + body, encoding="utf-8")
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
            self._drop_hold(task, hold)
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
                return self._identify_by_tag(attempt, task, wrapper, hold, out, tag)
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
                # The job may still be writing the checkpoint, so the route stays held.
                # The job stays in ``_submitted``: shutdown tries the cancel again.
                hold = self._leave_unsettled(task, hold, "cancel unconfirmed", job=job_ref)
                raise SlurmBackendError(
                    f"{exc}. {_reclaim_steps(task, hold, f'scancel {job_ref}')}"
                ) from exc
            self._drop_hold(task, hold)
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
        self._routes[job_ref] = (task, hold)
        self.log.info("submitted %s as SLURM job %s", task.key, job_ref)
        return attempt

    def _identify_by_tag(self, attempt: Attempt, task: RouteTask, wrapper: Path,
                         hold: Path, out: str, tag: str) -> Attempt:
        """sbatch exited zero, so a job exists, but its reply named none. Find it.

        Name and script are not enough: an older job of the same route (an earlier retry or
        run on this output root) has both, and while the new job is not yet visible it would
        be the only match. So each submission carries its own ``--comment`` tag, and only a
        job of the user's with this route's name *and* this tag is supervised. Otherwise
        nothing is cancelled or restored: an unknown job may still write the checkpoint, and
        the operator is told how to recover.
        """
        name = _job_name(task)
        found, failure = _own_jobs(name, tag)
        if found is not None and len(found) == 1:
            job_ref = found[0]
            attempt.handle = job_ref
            self._submitted.append(job_ref)
            self._routes[job_ref] = (task, hold)
            self.log.warning(
                "%s: sbatch reply %r carried no job id; identified SLURM job %s by its name and "
                "submission tag", task.key, out, job_ref,
            )
            return attempt

        if found is None:
            lookup = f"Looking it up by name {name} and tag {tag} failed: {failure}"
        elif not found:
            lookup = f"There is no job of yours named {name} with the tag {tag}"
        else:
            lookup = (f"Jobs {', '.join(found)} of yours are all named {name} with the tag "
                      f"{tag}, so none can be told apart")
        hold = self._leave_unsettled(task, hold, "unidentified job", job_name=name,
                                     comment=tag, script=str(wrapper), reply=out)
        find = f"squeue -u {_user() or '$USER'} --name {name} -o '%i %k'; scancel <id>"
        raise SlurmBackendError(
            "sbatch accepted a job but returned no recoverable job identity; "
            f"output was {out!r}. {lookup}. Refusing to restore the prior checkpoint or "
            "requeue while an unidentified scheduler job may still write it. "
            + _reclaim_steps(task, hold, find)
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
        _write_synced(aside, blob)
        self._aside_ledger(task, "set_aside", aside)
        return aside

    def _write_marker(self, task: RouteTask, event: str, reason: str, **info: object) -> Path:
        """Write ``<checkpoint>.unsettled-<unix ns>``, a synced note on the job that may write
        ``task``'s checkpoint. Raises OSError with no partial file left."""
        marker = task.result_path.with_name(
            f"{task.result_path.name}.unsettled-{time.time_ns()}")
        note = {"checkpoint": str(task.result_path), "reason": reason, **info}
        _write_synced(marker, (json.dumps(note, sort_keys=True) + "\n").encode("utf-8"))
        self._aside_ledger(task, event, marker, reason=reason, **info)
        return marker

    def _leave_unsettled(self, task: RouteTask, hold: Path, reason: str,
                         **info: object) -> Path:
        """This run gives up on a job that may still write ``task``'s checkpoint.

        The file holding the route stays, so the next run refuses to start until the operator
        has looked (see ``_refuse_leftovers``). A marker is rewritten to say why and which job;
        its replacement is on disk before the first goes, so the route is never left without
        one. Returns the file that now holds the route.
        """
        if _hold_kind(hold) == "aside":
            self._aside_ledger(task, "kept", hold, reason=reason, **info)
            return hold
        try:
            earlier = json.loads(hold.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            earlier = {}
        known = {k: v for k, v in earlier.items() if k not in ("checkpoint", "reason")}
        try:
            marker = self._write_marker(task, "kept", reason, **{**known, **info})
        except OSError as exc:
            self.log.error("%s: could not update %s (%s); it still stops the next run",
                           task.key, hold, exc)
            self._aside_ledger(task, "kept", hold, reason=reason, **info)
            return hold
        try:
            hold.unlink()
        except OSError as exc:  # two markers for one route still stop the next run
            self.log.warning("%s: could not delete %s: %s", task.key, hold, exc)
        return marker

    def _abandon(self, job_ref: str, why: str, reason: str) -> SlurmBackendError:
        """Stop managing ``job_ref``'s route, leaving it unsettled on disk; the error to raise.

        The job stays in ``_submitted``, so shutdown still tries to cancel it. Its route is
        no longer tracked, so even a cancel confirmed later deletes nothing.
        """
        entry = self._routes.pop(job_ref, None)
        if entry is None:
            return SlurmBackendError(why)
        task, hold = entry
        hold = self._leave_unsettled(task, hold, reason, job=job_ref)
        return SlurmBackendError(f"{why}. {_reclaim_steps(task, hold, f'scancel {job_ref}')}")

    def _drop_hold(self, task: RouteTask, hold: Path) -> None:
        """No job was left running: put the set-aside checkpoint back, or delete the marker."""
        try:
            if _hold_kind(hold) == "aside":
                os.replace(hold, task.result_path)  # the rename also removes the file
            else:
                hold.unlink()
            _fsync_dir(hold.parent)
        except OSError as exc:  # the file survives; startup refuses until reclaimed
            self.log.error("%s: could not %s %s: %s", task.key,
                           "restore" if _hold_kind(hold) == "aside" else "delete", hold, exc)
            return
        self._aside_ledger(task, "restored" if _hold_kind(hold) == "aside" else "discarded",
                           hold)

    def _release(self, job_ref: str) -> None:
        """The job is over: the file that held its route is no longer needed."""
        task, hold = self._routes.pop(job_ref, (None, None))
        if hold is None:
            return
        try:
            hold.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.log.warning("%s: could not delete %s: %s", task.key, hold, exc)
            return
        self._aside_ledger(task, "discarded", hold, job=job_ref)

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

        # Gate on *our* job ids, never on a name grep -- see the module docstring.
        queued, queue_missing = self._squeue_query(job_ref)
        if queued:
            self._unknown_since.pop(job_ref, None)
            self._gone_since.pop(job_ref, None)
            # A state still visible in squeue is active scheduler ownership. PENDING,
            # COMPLETING, REQUEUED and SUSPENDED pause the evaluator clock; only RUNNING
            # advances it. Terminal accounting below remains the source of truth.
            return self._poll_active_state(attempt, job_ref, self._base_state(queued))

        # The job has left the queue, or squeue cannot say: either way accounting decides.
        if queued == "":
            self._leave_queue(job_ref)
        accounting, missing = self._sacct_query(job_ref)
        if accounting is None:
            if queued is None:
                return self._poll_unreachable(attempt, job_ref, f"{queue_missing}; {missing}")
            return self._poll_without_accounting(attempt, job_ref, missing)
        self._unknown_since.pop(job_ref, None)
        state, exit_code = accounting
        base_state = self._base_state(state)
        if base_state in _NONTERMINAL_STATES:
            if job_ref in self._gone_since:
                return self._poll_after_the_queue(attempt, job_ref, base_state)
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

    def _leave_queue(self, job_ref: str) -> None:
        """squeue no longer lists the job: stop the route clock as a pause would.

        Only squeue listing the job again restarts it (through ``_poll_active_state``).
        """
        now = time.time()
        self._gone_since.setdefault(job_ref, now)
        if job_ref in self._running:
            self._running.remove(job_ref)
            self._paused_at[job_ref] = now

    def _poll_after_the_queue(self, attempt: Attempt, job_ref: str, state: str) -> bool:
        """Accounting still reports ``state`` for a job squeue no longer lists.

        Accounting lags the queue, so this neither starts nor resumes the route clock. The
        wait is bounded by ``route_timeout_s`` from when the job left the queue.
        """
        waited = time.time() - self._gone_since[job_ref]
        if waited > float(self.cfg.execution["route_timeout_s"]):
            self.kill(attempt, "wall-clock timeout")
            attempt.outcome = AttemptOutcome.TIMEOUT
            attempt.detail = (f"scancelled {waited:.0f}s after it left the queue; accounting "
                              f"still says {state}")
            return True
        return False

    def _poll_without_accounting(self, attempt: Attempt, job_ref: str, missing: str) -> bool:
        """The job has left the queue but accounting has no state: after a grace, a FAULT.

        An empty or failed accounting reply is absence of evidence. Settling it as a clean
        exit charged the model's record budget for an end nobody observed. After
        ``_ACCOUNTING_GRACE_S`` it settles as FAULT instead (a retry, never the record
        budget), with the route clock stopped where the job was last seen, and a best-effort
        ``scancel`` in case it is still alive somewhere.
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

    def _poll_unreachable(self, attempt: Attempt, job_ref: str, missing: str) -> bool:
        """squeue failed and accounting has no state: nothing says the job has ended.

        Settling would let the route be retried, and its old checkpoint deleted, beside a job
        that may still be running. After ``_ACCOUNTING_GRACE_S`` the run stops instead and
        leaves the route unsettled on disk for the operator.
        """
        now = time.time()
        since = self._unknown_since.setdefault(job_ref, now)
        if now - since < _ACCOUNTING_GRACE_S:
            return False
        raise self._abandon(
            job_ref,
            f"SLURM job {job_ref}: squeue cannot say whether it is queued and accounting has no "
            f"state after {now - since:.0f}s ({missing}); refusing to settle it while it may "
            "still write its checkpoint",
            "scheduler unreachable")

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
        """RUNNING time seen by polls, excluding paused residence and time out of the queue;
        None if never seen RUNNING.

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
    def _squeue_query(job_ref: str) -> Tuple[Optional[str], str]:
        """Return ``(state, "")`` while queued, ``("", "")`` once gone, or ``(None, why)``.

        Gone is a normal end: a real squeue prints nothing for a recently finished job, and
        exits 1 with "Invalid job id specified" once the job is purged (the same reply as for
        an id never issued). Any other failure, such as an unreachable controller, says
        nothing about the job.
        """
        job_id, cluster_args = SlurmBackend._job_id_and_cluster(job_ref)
        try:
            result = subprocess.run(["squeue", "-h", "-j", job_id, "-o", "%T"] + cluster_args,
                                    capture_output=True, text=True, env=_squeue_env())
        except OSError as exc:
            return None, f"squeue could not run ({exc.strerror or exc})"
        out = result.stdout.strip()
        if result.returncode == 0:
            return (out.splitlines()[0] if out else ""), ""
        if "Invalid job id specified" in result.stderr:
            return "", ""
        lines = result.stderr.strip().splitlines()
        reason = f": {lines[0].strip()[:200]}" if lines else ""
        return None, f"squeue failed (exit {result.returncode}){reason}"

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
        try:
            self._cancel_and_wait(job_ref)
        except SlurmBackendError as exc:
            raise self._abandon(job_ref, str(exc), "cancel unconfirmed") from exc
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
                self.log.warning("%s", self._abandon(job_ref, str(exc), "cancel unconfirmed"))

    def _cancel_and_wait(self, job_ref: str) -> None:
        job_id, cluster_args = self._job_id_and_cluster(job_ref)
        subprocess.call(["scancel"] + cluster_args + [job_id])
        deadline = time.time() + _CANCEL_WAIT_S
        while True:
            queued = self._squeue_query(job_ref)[0]
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
        self._release(job_ref)
        self._unknown_since.pop(job_ref, None)
        self._gone_since.pop(job_ref, None)
        self._running.discard(job_ref)
        self._started.discard(job_ref)
        self._paused_at.pop(job_ref, None)
        try:
            self._submitted.remove(job_ref)
        except ValueError:
            pass


def _job_name(task: RouteTask) -> str:
    return f"oodbench-{task.stem}"[:60]


def _submission_tag() -> str:
    """A fresh identity for one submission, carried in the job's ``--comment``."""
    return f"oodbench:{uuid.uuid4().hex}"


def _user() -> Optional[str]:
    """The effective user's name, which SLURM records; never LOGNAME or USER."""
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:  # no passwd entry, as in some containers
        return None


def _squeue_env() -> Dict[str, str]:
    """This process's environment without the ``SQUEUE_*`` defaults squeue reads as options.

    A user's ``SQUEUE_STATES=PENDING`` would hide a RUNNING job, which then reads as gone.
    ``SLURM_*`` settings such as ``SLURM_CONF`` still pass through.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("SQUEUE_")}


def _own_jobs(name: str, tag: str) -> Tuple[Optional[List[str]], str]:
    """IDs of the user's queued jobs named ``name`` whose comment is ``tag``.

    Returns ``(None, reason)`` when the lookup itself failed.
    """
    user = _user()
    if user is None:
        return None, "the current user name is unknown"
    try:
        done = subprocess.run(["squeue", "-h", "-u", user, "--name", name, "-o", "%i|%k"],
                              capture_output=True, text=True, env=_squeue_env())
    except OSError as exc:
        return None, f"squeue could not run ({exc})"
    if done.returncode != 0:
        first = (done.stderr.strip().splitlines() or [""])[0][:200]
        return None, f"squeue failed (exit {done.returncode}): {first}"
    found = []
    for line in done.stdout.splitlines():
        job_id, sep, comment = line.strip().partition("|")
        if sep and job_id.isdigit() and comment.strip() == tag:
            found.append(job_id)
    return found, ""


def _reclaim_steps(task: RouteTask, hold: Path, stop_job: str) -> str:
    """How an operator gets a route back after the runner refused to touch it."""
    if _hold_kind(hold) == "unsettled":
        return (f"There was no previous checkpoint at {task.result_path}. Stop the job "
                f"({stop_job}), confirm it has ended (sacct -j <id>), then delete {hold}, "
                "which keeps the next run from starting.")
    return (f"The previous checkpoint was set aside to {hold} and was not restored. To "
            f"recover: stop the job ({stop_job}), confirm it has ended (sacct -j <id>), then "
            f"move {hold} back to {task.result_path}.")


def _hold_kind(path: Path) -> str:
    """``"aside"`` or ``"unsettled"``: which kind of file holds a route on disk."""
    return _LEFTOVER_RE.match(path.name).group("kind")


def _write_synced(path: Path, blob: bytes) -> None:
    """Create ``path`` holding ``blob``, synced with its directory entry.

    Raises OSError (a full disk, say) with no partial file left behind.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        view = memoryview(blob)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            path.unlink()
        except OSError:  # never mask the write error that brought us here
            pass
        raise
    os.close(fd)
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Make a create, rename or delete in ``directory`` durable."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
