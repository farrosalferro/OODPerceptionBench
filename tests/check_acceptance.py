#!/usr/bin/env python3
"""OOD-PerceptionBench acceptance harness.

Bundle version : v0.9
Binds to       : arXiv v1

WHAT THIS DEFENDS AGAINST
-------------------------
A missing OOD asset does not crash anything. ``try_spawn_actor('static.prop.roadclosedsign')``
on a CARLA build without the content pack returns ``None``: the prop is simply absent, the ego
drives an empty road, the route reports ``Completed`` with a plausible Driving Score, and
nothing in the logs is red. For vehicles it is worse -- a blueprint that *is* registered can
still resolve to a different actor through ``attribute_filter``, so the route runs with a Tesla
standing in for the OOD vehicle and still completes.

Both failures produce numbers that look fine and mean nothing. This harness exists to turn them
into a red exit code.

ASSERTIONS, IN PRIORITY ORDER
-----------------------------
  A1  blueprint_spawned   The actor the scenario actually spawned has the ``type_id`` the route
                          XML asked for. Observed from the live actor via the TTR/DAR criterion
                          (``record["ttr_dar"]["agent_type"]`` is ``self._agent.type_id``).
                          THIS IS THE ONE THAT MATTERS -- A3 and A4 both pass on a broken
                          install; A1 does not.
  A2  criteria_attached   The ``ttr_dar`` block is present in the record, proving the criterion
                          patch landed and its events survived the statistics manager.
  A3  route_completed     Status is a clean completion (or matches the golden's status).
  A4  ds_within_tolerance Driving Score is within the golden's measured tolerance.

A1 is *never* satisfied by absence of evidence. If the record carries no ``ttr_dar`` block, or
the criterion recorded ``"unknown"``, A1 FAILS -- "we could not check" is reported as failure,
because on a broken install that is exactly what it looks like.

EXIT STATUS
-----------
    0  every assertion passed AND a golden bundle covered every route in the split
    1  at least one assertion FAILED
    2  usage, IO, or bundle-integrity error (nothing was assessed)
    3  INCONCLUSIVE -- A1..A3 passed but no golden bundle was available, so A4 did not run.
       Never 0. A run without goldens is not a pass.

Standard library only. No CARLA, no GPU, no network.

Usage
-----
    python3 check_acceptance.py --results-root /path/to/smoke_run_output
    python3 check_acceptance.py --results-root <out> --goldens goldens/pdmlite_v0.9.golden.json
    python3 check_acceptance.py --results-root <out> --tier core --json report.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "smoke"))

from materialize import (  # noqa: E402
    BINDS_TO,
    BUNDLE_VERSION,
    DEFAULT_ROUTES_ROOT,
    DEFAULT_SPLIT,
    SplitError,
    blueprint_in_xml,
    load_split,
    sha256_of,
    verify as verify_split,
)

DEFAULT_GOLDEN_DIR = os.path.join(HERE, "goldens")
GOLDEN_SCHEMA_ID = "ood-perceptionbench/golden/1"

#: Statuses that count as "the route reached the end under its own power".
CLEAN_STATUSES = frozenset({"Completed", "Perfect"})

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


# =======================================================================================
# Result reading
# =======================================================================================
@dataclass
class Checkpoint:
    """What we could learn from one leaderboard checkpoint JSON."""

    path: Optional[str] = None
    exists: bool = False
    parsable: bool = False
    final: bool = False
    record: Optional[dict] = None
    error: Optional[str] = None


def _is_final(doc: dict) -> bool:
    """Finalisation predicate, matching the runner's ``results.read``.

    Final iff ``_checkpoint.progress`` has length >= 2 with ``progress[0] >= progress[1]``
    and ``progress[1] > 0``, and ``_checkpoint.records`` is non-empty.
    """
    cp = doc.get("_checkpoint")
    if not isinstance(cp, dict):
        return False
    prog = cp.get("progress")
    recs = cp.get("records")
    if not isinstance(prog, list) or len(prog) < 2 or not isinstance(recs, list) or not recs:
        return False
    try:
        return prog[1] > 0 and prog[0] >= prog[1]
    except TypeError:
        return False


def read_checkpoint(path: str, stem: str) -> Checkpoint:
    cp = Checkpoint(path=path)
    if not os.path.isfile(path):
        cp.error = "result file does not exist"
        return cp
    cp.exists = True
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        cp.error = f"result file does not parse as JSON: {exc}"
        return cp
    cp.parsable = True
    cp.final = _is_final(doc)
    if not cp.final:
        cp.error = ("checkpoint is not finalised (progress/records incomplete) -- the route "
                    "did not finish, so nothing about it can be asserted")
        return cp

    records = doc["_checkpoint"]["records"]
    matches = [r for r in records if isinstance(r, dict) and stem in str(r.get("route_id", ""))]
    if len(matches) == 1:
        cp.record = matches[0]
    elif not matches:
        ids = [str(r.get("route_id")) for r in records if isinstance(r, dict)][:5]
        cp.error = (f"checkpoint holds {len(records)} record(s), none whose route_id names "
                    f"{stem!r} (saw {ids}). This result belongs to a different route.")
    else:
        cp.error = (f"checkpoint holds {len(matches)} records naming {stem!r}; cannot decide "
                    f"which one is the route's result")
    return cp


def locate_result(results_root: str, rel_path: str, stem: str, seed: int) -> tuple[Optional[str], str]:
    """Find the checkpoint JSON for one route. Returns (path or None, explanation)."""
    rel_dir = os.path.dirname(rel_path)
    fname = f"{stem}_seed{seed}.json"
    primary = os.path.join(results_root, rel_dir, "results", fname)
    if os.path.isfile(primary):
        return primary, "canonical mirrored path"

    hits = sorted(set(
        glob.glob(os.path.join(results_root, "**", "results", fname), recursive=True)
        + glob.glob(os.path.join(results_root, "**", fname), recursive=True)
    ))
    if len(hits) == 1:
        return hits[0], "found by search (non-canonical layout)"
    if not hits:
        return None, (f"no result found. Looked for {os.path.relpath(primary, results_root)} "
                      f"and for **/{fname} under {results_root}")
    return None, (f"ambiguous: {len(hits)} files named {fname} under {results_root} "
                  f"({[os.path.relpath(h, results_root) for h in hits[:4]]}). "
                  f"Point --results-root at a single run's output directory.")


def driving_score(record: dict, doc_values: Optional[list] = None) -> Optional[float]:
    scores = record.get("scores")
    if isinstance(scores, dict) and scores.get("score_composed") is not None:
        try:
            return float(scores["score_composed"])
        except (TypeError, ValueError):
            return None
    return None


# =======================================================================================
# Golden bundle
# =======================================================================================
@dataclass
class Golden:
    path: str
    doc: dict
    routes: dict
    tolerance_ds: float


def find_golden_bundles(golden_dir: str) -> list[str]:
    if not os.path.isdir(golden_dir):
        return []
    return sorted(
        p for p in glob.glob(os.path.join(golden_dir, "*.golden.json"))
        if not os.path.basename(p).startswith("EXAMPLE")
    )


def load_golden(path: str, split_sha: str, split_paths: set) -> Golden:
    """Load and integrity-check a golden bundle. Raises SplitError on any inconsistency."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SplitError(f"golden bundle {path} does not parse: {exc}")

    if doc.get("schema") != GOLDEN_SCHEMA_ID:
        raise SplitError(f"golden bundle {path}: schema is {doc.get('schema')!r}, "
                         f"expected {GOLDEN_SCHEMA_ID!r}")
    if doc.get("bundle_version") != BUNDLE_VERSION:
        raise SplitError(
            f"golden bundle {path} was generated for bundle {doc.get('bundle_version')!r}, "
            f"but this tree is {BUNDLE_VERSION!r}. A golden is only valid for the "
            f"content-pack version it was generated against -- regenerate it."
        )
    split_block = doc.get("split") or {}
    if split_block.get("sha256") != split_sha:
        raise SplitError(
            f"golden bundle {path} was generated for a DIFFERENT smoke split "
            f"(bundle says {str(split_block.get('sha256'))[:12]}..., this split is "
            f"{split_sha[:12]}...). Regenerate the goldens, or check out the split the "
            f"bundle was made for. Comparing across splits would be meaningless."
        )
    routes = doc.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise SplitError(f"golden bundle {path}: 'routes' is empty or not an object")

    tol = ((doc.get("tolerance") or {}).get("driving_score_abs"))
    if tol is None:
        raise SplitError(f"golden bundle {path}: tolerance.driving_score_abs is required. "
                         f"A golden without a measured tolerance is a wish, not a golden.")
    try:
        tol = float(tol)
    except (TypeError, ValueError):
        raise SplitError(f"golden bundle {path}: tolerance.driving_score_abs is not a number")

    uncovered = sorted(split_paths - set(routes))
    if uncovered:
        raise SplitError(
            f"golden bundle {path} claims this split but has no entry for "
            f"{len(uncovered)} of its routes, e.g. {uncovered[:3]}. A partial bundle would "
            f"silently downgrade those routes to unchecked."
        )
    return Golden(path=path, doc=doc, routes=routes, tolerance_ds=tol)


