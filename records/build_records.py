#!/usr/bin/env python3
"""OOD-PerceptionBench — derived per-route record generator (Tier A).

Single documented entry point that turns the raw closed-loop CARLA result tree
(one leaderboard checkpoint JSON per route) into ONE tidy table:

    one row per (model, category, scenario, route_id, level, prop, seed)

Emitted as CSV **and** parquet, a few MB, ships in-repo. This is what lets
anyone re-verify every number in the paper without a GPU and without
re-simulating anything.

CONSOLIDATES (does not reimplement) the already-trusted logic of the authors'
original analysis tools, which are not part of this repository:
  - row extraction, Infinity/NaN handling
  - OOD-collision attribution (the second headline metric)
  - Bench2Drive Success Rate and its infraction skip-set

and applies `rename_map.json` (bundled beside this script) so that both the prop
tokens and the resolved `agent_type` blueprint ids in the records agree with the
route XMLs and the manifest. The rename is part of the generator, not a
post-processing step: the released CSV/parquet must be reproducible by running
this file, with nothing applied to the output afterwards by hand.

Usage
-----
    python build_records.py --results-root /path/to/ood_benchmark_v2 --out-dir .

    # single model, for development
    python build_records.py --results-root ... --models tcp --no-write

The results root is READ-ONLY. This script never writes outside --out-dir.
It writes THREE files in one pass — .csv, .parquet and .meta.json — and the
meta records the sha256 of the other two plus of this script. Regenerate all of
them together; editing any one of them by hand breaks `check_meta.py`.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Version stamp (standing rule 1: every artifact binds to an arXiv version)
# --------------------------------------------------------------------------
RECORDS_SCHEMA = "ood-perceptionbench/records/1"
RECORDS_VERSION = "v0.9"
BINDS_TO = "arXiv v1"

# The rename map ships beside this script so the published records can be
# regenerated from this repository alone — the artifact is not reproducible from
# "the shipped code" if a required input lives outside the bundle.
DEFAULT_RENAME_MAP = Path(__file__).resolve().parent / "rename_map.json"

# --------------------------------------------------------------------------
# Locked cohort + directory map.
#
# Model directory names inside the results tree do NOT always equal the model
# key used in the paper. The map below is DERIVED FROM (and verified against)
# the absolute_path column of the frozen paper CSVs — it is not guesswork:
#   uniad   ships pedestrian/static under `uniad_base` (the `*_tiny` split is
#           a different, dropped experiment)
#   pdmlite ships vehicle under `pdmlite_v2` (the `pdmlite` vehicle dir holds
#           343 stale JSONs from a superseded sweep)
# Sibling dirs `pdm_lite_debug`, `reasonplan`, `sparsedrivev2`, `uniad_tiny`
# and `_archive_stale` are scaffolding and are NEVER scanned.
# --------------------------------------------------------------------------
CATEGORIES = ("pedestrian", "static", "vehicle")
LEVELS = ("base", "visual_shift", "geometric_shift")

# 17 end-to-end models (the headline cohort) + pdmlite (privileged ceiling,
# rows present but excluded from N=17 and from every statistical test).
E2E_MODELS = [
    "admlp", "bridgedrive", "diffad", "drivemoe", "drivetransformer",
    "hipad", "hydra_next", "lead", "minddrive", "orion", "orion_lite",
    "simlingo", "sparsedrive_v2", "tcp", "tfpp", "uniad", "vad",
]
CEILING_MODELS = ["pdmlite"]
ALL_MODELS = E2E_MODELS + CEILING_MODELS

RESULT_DIR_OVERRIDES = {
    ("uniad", "pedestrian"): "uniad_base",
    ("uniad", "static"): "uniad_base",
    ("pdmlite", "vehicle"): "pdmlite_v2",
}

# Expected route counts per category (the canonical 475-route set).
EXPECTED_ROUTES = {"pedestrian": 162, "static": 70, "vehicle": 243}

DEFAULT_SEED = 42

FILENAME_RE = re.compile(r"^route_(\d+)_(.+)_seed(\d+)\.json$")
TYPE_RE = re.compile(r"type=([^\s]+)")

COMPLETED_STATUSES = {"Completed", "Perfect"}

# Bench2Drive Success-Rate skip-set. NOT just min_speed: this benchmark stuffs
# secondary-metric measurement payloads into the infractions dict, and a naive
# port of the official tool would wrongly fail a clean route.
INFRACTION_SKIP_KEYS = {
    "min_speed_infractions",
    "ttr_dar",
    "ttr_dar_analytic",
    "interaction_correctness",
    "ic_analytic",
}

# Every infraction list key we count. Order fixed for a stable schema.
INFRACTION_KEYS = [
    "collisions_layout",
    "collisions_pedestrian",
    "collisions_vehicle",
    "red_light",
    "stop_infraction",
    "outside_route_lanes",
    "min_speed_infractions",
    "yield_emergency_vehicle_infractions",
    "scenario_timeouts",
    "route_dev",
    "vehicle_blocked",
    "route_timeout",
]

# Leaderboard `labels` -> flat column. These are the per-km / aggregate
# infraction *rates* the leaderboard reports, distinct from the raw counts.
LABEL_COLUMNS = {
    "Avg. driving score": "driving_score",
    "Avg. route completion": "route_completion",
    "Avg. infraction penalty": "infraction_penalty",
    "Collisions with pedestrians": "collisions_pedestrians",
    "Collisions with vehicles": "collisions_vehicles",
    "Collisions with layout": "collisions_layout",
    "Off-road infractions": "off_road_infractions",
}

# ttr_dar payload fields carried through verbatim (top level: record['ttr_dar']).
TTR_DAR_FIELDS = [
    "ttr", "dar", "ttc_at_reaction", "reaction_detected", "agent_type",
    "t_obs_frame", "t_react_frame", "closing_velocity",
    "reaction_cause", "reaction_value", "reaction_threshold",
    "v_start", "v_end",
    "final_distance", "final_closing_velocity", "final_ttc",
]


# --------------------------------------------------------------------------
# JSON loading (verbatim semantics from eval/json_to_csv_results_v2.py)
# --------------------------------------------------------------------------
def _sanitize_value(val):
    if val is None:
        return ""
    if isinstance(val, float):
        if math.isinf(val):
            return "Infinity" if val > 0 else "-Infinity"
        if math.isnan(val):
            return "NaN"
    return val


def _sanitize_json_floats(obj):
    if isinstance(obj, float):
        if math.isinf(obj):
            return "Infinity" if obj > 0 else "-Infinity"
        if math.isnan(obj):
            return "NaN"
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_json_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_json_floats(v) for v in obj]
    return obj


def load_json_with_infinity(filepath: Path):
    """Leaderboard checkpoints can contain bare Infinity/NaN literals."""
    content = filepath.read_text()
    content = re.sub(r"\bInfinity\b", "1e999", content)
    content = re.sub(r"-\bInfinity\b", "-1e999", content)
    content = re.sub(r"\bNaN\b", "null", content)
    return json.loads(content)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def result_dir_for(results_root: Path, model: str, category: str) -> Path:
    name = RESULT_DIR_OVERRIDES.get((model, category), model)
    return results_root / category / name


def find_result_jsons(model_root: Path, seed: int) -> list[Path]:
    """All route JSONs for one (model, category), seed-filtered.

    TRAP (project history): result paths carry a leading `{scenario}/{level}/`
    component and the JSONs live several levels deeper, inside a directory
    literally named `results`. Globbing at the wrong depth makes a completed
    sweep look empty — this has already produced a false "0/78" once.
    """
    suffix = f"_seed{seed}.json"
    found: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(model_root):
        if os.path.basename(dirpath) != "results":
            continue
        for fname in filenames:
            if fname.startswith("route_") and fname.endswith(suffix):
                found.append(Path(dirpath) / fname)
    return sorted(found)


def parse_path_info(filepath: Path, model_root: Path) -> dict:
    rel = filepath.relative_to(model_root)
    parts = rel.parts
    scenario = parts[0] if len(parts) > 0 else ""
    level = parts[1] if len(parts) > 1 else ""
    if level not in LEVELS:
        level = ""
    m = FILENAME_RE.match(filepath.name)
    if m:
        route_id, variant, seed = m.group(1), m.group(2), m.group(3)
    else:
        route_id, variant, seed = filepath.stem, "", ""
    return dict(scenario=scenario, level=level, route_id=route_id,
                variant=variant, seed=seed)


# --------------------------------------------------------------------------
# Row extraction
# --------------------------------------------------------------------------
def extract_row(filepath: Path, model: str, category: str,
                model_root: Path, results_root: Path) -> dict | None:
    try:
        data = load_json_with_infinity(filepath)
    except Exception as exc:  # noqa: BLE001 - surfaced in the error report
        return {"__error__": f"{filepath}: {exc}"}

    checkpoint = data.get("_checkpoint", {}) or {}
    records = checkpoint.get("records", []) or []
    record = records[0] if records else {}

    info = parse_path_info(filepath, model_root)

    labels = data.get("labels", []) or []
    values = data.get("values", []) or []
    label_to_value = {lab: values[i] for i, lab in enumerate(labels) if i < len(values)}

    scores = record.get("scores", {}) or {}
    meta = record.get("meta", {}) or {}
    infractions = record.get("infractions", {}) or {}
    ttr_dar = record.get("ttr_dar") or {}

    row = {
        "model": model,
        "category": category,
        "scenario": info["scenario"],
        "level": info["level"],
        "route_id": info["route_id"],
        "variant": info["variant"],
        "seed": info["seed"],
        "scenario_name": record.get("scenario_name", ""),
        "town_name": record.get("town_name", ""),
        "weather_id": record.get("weather_id", ""),
        "status": record.get("status", ""),
        # Per-route scores straight off the record (the authoritative values).
        "score_route": scores.get("score_route", ""),
        "score_penalty": scores.get("score_penalty", ""),
        "score_composed": scores.get("score_composed", ""),
        "route_length": meta.get("route_length", ""),
        "duration_game": meta.get("duration_game", ""),
        "duration_system": meta.get("duration_system", ""),
    }
    # Leaderboard aggregate labels (per-route file => identical to scores,
    # kept because the frozen analysis columns are named this way).
    for label, col in LABEL_COLUMNS.items():
        row[col] = label_to_value.get(label, "")

    # Raw infraction event counts (one column per key).
    n_infractions_scoring = 0
    for key in INFRACTION_KEYS:
        msgs = infractions.get(key, []) or []
        n = len(msgs) if isinstance(msgs, list) else 0
        row[f"n_{key}"] = n
        if key not in INFRACTION_SKIP_KEYS:
            n_infractions_scoring += n
    row["n_infractions_scoring"] = n_infractions_scoring

    # ttr_dar payload (record['ttr_dar'], TOP LEVEL - not nested).
    # Four models (bridgedrive, diffad, hipad, sparsedrive_v2) ran through a
    # stale statistics_manager.py fork that silently dropped the criterion
    # events, so their payload is absent. Recorded as missing, never fabricated.
    row["ttr_dar_present"] = bool(ttr_dar)
    for field in TTR_DAR_FIELDS:
        row[field] = _sanitize_value(ttr_dar.get(field))
    all_reactions = ttr_dar.get("all_reactions", []) or []
    row["num_reactions"] = ttr_dar.get("num_reactions", len(all_reactions))
    row["all_reactions"] = (
        json.dumps(_sanitize_json_floats(all_reactions)) if all_reactions else ""
    )

    # Bench2Drive Success Rate (paper Eq. 1) — see SUCCESS_RATE.md for the
    # benchmark-specific skip-set.
    row["success"] = _route_is_success(row["status"], records)

    # Actor types touched by ANY collision, cached so the OOD-attribution pass
    # needs no second read of the tree.
    row["__collision_types__"] = _collision_types(records)

    # Portable provenance: path relative to the results root, never absolute.
    row["source_relpath"] = str(filepath.relative_to(results_root))
    return row


def _route_is_success(status: str, records: list[dict]) -> bool:
    if status not in COMPLETED_STATUSES or not records:
        return False
    for record in records:
        for key, messages in (record.get("infractions", {}) or {}).items():
            if key in INFRACTION_SKIP_KEYS:
                continue
            if messages:
                return False
    return True


def _collision_types(records: list[dict]) -> list[str]:
    found = []
    for record in records:
        infractions = record.get("infractions", {}) or {}
        for key in ("collisions_vehicle", "collisions_pedestrian", "collisions_layout"):
            for message in infractions.get(key, []) or []:
                m = TYPE_RE.search(message)
                if m:
                    found.append(m.group(1))
    return found


# --------------------------------------------------------------------------
# OOD-agent collision attribution (the paper's SECOND headline metric)
# --------------------------------------------------------------------------
# Sentinel agent_type values written by the criterion when it could not
# identify the OOD actor. They are NOT blueprint ids and must not be treated as
# evidence when inferring the variant -> agent_type map: a single sentinel would
# otherwise make an otherwise-unanimous variant look "ambiguous" and silently
# drop it. ADMLP (the perception-free baseline) emits exactly 9 of these, one
# per vehicle variant, which is enough to empty the whole vehicle map.
#
# Asymmetric on purpose:
#   - map building: a sentinel counts as ABSENT
#   - row filling:  a sentinel is left in place (only an EMPTY agent_type is
#                   back-filled), so those rows still score 0 OOD hits.
#
# This is NOT parity with the authors' original collision-enrichment tool, and
# an earlier version of this comment wrongly claimed it was. That tool has no
# sentinel concept at all: it adds every non-empty agent_type to the candidate
# set, so the 9 ADMLP `"unknown"` rows would make all 9 vehicle variants look
# ambiguous and empty the entire vehicle fallback map (README 5.2). The
# exclusion here is therefore a deliberate, load-bearing DIVERGENCE, not a port.
#
# It is also the behaviour that reproduces the paper: the frozen per-model CSVs
# carry the correct, unambiguous vehicle types, and validate_against_frozen.py
# proves row by row that this generator agrees with them. Reintroducing "parity"
# would silently zero the OOD-collision metric for the four models whose
# statistics_manager fork dropped the ttr_dar payload.
SENTINEL_AGENT_TYPES = {"unknown"}


def build_agent_type_fallback(rows: list[dict]) -> tuple[dict, dict]:
    """variant -> agent_type map, per category, inferred across ALL models.

    Semantics of eval/enrich_ood_agent_collisions.py: a variant only gets a
    fallback when EVERY model that recorded a real agent_type for it agrees.
    Ambiguous variants are dropped. Order-independent, therefore deterministic.

    This cross-model inference is what supplies agent_type for the models whose
    stale statistics_manager fork dropped the ttr_dar payload.
    """
    candidates: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for row in rows:
        variant = (str(row.get("variant") or "")).strip()
        agent_type = (str(row.get("agent_type") or "")).strip()
        if not variant or not agent_type or agent_type in SENTINEL_AGENT_TYPES:
            continue
        candidates[row["category"]][variant].add(agent_type)

    inferred = {
        cat: {v: next(iter(t)) for v, t in per_variant.items() if len(t) == 1}
        for cat, per_variant in candidates.items()
    }
    ambiguous = {
        cat: {v: sorted(t) for v, t in per_variant.items() if len(t) > 1}
        for cat, per_variant in candidates.items()
    }
    ambiguous = {k: v for k, v in ambiguous.items() if v}
    return inferred, ambiguous


def attribute_ood_collisions(rows: list[dict], fallback: dict) -> Counter:
    stats = Counter()
    for row in rows:
        agent_type = (str(row.get("agent_type") or "")).strip()
        source = "record"
        if agent_type in SENTINEL_AGENT_TYPES:
            # Left in place, not back-filled (see SENTINEL_AGENT_TYPES).
            source = "sentinel"
        elif not agent_type:
            agent_type = fallback.get(row["category"], {}).get(
                (str(row.get("variant") or "")).strip(), ""
            )
            source = "fallback" if agent_type else "missing"
        row["agent_type"] = agent_type
        row["agent_type_source"] = source
        stats[source] += 1

        hit_count = sum(1 for t in row["__collision_types__"] if t == agent_type)
        row["ood_agent_collision_count"] = hit_count
        row["collided_with_ood_agent"] = hit_count > 0
        row["ood_agent_hit"] = hit_count > 0  # release-facing alias
    return stats


# --------------------------------------------------------------------------
# ood.* rename map
# --------------------------------------------------------------------------
def load_rename_map(path: Path) -> dict:
    data = json.loads(path.read_text())
    return {
        "blueprint_id_map": data.get("blueprint_id_map", {}),
        "token_map": data.get("token_map", {}),
        "version": data.get("version"),
        "binds_to": data.get("binds_to"),
    }


def apply_rename(rows: list[dict], rename: dict) -> Counter:
    """Apply the ood.* rename IN PLACE. Two independent substitutions:

        `variant`    -> `prop_raw` (verbatim on-disk token) and
                        `prop`     (released token, via token_map)
        `agent_type` -> rewritten to the released vehicle.ood.* blueprint id,
                        via blueprint_id_map, IN PLACE

    The raw result tree predates the rename and is keyed by the OLD tokens
    (`sedane`, `amv`); only those two actually change. `prop_raw` preserves the
    on-disk token, which is the join key back to the result tree and to the
    frozen analysis CSVs, so nothing is lost by renaming `prop` in place.

    `agent_type` is likewise renamed in place rather than being duplicated into
    a second `agent_type_renamed` column. One resolved blueprint id per row, in
    the released namespace, is the schema; the pre-rename id is recoverable from
    this map at any time and is not worth a redundant column.

    ORDERING IS LOAD-BEARING. This must run AFTER attribute_ood_collisions(),
    which matches `agent_type` against the actor types parsed out of the raw
    collision messages. Those messages carry the OLD vendor ids, so renaming
    first would drop every vehicle OOD hit to zero — silently, since the column
    would still look well-formed. Renaming afterwards cannot move a number: the
    hit counts are already computed and frozen on the row by then. The loop below
    raises if it is reached before attribution, rather than relying on a future
    reader noticing the ordering.
    """
    token_map = rename["token_map"]
    bp_map = rename["blueprint_id_map"]
    stats = Counter()
    for row in rows:
        if "ood_agent_collision_count" not in row:
            raise RuntimeError(
                "apply_rename() ran before attribute_ood_collisions(); the OOD "
                "collision metric would be computed against renamed ids that "
                "never appear in the raw collision messages. Fix the call order."
            )
        raw = str(row.get("variant") or "")
        row["prop_raw"] = raw
        renamed = token_map.get(raw, raw)
        row["prop"] = renamed
        stats["prop_renamed" if renamed != raw else "prop_unchanged"] += 1

        at = str(row.get("agent_type") or "")
        at_renamed = bp_map.get(at, at)
        row["agent_type"] = at_renamed
        if at_renamed != at:
            stats["agent_type_renamed"] += 1

    # No pre-rename id may survive into the released table. This is the check
    # that keeps a trademarked vendor id out of a published safety benchmark.
    leaked = sorted({r["agent_type"] for r in rows if r["agent_type"] in bp_map})
    if leaked:
        raise RuntimeError(f"pre-rename blueprint ids survived the rename: {leaked}")
    leaked_props = sorted({r["prop"] for r in rows if r["prop"] in token_map
                           and token_map[r["prop"]] != r["prop"]})
    if leaked_props:
        raise RuntimeError(f"pre-rename prop tokens survived the rename: {leaked_props}")
    return stats


# --------------------------------------------------------------------------
# Output schema
# --------------------------------------------------------------------------
COLUMNS = [
    # identity ------------------------------------------------------------
    "model", "category", "scenario", "route_id", "level", "prop", "seed",
    "prop_raw", "variant", "scenario_name", "town_name", "weather_id",
    # primary outcome -----------------------------------------------------
    "status", "success",
    "score_composed", "score_route", "score_penalty",
    "driving_score", "route_completion", "infraction_penalty",
    # OOD-collision attribution (2nd headline metric) ---------------------
    # `agent_type` is POST-rename (see apply_rename): one resolved blueprint id
    # per row, in the released vehicle.ood.* namespace. There is deliberately no
    # second `agent_type_renamed` column.
    "ood_agent_hit", "ood_agent_collision_count",
    "collided_with_ood_agent",
    "agent_type", "agent_type_source",
    # infraction counts ---------------------------------------------------
    *[f"n_{k}" for k in INFRACTION_KEYS],
    "n_infractions_scoring",
    # leaderboard infraction rates ----------------------------------------
    "collisions_pedestrians", "collisions_vehicles", "collisions_layout",
    "off_road_infractions",
    # run meta ------------------------------------------------------------
    "route_length", "duration_game", "duration_system",
    # secondary metrics (unvalidated; absent for the stale-fork models) ----
    "ttr_dar_present",
    *TTR_DAR_FIELDS[:4], *TTR_DAR_FIELDS[5:],  # agent_type already above
    "num_reactions", "all_reactions",
    # provenance ----------------------------------------------------------
    "source_relpath",
]
# de-duplicate while preserving order (collisions_layout appears as both a
# raw count `n_collisions_layout` and the leaderboard rate `collisions_layout`)
COLUMNS = list(dict.fromkeys(COLUMNS))

# --------------------------------------------------------------------------
# Parquet typing.
#
# The CSV is written verbatim from the extraction so it reproduces the frozen
# analysis CSVs cell-for-cell (including the literal token "Infinity", which
# occurs 1367 times in the secondary-metric columns and must not be lost).
# The parquet gets a real schema instead: numeric columns become float64 with
# IEEE inf preserved, flags become nullable booleans, everything else string.
# --------------------------------------------------------------------------
BOOL_COLS = [
    "success", "ood_agent_hit", "collided_with_ood_agent",
    "ttr_dar_present", "reaction_detected",
]
NUMERIC_COLS = [
    "seed",
    "score_composed", "score_route", "score_penalty",
    "driving_score", "route_completion", "infraction_penalty",
    "ood_agent_collision_count",
    *[f"n_{k}" for k in INFRACTION_KEYS], "n_infractions_scoring",
    "collisions_pedestrians", "collisions_vehicles", "collisions_layout",
    "off_road_infractions",
    "route_length", "duration_game", "duration_system",
    "ttr", "dar", "ttc_at_reaction",
    "t_obs_frame", "t_react_frame", "closing_velocity",
    "reaction_value", "reaction_threshold", "v_start", "v_end",
    "final_distance", "final_closing_velocity", "final_ttc",
    "num_reactions",
]


def typed_frame(df):
    """Return a copy of `df` with an explicit, parquet-safe dtype schema."""
    import pandas as pd

    out = df.copy()
    for col in NUMERIC_COLS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    for col in BOOL_COLS:
        if col not in out.columns:
            continue
        s = out[col].map(
            lambda v: True if str(v).strip().lower() == "true"
            else (False if str(v).strip().lower() == "false" else None)
        )
        out[col] = s.astype("boolean")
    for col in out.columns:
        if col in NUMERIC_COLS or col in BOOL_COLS:
            continue
        out[col] = out[col].astype(str).replace({"nan": "", "None": ""})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results-root", required=True, type=Path,
                    help="Root of the raw result tree (READ-ONLY). "
                         "Layout: <root>/<category>/<model>/<scenario>/<level>/**/results/*.json")
    ap.add_argument("--rename-map", type=Path, default=DEFAULT_RENAME_MAP,
                    help="ood.* rename map (default: the copy bundled beside "
                         "this script, so the release artifact is reproducible "
                         "from this repository alone)")
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--results-root-label", default=None,
                    help="What to record as `results_root` in meta.json. "
                         "Defaults to '<results_root>/<dirname>', because the "
                         "absolute path of the authors' 766 GB result tree is "
                         "machine-specific and does not belong in a published "
                         "artifact. Pass the real path if you want it recorded.")
    ap.add_argument("--models", nargs="+", default=None,
                    help="Subset of model keys (default: all 18). A subset run "
                         "narrows the cross-model agent_type fallback map, so it "
                         "is for development only — the release artifact needs all.")
    ap.add_argument("--categories", nargs="+", default=list(CATEGORIES))
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--basename", default="ood_perceptionbench_records")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    results_root: Path = args.results_root.resolve()
    if not results_root.is_dir():
        print(f"ERROR: results root not found: {results_root}", file=sys.stderr)
        return 2

    models = args.models or ALL_MODELS
    unknown = [m for m in models if m not in ALL_MODELS]
    if unknown:
        print(f"ERROR: unknown model keys: {unknown}", file=sys.stderr)
        return 2
    partial = set(models) != set(ALL_MODELS) or set(args.categories) != set(CATEGORIES)

    all_rows: list[dict] = []
    errors: list[str] = []
    counts: dict[str, int] = {}

    for category in args.categories:
        for model in models:
            model_root = result_dir_for(results_root, model, category)
            if not model_root.is_dir():
                errors.append(f"MISSING RESULT DIR: {model}/{category} -> {model_root}")
                counts[f"{model}/{category}"] = 0
                continue
            files = find_result_jsons(model_root, args.seed)
            with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
                rows = list(pool.map(
                    lambda f: extract_row(f, model, category, model_root, results_root),
                    files))
            ok = []
            for r in rows:
                if r is None:
                    continue
                if "__error__" in r:
                    errors.append(r["__error__"])
                    continue
                ok.append(r)
            all_rows.extend(ok)
            counts[f"{model}/{category}"] = len(ok)
            exp = EXPECTED_ROUTES.get(category)
            flag = "" if exp is None or len(ok) == exp else f"  <-- EXPECTED {exp}"
            print(f"  {model:18s} {category:11s} {len(ok):4d}{flag}", flush=True)

    if not all_rows:
        print("ERROR: no rows produced", file=sys.stderr)
        return 1

    # ---- cross-model agent_type inference, then OOD attribution ----------
    fallback, ambiguous = build_agent_type_fallback(all_rows)
    if ambiguous:
        print(f"WARNING: ambiguous variant->agent_type mappings dropped: "
              f"{ {c: len(v) for c, v in ambiguous.items()} }")
    at_stats = attribute_ood_collisions(all_rows, fallback)

    # ---- ood.* rename ---------------------------------------------------
    rename = load_rename_map(args.rename_map)
    rn_stats = apply_rename(all_rows, rename)

    for row in all_rows:
        row.pop("__collision_types__", None)

    # ---- reconciliation --------------------------------------------------
    recon = []
    for category in args.categories:
        exp = EXPECTED_ROUTES.get(category)
        for model in models:
            got = counts.get(f"{model}/{category}", 0)
            recon.append({"model": model, "category": category,
                          "rows": got, "expected": exp,
                          "ok": (exp is None or got == exp)})
    n_bad = sum(1 for r in recon if not r["ok"])

    import pandas as pd  # imported late so --help works without pandas

    df = pd.DataFrame(all_rows)
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""
    extra = [c for c in df.columns if c not in COLUMNS]
    df = df[COLUMNS + extra]
    df = df.sort_values(["model", "category", "scenario", "level", "route_id",
                         "prop", "seed"], kind="mergesort").reset_index(drop=True)

    print(f"\nRows: {len(df)}   models: {df['model'].nunique()}   "
          f"categories: {df['category'].nunique()}")
    print(f"agent_type source: {dict(at_stats)}")
    print(f"rename: {dict(rn_stats)}")
    print(f"row-count reconciliation: {len(recon) - n_bad}/{len(recon)} cells OK")
    if errors:
        print(f"ERRORS ({len(errors)}):")
        for e in errors[:20]:
            print("   ", e)

    if args.no_write:
        return 0

    out_dir: Path = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{args.basename}_{RECORDS_VERSION}.csv"
    pq_path = out_dir / f"{args.basename}_{RECORDS_VERSION}.parquet"

    df.to_csv(csv_path, index=False)
    try:
        typed_frame(df).to_parquet(pq_path, index=False)
        pq_written = True
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: parquet not written ({exc}); install pyarrow", file=sys.stderr)
        pq_written = False

    def _sha(p: Path) -> str:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    # Provenance: bind the artifacts to the exact bytes of the generator and of
    # the rename map that produced them. check_meta.py re-derives both.
    results_root_label = (args.results_root_label
                          or f"<results_root>/{results_root.name}")

    meta = {
        "schema": RECORDS_SCHEMA,
        "version": RECORDS_VERSION,
        "binds_to": BINDS_TO,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "records/build_records.py",
        "generator_sha256": _sha(Path(__file__).resolve()),
        "rename_map_sha256": _sha(args.rename_map.resolve()),
        "seed": args.seed,
        "seed_note": "Seed 42 only. Every number in the paper is seed 42; the "
                     "multiseed tree is a paper-side robustness appendix and is "
                     "NOT part of this release.",
        "results_root": results_root_label,
        "rename_map_version": rename.get("version"),
        "rename_map_binds_to": rename.get("binds_to"),
        "partial_run": partial,
        "models": sorted(models),
        "e2e_models_n": len([m for m in models if m in E2E_MODELS]),
        "ceiling_models_excluded_from_stats": CEILING_MODELS,
        "categories": list(args.categories),
        "n_rows": int(len(df)),
        "n_columns": int(len(df.columns)),
        "columns": list(df.columns),
        "expected_routes_per_category": EXPECTED_ROUTES,
        "row_count_reconciliation": recon,
        "row_count_cells_ok": len(recon) - n_bad,
        "row_count_cells_total": len(recon),
        "agent_type_source_counts": dict(at_stats),
        "agent_type_fallback_map": fallback,
        "agent_type_ambiguous_dropped": ambiguous,
        "rename_stats": dict(rn_stats),
        "ttr_dar_present_by_model": (
            df.groupby("model")["ttr_dar_present"].mean().round(4).to_dict()
        ),
        "errors": errors,
        "artifacts": {
            "csv": {"path": csv_path.name, "sha256": _sha(csv_path),
                    "bytes": csv_path.stat().st_size},
        },
    }
    if pq_written:
        meta["artifacts"]["parquet"] = {
            "path": pq_path.name, "sha256": _sha(pq_path),
            "bytes": pq_path.stat().st_size,
        }
    (out_dir / f"{args.basename}_{RECORDS_VERSION}.meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=False) + "\n")

    print(f"\nWrote {csv_path}")
    if pq_written:
        print(f"Wrote {pq_path}")
    print(f"Wrote {out_dir / f'{args.basename}_{RECORDS_VERSION}.meta.json'}")
    return 0 if (not errors and n_bad == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
