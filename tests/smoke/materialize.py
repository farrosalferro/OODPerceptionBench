#!/usr/bin/env python3
"""Materialise the smoke split into a runnable route directory.

Bundle version : v0.9
Binds to       : arXiv v1

The split is *defined* by ``SMOKE_SPLIT.tsv`` (paths + sha256 into the frozen 475-route
bundle) and is *materialised* on demand into a directory the runner can be pointed at.

Why not just commit a second copy of the nine XMLs?

  Because a committed copy drifts. The canonical route tree is the definition of the
  benchmark; a duplicate that silently diverges from it would make the acceptance test
  assert against something the benchmark no longer says. Copying at materialise time,
  with the sha256 verified against the frozen manifest on every run, makes divergence
  impossible rather than unlikely.

The materialised tree preserves ``<category>/<scenario>/<level>/`` so that result paths
are byte-identical to the ones a full 475-route sweep would produce. Dropping that
component is the known trap documented in the runner's ``DESIGN.md`` section 5.

Standard library only.

Usage
-----
    python3 materialize.py                          # -> ./routes  (next to this file)
    python3 materialize.py --tier core              # 6 routes instead of 9
    python3 materialize.py --out /tmp/smoke_routes
    python3 materialize.py --verify-only            # check the split against the frozen bundle

Exit status
-----------
    0  materialised (or verified) successfully
    1  a route is missing, modified, or disagrees with the frozen manifest
    2  usage / IO error
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys

BUNDLE_VERSION = "v0.9"
BINDS_TO = "arXiv v1"

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(TESTS_DIR)

DEFAULT_SPLIT = os.path.join(HERE, "SMOKE_SPLIT.tsv")
DEFAULT_ROUTES_ROOT = os.path.join(REPO_ROOT, "routes")
DEFAULT_OUT = os.path.join(HERE, "routes")

TIERS = ("core", "extended")

# Same tag vocabulary the route freeze validator uses. Kept local on purpose: tests/ must
# not import from routes/ or runner/, so that a broken sibling cannot disable this check.
BLUEPRINT_TAGS = (
    "obstacle_blueprint",
    "pedestrian_blueprint",
    "front_vehicle_model",
    "cut_in_vehicle_model",
    "parked_vehicle_model",
    "blueprint_name",
)
BLUEPRINT_RE = re.compile(r"<(" + "|".join(BLUEPRINT_TAGS) + r")\s+value=\"([^\"]*)\"\s*/?>")


class SplitError(Exception):
    pass


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_tsv(path: str) -> list[dict]:
    """Read a '#'-commented TSV with a header row into a list of dicts."""
    rows: list[dict] = []
    header = None
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.split("\t")
            if header is None:
                header = fields
                continue
            if len(fields) != len(header):
                raise SplitError(
                    f"{os.path.basename(path)} line {lineno}: expected {len(header)} "
                    f"tab-separated fields, got {len(fields)}"
                )
            rows.append(dict(zip(header, fields)))
    if header is None:
        raise SplitError(f"{path}: no header row")
    return rows


def load_split(split_path: str, tier: str = "all") -> list[dict]:
    rows = read_tsv(split_path)
    required = {"path", "sha256", "category", "scenario", "level", "prop_blueprint_id", "tier"}
    missing_cols = required - set(rows[0]) if rows else required
    if missing_cols:
        raise SplitError(f"{split_path}: missing column(s) {sorted(missing_cols)}")
    for r in rows:
        if r["tier"] not in TIERS:
            raise SplitError(f"{split_path}: unknown tier {r['tier']!r} for {r['path']}")
    if tier == "all":
        return rows
    if tier == "core":
        return [r for r in rows if r["tier"] == "core"]
    raise SplitError(f"unknown tier selector {tier!r} (use 'core' or 'all')")


def blueprint_in_xml(text: str) -> str:
    """The single OOD blueprint id declared by a route XML.

    One file, one route, one prop -- anything else means the route tree changed shape and
    the acceptance test's expectation can no longer be derived. That is an error, not a
    thing to guess about.
    """
    found = BLUEPRINT_RE.findall(text)
    if len(found) != 1:
        raise SplitError(f"expected exactly one blueprint tag, found {len(found)}")
    return found[0][1]


def verify(rows: list[dict], routes_root: str) -> list[str]:
    """Return a list of problems; empty means the split agrees with the frozen bundle."""
    problems: list[str] = []
    for r in rows:
        src = os.path.join(routes_root, r["path"])
        if not os.path.isfile(src):
            problems.append(f"MISSING   {r['path']} (not found under {routes_root})")
            continue
        actual = sha256_of(src)
        if actual != r["sha256"]:
            problems.append(
                f"MODIFIED  {r['path']}\n"
                f"            split says  {r['sha256']}\n"
                f"            file is     {actual}\n"
                f"            The route XML and the split disagree. Either the route tree "
                f"was edited (the benchmark definition changed) or the split is stale. "
                f"Do not run the acceptance test until this is resolved."
            )
            continue
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        try:
            bp = blueprint_in_xml(text)
        except SplitError as exc:
            problems.append(f"UNPARSED  {r['path']}: {exc}")
            continue
        if bp != r["prop_blueprint_id"]:
            problems.append(
                f"DISAGREE  {r['path']}: XML declares {bp!r}, split says "
                f"{r['prop_blueprint_id']!r}"
            )
        parts = r["path"].split("/")
        if len(parts) != 4 or (parts[0], parts[1], parts[2]) != (
            r["category"], r["scenario"], r["level"]
        ):
            problems.append(
                f"SHAPE     {r['path']}: path does not match its "
                f"category/scenario/level columns"
            )
    return problems


def write_manifest(rows: list[dict], out_dir: str, tier: str) -> str:
    """Emit a MANIFEST.tsv the runner's ``routes.manifest`` can consume directly.

    Same schema as the frozen routes/MANIFEST.tsv (path + sha256 + metadata), so
    ``strict_manifest: true`` works against the materialised tree as well.
    """
    path = os.path.join(out_dir, "MANIFEST.tsv")
    cols = ["path", "sha256", "category", "scenario", "level", "base_route_id",
            "prop_blueprint_id"]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# OOD-PerceptionBench -- SMOKE SPLIT manifest (generated, do not edit)\n")
        fh.write(f"# bundle_version: {BUNDLE_VERSION}\n")
        fh.write(f"# binds_to: {BINDS_TO}\n")
        fh.write(f"# routes: {len(rows)}\n")
        fh.write(f"# tier: {tier}\n")
        fh.write("# generated_by: tests/smoke/materialize.py\n")
        fh.write("# NOT REPORTABLE -- this is an acceptance-test split, not a benchmark split.\n")
        fh.write("\t".join(cols) + "\n")
        for r in rows:
            fh.write("\t".join(r.get(c, "") for c in cols) + "\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--split", default=DEFAULT_SPLIT, help="split definition TSV")
    ap.add_argument("--routes-root", default=DEFAULT_ROUTES_ROOT,
                    help="root of the frozen 475-route bundle (default: <repo>/routes)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="directory to materialise into (default: tests/smoke/routes)")
    ap.add_argument("--tier", choices=("core", "all"), default="all",
                    help="'core' = 6 routes, 'all' = 9 routes (default). 'all' is the one "
                         "that covers every asset shipped in v0.9.")
    ap.add_argument("--verify-only", action="store_true",
                    help="check the split against the frozen bundle and exit; write nothing")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a non-empty output directory")
    args = ap.parse_args()

    print(f"OOD-PerceptionBench smoke split  [{BUNDLE_VERSION}, binds to {BINDS_TO}]")

    try:
        rows = load_split(args.split, args.tier)
    except (SplitError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not rows:
        print(f"ERROR: split {args.split} selected zero routes for tier {args.tier!r}",
              file=sys.stderr)
        return 2

    print(f"split      : {args.split}")
    print(f"routes root: {args.routes_root}")
    print(f"tier       : {args.tier}  ({len(rows)} route(s))")

    if not os.path.isdir(args.routes_root):
        print(f"\nERROR: routes root is not a directory: {args.routes_root}\n"
              f"       Point --routes-root at the frozen route bundle "
              f"(the directory holding static/, pedestrian/, vehicle/ and MANIFEST.tsv).",
              file=sys.stderr)
        return 2

    problems = verify(rows, args.routes_root)
    if problems:
        print(f"\nFAILED: {len(problems)} problem(s) verifying the split against the frozen "
              f"route bundle:\n")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"verified   : all {len(rows)} route(s) present and sha256-identical to the "
          f"frozen bundle")

    if args.verify_only:
        print("\nPASSED (verify-only; nothing written)")
        return 0

    out = os.path.abspath(args.out)
    if os.path.isdir(out) and os.listdir(out) and not args.force:
        # Re-materialising is the normal case, so only refuse if the directory holds
        # something we did not put there.
        stray = [f for f in os.listdir(out) if f not in {"MANIFEST.tsv"} | set(
            r["path"].split("/")[0] for r in rows)]
        if stray:
            print(f"\nERROR: output directory {out} is not empty and holds unrecognised "
                  f"entries {stray[:5]}.\n       Use --force to overwrite, or pick another "
                  f"--out.", file=sys.stderr)
            return 2

    for r in rows:
        src = os.path.join(args.routes_root, r["path"])
        dst = os.path.join(out, r["path"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    manifest = write_manifest(rows, out, args.tier)

    print(f"\nmaterialised {len(rows)} route(s) into {out}")
    print(f"manifest     {manifest}")
    print("\nPoint the runner at it:")
    print("  routes:")
    print(f"    root: {out}")
    print(f"    manifest: {manifest}")
    print("    strict_manifest: true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
