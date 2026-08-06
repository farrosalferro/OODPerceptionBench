#!/usr/bin/env python3
"""Freeze validator for the OOD-PerceptionBench canonical route set.

Bundle version : v0.9
Binds to       : arXiv v1

This script is the acceptance test for the frozen route tree. It is deliberately
self-contained: every expected count, every expected blueprint id and every
expected base-route id is hard-coded below, so the file *is* the machine-readable
definition of the benchmark at v0.9. If a future asset replacement changes any of
these, the constants must be bumped together with a new version stamp -- a silent
edit of the route tree can never pass unnoticed.

Standard library only. No network, no cluster paths, no third-party imports.

Usage
-----
    python3 validate_routes.py                     # validate the tree next to this file
    python3 validate_routes.py --root /path/to/routes
    python3 validate_routes.py --rename-map /path/to/rename_map.json   # optional extra check

Exit status
-----------
    0  every check passed
    1  at least one check failed (each failure is printed with a specific message)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict

# --------------------------------------------------------------------------------------
# Version stamp (coordinator standing rule 1)
# --------------------------------------------------------------------------------------

BUNDLE_VERSION = "v0.9"
BINDS_TO = "arXiv v1"

# --------------------------------------------------------------------------------------
# The frozen definition of the benchmark
# --------------------------------------------------------------------------------------

CATEGORIES = ("static", "pedestrian", "vehicle")
LEVELS = ("base", "visual_shift", "geometric_shift")

EXPECTED_TOTAL = 475
EXPECTED_PER_CATEGORY = {"static": 70, "pedestrian": 162, "vehicle": 243}

MANIFEST_NAME = "MANIFEST.tsv"
MANIFEST_COLUMNS = [
    "path",
    "sha256",
    "category",
    "scenario",
    "level",
    "base_route_id",
    "prop_blueprint_id",
]

# Files allowed to sit at the root of the frozen bundle beside the three category dirs.
ALLOWED_ROOT_FILES = {
    MANIFEST_NAME,
    "validate_routes.py",
    "make_manifest.py",
    "EXCLUSIONS.md",
    "README.md",
    "VERSION",
}

# The XML tag that carries the OOD prop/agent blueprint, per scenario family.
BLUEPRINT_TAGS = (
    "obstacle_blueprint",      # static:      ConstructionObstacle*
    "pedestrian_blueprint",    # pedestrian:  all four scenarios
    "front_vehicle_model",     # vehicle:     hard_break
    "cut_in_vehicle_model",    # vehicle:     parking_cut_in
    "parked_vehicle_model",    # vehicle:     parked_obstacle*, vehicle_opens_door_two_ways
    "blueprint_name",          # vehicle:     invading_turn
)
BLUEPRINT_RE = re.compile(
    r"<(" + "|".join(BLUEPRINT_TAGS) + r")\s+value=\"([^\"]*)\"\s*/?>"
)
ROUTE_ID_RE = re.compile(r"<route\s+id=\"([^\"]+)\"")
ROUTE_FILENAME_RE = re.compile(r"^route_(\d+)_(.+)\.xml$")

# Per (category, level): the exact prop token vocabulary, identical for every scenario in
# that category and for every base route in that scenario. Tokens are the trailing
# component of the blueprint id and also the filename suffix.
EXPECTED_LEVEL_PROPS = {
    "static": {
        "base": ["trafficwarning"],
        "visual_shift": [
            "europianarrowboardtrailer",
            "trafficarrowboard",
            "trafficmessageboard",
        ],
        "geometric_shift": [
            "concreteroadbarrier",
            "roadclosedbarricade",
            "roadclosedsign",
        ],
    },
    "pedestrian": {
        "base": ["0001", "0014", "0028"],
        "visual_shift": ["astronaut", "firefighter", "soldier"],
        "geometric_shift": ["boar", "deliveryrobot", "wheelchair"],
    },
    "vehicle": {
        "base": ["cooper_s_2021", "coupe_2020", "mkz_2020"],
        "visual_shift": ["hatchback", "sedan", "suv"],
        "geometric_shift": ["armoredvan", "dumptruck", "roadroller"],
    },
}

# Full blueprint id for every token above. This pins the *namespace*, not just the token:
# it is what makes a regression to a vendor-branded vehicle id a hard failure.
EXPECTED_BLUEPRINT_ID = {
    ("static", "trafficwarning"): "static.prop.trafficwarning",
    ("static", "europianarrowboardtrailer"): "static.prop.europianarrowboardtrailer",
    ("static", "trafficarrowboard"): "static.prop.trafficarrowboard",
    ("static", "trafficmessageboard"): "static.prop.trafficmessageboard",
    ("static", "concreteroadbarrier"): "static.prop.concreteroadbarrier",
    ("static", "roadclosedbarricade"): "static.prop.roadclosedbarricade",
    ("static", "roadclosedsign"): "static.prop.roadclosedsign",
    ("pedestrian", "0001"): "walker.pedestrian.0001",
    ("pedestrian", "0014"): "walker.pedestrian.0014",
    ("pedestrian", "0028"): "walker.pedestrian.0028",
    ("pedestrian", "astronaut"): "walker.pedestrian.astronaut",
    ("pedestrian", "firefighter"): "walker.pedestrian.firefighter",
    ("pedestrian", "soldier"): "walker.pedestrian.soldier",
    ("pedestrian", "boar"): "walker.pedestrian.boar",
    ("pedestrian", "deliveryrobot"): "walker.pedestrian.deliveryrobot",
    ("pedestrian", "wheelchair"): "walker.pedestrian.wheelchair",
    ("vehicle", "cooper_s_2021"): "vehicle.mini.cooper_s_2021",
    ("vehicle", "coupe_2020"): "vehicle.mercedes.coupe_2020",
    ("vehicle", "mkz_2020"): "vehicle.lincoln.mkz_2020",
    ("vehicle", "hatchback"): "vehicle.ood.hatchback",
    ("vehicle", "sedan"): "vehicle.ood.sedan",
    ("vehicle", "suv"): "vehicle.ood.suv",
    ("vehicle", "armoredvan"): "vehicle.ood.armoredvan",
    ("vehicle", "dumptruck"): "vehicle.ood.dumptruck",
    ("vehicle", "roadroller"): "vehicle.ood.roadroller",
}

# The 55 base routes, frozen. Absences here are the deliberate exclusions documented in
# EXCLUSIONS.md; pinning the ids means a "helpfully restored" route fails the validator.
EXPECTED_BASE_ROUTES = {
    "static": {
        "construction_obstacle": ["2509", "2513", "24367", "24785", "24795"],
        "construction_obstacle_two_ways": ["1825", "1833", "2606", "20920", "25424"],
    },
    "pedestrian": {
        "dynamic_object_crossing": ["24211", "24224", "24252", "24333"],
        "parking_crossing_pedestrian": ["3248", "3255", "18252", "24206", "24294"],
        "pedestrian_crossing": ["14194", "25863", "27515", "27529", "27582"],
        "vehicle_turning_route_pedestrian": ["2164", "3731", "10857", "11381"],
    },
    "vehicle": {
        "hard_break": ["3540", "24330", "24781", "26406", "26456"],
        "invading_turn": ["2790", "2802", "3572", "3575"],
        "parked_obstacle": ["1773", "2554", "24757", "25318", "25383"],
        "parked_obstacle_two_ways": ["2668", "25865", "25896"],
        "parking_cut_in": ["1711", "18305", "18311", "18356", "24759"],
        "vehicle_opens_door_two_ways": ["3464", "3472", "3476", "3482", "25928"],
    },
}

# Routes deliberately dropped on ALL THREE levels. Asserting their absence is what keeps
# a well-meaning "gap fix" from silently breaking base/visual/geometric parity.
DELIBERATELY_EXCLUDED = [
    ("vehicle", "parked_obstacle_two_ways", "3457"),
    ("vehicle", "parked_obstacle_two_ways", "2664"),
    ("vehicle", "invading_turn", "3564"),
    ("pedestrian", "dynamic_object_crossing", "17752"),
    ("pedestrian", "vehicle_turning_route_pedestrian", "3737"),
]

# Vendor / trademark tokens that must not survive anywhere in the frozen bundle
# (file contents, file names, directory names). Matched case-insensitively as raw
# substrings -- the strictest form; verified to be free of false positives on this tree.
FORBIDDEN_TOKENS = ("inkas", "caterpillar", "hamm", "tohoku")

# This file necessarily spells the forbidden tokens out (they are the needles). It is the
# single, explicit exemption from the content scan; its path is still scanned.
TOKEN_SCAN_EXEMPT_FILES = {"validate_routes.py"}

# Scaffolding trees that must never be staged. Matched against every path component.
FORBIDDEN_PATH_PATTERNS = (
    re.compile(r".*_debug$"),
    re.compile(r".*_missing$"),
    re.compile(r".*_smoke$"),
    re.compile(r"^static_smoke_reasonplan$"),
    re.compile(r".*_fix$"),
    re.compile(r".*_revision$"),
    re.compile(r"^ood_benchmark$"),
    re.compile(r"^ood_benchmark_.*$"),
    re.compile(r"^family_.*$"),
    re.compile(r"^backups$"),
    re.compile(r"^w2_backup.*$"),
)


# --------------------------------------------------------------------------------------
# Machinery
# --------------------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks += 1
        if ok:
            print(f"  [ ok ] {name}")
        else:
            print(f"  [FAIL] {name}: {detail}")
            self.failures.append(f"{name}: {detail}")
        return ok

    def fail(self, name: str, detail: str) -> None:
        self.check(name, False, detail)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk_files(root: str) -> list[str]:
    """Every regular file under root, as a relative posix path."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def read_manifest(path: str) -> tuple[list[dict], list[str], list[str]]:
    """Return (rows, header, comment_lines). Lines starting with '#' are provenance."""
    comments, header, rows = [], None, []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if not line:
                continue
            if line.startswith("#"):
                comments.append(line)
                continue
            fields = line.split("\t")
            if header is None:
                header = fields
                continue
            if len(fields) != len(header):
                raise ValueError(
                    f"{MANIFEST_NAME} line {lineno}: expected {len(header)} "
                    f"tab-separated fields, got {len(fields)}"
                )
            rows.append(dict(zip(header, fields)))
    if header is None:
        raise ValueError(f"{MANIFEST_NAME} has no header row")
    return rows, header, comments


