#!/usr/bin/env python3
"""Regenerate MANIFEST.tsv for the frozen OOD-PerceptionBench route set.

Bundle version : v0.9
Binds to       : arXiv v1

Emits one tab-separated row per route XML:

    path  sha256  category  scenario  level  base_route_id  prop_blueprint_id

`path` is relative to this directory and always uses forward slashes, so the bundle
stays relocatable. Provenance lines are written as leading `#` comments; read the file
with e.g. ``pandas.read_csv("MANIFEST.tsv", sep="\\t", comment="#")``.

This script only *writes* the manifest. The acceptance test is `validate_routes.py`,
which independently re-derives every column from the files themselves and re-hashes
each one -- run it after any regeneration.

Standard library only. No network, no cluster paths, no third-party imports.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys

BUNDLE_VERSION = "v0.9"
BINDS_TO = "arXiv v1"

CATEGORIES = ("static", "pedestrian", "vehicle")
LEVELS = ("base", "visual_shift", "geometric_shift")
COLUMNS = [
    "path",
    "sha256",
    "category",
    "scenario",
    "level",
    "base_route_id",
    "prop_blueprint_id",
]

BLUEPRINT_TAGS = (
    "obstacle_blueprint",
    "pedestrian_blueprint",
    "front_vehicle_model",
    "cut_in_vehicle_model",
    "parked_vehicle_model",
    "blueprint_name",
)
BLUEPRINT_RE = re.compile(r"<(" + "|".join(BLUEPRINT_TAGS) + r")\s+value=\"([^\"]*)\"\s*/?>")
ROUTE_FILENAME_RE = re.compile(r"^route_(\d+)_(.+)\.xml$")


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Regenerate MANIFEST.tsv")
    ap.add_argument(
        "--root",
        default=os.path.dirname(os.path.abspath(__file__)),
        help="root of the route bundle (default: directory containing this script)",
    )
    ap.add_argument("--out", default=None, help="output path (default: <root>/MANIFEST.tsv)")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    out = args.out or os.path.join(root, "MANIFEST.tsv")

    rows = []
    for cat in CATEGORIES:
        cat_dir = os.path.join(root, cat)
        if not os.path.isdir(cat_dir):
            print(f"error: missing category directory {cat_dir}", file=sys.stderr)
            return 1
        for dirpath, _dirnames, filenames in os.walk(cat_dir):
            for fn in sorted(filenames):
                if not fn.endswith(".xml"):
                    continue
                abs_path = os.path.join(dirpath, fn)
                rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
                parts = rel.split("/")
                if len(parts) != 4 or parts[2] not in LEVELS:
                    print(f"error: unexpected layout for {rel}", file=sys.stderr)
                    return 1
                _, scenario, level, _ = parts
                m = ROUTE_FILENAME_RE.match(fn)
                if not m:
                    print(f"error: unexpected filename {rel}", file=sys.stderr)
                    return 1
                base_route_id = m.group(1)
                with open(abs_path, encoding="utf-8") as fh:
                    text = fh.read()
                found = BLUEPRINT_RE.findall(text)
                if len(found) != 1:
                    print(
                        f"error: {rel} declares {len(found)} blueprint tags, expected 1",
                        file=sys.stderr,
                    )
                    return 1
                rows.append(
                    [rel, sha256_of(abs_path), cat, scenario, level, base_route_id, found[0][1]]
                )

    rows.sort(key=lambda row: row[0])

    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# OOD-PerceptionBench -- canonical route manifest\n")
        fh.write(f"# bundle_version: {BUNDLE_VERSION}\n")
        fh.write(f"# binds_to: {BINDS_TO}\n")
        fh.write(f"# routes: {len(rows)}\n")
        fh.write("# generated_by: make_manifest.py (routes freeze)\n")
        fh.write("# paths are relative to this file's directory; separator is a single TAB\n")
        fh.write("\t".join(COLUMNS) + "\n")
        for row in rows:
            fh.write("\t".join(row) + "\n")

    print(f"wrote {out} with {len(rows)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
