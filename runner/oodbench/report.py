"""Final report and the exit-code contract.

The load-bearing distinction (DESIGN.md section 6):

    A model failing routes is not a runner failure.

A model that scores ``Failed - TickRuntime`` on all 475 routes has produced a valid -- if
unflattering -- benchmark result, and that run exits 0. Exit 1 means *we do not know* the
answer for some route. Conflating the two would either make honest results look like
infrastructure failures or, far worse, let an unfinished sweep pass as a result.
"""

from __future__ import annotations

import json
import platform
import socket
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import (ARXIV_VERSION, BENCHMARK_RELEASE, EXIT_AGENT_FATAL, EXIT_INTERRUPTED,
               EXIT_NO_WORKERS, EXIT_OK, EXIT_PARTIAL, __version__)
from . import results as results_mod
from .plan import RouteTask
from .state import RunState

EXIT_MEANING = {
    EXIT_OK: "every planned route has a final record on disk",
    EXIT_PARTIAL: "partial sweep: at least one planned route has no final record",
    2: "configuration or preflight error",
    EXIT_INTERRUPTED: "interrupted by signal; children reaped and state written",
    EXIT_NO_WORKERS: "all workers quarantined / no usable GPU",
    EXIT_AGENT_FATAL: "fatal agent misconfiguration (sensor configuration rejected)",
}


@dataclass
class RouteOutcome:
    key: str
    route: str
    seed: int
    result_path: str
    final: bool
    status: Optional[str]
    score_composed: Optional[float]
    attempts_record: int = 0
    attempts_tickruntime: int = 0
    attempts_infra: int = 0
    last_reason: Optional[str] = None
    last_worker: Optional[int] = None
    duration_s: Optional[float] = None

    @property
    def complete(self) -> bool:
        return self.final


@dataclass
class Report:
    started_at: float
    finished_at: float = 0.0
    config_digest: str = ""
    config_path: Optional[str] = None
    outcomes: List[RouteOutcome] = field(default_factory=list)
    planned: int = 0
    skipped_done: int = 0
    skipped_exhausted: int = 0
    quarantined_workers: List[int] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    interrupted: bool = False
    fatal_agent: bool = False
    all_workers_quarantined: bool = False
    protocol_seed_deviation: bool = False
    seed: int = 42
    backend: str = "local"

    # ---------------------------------------------------------------------------------
    @property
    def incomplete(self) -> List[RouteOutcome]:
        return [o for o in self.outcomes if not o.complete]

    @property
    def status_counts(self) -> Dict[str, int]:
        c = Counter(o.status or "(no record)" for o in self.outcomes)
        return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

    def exit_code(self) -> int:
        """The one and only place an exit code is decided.

        There is no path where a route without a final record yields 0. The ordering is
        deliberate: the most specific diagnosis wins, but every branch below EXIT_OK is
        non-zero, so a partial sweep cannot slip through whichever branch it takes.
        """
        if self.fatal_agent:
            return EXIT_AGENT_FATAL
        if self.all_workers_quarantined:
            return EXIT_NO_WORKERS
        if self.interrupted:
            return EXIT_INTERRUPTED
        if self.incomplete:
            return EXIT_PARTIAL
        return EXIT_OK

    # ---------------------------------------------------------------------------------
    def to_dict(self) -> Dict:
        code = self.exit_code()
        return {
            "runner": {
                "name": "OOD-PerceptionBench runner",
                "version": __version__,
                "first_cut": True,
            },
            # Standing rule: every artifact carries a version stamp and the arXiv version it
            # corresponds to. This is the guard against two incomparable score sets entering
            # the literature under the same benchmark name.
            "benchmark": {
                "release": BENCHMARK_RELEASE,
                "arxiv_version": ARXIV_VERSION,
                "seed": self.seed,
                "protocol_seed_deviation": self.protocol_seed_deviation,
            },
            "run": {
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "wall_seconds": round(max(0.0, self.finished_at - self.started_at), 1),
                "host": socket.gethostname(),
                "platform": platform.platform(),
                "backend": self.backend,
                "config_path": self.config_path,
                "config_digest": self.config_digest,
                "interrupted": self.interrupted,
            },
            "totals": {
                "planned": self.planned,
                "complete": self.planned - len(self.incomplete),
                "incomplete": len(self.incomplete),
                "skipped_already_done": self.skipped_done,
                "skipped_budget_exhausted": self.skipped_exhausted,
                "by_status": self.status_counts,
            },
            "quarantined_workers": self.quarantined_workers,
            "warnings": self.warnings,
            "incomplete_routes": [
                {
                    "key": o.key,
                    "route": o.route,
                    "seed": o.seed,
                    "last_status": o.status,
                    "attempts": {
                        "record": o.attempts_record,
                        "tickruntime": o.attempts_tickruntime,
                        "infra": o.attempts_infra,
                    },
                    "reason": o.last_reason,
                    "result_path": o.result_path,
                }
                for o in self.incomplete
            ],
            "routes": [
                {
                    "key": o.key,
                    "route": o.route,
                    "seed": o.seed,
                    "status": o.status,
                    "score_composed": o.score_composed,
                    "final": o.final,
                    "attempts": {
                        "record": o.attempts_record,
                        "tickruntime": o.attempts_tickruntime,
                        "infra": o.attempts_infra,
                    },
                    "worker": o.last_worker,
                    "duration_s": o.duration_s,
                    "result_path": o.result_path,
                }
                for o in self.outcomes
            ],
            "exit": {"code": code, "meaning": EXIT_MEANING.get(code, "unknown")},
        }

    def to_markdown(self) -> str:
        code = self.exit_code()
        lines = [
            "# OOD-PerceptionBench run report",
            "",
            f"- **Benchmark release:** {BENCHMARK_RELEASE} (binds to arXiv {ARXIV_VERSION})",
            f"- **Runner version:** {__version__} — FIRST CUT",
            f"- **Backend:** {self.backend}",
            f"- **Seed:** {self.seed}"
            + ("  ⚠ **not the published protocol seed 42**" if self.protocol_seed_deviation else ""),
            f"- **Config digest:** `{self.config_digest[:16]}`",
            f"- **Wall time:** {round(max(0.0, self.finished_at - self.started_at) / 60, 1)} min",
            "",
            "## Totals",
            "",
            f"| planned | complete | incomplete | skipped (done) | skipped (budget spent) |",
            f"|---:|---:|---:|---:|---:|",
            f"| {self.planned} | {self.planned - len(self.incomplete)} | "
            f"{len(self.incomplete)} | {self.skipped_done} | {self.skipped_exhausted} |",
            "",
            "## Status breakdown",
            "",
            "| status | n |",
            "|---|---:|",
        ]
        for status, n in self.status_counts.items():
            lines.append(f"| `{status}` | {n} |")

        if self.incomplete:
            lines += [
                "",
                "## Incomplete routes — NO benchmark result was produced for these",
                "",
                "| route | seed | last status | record | tick | infra | reason |",
                "|---|---:|---|---:|---:|---:|---|",
            ]
            for o in self.incomplete:
                lines.append(
                    f"| `{o.key}` | {o.seed} | {o.status or '—'} | {o.attempts_record} | "
                    f"{o.attempts_tickruntime} | {o.attempts_infra} | {o.last_reason or '—'} |"
                )

        if self.quarantined_workers:
            lines += ["", f"## Quarantined workers: {self.quarantined_workers}",
                      "",
                      "Repeated infrastructure failures on the same worker are the signature of "
                      "one wedged GPU. Re-probe that device before reusing it."]

        if self.warnings:
            lines += ["", "## Warnings", ""] + [f"- {w}" for w in self.warnings]

        lines += [
            "",
            "## Exit",
            "",
            f"`{code}` — {EXIT_MEANING.get(code, 'unknown')}",
            "",
            "A model failing routes is **not** a runner failure: a route whose final record says "
            "`Failed - TickRuntime` is a valid benchmark result and counts as complete. Exit 1 "
            "means some route has no final record at all, so its answer is unknown.",
        ]
        return "\n".join(lines) + "\n"

    def write(self, out_root: Path) -> Dict[str, Path]:
        d = out_root / "_runner"
        d.mkdir(parents=True, exist_ok=True)
        json_path = d / "report.json"
        md_path = d / "report.md"
        json_path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        md_path.write_text(self.to_markdown(), encoding="utf-8")
        return {"json": json_path, "markdown": md_path}


