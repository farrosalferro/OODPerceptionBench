"""
common.py — the shared contract for the static-asset import pipeline.

Every stage script imports from here so that naming, paths, the anchor
constants, the JSON verdict envelope, and the state file stay consistent.

Artifact version: v0.9 — corresponds to arXiv v1 of the OOD-PerceptionBench paper.

Machine-specific locations are NOT hardcoded here. They resolve through
``site_config`` (see ``site_config.example.yaml``), which has no defaults: a
name below that depends on your machine raises a message naming the config key
the first time it is read. That is deliberate — a wrong-but-plausible default is
how an asset silently ends up cooked into a CARLA nobody launches.

This module is pure stdlib + optional PyYAML; it must import cleanly under the
CARLA client interpreter, the CARLA build interpreter, AND Blender's bundled
python. Do NOT add heavy deps here.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

# site_config lives one directory up from this stage package.
_STAGES_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _STAGES_ROOT not in sys.path:
    sys.path.insert(0, _STAGES_ROOT)
import site_config  # noqa: E402

# --------------------------------------------------------------------------
# Classification anchor. L=X, W=Y, H=Z.
# --------------------------------------------------------------------------
ANCHOR_LWH = (2.3734, 2.8706, 3.5695)   # trafficwarning, the single static reference
VISUAL_DELTA_MAX = 0.20                  # weakest-link 20% rule; no ambiguous band
CARLA_VERSION_TAG = "0.9.15-dirty"

# UE collision-mesh name prefixes — exclude these when measuring RENDER dims.
COLLISION_MESH_PREFIXES = ("UCX_", "UBX_", "USP_", "UCP_", "MCDCX_")

# Master material (built once).
MASTER_MATERIAL_PATH = "/Game/OODProps/M_OODPropMaster"

# shift_type (manifest) -> on-disk directory name. NB: geometric -> "geometry_shift".
SHIFT_DIR = {"visual": "visual_shift", "geometric": "geometry_shift"}

# --------------------------------------------------------------------------
# Site-dependent names. Each entry maps a legacy module attribute to the
# site_config key it now comes from, plus an optional suffix appended to it.
# Resolution is LAZY (PEP 562 module __getattr__) so that importing this module
# for its pure helpers — make_verdict, names_for, the manifest loader — never
# requires a configured site.
# --------------------------------------------------------------------------
_SITE = {
    # toolchain
    "PY_CLIENT":             ("client_python", ""),
    "CONDA_ROOT":             ("conda_root", ""),
    "COOK_CONDA_ENV":         ("build_python_env", ""),
    "BLENDER":                ("blender_bin", ""),
    "ANCHOR_FBX":             ("anchor_fbx", ""),
    "UE4_ROOT":               ("ue_root", ""),
    "UE4_EDITOR_CMD":         ("ue_root", "/Engine/Binaries/Linux/UE4Editor-Cmd"),
    # CARLA source build: Content + `make package`
    "CARLA_UE_ROOT":          ("carla_src", ""),
    "UPROJECT":               ("carla_src", "/Unreal/CarlaUE4/CarlaUE4.uproject"),
    "CARLA_CONTENT":          ("carla_src", "/Unreal/CarlaUE4/Content"),
    "CARLA_DIST":             ("carla_src", "/Dist"),
    # Packaged CARLA: install target + launched server. NOT the same as carla_src.
    "CARLA_SERVER_ROOT":      ("carla_pkg", ""),
    "CARLA_SERVER_SH":        ("carla_pkg", "/CarlaUE4.sh"),
    "CARLA_IMPORT_DIR":       ("carla_pkg", "/Import"),
    "CARLA_IMPORT_ASSETS_SH": ("carla_pkg", "/ImportAssets.sh"),
    # benchmark repo + working dirs
    "REPO_ROOT":              ("bench_root", ""),
    "B2D":                    ("bench_root", ""),
    "PERCEPTION":             ("bench_root", ""),
    "IMPORT_AUTOMATION_DIR":  ("work_root", ""),
    "RENDERS_DIR":            ("work_root", "/renders"),
    "STATE_DIR":              ("work_root", "/state"),
    "ROUTE_TEMPLATE":         ("bench_root", "/routes/templates/static/route_2509_template.xml"),
    "RUN_TEMPLATE":           ("bench_root", "/routes/templates/static/run_template.sh"),
    "ROUTE_DATA_DIR":         ("work_root", "/routes/static"),          # + /<shift_dir>/
    "RUN_SCRIPT_DIR":         ("work_root", "/runs/static"),            # + /<shift_dir>/
    "SAVE_ROOT":              ("results_root", "/static"),              # + /<shift_dir>/
}

# Optional second install target (only needed when you cook on one machine and
# evaluate on another). Unset -> None rather than an error.
_SITE_OPTIONAL = {
    "SECONDARY_CARLA_ROOT":   "secondary_carla_pkg",
    "SECONDARY_SSH_HOST":     "secondary_ssh_host",
    "SECONDARY_PARTITIONS":   "secondary_submit_partitions",
}

# This stage package's own directory — not site-dependent.
STAGES_DIR = os.path.dirname(os.path.abspath(__file__))


def __getattr__(name):  # PEP 562
    if name in _SITE:
        key, suffix = _SITE[name]
        return site_config.get(key) + suffix
    if name in _SITE_OPTIONAL:
        return site_config.get_optional(_SITE_OPTIONAL[name])
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__():
    return sorted(list(globals()) + list(_SITE) + list(_SITE_OPTIONAL))

# --------------------------------------------------------------------------
# Naming. Three forms are in play:
#   asset_name  CamelCase     -> SM name, Content folder, Package name, route id suffix(lowered)
#   blueprint   lower, joined -> static.prop.<asset_name.lower()>
#   slug        snake (src    -> checkpoint json + save-dir grouping (matches worked example)
#               dir basename)
# --------------------------------------------------------------------------
@dataclass
class Names:
    asset_name: str
    blueprint_id: str
    sm_name: str
    content_root: str          # /Game/<asset_name>
    sm_object_path: str        # /Game/<asset_name>/Static/Other/<asset_name>/SM_<asset_name>
    sm_package_path: str       # ...SM_<asset_name>.SM_<asset_name>   (Package.json `path`)
    package_name: str
    dist_tar: str              # <asset_name>_0.9.15-dirty.tar.gz
    route_id: str
    route_filename: str        # route_2509_<lower>.xml
    slug: str


def names_for(asset_name: str, source_dir: str = "") -> Names:
    a = asset_name
    low = a.lower()
    content_root = f"/Game/{a}"
    sm_obj = f"/Game/{a}/Static/Other/{a}/SM_{a}"
    slug = os.path.basename(os.path.normpath(source_dir)) if source_dir else low
    # many assets keep the fbx in a generic .../source (or src) subdir — that makes a
    # useless, collision-prone slug; fall back to the asset name in that case.
    if slug.lower() in ("source", "src", "", "."):
        slug = low
    return Names(
        asset_name=a,
        blueprint_id=f"static.prop.{low}",
        sm_name=f"SM_{a}",
        content_root=content_root,
        sm_object_path=sm_obj,
        sm_package_path=f"{sm_obj}.SM_{a}",
        package_name=a,
        dist_tar=f"{a}_{CARLA_VERSION_TAG}.tar.gz",
        route_id=f"route_2509_{low}",
        route_filename=f"route_2509_{low}.xml",
        slug=slug,
    )


def validate_asset_name(asset_name: str) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", asset_name):
        raise ValueError(
            f"asset_name must be CamelCase alphanumerics (no spaces/underscores): {asset_name!r}"
        )


# --------------------------------------------------------------------------
# Manifest (input contract, ).
# --------------------------------------------------------------------------
@dataclass
class Manifest:
    asset_name: str
    source_dir: str
    shift_type: str                       # "visual" | "geometric"
    target_dims: dict                     # {mode: match_anchor} | {mode: absolute, L,W,H}
    fbx: Optional[str] = None
    textures_dir: str = "textures"
    allow_nonuniform: bool = False
    ingest_secondary: bool = False        # optional: ALSO install the cooked tar into secondary_carla_pkg
    front_axis: Optional[str] = None
    path: Optional[str] = None            # filled by loader

    def validate(self) -> None:
        validate_asset_name(self.asset_name)
        if self.shift_type not in ("visual", "geometric"):
            raise ValueError(f"shift_type must be visual|geometric, got {self.shift_type!r}")
        if not os.path.isdir(self.source_dir):
            raise ValueError(f"source_dir does not exist: {self.source_dir}")
        mode = (self.target_dims or {}).get("mode")
        if self.shift_type == "visual":
            if mode != "match_anchor":
                raise ValueError("visual shift requires target_dims.mode=match_anchor")
        else:  # geometric
            if mode != "absolute":
                raise ValueError("geometric shift requires target_dims.mode=absolute")
            for k in ("L", "W", "H"):
                v = self.target_dims.get(k)
                if not isinstance(v, (int, float)) or v <= 0:
                    raise ValueError(f"geometric target_dims.{k} must be a positive number (meters)")
        if self.front_axis is not None and self.front_axis not in (
            "+X", "-X", "+Y", "-Y", "+Z", "-Z", "X", "Y", "Z"
        ):
            raise ValueError(f"front_axis must be a signed axis like -Y, got {self.front_axis!r}")

    @property
    def shift_dir(self) -> str:
        return SHIFT_DIR[self.shift_type]


def load_manifest(path: str) -> Manifest:
    with open(path) as f:
        text = f.read()
    data = None
    if path.endswith((".yaml", ".yml")):
        import yaml
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    known = {f for f in Manifest.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown manifest keys: {sorted(unknown)}")
    m = Manifest(
        asset_name=data["asset_name"],
        source_dir=data["source_dir"],
        shift_type=data["shift_type"],
        target_dims=data["target_dims"],
        fbx=data.get("fbx"),
        textures_dir=data.get("textures_dir", "textures"),
        allow_nonuniform=bool(data.get("allow_nonuniform", False)),
        ingest_secondary=bool(data.get("ingest_secondary", False)),
        front_axis=data.get("front_axis"),
    )
    m.path = os.path.abspath(path)
    m.validate()
    return m


# --------------------------------------------------------------------------
# JSON verdict envelope — every stage script emits exactly this to its --out file.
# --------------------------------------------------------------------------
def make_verdict(stage: str, ok: bool, data: dict | None = None, error: str | None = None) -> dict:
    return {
        "stage": stage,
        "ok": bool(ok),
        "data": data or {},
        "error": error,
        "ts": time.time(),
    }


def write_verdict(out_path: str, verdict: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(verdict, f, indent=2)


def read_verdict(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Resumable state file. One JSON per asset under <work_root>/state.
# The stage names correspond to the checkpoints in import_procedure_static.md.
# --------------------------------------------------------------------------
STAGE_ORDER = [
    "manifest_texture",   # manifest validation + texture classification
    "blender",            # align/scale/export      (CHECKPOINT 1)
    "ue_import",          # import/material/collision (CHECKPOINT 2)
    "cook_package",       # cook + install          (CHECKPOINT 5)
    "ingest_secondary",   # OPTIONAL second install target; inert unless configured
    "probe",              # spawn/orientation/blocking (CHECKPOINT 4)
    "route_check",        # route run               (CHECKPOINT 6)
]


def _state_dir() -> str:
    return site_config.get("work_root") + "/state"


def state_path(asset_name: str) -> str:
    return os.path.join(_state_dir(), f"{asset_name}.state.json")


def load_or_init_state(asset_name: str, manifest_path: str) -> dict:
    p = state_path(asset_name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {
        "asset_name": asset_name,
        "manifest_path": manifest_path,
        "stages": {s: {"done": False} for s in STAGE_ORDER},
    }


def save_state(state: dict) -> None:
    os.makedirs(_state_dir(), exist_ok=True)
    p = state_path(state["asset_name"])
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, p)


def mark_stage(state: dict, stage: str, *, done: bool, verdict: dict | None = None,
               artifacts: dict | None = None, approved: bool | None = None) -> dict:
    st = state["stages"].setdefault(stage, {})
    st["done"] = done
    if verdict is not None:
        st["verdict"] = verdict
    if artifacts is not None:
        st["artifacts"] = {**st.get("artifacts", {}), **artifacts}
    if approved is not None:
        st["approved"] = approved
    save_state(state)
    return state
