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
from . import plan as plan_mod
from . import results as results_mod
from .plan import RouteTask
from .state import RunState

EXIT_MEANING = {
    EXIT_OK: "every planned route has a settled result",
    EXIT_PARTIAL: "partial sweep: at least one planned route has no settled result",
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
    #: The ledger half of completeness (DESIGN.md 6A.7/6A.8). A record can be on disk without
    #: this run having produced or settled it -- a failed launch preserves an earlier attempt's
    #: record, and preserving it must not be mistaken for answering the route.
    settled: bool = False
    attempts_record: int = 0
    attempts_tickruntime: int = 0
    #: The *consecutive* infra failures the gate compares against ``retry.infra_budget``.
    attempts_infra: int = 0
    #: Every infra failure this route ever had. Differs from :attr:`attempts_infra` once an
    #: attempt that produced its own record cleared the streak, or ``--retry-infra-exhausted``
    #: did; the report keeps both so a settled route still shows what the machine cost.
    attempts_infra_total: int = 0
    attempts_killed: int = 0
    last_reason: Optional[str] = None
    last_worker: Optional[int] = None
    duration_s: Optional[float] = None
    #: Machine-readable: *why* this route has no settled result. Set by :func:`build`; one of
    #: ``no_record`` (nothing final was ever written), ``unrefreshed_record`` (a record is on
    #: disk but this run never settled it -- typically preserved by a failed launch, or the
    #: infra budget ran out before the planned retry could run), ``not_reached`` (the planning
    #: loop never got to this route). The same headline number, three different problems.
    unsettled_reason: Optional[str] = None

    @property
    def complete(self) -> bool:
        """Both halves, and they are independent.

        The *disk* half is kept because the report must reflect what is actually there: a
        result file deleted after the fact makes the route incomplete whatever the ledger says.
        The *ledger* half is what stops a record that is merely present from counting as an
        answer.
        """
        return self.final and self.settled


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
        """The benchmark result: one row per status, over the routes that have a settled answer.

        Deliberately **not** over every outcome. A route the totals call incomplete must not
        also appear here as though its record were a result -- an operator reads the two numbers
        side by side and a downstream aggregator sums this one. ``status_counts`` therefore sums
        to ``complete`` and :attr:`unsettled_counts` sums to ``incomplete``.
        """
        c = Counter(o.status or "(no record)" for o in self.outcomes if o.complete)
        return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

    @property
    def unsettled_counts(self) -> Dict[str, int]:
        """The same breakdown for routes with no settled result, kept strictly apart."""
        c = Counter(
            (o.status or "(no record)") if o.final else "(no record)"
            for o in self.outcomes if not o.complete
        )
        return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

    def exit_code(self) -> int:
        """The one and only place an exit code is decided.

        There is no path where a route without a settled result yields 0. The ordering is
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
                # by_status sums to `complete`; by_status_unsettled sums to `incomplete`. The
                # split is the point: one route must never be counted as both a result and a
                # gap.
                "by_status": self.status_counts,
                "by_status_unsettled": self.unsettled_counts,
            },
            "quarantined_workers": self.quarantined_workers,
            "warnings": self.warnings,
            "incomplete_routes": [
                {
                    "key": o.key,
                    "route": o.route,
                    "seed": o.seed,
                    "last_status": o.status,
                    # A record can be on disk here: `final` plus `unsettled_reason` is what
                    # separates "nothing was ever written" from "a stale record was preserved
                    # and the retries never ran".
                    "final": o.final,
                    "unsettled_reason": o.unsettled_reason,
                    "attempts": {
                        "record": o.attempts_record,
                        "tickruntime": o.attempts_tickruntime,
                        "infra": o.attempts_infra,
                        "infra_total": o.attempts_infra_total,
                        "killed": o.attempts_killed,
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
                    "settled": o.settled,
                    "complete": o.complete,
                    "unsettled_reason": o.unsettled_reason,
                    "attempts": {
                        "record": o.attempts_record,
                        "tickruntime": o.attempts_tickruntime,
                        "infra": o.attempts_infra,
                        "infra_total": o.attempts_infra_total,
                        "killed": o.attempts_killed,
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
            "## Status breakdown — routes with a settled result",
            "",
            "| status | n |",
            "|---|---:|",
        ]
        for status, n in self.status_counts.items():
            lines.append(f"| `{status}` | {n} |")

        if self.incomplete:
            lines += [
                "",
                "## Routes with NO settled result",
                "",
                "Not all of these are empty: `unsettled = unrefreshed_record` means a record "
                "**is** on disk, from an earlier attempt, and the retries this run planned never "
                "ran. It was preserved, not refreshed, and is not counted as a result.",
                "",
                "| route | seed | last status | on disk | unsettled | record | tick | infra | "
                "killed | reason |",
                "|---|---:|---|---|---|---:|---:|---:|---:|---|",
            ]
            for o in self.incomplete:
                lines.append(
                    f"| `{o.key}` | {o.seed} | {o.status or '—'} | "
                    f"{'final record' if o.final else 'nothing'} | "
                    f"{o.unsettled_reason or '—'} | {o.attempts_record} | "
                    f"{o.attempts_tickruntime} | {o.attempts_infra} | {o.attempts_killed} | "
                    f"{o.last_reason or '—'} |"
                )
            if any(o.attempts_infra for o in self.incomplete):
                # `infra` is the budget that never settles, so an operator reading this section
                # needs the way out in the same place, not three documents away.
                lines += ["", f"> {plan_mod.INFRA_RECOVERY_HINT}"]

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
            "means some route has no *settled* result — either nothing was written, or what is "
            "there was preserved from an earlier attempt and never refreshed — so its answer is "
            "unknown.",
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
        # Completeness has two halves and this is the only place they meet: what is on disk
        # (re-read, never trusted from memory) and whether the ledger settled it. A record can
        # be present without this run having produced or settled it.
        settled = bool(st.finished) if st else False
        if not rec.final or not settled:
            # Order matters and was wrong: `rec.final` was tested first, which made
            # `not_reached` unreachable for any route with a record on disk -- and a route the
            # planning loop never reached is *precisely* a route whose record is left over from
            # an earlier run. Two of the three cases the field exists to separate collapsed into
            # one. The ledger question ("was this route ever looked at?") is strictly prior to
            # the disk question ("is there a record?"), so it is asked first.
            if st is None:
                reason = "not_reached"
            elif rec.final:
                reason = "unrefreshed_record"
            else:
                reason = "no_record"
        else:
            reason = None
        rep.outcomes.append(RouteOutcome(
            key=task.key,
            route=str(task.xml),
            seed=task.seed,
            result_path=str(task.result_path),
            final=rec.final,
            status=rec.status if rec.final else None,
            score_composed=rec.score_composed if rec.final else None,
            settled=settled,
            attempts_record=st.attempts_record if st else 0,
            attempts_tickruntime=st.attempts_tickruntime if st else 0,
            attempts_infra=st.attempts_infra if st else 0,
            attempts_infra_total=st.attempts_infra_total if st else 0,
            attempts_killed=st.attempts_killed if st else 0,
            last_reason=(st.last_reason if st else None) or rec.error,
            last_worker=st.last_worker if st else None,
            duration_s=st.last_duration_s if st else None,
            unsettled_reason=reason,
        ))

    # Report-time seed check over the TREE, not over the planned paths (DESIGN.md 6A.10). The
    # planned paths carry the seed by construction and plan.assert_seed_consistency already
    # checks them; what that cannot see is a result file from a *different* seed sitting in the
    # same output root, which any aggregator globbing `results/*.json` would average across.
    if tasks:
        allowed = {t.seed for t in tasks}
        strays = plan_mod.foreign_seed_files(tasks[0].out_root, allowed)
        if strays:
            shown = ", ".join(p.name for p in strays[:5]) + (" ..." if len(strays) > 5 else "")
            rep.warnings.append(
                f"{len(strays)} result file(s) in this output root carry a seed outside the "
                f"configured set {sorted(allowed)}: {shown}. They were NOT counted -- only the "
                f"planned paths are ever read -- but a tree holding two seeds is a tree an "
                f"aggregator will average across. Use a separate output root per seed."
            )
    return rep