# =======================================================================================
# Assertions
# =======================================================================================
@dataclass
class AssertionResult:
    name: str
    verdict: str
    detail: str = ""


@dataclass
class RouteResult:
    path: str
    stem: str
    category: str
    level: str
    expected_blueprint: str
    asset_class: str
    tier: str
    result_path: Optional[str] = None
    status: Optional[str] = None
    observed_blueprint: Optional[str] = None
    ds: Optional[float] = None
    golden_ds: Optional[float] = None
    assertions: list = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(a.verdict == FAIL for a in self.assertions)

    @property
    def skipped(self) -> bool:
        return any(a.verdict == SKIP for a in self.assertions)


def _tesla_hint(expected: str, observed: str) -> str:
    if observed.startswith("vehicle.") and not expected.startswith("vehicle."):
        return (" A vehicle was spawned where a non-vehicle blueprint was requested: this is "
                "CARLA's create_blueprint falling back after the requested id failed to "
                "resolve. The content pack is not installed (or not registered).")
    if expected.startswith("vehicle.") and observed.startswith("vehicle.") and observed != expected:
        return (" Both are vehicles but not the same one: the requested blueprint did not "
                "resolve and attribute_filter picked a different vehicle. A registered-looking "
                "id is not enough -- check the VehicleFactory entry.")
    if expected.split(".")[0] != observed.split(".")[0]:
        return (" The actor is not even in the requested blueprint family, so the requested id "
                "was not honoured at all.")
    return ""