def build(tasks: Sequence[RouteTask], state: RunState, *, started_at: float,
          config_digest: str, config_path: Optional[str], seed: int, backend: str,
          skipped_done: int, skipped_exhausted: int, quarantined: Sequence[int],
          warnings: Sequence[str], interrupted: bool, fatal_agent: bool,
          all_workers_quarantined: bool = False) -> Report:
    """Re-read every planned route's result from disk and assemble the report.

    Deliberately re-reads rather than trusting in-memory bookkeeping: the report is the thing
    an operator will believe, so it must reflect what is actually on disk.
    """
    rep = Report(
        started_at=started_at,
        finished_at=time.time(),
        config_digest=config_digest,
        config_path=config_path,
        planned=len(tasks),
        skipped_done=skipped_done,
        skipped_exhausted=skipped_exhausted,
        quarantined_workers=list(quarantined),
        warnings=list(warnings),
        interrupted=interrupted,
        fatal_agent=fatal_agent,
        all_workers_quarantined=all_workers_quarantined,
        protocol_seed_deviation=(seed != 42),
        seed=seed,
        backend=backend,
    )
    for task in tasks:
        rec = results_mod.read(task.result_path)
        st = state.tasks.get(task.key)
        rep.outcomes.append(RouteOutcome(
            key=task.key,
            route=str(task.xml),
            seed=task.seed,
            result_path=str(task.result_path),
            final=rec.final,
            status=rec.status if rec.final else None,
            score_composed=rec.score_composed if rec.final else None,
            attempts_record=st.attempts_record if st else 0,
            attempts_tickruntime=st.attempts_tickruntime if st else 0,
            attempts_infra=st.attempts_infra if st else 0,
            last_reason=(st.last_reason if st else None) or rec.error,
            last_worker=st.last_worker if st else None,
            duration_s=st.last_duration_s if st else None,
        ))
    return rep
