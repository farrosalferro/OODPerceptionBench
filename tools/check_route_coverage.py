#!/usr/bin/env python3
"""Assert that every scenario type used by the canonical route set exists in the
patched upstream tree — and that the route set is exactly 475 routes.

This is the check that catches the failure mode the patch set was built to avoid:
a scenario module that lives only in a private working tree, is therefore
invisible to `git diff`, and is silently missing from the release. When that
happens the affected routes do not crash loudly — CARLA's route builder raises
inside `get_all_scenario_classes()`, which is unguarded, so the whole sweep dies;
or worse, a subtly wrong class is picked up and routes score plausibly.

Run standalone:
    python3 tools/check_route_coverage.py --upstream-dir /path/to/carla_garage

Exits non-zero on any mismatch. Skips gracefully (exit 0, with a message) if
routes/ has not been populated yet — route freezing is a separate workstream.
"""
from __future__ import annotations

import argparse
import ast
import glob
import os
import re
import sys

EXPECTED_TOTAL = 475
EXPECTED_PER_CATEGORY = {"static": 70, "pedestrian": 162, "vehicle": 243}
EXPECTED_LEVELS = {"base", "visual_shift", "geometric_shift"}

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def scenario_classes(upstream_dir: str) -> dict[str, str]:
    """Mimic CARLA's RouteScenario.get_all_scenario_classes(): every top-level
    class in every module under srunner/scenarios/."""
    pattern = os.path.join(upstream_dir, "Bench2Drive", "scenario_runner",
                           "srunner", "scenarios", "*.py")
    found: dict[str, str] = {}
    files = glob.glob(pattern)
    if not files:
        sys.exit(f"ERROR: no scenario modules found under {pattern}\n"
                 f"       Is --upstream-dir a patched carla_garage checkout?")
    for path in files:
        try:
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        except SyntaxError as exc:
            sys.exit(f"ERROR: {path} does not parse: {exc}")
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                found.setdefault(node.name, os.path.basename(path))
    return found


def route_files(routes_dir: str) -> list[str]:
    return sorted(glob.glob(os.path.join(routes_dir, "*", "*", "*", "*.xml")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream-dir", required=True,
                    help="patched carla_garage checkout (output of setup.sh)")
    ap.add_argument("--routes-dir", default=os.path.join(REPO_ROOT, "routes"),
                    help="route tree; default <repo>/routes")
    args = ap.parse_args()

    files = route_files(args.routes_dir)
    if not files:
        print(f"routes/ is empty ({args.routes_dir}) — route freeze has not landed yet. "
              f"Skipping coverage check.")
        return 0

    # ---- counts -----------------------------------------------------------
    per_cat: dict[str, int] = {}
    bad_level: list[str] = []
    for f in files:
        rel = os.path.relpath(f, args.routes_dir)
        cat, _scen, level, _name = rel.split(os.sep)
        per_cat[cat] = per_cat.get(cat, 0) + 1
        if level not in EXPECTED_LEVELS:
            bad_level.append(rel)

    ok = True
    if len(files) != EXPECTED_TOTAL:
        print(f"FAIL total routes: got {len(files)}, expected {EXPECTED_TOTAL}")
        ok = False
    if per_cat != EXPECTED_PER_CATEGORY:
        print(f"FAIL per-category counts: got {per_cat}, expected {EXPECTED_PER_CATEGORY}")
        ok = False
    if bad_level:
        print(f"FAIL {len(bad_level)} route(s) under an unexpected level dir, "
              f"e.g. {bad_level[0]}")
        ok = False

    # ---- scenario-class coverage -----------------------------------------
    used: dict[str, str] = {}
    for f in files:
        for t in re.findall(r'type="([^"]+)"', open(f, encoding="utf-8").read()):
            used.setdefault(t, os.path.relpath(f, args.routes_dir))

    available = scenario_classes(args.upstream_dir)
    missing = {t: r for t, r in used.items() if t not in available}

    print(f"routes: {len(files)}  categories: {per_cat}")
    print(f"scenario types used by routes: {len(used)}")
    print(f"scenario classes available in patched tree: {len(available)}")

    if missing:
        ok = False
        print("\nFAIL — scenario types used by routes but ABSENT from the patched tree:")
        for t, r in sorted(missing.items()):
            print(f"  {t}    (first used by {r})")
        print("\nThis means the patch set is incomplete. A scenario module that exists "
              "only in a private working tree will not appear in `git diff` and will be "
              "silently omitted from patches/.")
    else:
        for t in sorted(used):
            print(f"  OK  {t:44s} <- {available[t]}")

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