def assess_route(row: dict, results_root: str, routes_root: str, seed: int,
                 golden: Optional[Golden]) -> RouteResult:
    rel = row["path"]
    stem = os.path.splitext(os.path.basename(rel))[0]
    rr = RouteResult(
        path=rel,
        stem=stem,
        category=row["category"],
        level=row["level"],
        expected_blueprint=row["prop_blueprint_id"],
        asset_class=row.get("asset_class", ""),
        tier=row.get("tier", ""),
    )

    result_path, why = locate_result(results_root, rel, stem, seed)
    rr.result_path = result_path
    if result_path is None:
        for name in ("A1 blueprint_spawned", "A2 criteria_attached", "A3 route_completed"):
            rr.assertions.append(AssertionResult(name, FAIL, why))
        rr.assertions.append(AssertionResult("A4 ds_within_tolerance", FAIL, why))
        return rr

    cp = read_checkpoint(result_path, stem)
    if cp.record is None:
        why = cp.error or "unreadable result"
        for name in ("A1 blueprint_spawned", "A2 criteria_attached", "A3 route_completed",
                     "A4 ds_within_tolerance"):
            rr.assertions.append(AssertionResult(name, FAIL, why))
        return rr

    rec = cp.record
    rr.status = rec.get("status")
    rr.ds = driving_score(rec)
    ttr = rec.get("ttr_dar")

    # ---- A1 -- the one that matters ---------------------------------------------------
    if not isinstance(ttr, dict):
        rr.assertions.append(AssertionResult(
            "A1 blueprint_spawned", FAIL,
            "UNVERIFIABLE: the record carries no 'ttr_dar' block, so the spawned actor's "
            "type_id was never observed. This is reported as FAILURE, not as skipped: on an "
            "install where the OOD asset is missing, the route completes and looks exactly "
            "like this. Fix A2 first (the criterion patch or the statistics manager is not "
            "in place), then re-run."))
    else:
        observed = ttr.get("agent_type")
        rr.observed_blueprint = observed
        if not observed or observed == "unknown":
            rr.assertions.append(AssertionResult(
                "A1 blueprint_spawned", FAIL,
                f"the criterion recorded agent_type={observed!r}: it held no actor when the "
                f"route ended. Expected {rr.expected_blueprint!r}. THIS IS THE SIGNATURE OF A "
                f"SILENT SPAWN FAILURE -- the blueprint is very likely not registered in this "
                f"CARLA build. Run probe_blueprints.py."))
        elif observed != rr.expected_blueprint:
            rr.assertions.append(AssertionResult(
                "A1 blueprint_spawned", FAIL,
                f"route XML asks for {rr.expected_blueprint!r}, the actor that actually "
                f"spawned is {observed!r}.{_tesla_hint(rr.expected_blueprint, observed)}"))
        else:
            rr.assertions.append(AssertionResult(
                "A1 blueprint_spawned", PASS,
                f"spawned actor type_id == {observed}"))

    # ---- A2 ---------------------------------------------------------------------------
    if isinstance(ttr, dict):
        infr = rec.get("infractions")
        routed = isinstance(infr, dict) and "ttr_dar" in infr
        rr.assertions.append(AssertionResult(
            "A2 criteria_attached", PASS,
            f"ttr_dar present ({len(ttr)} field(s));"
            f" infractions.ttr_dar {'present' if routed else 'ABSENT'}"))
    else:
        rr.assertions.append(AssertionResult(
            "A2 criteria_attached", FAIL,
            "no 'ttr_dar' key in the record. Either the TTRDARCriterion patch "
            "(patches/020-*atomic_criteria*) did not apply, or the leaderboard is using a "
            "statistics_manager fork that drops the TTR_DAR_MEASUREMENT event. Both produce "
            "records that look complete and quietly lose the criterion."))

    # ---- A3 ---------------------------------------------------------------------------
    gr = (golden.routes.get(rel) if golden else None) or {}
    expected_status = gr.get("status")
    if expected_status:
        ok = rr.status == expected_status
        rr.assertions.append(AssertionResult(
            "A3 route_completed", PASS if ok else FAIL,
            f"status={rr.status!r}" + ("" if ok else f", golden says {expected_status!r}")))
    else:
        ok = rr.status in CLEAN_STATUSES
        rr.assertions.append(AssertionResult(
            "A3 route_completed", PASS if ok else FAIL,
            f"status={rr.status!r}" + ("" if ok else
                                       f", expected one of {sorted(CLEAN_STATUSES)}")))

    # ---- A4 ---------------------------------------------------------------------------
    if golden is None:
        rr.assertions.append(AssertionResult(
            "A4 ds_within_tolerance", SKIP,
            "no golden bundle available -- see goldens/GENERATING.md"))
    elif gr.get("driving_score") is None:
        rr.assertions.append(AssertionResult(
            "A4 ds_within_tolerance", FAIL,
            f"golden bundle {os.path.basename(golden.path)} has no driving_score for this "
            f"route"))
    elif rr.ds is None:
        rr.assertions.append(AssertionResult(
            "A4 ds_within_tolerance", FAIL,
            "the record carries no scores.score_composed"))
    else:
        rr.golden_ds = float(gr["driving_score"])
        delta = abs(rr.ds - rr.golden_ds)
        ok = delta <= golden.tolerance_ds
        rr.assertions.append(AssertionResult(
            "A4 ds_within_tolerance", PASS if ok else FAIL,
            f"DS={rr.ds:.2f} golden={rr.golden_ds:.2f} |delta|={delta:.2f} "
            f"tolerance={golden.tolerance_ds:.2f}"))

    return rr