def parse_route_xml(text: str) -> tuple[str | None, list[str]]:
    """Return (route id attribute, [blueprint ids found])."""
    rid = ROUTE_ID_RE.search(text)
    return (rid.group(1) if rid else None), [m[1] for m in BLUEPRINT_RE.findall(text)]


# --------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--root",
        default=os.path.dirname(os.path.abspath(__file__)),
        help="root of the frozen route bundle (default: directory containing this script)",
    )
    ap.add_argument(
        "--rename-map",
        default=None,
        help="optional rename_map.json to cross-check blueprint ids against",
    )
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    print(f"OOD-PerceptionBench route freeze validator  [{BUNDLE_VERSION}, binds to {BINDS_TO}]")
    print(f"root: {root}\n")

    r = Report()

    # ---- 0. bundle layout ------------------------------------------------------------
    print("[0] bundle layout")
    if not os.path.isdir(root):
        print(f"  [FAIL] root is not a directory: {root}")
        return 1
    top = sorted(os.listdir(root))
    top_dirs = [d for d in top if os.path.isdir(os.path.join(root, d)) and d != "__pycache__"]
    top_files = [f for f in top if os.path.isfile(os.path.join(root, f))]
    r.check(
        "top-level directories are exactly the three categories",
        sorted(top_dirs) == sorted(CATEGORIES),
        f"found {top_dirs}",
    )
    unexpected_files = sorted(set(top_files) - ALLOWED_ROOT_FILES)
    r.check("no unexpected files at bundle root", not unexpected_files, f"found {unexpected_files}")
    for required in (MANIFEST_NAME, "EXCLUSIONS.md", "VERSION"):
        r.check(f"{required} present", os.path.isfile(os.path.join(root, required)), "missing")

    all_files = walk_files(root)
    xml_files = [f for f in all_files if f.endswith(".xml")]
    non_xml_in_categories = [
        f for f in all_files if f.split("/")[0] in CATEGORIES and not f.endswith(".xml")
    ]
    r.check(
        "category trees contain only .xml files",
        not non_xml_in_categories,
        f"found {non_xml_in_categories[:10]}",
    )

    # ---- 1. no excluded / scaffolding directory names --------------------------------
    print("\n[1] scaffolding exclusion")
    offending = []
    for rel in all_files:
        for comp in rel.split("/"):
            stem = comp[:-4] if comp.endswith(".xml") else comp
            for pat in FORBIDDEN_PATH_PATTERNS:
                if pat.match(stem):
                    offending.append(rel)
                    break
    r.check(
        "no excluded-directory / scaffolding name anywhere in the tree",
        not offending,
        f"{len(offending)} path(s), e.g. {sorted(set(offending))[:5]}",
    )

    # ---- 2. counts -------------------------------------------------------------------
    print("\n[2] counts")
    r.check(
        f"total route XMLs == {EXPECTED_TOTAL}",
        len(xml_files) == EXPECTED_TOTAL,
        f"found {len(xml_files)}",
    )
    per_cat = defaultdict(int)
    for f in xml_files:
        per_cat[f.split("/")[0]] += 1
    for cat in CATEGORIES:
        r.check(
            f"{cat} count == {EXPECTED_PER_CATEGORY[cat]}",
            per_cat[cat] == EXPECTED_PER_CATEGORY[cat],
            f"found {per_cat[cat]}",
        )

    # ---- 3. path shape + per-file content --------------------------------------------
    print("\n[3] path shape and per-file content")
    records = []  # (rel, cat, scen, lvl, base_id, token, blueprint)
    shape_bad, tag_bad, id_bad, token_bad = [], [], [], []
    for rel in xml_files:
        parts = rel.split("/")
        if len(parts) != 4 or parts[2] not in LEVELS:
            shape_bad.append(rel)
            continue
        cat, scen, lvl, fn = parts
        m = ROUTE_FILENAME_RE.match(fn)
        if not m:
            shape_bad.append(rel)
            continue
        base_id, token = m.group(1), m.group(2)
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            text = fh.read()
        route_attr, bps = parse_route_xml(text)
        if len(bps) != 1:
            tag_bad.append(f"{rel} (found {len(bps)} blueprint tags)")
            continue
        bp = bps[0]
        if route_attr != f"route_{base_id}_{token}":
            id_bad.append(f"{rel} (route id attribute is {route_attr!r})")
        if bp.split(".")[-1] != token:
            token_bad.append(f"{rel} (filename token {token!r} vs blueprint {bp!r})")
        records.append((rel, cat, scen, lvl, base_id, token, bp))

    r.check(
        "every path is <category>/<scenario>/<level>/route_<id>_<token>.xml",
        not shape_bad,
        f"{len(shape_bad)} bad, e.g. {shape_bad[:5]}",
    )
    r.check(
        "every route XML declares exactly one blueprint tag",
        not tag_bad,
        f"{len(tag_bad)} bad, e.g. {tag_bad[:5]}",
    )
    r.check(
        "<route id=...> matches the filename stem",
        not id_bad,
        f"{len(id_bad)} bad, e.g. {id_bad[:5]}",
    )
    r.check(
        "filename token matches the blueprint id's trailing component",
        not token_bad,
        f"{len(token_bad)} bad, e.g. {token_bad[:5]}",
    )

    # ---- 4. blueprint vocabulary ------------------------------------------------------
    print("\n[4] blueprint vocabulary")
    bp_bad = []
    for rel, cat, scen, lvl, base_id, token, bp in records:
        expected = EXPECTED_BLUEPRINT_ID.get((cat, token))
        if expected is None:
            bp_bad.append(f"{rel} (unknown token {token!r} for category {cat})")
        elif bp != expected:
            bp_bad.append(f"{rel} (blueprint {bp!r}, expected {expected!r})")
    r.check(
        "every blueprint id matches the frozen v0.9 vocabulary",
        not bp_bad,
        f"{len(bp_bad)} bad, e.g. {bp_bad[:5]}",
    )
    seen_bps = {rec[6] for rec in records}
    r.check(
        "no blueprint id outside the frozen vocabulary appears",
        seen_bps <= set(EXPECTED_BLUEPRINT_ID.values()),
        f"extra {sorted(seen_bps - set(EXPECTED_BLUEPRINT_ID.values()))}",
    )

    # ---- 5. structural parity ---------------------------------------------------------
    print("\n[5] structural parity (base / visual_shift / geometric_shift)")
    grid = defaultdict(set)  # (cat, scen, lvl) -> {(base_id, token)}
    for rel, cat, scen, lvl, base_id, token, bp in records:
        grid[(cat, scen, lvl)].add((base_id, token))

    parity_bad, cross_bad, vocab_bad, ids_bad, dup_bad = [], [], [], [], []
    for cat in CATEGORIES:
        scenarios = sorted(EXPECTED_BASE_ROUTES[cat])
        found_scenarios = sorted({s for c, s, _ in grid if c == cat})
        if found_scenarios != scenarios:
            r.fail(
                f"{cat}: scenario set",
                f"found {found_scenarios}, expected {scenarios}",
            )
            continue
        for scen in scenarios:
            expected_ids = set(EXPECTED_BASE_ROUTES[cat][scen])
            per_level_ids = {}
            for lvl in LEVELS:
                cell = grid[(cat, scen, lvl)]
                ids = {b for b, _ in cell}
                toks = {t for _, t in cell}
                per_level_ids[lvl] = ids
                expected_toks = set(EXPECTED_LEVEL_PROPS[cat][lvl])
                if toks != expected_toks:
                    vocab_bad.append(
                        f"{cat}/{scen}/{lvl}: props {sorted(toks)} != {sorted(expected_toks)}"
                    )
                if len(cell) != len(ids) * len(toks):
                    dup_bad.append(
                        f"{cat}/{scen}/{lvl}: {len(cell)} files for "
                        f"{len(ids)} routes x {len(toks)} props"
                    )
                missing = {
                    (b, t) for b in ids for t in toks
                } - cell
                if missing:
                    cross_bad.append(f"{cat}/{scen}/{lvl}: missing {sorted(missing)[:4]}")
                if ids != expected_ids:
                    ids_bad.append(
                        f"{cat}/{scen}/{lvl}: base routes "
                        f"+{sorted(ids - expected_ids)} -{sorted(expected_ids - ids)}"
                    )
            if not (per_level_ids["base"] == per_level_ids["visual_shift"] == per_level_ids["geometric_shift"]):
                parity_bad.append(f"{cat}/{scen}: base/visual/geometric cover different routes")

    r.check("each scenario covers the same base routes on all three levels", not parity_bad,
            "; ".join(parity_bad[:3]))
    r.check("each (scenario, level) is a complete route x prop cross product", not cross_bad,
            "; ".join(cross_bad[:3]))
    r.check("each (scenario, level) has no duplicate route/prop pair", not dup_bad,
            "; ".join(dup_bad[:3]))
    r.check("per-level prop vocabulary is identical across scenarios in a category",
            not vocab_bad, "; ".join(vocab_bad[:3]))
    r.check("base-route ids match the frozen v0.9 set", not ids_bad, "; ".join(ids_bad[:3]))

    n_visual = {cat: len(EXPECTED_LEVEL_PROPS[cat]["visual_shift"]) for cat in CATEGORIES}
    n_geo = {cat: len(EXPECTED_LEVEL_PROPS[cat]["geometric_shift"]) for cat in CATEGORIES}
    r.check(
        "visual and geometric levels carry the same number of props per category",
        n_visual == n_geo,
        f"visual {n_visual} vs geometric {n_geo}",
    )

    total_base_routes = sum(len(v) for cat in CATEGORIES for v in EXPECTED_BASE_ROUTES[cat].values())
    r.check("55 base routes across the benchmark", total_base_routes == 55,
            f"found {total_base_routes}")

    # ---- 6. deliberate exclusions stay excluded ---------------------------------------
    print("\n[6] deliberate exclusions")
    resurrected = []
    present = {(c, s, b) for _, c, s, _, b, _, _ in records}
    for cat, scen, rid in DELIBERATELY_EXCLUDED:
        if (cat, scen, rid) in present:
            resurrected.append(f"{cat}/{scen} route_{rid}")
    r.check(
        "the five deliberately excluded routes are absent on every level",
        not resurrected,
        f"resurrected: {resurrected} -- see EXCLUSIONS.md, restoring them breaks parity",
    )

    # ---- 7. trademark scan -------------------------------------------------------------
    print("\n[7] trademark / vendor-token scan")
    path_hits, content_hits = [], []
    for rel in all_files:
        low = rel.lower()
        for tok in FORBIDDEN_TOKENS:
            if tok in low:
                path_hits.append(rel)
                break
        if os.path.basename(rel) in TOKEN_SCAN_EXEMPT_FILES:
            continue
        with open(os.path.join(root, rel), "rb") as fh:
            blob = fh.read().decode("utf-8", errors="ignore").lower()
        for tok in FORBIDDEN_TOKENS:
            if tok in blob:
                content_hits.append(f"{rel} ({tok})")
                break
    r.check("no vendor/trademark token in any path", not path_hits, f"{path_hits[:5]}")
    r.check(
        "no vendor/trademark token in any file content",
        not content_hits,
        f"{len(content_hits)} file(s), e.g. {content_hits[:5]}",
    )
    print(
        f"         (scanned {len(all_files)} files; exempt by design: "
        f"{sorted(TOKEN_SCAN_EXEMPT_FILES)} -- it declares the tokens it searches for)"
    )

    # ---- 8. manifest ------------------------------------------------------------------
    print("\n[8] MANIFEST.tsv")
    manifest_path = os.path.join(root, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        r.fail("manifest readable", "MANIFEST.tsv missing")
    else:
        try:
            rows, header, comments = read_manifest(manifest_path)
        except ValueError as exc:
            r.fail("manifest parses", str(exc))
            rows, header, comments = [], [], []
        r.check("manifest header is the agreed schema", header == MANIFEST_COLUMNS,
                f"found {header}")
        r.check(f"manifest has {EXPECTED_TOTAL} data rows", len(rows) == EXPECTED_TOTAL,
                f"found {len(rows)}")
        stamp = "\n".join(comments)
        r.check(
            "manifest carries the version stamp",
            BUNDLE_VERSION in stamp and BINDS_TO in stamp,
            f"expected {BUNDLE_VERSION!r} and {BINDS_TO!r} in the '#' header",
        )

        by_path = {}
        dup_rows, bad_path = [], []
        for row in rows:
            p = row.get("path", "")
            if p in by_path:
                dup_rows.append(p)
            by_path[p] = row
            if p.startswith("/") or ".." in p.split("/") or "\\" in p:
                bad_path.append(p)
        r.check("manifest paths are unique", not dup_rows, f"{dup_rows[:5]}")
        r.check("manifest paths are relative and contained", not bad_path, f"{bad_path[:5]}")

        missing_rows = sorted(set(xml_files) - set(by_path))
        extra_rows = sorted(set(by_path) - set(xml_files))
        r.check("every staged XML has a manifest row", not missing_rows,
                f"{len(missing_rows)} missing, e.g. {missing_rows[:5]}")
        r.check("every manifest row points at a staged XML", not extra_rows,
                f"{len(extra_rows)} extra, e.g. {extra_rows[:5]}")

        sha_bad, field_bad = [], []
        for rel, cat, scen, lvl, base_id, token, bp in records:
            row = by_path.get(rel)
            if row is None:
                continue
            actual = sha256_of(os.path.join(root, rel))
            if row["sha256"] != actual:
                sha_bad.append(f"{rel} (manifest {row['sha256'][:12]}..., actual {actual[:12]}...)")
            derived = {
                "category": cat,
                "scenario": scen,
                "level": lvl,
                "base_route_id": base_id,
                "prop_blueprint_id": bp,
            }
            for k, v in derived.items():
                if row[k] != v:
                    field_bad.append(f"{rel}: {k} manifest={row[k]!r} actual={v!r}")
        r.check("every file's sha256 matches MANIFEST", not sha_bad,
                f"{len(sha_bad)} mismatch, e.g. {sha_bad[:3]}")
        r.check("manifest metadata columns agree with the files", not field_bad,
                f"{len(field_bad)} mismatch, e.g. {field_bad[:3]}")

    # ---- 9. VERSION -------------------------------------------------------------------
    print("\n[9] VERSION")
    version_path = os.path.join(root, "VERSION")
    if os.path.isfile(version_path):
        with open(version_path, encoding="utf-8") as fh:
            vtext = fh.read()
        r.check("VERSION records the bundle version", BUNDLE_VERSION in vtext,
                f"{BUNDLE_VERSION!r} not found")
        r.check("VERSION records the arXiv binding", BINDS_TO in vtext,
                f"{BINDS_TO!r} not found")

    # ---- 10. optional rename_map cross-check -------------------------------------------
    if args.rename_map:
        print("\n[10] rename_map.json cross-check")
        try:
            with open(args.rename_map, encoding="utf-8") as fh:
                rmap = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            r.fail("rename_map readable", str(exc))
        else:
            bid = rmap.get("blueprint_id_map", {})
            new_ids = set(bid.values())
            old_ids = set(bid)
            r.check("no pre-rename blueprint id survives in the tree",
                    not (seen_bps & old_ids), f"{sorted(seen_bps & old_ids)}")
            ood_in_tree = {b for b in seen_bps if b.startswith("vehicle.ood.")}
            r.check("every vehicle.ood.* id in the tree is a rename_map target",
                    ood_in_tree <= new_ids, f"unmapped {sorted(ood_in_tree - new_ids)}")
            r.check("every rename_map target is used by the tree",
                    new_ids <= ood_in_tree, f"unused {sorted(new_ids - ood_in_tree)}")

    # ---- verdict -----------------------------------------------------------------------
    print()
    if r.failures:
        print(f"FAILED: {len(r.failures)} of {r.checks} checks failed")
        for f in r.failures:
            print(f"  - {f}")
        return 1
    print(f"PASSED: all {r.checks} checks green "
          f"({EXPECTED_TOTAL} routes, {total_base_routes} base routes, "
          f"{BUNDLE_VERSION} / {BINDS_TO})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
