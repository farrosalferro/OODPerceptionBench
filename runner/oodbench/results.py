"""Result-file interpretation: finalization predicate and status taxonomy.

Everything the runner decides about whether a route is done, retryable, or a fatal
misconfiguration flows through this module. See DESIGN.md section 5.

The status strings are taken from the vendored leaderboard's ``statistics_manager.py``
(``FAILURE_MESSAGES`` plus the ``Perfect``/``Completed``/``Failed`` construction) and
``leaderboard_evaluator.py`` (the ``TickRuntime`` handler), and cross-checked against ~4,000
real records from the published seed-42 sweep.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------------------
STATUS_PERFECT = "Perfect"
STATUS_COMPLETED = "Completed"
STATUS_FAILED = "Failed"
STATUS_TICKRUNTIME = "Failed - TickRuntime"
STATUS_BLOCKED = "Failed - Agent got blocked"
STATUS_DEVIATED = "Failed - Agent deviated from the route"
STATUS_TIMED_OUT = "Failed - Agent timed out"
STATUS_AGENT_SETUP = "Failed - Agent couldn't be set up"
STATUS_AGENT_CRASHED = "Failed - Agent crashed"
STATUS_SIM_CRASHED = "Failed - Simulation crashed"
STATUS_SENSORS_INVALID = "Failed - Agent's sensors were invalid"

#: Route finished and the outcome is a legitimate benchmark result. Never retried.
ACCEPT_STATUSES = frozenset({
    STATUS_PERFECT,
    STATUS_COMPLETED,
    STATUS_BLOCKED,
    STATUS_DEVIATED,
    STATUS_TIMED_OUT,
})

#: Exactly the set the internal SLURM orchestrator resubmits. Bounded retry, then accepted.
RETRY_STATUSES = frozenset({
    STATUS_FAILED,
    STATUS_AGENT_SETUP,
    STATUS_AGENT_CRASHED,
    STATUS_SIM_CRASHED,
})

#: Model-side: the agent is slower than CARLA's tick budget. Own budget, default 0 retries,
#: matching the internal orchestrator (which does not resubmit it) and the runbook.
TICKRUNTIME_STATUSES = frozenset({STATUS_TICKRUNTIME})

#: The agent's sensor configuration was rejected. It will fail identically on every route, so
#: retrying is pure waste -- abort the whole sweep instead.
FATAL_STATUSES = frozenset({STATUS_SENSORS_INVALID})

KNOWN_STATUSES = ACCEPT_STATUSES | RETRY_STATUSES | TICKRUNTIME_STATUSES | FATAL_STATUSES


class Disposition(Enum):
    """What the runner should do about a record it just read."""

    ACCEPT = "accept"            # legitimate outcome, done
    RETRY_RECORD = "retry"       # spend a record-budget attempt
    RETRY_TICKRUNTIME = "retry_tickruntime"   # spend a tickruntime-budget attempt
    FATAL = "fatal"              # abort the sweep
    UNKNOWN = "unknown"          # unrecognised status: retry, and warn loudly


def classify(status: Optional[str]) -> Disposition:
    if status in ACCEPT_STATUSES:
        return Disposition.ACCEPT
    if status in RETRY_STATUSES:
        return Disposition.RETRY_RECORD
    if status in TICKRUNTIME_STATUSES:
        return Disposition.RETRY_TICKRUNTIME
    if status in FATAL_STATUSES:
        return Disposition.FATAL
    return Disposition.UNKNOWN


# ---------------------------------------------------------------------------------------
# Reading a result file
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ResultRecord:
    """What we could learn from a checkpoint JSON on disk."""

    exists: bool
    parsable: bool
    final: bool
    status: Optional[str]
    score_composed: Optional[float]
    n_records: int
    error: Optional[str] = None

    @property
    def disposition(self) -> Disposition:
        if not self.final:
            return Disposition.UNKNOWN
        return classify(self.status)


_MISSING = ResultRecord(False, False, False, None, None, 0, "file does not exist")


def read(result_path: str | Path) -> ResultRecord:
    """Read and interpret a leaderboard checkpoint JSON.

    A file is **final** iff it exists, parses, ``_checkpoint.progress`` has length >= 2 with
    ``progress[0] >= progress[1]`` and ``progress[1] > 0``, and ``_checkpoint.records`` is
    non-empty.

    The ``progress[1] > 0`` clause is stricter than the internal ``result_is_final()``, which
    would accept a degenerate ``[0, 0]``. The difference is one-directional: it can only make
    the runner re-run something, never skip something unfinished.
    """
    p = Path(result_path)
    if not p.is_file():
        return _MISSING
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError) as exc:
        return ResultRecord(True, False, False, None, None, 0, f"unreadable: {exc}")

    if not isinstance(data, dict):
        return ResultRecord(True, False, False, None, None, 0, "top level is not an object")

    ck = data.get("_checkpoint")
    if not isinstance(ck, dict):
        return ResultRecord(True, True, False, None, None, 0, "no _checkpoint object")

    progress = ck.get("progress")
    records = ck.get("records")
    n_records = len(records) if isinstance(records, list) else 0

    def _count(v):
        """Accept an int or an integral float; reject bool and everything else."""
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return None

    done = _count(progress[0]) if isinstance(progress, list) and len(progress) >= 2 else None
    total = _count(progress[1]) if isinstance(progress, list) and len(progress) >= 2 else None

    final = (
        done is not None
        and total is not None
        and total > 0
        and done >= total
        and n_records > 0
    )

    status: Optional[str] = None
    score: Optional[float] = None
    if n_records > 0 and isinstance(records[0], dict):
        raw_status = records[0].get("status")
        status = raw_status if isinstance(raw_status, str) else None
        scores = records[0].get("scores")
        if isinstance(scores, dict):
            raw_score = scores.get("score_composed")
            if isinstance(raw_score, (int, float)):
                score = float(raw_score)

    err = None
    if not final:
        err = f"not finalized (progress={progress!r}, records={n_records})"

    return ResultRecord(True, True, final, status, score, n_records, err)


def is_final(result_path: str | Path) -> bool:
    return read(result_path).final


VALID_RESUME_MODES = ("skip_terminal", "skip_any_final", "none")


def should_skip_on_resume(record: ResultRecord, mode: str) -> bool:
    """Record-only half of the resume predicate.

    Returns True when the record alone is enough to decide "do not run this route again".
    Statuses that *might* be retried are deliberately **not** decided here -- they depend on how
    much retry budget the ledger says is left, which is the planner's job (see
    :func:`oodbench.plan.decide`). Keeping the two halves separate is what stops a resumed run
    from either re-running an accepted route or granting a fresh budget to a failed one.

    * ``skip_terminal``  -- skip iff final AND the outcome is a legitimate benchmark result.
      Within-run and across-run retry semantics then agree.
    * ``skip_any_final`` -- skip iff final, whatever the status. Exactly the internal
      ``--skip_if_final``. Preserved so internal sweeps can be reproduced bit-for-bit; see
      DESIGN.md section 5 for the silent-acceptance hazard it carries.
    * ``none``           -- never skip.
    """
    if mode not in VALID_RESUME_MODES:
        raise ValueError(f"unknown resume mode: {mode!r}")
    if mode == "none":
        return False
    if not record.final:
        return False
    if mode == "skip_any_final":
        return True
    # skip_terminal
    return record.disposition is Disposition.ACCEPT