# =======================================================================================
# Reporting
# =======================================================================================
def banner(text: str, ch: str = "=") -> str:
    line = ch * 78
    return f"\n{line}\n{text}\n{line}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results-root", default=None,
                    help="output root of a smoke-split run (the runner's output.root). "
                         "Required unless --validate-goldens-only.")
    ap.add_argument("--validate-goldens-only", action="store_true",
                    help="check the split's integrity and the golden bundle's integrity, then "
                         "stop. Runs no assertions and needs no results -- this is the part CI "
                         "can do, since a GitHub-hosted runner has neither a GPU nor CARLA. "
                         "Exit 0 = bundle present and self-consistent, 2 = bundle broken, "
                         "3 = no bundle.")
    ap.add_argument("--split", default=DEFAULT_SPLIT)
    ap.add_argument("--routes-root", default=DEFAULT_ROUTES_ROOT,
                    help="frozen 475-route bundle, used to re-derive each route's expectation")
    ap.add_argument("--tier", choices=("core", "all"), default="all")
    ap.add_argument("--seed", type=int, default=42,
                    help="published protocol is seed 42 only")
    ap.add_argument("--goldens", default=None,
                    help="golden bundle JSON. Default: the single *.golden.json in "
                         "tests/goldens/, if exactly one exists.")
    ap.add_argument("--golden-dir", default=DEFAULT_GOLDEN_DIR)
    ap.add_argument("--json", dest="json_out", default=None,
                    help="write a machine-readable report here")
    args = ap.parse_args()

    if not args.validate_goldens_only and not args.results_root:
        print("ERROR: --results-root is required (or pass --validate-goldens-only).",
              file=sys.stderr)
        return 2

    print(f"OOD-PerceptionBench acceptance harness  [{BUNDLE_VERSION}, binds to {BINDS_TO}]")
    print(f"split       : {args.split}")
    print(f"routes root : {args.routes_root}")
    print(f"results root: {args.results_root or '(none -- validating goldens only)'}")
    print(f"seed        : {args.seed}")

    # ---- load + integrity-check the split ---------------------------------------------
    try:
        rows = load_split(args.split, args.tier)
    except (SplitError, OSError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
    if not rows:
        print(f"\nERROR: tier {args.tier!r} selected zero routes", file=sys.stderr)
        return 2

    if not os.path.isdir(args.routes_root):
        print(f"\nERROR: routes root is not a directory: {args.routes_root}", file=sys.stderr)
        return 2
    problems = verify_split(rows, args.routes_root)
    if problems:
        print(f"\nERROR: the smoke split does not agree with the frozen route bundle "
              f"({len(problems)} problem(s)). Nothing was assessed.\n")
        for p in problems:
            print(f"  {p}")
        return 2
    print(f"tier        : {args.tier}  ({len(rows)} route(s), split verified against the "
          f"frozen bundle)")

    # Re-derive every expectation from the XML itself, not from the split's own column.
    for r in rows:
        with open(os.path.join(args.routes_root, r["path"]), encoding="utf-8") as fh:
            r["prop_blueprint_id"] = blueprint_in_xml(fh.read())

    split_sha = sha256_of(args.split)
    split_paths = {r["path"] for r in rows}

    # ---- goldens ------------------------------------------------------------------------
    golden: Optional[Golden] = None
    golden_note = ""
    if args.goldens:
        candidates = [args.goldens]
    else:
        candidates = find_golden_bundles(args.golden_dir)
    if len(candidates) > 1:
        print(f"\nERROR: {len(candidates)} golden bundles in {args.golden_dir}; name one with "
              f"--goldens.\n  " + "\n  ".join(os.path.basename(c) for c in candidates),
              file=sys.stderr)
        return 2
    if candidates:
        try:
            golden = load_golden(candidates[0], split_sha, split_paths)
        except SplitError as exc:
            print(f"\nERROR: {exc}", file=sys.stderr)
            return 2
        print(f"goldens     : {golden.path}")
        ref = (golden.doc.get("reference_agent") or {}).get("name", "?")
        print(f"              reference agent {ref}, DS tolerance "
              f"+/-{golden.tolerance_ds:.2f}")
    else:
        golden_note = (f"no *.golden.json in {args.golden_dir}")
        print(f"goldens     : NONE ({golden_note})")

    if args.validate_goldens_only:
        if golden is None:
            print(banner(
                "NO GOLDEN BUNDLE.\n"
                f"{golden_note}\n"
                "The split itself is intact and agrees with the frozen route bundle, but there "
                "is nothing\nto validate against. See goldens/GENERATING.md. Exit code 3.", "?"))
            return 3
        print(banner(
            f"GOLDEN BUNDLE OK -- {os.path.basename(golden.path)}\n"
            f"  covers all {len(rows)} route(s) of tier {args.tier}\n"
            f"  pinned to this split (sha256 {split_sha[:12]}...) and to bundle "
            f"{BUNDLE_VERSION}\n"
            f"  DS tolerance +/-{golden.tolerance_ds:.2f}  "
            f"({(golden.doc.get('tolerance') or {}).get('policy', '')})\n\n"
            "This validates the bundle's integrity only. Executing the routes needs a GPU and a\n"
            "running CARLA, which no hosted CI runner has -- run check_acceptance.py on a GPU "
            "host."))
        return 0

    # ---- assess -------------------------------------------------------------------------
    results = [assess_route(r, args.results_root, args.routes_root, args.seed, golden)
               for r in rows]

    print(banner("PER-ROUTE ASSERTIONS"))
    for rr in results:
        head = "PASS" if not rr.failed else "FAIL"
        print(f"\n[{head}] {rr.path}")
        print(f"       expected blueprint : {rr.expected_blueprint}   ({rr.asset_class}, "
              f"tier={rr.tier})")
        if rr.result_path:
            print(f"       result             : {rr.result_path}")
        for a in rr.assertions:
            print(f"       {a.verdict:4s} {a.name:24s} {a.detail}")

    # ---- summary -------------------------------------------------------------------------
    n_fail = sum(1 for r in results if r.failed)
    n_skip = sum(1 for r in results if r.skipped and not r.failed)
    per_assertion: dict[str, dict[str, int]] = {}
    for rr in results:
        for a in rr.assertions:
            per_assertion.setdefault(a.name, {PASS: 0, FAIL: 0, SKIP: 0})[a.verdict] += 1

    print(banner("SUMMARY"))
    for name in sorted(per_assertion):
        c = per_assertion[name]
        print(f"  {name:24s} pass={c[PASS]:2d}  fail={c[FAIL]:2d}  skip={c[SKIP]:2d}")
    print(f"\n  routes: {len(results)}   failing: {n_fail}")

    if n_fail:
        verdict, code = "FAILED", 1
        print(banner(
            "FAILED -- this install does not measure what the benchmark claims to measure.\n"
            "Any score produced by it is not comparable with the published baselines.\n"
            "If A1 failed, start with:  python3 tests/probe_blueprints.py --host <h> --port <p>",
            "!"))
    elif golden is None:
        verdict, code = "INCONCLUSIVE", 3
        print(banner(
            "INCONCLUSIVE -- NOT A PASS.\n"
            f"A1..A3 passed on all {len(results)} route(s), so the OOD blueprints did spawn "
            "with the\nright type_id and the criteria attached. But no golden bundle was "
            "available, so the\nDriving Score was never compared against a reference "
            f"({golden_note}).\n\n"
            "Generate goldens on a GPU host: tests/goldens/GENERATING.md\n"
            "Exit code 3. Do not script this as success.", "?"))
    else:
        verdict, code = "PASSED", 0
        print(banner(
            f"PASSED -- {len(results)} route(s), all four assertions, against\n"
            f"{os.path.basename(golden.path)}."))

    if args.json_out:
        report = {
            "schema": "ood-perceptionbench/acceptance-report/1",
            "bundle_version": BUNDLE_VERSION,
            "binds_to": BINDS_TO,
            "verdict": verdict,
            "exit_code": code,
            "reportable": False,
            "split": {"path": args.split, "sha256": split_sha, "tier": args.tier,
                      "n_routes": len(rows)},
            "results_root": os.path.abspath(args.results_root),
            "seed": args.seed,
            "golden_bundle": os.path.abspath(golden.path) if golden else None,
            "golden_absent_reason": golden_note or None,
            "per_assertion": per_assertion,
            "routes": [asdict(r) for r in results],
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)) or ".", exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=False)
            fh.write("\n")
        print(f"\nreport written to {args.json_out}")

    return code


if __name__ == "__main__":
    sys.exit(main())
