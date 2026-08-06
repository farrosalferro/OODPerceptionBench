"""Route discovery, output-path mirroring, manifest integrity, and the resume decision.

The single most important thing here is :func:`RouteTask.from_xml`: **every** output path is
derived from the route XML's path relative to the routes root, by one function used for
planning, resuming and reporting alike. See DESIGN.md section 5 -- the known trap is code that
reassembles a result path out of known parts, drops the ``{scenario}/{level}/`` component, and
then reports a finished sweep as empty.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import results as results_mod
from .results import Disposition, ResultRecord

#: Directory-name patterns that mark internal scaffolding trees which must never be evaluated.
#: These are *warned* about, never silently filtered -- the released route tree is the source of
#: truth, and the shipped smoke split legitimately contains "smoke" in its path.
SCAFFOLDING_HINTS = ("_debug", "_missing", "_smoke", "_fix", "_revision", "family_")


class PlanError(Exception):
    pass


class Decision(Enum):
    RUN = "run"
    SKIP_DONE = "skip_done"          # already has an acceptable final record
    SKIP_EXHAUSTED = "skip_exhausted"  # final record, retryable, but budget already spent
    FATAL = "fatal"                  # pre-existing record says the agent config is broken


@dataclass(frozen=True)
class RouteTask:
    """One (route XML, seed) unit of work, with every derived path attached."""

    xml: Path
    rel_dir: PurePosixPath      # route's directory, relative to routes root
    stem: str
    seed: int
    repetition: int
    out_root: Path

    # -- derived paths -------------------------------------------------------------------
    @property
    def key(self) -> str:
        """Stable identity used as the ledger key. Includes the seed by construction."""
        return f"{self.rel_dir.as_posix()}/{self.stem}_seed{self.seed}"

    @property
    def result_path(self) -> Path:
        return self.out_root / self.rel_dir / "results" / f"{self.stem}_seed{self.seed}.json"

    @property
    def agent_log_dir(self) -> Path:
        return self.out_root / self.rel_dir / "logs" / f"{self.stem}_seed{self.seed}"

    @property
    def job_script(self) -> Path:
        return (self.out_root / "_runner" / "jobs" / self.rel_dir
                / f"{self.stem}_seed{self.seed}.sh")

    @property
    def stdout_path(self) -> Path:
        return (self.out_root / "_runner" / "logs" / self.rel_dir
                / f"{self.stem}_seed{self.seed}.out")

    @property
    def stderr_path(self) -> Path:
        return (self.out_root / "_runner" / "logs" / self.rel_dir
                / f"{self.stem}_seed{self.seed}.err")

    @property
    def carla_record_dir(self) -> Path:
        return self.out_root / self.rel_dir / "carla_records" / f"{self.stem}_seed{self.seed}"

    def mkdirs(self) -> None:
        for d in (self.result_path.parent, self.agent_log_dir, self.job_script.parent,
                  self.stdout_path.parent):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_xml(cls, xml: Path, routes_root: Path, out_root: Path, seed: int,
                 repetition: int) -> "RouteTask":
        rel = xml.resolve().relative_to(routes_root.resolve())
        return cls(
            xml=xml.resolve(),
            rel_dir=PurePosixPath(rel.parent.as_posix()) if str(rel.parent) != "." else PurePosixPath(""),
            stem=xml.stem,
            seed=seed,
            repetition=repetition,
            out_root=out_root.resolve(),
        )


# ---------------------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------------------
def discover(routes_root: Path) -> List[Path]:
    """All ``*.xml`` under ``routes_root``, ordered by relative POSIX path.

    The order is deterministic so the *plan* is byte-identical across runs. Execution order
    across workers is not deterministic and cannot be; nothing seed-bearing depends on it.
    """
    root = Path(routes_root).resolve()
    if not root.is_dir():
        raise PlanError(f"routes.root is not a directory: {root}")
    xmls = [p for p in root.rglob("*.xml") if p.is_file()]
    xmls.sort(key=lambda p: p.resolve().relative_to(root).as_posix())
    if not xmls:
        raise PlanError(f"no *.xml route files found under {root}")
    return xmls


def scaffolding_warnings(xmls: Sequence[Path], routes_root: Path) -> List[str]:
    root = Path(routes_root).resolve()
    hits: Dict[str, int] = {}
    for x in xmls:
        rel = x.resolve().relative_to(root)
        for part in rel.parts:
            for hint in SCAFFOLDING_HINTS:
                if hint in part:
                    hits[part] = hits.get(part, 0) + 1
    return [
        f"route tree contains {n} route(s) under a directory named '{name}', which matches an "
        f"internal scaffolding pattern. The canonical set is 475 routes "
        f"(70 static / 162 pedestrian / 243 vehicle). Verify this is intended."
        for name, n in sorted(hits.items())
    ]


def breakdown(xmls: Sequence[Path], routes_root: Path, depth: int = 2) -> List[Tuple[str, int]]:
    """Route counts grouped by the first ``depth`` path components, for the preflight banner."""
    root = Path(routes_root).resolve()
    counts: Dict[str, int] = {}
    for x in xmls:
        rel = x.resolve().relative_to(root)
        key = "/".join(rel.parts[:depth]) if len(rel.parts) > 1 else "(root)"
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items())


# ---------------------------------------------------------------------------------------
# Manifest integrity (consumes the frozen routes/MANIFEST.tsv)
# ---------------------------------------------------------------------------------------
@dataclass
class ManifestCheck:
    total_expected: int
    total_found: int
    missing: List[str] = field(default_factory=list)
    extra: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.modified)

    def summary(self) -> str:
        if self.ok:
            return (f"manifest OK: {self.total_found}/{self.total_expected} routes match by path "
                    f"and sha256")
        bits = [f"manifest MISMATCH ({self.total_found} found vs {self.total_expected} expected)"]
        for label, items in (("missing", self.missing), ("extra", self.extra),
                             ("modified", self.modified)):
            if items:
                shown = ", ".join(items[:5]) + (" ..." if len(items) > 5 else "")
                bits.append(f"  {len(items)} {label}: {shown}")
        return "\n".join(bits)


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def check_manifest(xmls: Sequence[Path], routes_root: Path, manifest_path: Path) -> ManifestCheck:
    """Compare the discovered route set against a frozen TSV manifest.

    The manifest is expected to have a header row and at least ``path`` and ``sha256`` columns
    (any order, extra columns ignored). Paths are relative to the routes root.

    This is a far stronger integrity check than any directory-name heuristic: it catches an
    *edited* route XML, which no name pattern ever would, and an edited route silently changes
    the benchmark definition.
    """
    root = Path(routes_root).resolve()
    lines = Path(manifest_path).read_text(encoding="utf-8").splitlines()
    rows = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    if not rows:
        raise PlanError(f"manifest {manifest_path} is empty")

    header = [c.strip().lower() for c in rows[0].split("\t")]
    if "path" not in header:
        raise PlanError(f"manifest {manifest_path}: header has no 'path' column (got {header})")
    ipath = header.index("path")
    isha = header.index("sha256") if "sha256" in header else None

    expected: Dict[str, Optional[str]] = {}
    for row in rows[1:]:
        cols = row.split("\t")
        if len(cols) <= ipath:
            continue
        rel = cols[ipath].strip().lstrip("./")
        sha = cols[isha].strip() if (isha is not None and len(cols) > isha) else None
        expected[rel] = sha or None

    found: Dict[str, Path] = {
        x.resolve().relative_to(root).as_posix(): x for x in xmls
    }

    missing = sorted(set(expected) - set(found))
    extra = sorted(set(found) - set(expected))
    modified: List[str] = []
    for rel in sorted(set(expected) & set(found)):
        want = expected[rel]
        if want and _sha256(found[rel]) != want:
            modified.append(rel)

    return ManifestCheck(
        total_expected=len(expected),
        total_found=len(found),
        missing=missing,
        extra=extra,
        modified=modified,
    )


# ---------------------------------------------------------------------------------------
# Plan construction + resume decision
# ---------------------------------------------------------------------------------------
def build_tasks(xmls: Sequence[Path], routes_root: Path, out_root: Path, base_seed: int,
                repetitions: int) -> List[RouteTask]:
    """One task per (route, repetition).

    ``seed = base_seed + repetition_index`` -- a function of the route and the repetition only.
    Nothing in the seed path reads the worker index, the GPU, the port or the attempt number.
    """
    tasks: List[RouteTask] = []
    for rep in range(repetitions):
        for xml in xmls:
            tasks.append(RouteTask.from_xml(xml, Path(routes_root), Path(out_root),
                                            seed=base_seed + rep, repetition=rep))
    return tasks


@dataclass(frozen=True)
class TaskDecision:
    decision: Decision
    record: ResultRecord
    reason: str


def decide(task: RouteTask, ledger_attempts: Dict[str, int], resume_mode: str,
           record_budget: int, tickruntime_budget: int, infra_budget: int) -> TaskDecision:
    """Should this task run in this invocation?

    ``ledger_attempts`` carries the *persisted* attempt counters for this task key
    (``{"record": n, "tickruntime": n, "infra": n}``). Budgets are a property of the route, not
    of the process: three interrupted restarts must not buy three times the budget.
    """
    record = results_mod.read(task.result_path)

    if record.final and record.disposition is Disposition.FATAL:
        return TaskDecision(Decision.FATAL, record,
                            f"existing record has status {record.status!r}, which means the "
                            f"agent's sensor configuration is rejected -- it will fail "
                            f"identically on every route")

    if results_mod.should_skip_on_resume(record, resume_mode):
        return TaskDecision(Decision.SKIP_DONE, record,
                            f"final record with status {record.status!r}")

    if resume_mode == "none":
        return TaskDecision(Decision.RUN, record, "resume.mode=none: re-running")

    if record.final:
        disp = record.disposition
        if disp is Disposition.RETRY_RECORD or disp is Disposition.UNKNOWN:
            spent, budget, label = ledger_attempts.get("record", 0), record_budget, "record"
        elif disp is Disposition.RETRY_TICKRUNTIME:
            spent, budget, label = (ledger_attempts.get("tickruntime", 0),
                                    tickruntime_budget, "tickruntime")
        else:  # ACCEPT was handled by should_skip_on_resume for skip_terminal
            return TaskDecision(Decision.SKIP_DONE, record,
                                f"final record with status {record.status!r}")
        if spent >= budget:
            return TaskDecision(
                Decision.SKIP_EXHAUSTED, record,
                f"final record with status {record.status!r}; {label} retry budget already "
                f"spent ({spent}/{budget}) in an earlier run")
        return TaskDecision(Decision.RUN, record,
                            f"retryable status {record.status!r}, {label} budget "
                            f"{spent}/{budget}")

    # No final record. Only the infra budget can be exhausted here.
    if ledger_attempts.get("infra", 0) >= infra_budget:
        return TaskDecision(
            Decision.SKIP_EXHAUSTED, record,
            f"no final record and the infrastructure retry budget is spent "
            f"({ledger_attempts.get('infra', 0)}/{infra_budget}) -- this route has NOT produced "
            f"a benchmark result")
    return TaskDecision(Decision.RUN, record, record.error or "no result yet")


def assert_seed_consistency(tasks: Iterable[RouteTask], expected_seed_base: int,
                            repetitions: int) -> None:
    """Guard against a resumed run silently mixing seeds.

    The seed is in the result *filename*, so a mismatch is structurally visible; this makes it
    an error rather than something a reader has to notice.
    """
    allowed = {expected_seed_base + r for r in range(repetitions)}
    for t in tasks:
        if t.seed not in allowed:
            raise PlanError(
                f"seed consistency violation: task {t.key} carries seed {t.seed}, "
                f"expected one of {sorted(allowed)}"
            )
        suffix = f"_seed{t.seed}.json"
        if not t.result_path.name.endswith(suffix):
            raise PlanError(
                f"seed consistency violation: {t.result_path} does not end with {suffix}"
            )
