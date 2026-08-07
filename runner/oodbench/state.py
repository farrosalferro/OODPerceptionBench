"""Persistent attempt ledger.

Retry budgets are a property of the *route*, not of the process: three interrupted restarts
must not buy three times the budget. The ledger is therefore written to disk and reloaded on
resume.

Writes are atomic (temp file + ``os.replace``) after every transition, so a ``SIGKILL`` of the
runner cannot leave a torn ledger behind.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

STATE_VERSION = 1

#: Which §6A accounting model wrote this ledger. **Not the runner version and not the config
#: digest** -- it is the third question, and conflating it with either is what a cross-review
#: caught (2026-08-07, round 3, findings 4/5, raised independently by two labs).
#:
#: The config digest answers *"did the operator change a setting?"*, and it is right that adding
#: `retry.killed_budget` at its default does not move it: a key nobody set is not a setting they
#: changed (see :data:`config.DIGEST_COMPAT_DEFAULTS`). But the accounting model changed
#: *alongside* that key. A ledger written before epoch 2 holds `attempts_record` counts that
#: include kill-shaped ends -- which now charge the separate `killed` axis -- and no
#: `attempts_killed` history at all. Resuming it is legitimate, and the counters are preserved
#: exactly; what is not legitimate is doing so **silently**, because settlement timing and the
#: final status a route lands on can both differ from what the earlier run would have produced.
#:
#: Bump this whenever a rule in DESIGN.md §6A.5 changes which budget a cell charges, or whether
#: it settles. Do NOT bump it for a bug fix that leaves the table intact.
#:
#: * epoch 1 -- pre-§6A: every final retryable record charged `record`, including abnormal ends.
#: * epoch 2 -- §6A as of release v0.9: `killed` and `tickruntime` are separate axes, `infra` is
#:   a consecutive streak, and completeness is `final AND settled`.
ACCOUNTING_EPOCH = 2


@dataclass
class TaskState:
    key: str
    attempts_record: int = 0        # attempts that produced a retryable *record*
    attempts_tickruntime: int = 0   # attempts that produced Failed - TickRuntime
    #: **Consecutive** attempts that produced no record of their own -- the value the
    #: ``infra_budget`` gate is compared against. Cleared by any attempt that produced a final
    #: record the way it ended could not have manufactured, exactly like the worker-quarantine
    #: counter and for the same reason: what the budget bounds is *a machine that is broken
    #: now*, not a lifetime tally of unrelated hiccups. Letting scattered failures accumulate
    #: made a route that had been running fine unsettleable mid-run, which no recovery outside
    #: the run could reach (DESIGN.md 6A.5).
    attempts_infra: int = 0
    #: Every infra failure this route has ever had, never cleared. Audit only -- nothing gates
    #: on it. Without it, clearing the streak would erase the evidence that the machine gave
    #: this route trouble at all.
    attempts_infra_total: int = 0
    #: Attempts that ended abnormally (killed / faulted) while a crash-shaped record was on
    #: disk. Ambiguous by construction: that is exactly what a dying simulator writes, and also
    #: exactly what a route that finished and hung in teardown leaves behind. Its own axis so a
    #: kill cannot spend the model's record retries -- and a *bounded* one, so the route still
    #: settles on that record rather than becoming unsettleable. See DESIGN.md 6A.5.
    attempts_killed: int = 0
    last_status: Optional[str] = None
    last_reason: Optional[str] = None
    last_worker: Optional[int] = None
    last_duration_s: Optional[float] = None
    total_runtime_s: float = 0.0
    #: Settlement, NOT "an attempt happened". Means: the result file for this route holds an
    #: answer this run is no longer obliged to improve on (DESIGN.md 6A.7). Set by
    #: ``plan.decide`` (SKIP_DONE, and SKIP_EXHAUSTED when the record's own budget is spent),
    #: cleared by ``plan.decide`` on RUN, and set by ``Runner._settle`` per the 6A.5 tables.
    finished: bool = False

    def budgets(self) -> Dict[str, int]:
        """The counters a gate compares against a budget -- and only those.

        ``infra`` is the consecutive streak, not :attr:`attempts_infra_total`: the streak is
        what ``retry.infra_budget`` bounds. The total is deliberately absent so no gate can
        grow a dependency on it by accident.
        """
        return {
            "record": self.attempts_record,
            "tickruntime": self.attempts_tickruntime,
            "infra": self.attempts_infra,
            "killed": self.attempts_killed,
        }

    @property
    def total_attempts(self) -> int:
        """How many attempts this route has actually cost. Uses the infra *total*, not the
        streak: the streak is a gate value that gets cleared, and a count of work done must not
        go down."""
        return (self.attempts_record + self.attempts_tickruntime + self.attempts_infra_total
                + self.attempts_killed)


@dataclass
class RunState:
    path: Path
    version: int = STATE_VERSION
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    config_digest: Optional[str] = None
    tasks: Dict[str, TaskState] = field(default_factory=dict)
    #: Digest found in an existing ledger, before we overwrote it with this run's digest.
    config_digest_on_disk: Optional[str] = None
    #: :data:`ACCOUNTING_EPOCH` found in an existing ledger. ``None`` means the ledger predates
    #: the field entirely, which is itself the answer: only epoch 1 ever wrote a ledger without
    #: it, so an absent value is read as 1 rather than as "unknown".
    accounting_epoch_on_disk: Optional[int] = None
    #: Free-text note surfaced in the report (e.g. "the previous ledger was unreadable").
    note: Optional[str] = None

    # -- lifecycle ------------------------------------------------------------------------
    @classmethod
    def load_or_create(cls, path: str | os.PathLike, config_digest: str) -> "RunState":
        p = Path(path)
        if not p.is_file():
            return cls(path=p, config_digest=config_digest)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # A corrupt ledger must not silently reset every budget to zero -- that is how a
            # route with a stuck infrastructure failure gets retried forever. Move it aside so
            # the operator can see it happened, and start clean.
            backup = p.with_suffix(p.suffix + f".corrupt.{int(time.time())}")
            try:
                p.replace(backup)
            except OSError:
                pass
            state = cls(path=p, config_digest=config_digest)
            state.note = f"previous ledger was unreadable and was moved to {backup}"
            return state

        state = cls(
            path=p,
            version=int(data.get("version", STATE_VERSION)),
            created_at=float(data.get("created_at", time.time())),
            config_digest=data.get("config_digest"),
            )
        for key, raw in (data.get("tasks") or {}).items():
            known = {f for f in TaskState.__dataclass_fields__}  # noqa: SLF001
            st = TaskState(**{k: v for k, v in raw.items() if k in known})
            if "attempts_infra_total" not in raw:
                # A ledger written before the streak/total split. Seed the total from the
                # counter that used to be the lifetime tally, so the audit trail survives the
                # upgrade -- and, more importantly, do NOT seed the streak from anything: the
                # gate value is whatever that older ledger recorded, unchanged, so upgrading
                # the runner cannot silently un-gate (or re-gate) a route.
                st.attempts_infra_total = st.attempts_infra
            state.tasks[key] = st
        state.config_digest_on_disk = state.config_digest
        state.config_digest = config_digest
        # Absent means epoch 1: the field was introduced with epoch 2, so there is no ambiguity
        # to preserve. Read before the save that will stamp this run's epoch over it.
        state.accounting_epoch_on_disk = int(data.get("accounting_epoch", 1))
        return state

    def clear_infra_exhaustion(self, infra_budget: int) -> List[str]:
        """Reset the infra gate -- and nothing else -- for every route that has hit it.

        The one lossless escape from an infra-exhausted route (DESIGN.md 6A.5/6A.8). ``infra``
        is the budget that deliberately does not settle: a route that exhausts it is reported
        as having no settled answer, and *stays* that way across resumes, because the budget is
        persisted. That is right for the run that observed the failures and wrong for ever
        after: infra exhaustion is a statement about the machine at a point in time, and the
        machine is the one thing an operator can go and fix. Without this verb the only escapes
        were deleting the ledger (losing every budget) or ``--resume-mode none --force``
        (overwriting the tree) -- both destructive, which is what made the gate a dead end
        rather than a delay.

        Touches exactly one field. Records, settlement bits, and the record / tickruntime /
        killed budgets are untouched, so this cannot manufacture a benchmark result: it buys
        attempts, not answers. :attr:`TaskState.attempts_infra_total` keeps the evidence.

        Returns the keys it cleared, so the caller can report them.
        """
        cleared: List[str] = []
        for key, st in sorted(self.tasks.items()):
            if st.attempts_infra >= infra_budget and st.attempts_infra > 0:
                st.attempts_infra = 0
                cleared.append(key)
        return cleared

    def get(self, key: str) -> TaskState:
        st = self.tasks.get(key)
        if st is None:
            st = TaskState(key=key)
            self.tasks[key] = st
        return st

    def save(self) -> None:
        self.updated_at = time.time()
        payload = {
            "version": self.version,
            "accounting_epoch": ACCOUNTING_EPOCH,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "config_digest": self.config_digest,
            "tasks": {k: asdict(v) for k, v in sorted(self.tasks.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def config_changed(self) -> bool:
        """True when resuming into an output root produced by a *different* configuration.

        Not fatal -- changing the worker count or a timeout between runs is normal and harmless
        -- but it is reported, because changing the agent or the CARLA build mid-sweep would
        produce a result tree that mixes two things.
        """
        return (self.config_digest_on_disk is not None
                and self.config_digest_on_disk != self.config_digest)

    def accounting_model_changed(self) -> bool:
        """True when resuming a ledger written under a **different §6A accounting model**.

        The third question, kept apart from the other two on purpose (see
        :data:`ACCOUNTING_EPOCH`). ``config_changed`` asks whether the operator changed a
        setting; this asks whether the *runner* changed what a setting means. They came to be
        conflated because :data:`config.DIGEST_COMPAT_DEFAULTS` -- correctly -- stops a schema
        addition from looking like a settings change, which had the side effect of making the
        accounting change that arrived with it invisible too.

        Not fatal. The counters are preserved exactly and resuming is a legitimate thing to do;
        but a route can settle after a different number of attempts, and on a different status,
        than the run that started the tree would have produced, so the operator is told.
        """
        return (self.accounting_epoch_on_disk is not None
                and self.accounting_epoch_on_disk != ACCOUNTING_EPOCH)
