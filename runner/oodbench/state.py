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
from typing import Dict, Optional

STATE_VERSION = 1


@dataclass
class TaskState:
    key: str
    attempts_record: int = 0        # attempts that produced a retryable *record*
    attempts_tickruntime: int = 0   # attempts that produced Failed - TickRuntime
    attempts_infra: int = 0         # attempts that produced no record at all
    last_status: Optional[str] = None
    last_reason: Optional[str] = None
    last_worker: Optional[int] = None
    last_duration_s: Optional[float] = None
    total_runtime_s: float = 0.0
    finished: bool = False

    def budgets(self) -> Dict[str, int]:
        return {
            "record": self.attempts_record,
            "tickruntime": self.attempts_tickruntime,
            "infra": self.attempts_infra,
        }

    @property
    def total_attempts(self) -> int:
        return self.attempts_record + self.attempts_tickruntime + self.attempts_infra


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
            state.tasks[key] = TaskState(**{k: v for k, v in raw.items() if k in known})
        state.config_digest_on_disk = state.config_digest
        state.config_digest = config_digest
        return state

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
