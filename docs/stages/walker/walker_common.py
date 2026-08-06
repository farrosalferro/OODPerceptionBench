"""
walker_common.py — shared contract for the PEDESTRIAN validation pipeline.

This is the walker analogue of the static pipeline's common.py. It does NOT fork that file:
it imports the genuinely-shared, location-independent helpers (verdict envelope, naming
validator) plus the site-resolved toolchain paths, and adds only the pedestrian-specific
naming, paths, the 4-scenario map, the test manifest, and a separate resumable state file.

Artifact version: v0.9 — corresponds to arXiv v1 of the OOD-PerceptionBench paper.

VALIDATION scope: this pipeline assumes the walker is ALREADY cooked + registered as
`walker.pedestrian.<name>`. It does not import/cook/register anything — that is the manual
procedure in import_procedure_walker.md.

Machine-specific locations resolve through ``site_config``; there are no defaults.
Pure stdlib + optional PyYAML (for the manifest).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

# --------------------------------------------------------------------------
# Reuse the static pipeline's shared contract (don't fork it).
# stages/walker/walker_common.py -> stages/ -> stages/static
# --------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))                 # .../stages/walker
_STAGES_ROOT = os.path.dirname(_HERE)                              # .../stages
_STATIC_STAGES = os.path.join(_STAGES_ROOT, "static")
for _p in (_STAGES_ROOT, _STATIC_STAGES):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import site_config  # noqa: E402
import common as sc  # noqa: E402  (static common.py)

# Re-export the location-independent helpers the pedestrian stages need.
make_verdict = sc.make_verdict
write_verdict = sc.write_verdict
read_verdict = sc.read_verdict
validate_camelcase = sc.validate_asset_name        # CamelCase-alphanumerics validator
CARLA_VERSION_TAG = sc.CARLA_VERSION_TAG           # "0.9.15-dirty"

# Site-dependent names are delegated LAZILY to the static contract (see __getattr__ below), so
# that importing this module for its pure helpers never requires a configured site. The set is:
#   PY_CLIENT CONDA_ROOT COOK_CONDA_ENV
#   CARLA_SERVER_ROOT CARLA_SERVER_SH CARLA_IMPORT_DIR CARLA_IMPORT_ASSETS_SH
#   CARLA_UE_ROOT CARLA_DIST CARLA_CONTENT REPO_ROOT B2D
_DELEGATED = (
    "PY_CLIENT", "CONDA_ROOT", "COOK_CONDA_ENV",
    "CARLA_SERVER_ROOT", "CARLA_SERVER_SH", "CARLA_IMPORT_DIR", "CARLA_IMPORT_ASSETS_SH",
    "CARLA_UE_ROOT", "CARLA_DIST", "CARLA_CONTENT", "REPO_ROOT", "B2D",
    "SECONDARY_CARLA_ROOT", "SECONDARY_SSH_HOST", "SECONDARY_PARTITIONS",
)

# --------------------------------------------------------------------------
# Pedestrian path names, resolved from site_config (see site_config.example.yaml).
# --------------------------------------------------------------------------
PED_STAGES_DIR = _HERE

_PED_SITE = {
    "PED_RUNS_DIR":       ("work_root", "/runs/pedestrian"),
    "PED_STATE_DIR":      ("work_root", "/state/pedestrian"),
    "PED_ROUTE_DATA_DIR": ("work_root", "/routes/pedestrian"),           # + /<subfolder>/...
    "PED_RUN_SCRIPT_DIR": ("work_root", "/runs/pedestrian"),             # + /<subfolder>/run_<slug>.sh
    "PED_RUN_TEMPLATE":   ("bench_root", "/routes/templates/pedestrian/run_template.sh"),
    "PED_SAVE_ROOT":      ("results_root", "/pedestrian"),               # + /<subfolder>/
}


def __getattr__(name):  # PEP 562
    if name in _PED_SITE:
        key, suffix = _PED_SITE[name]
        return site_config.get(key) + suffix
    if name in _DELEGATED:
        return getattr(sc, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__():
    return sorted(list(globals()) + list(_PED_SITE) + list(_DELEGATED))


def _site(name: str) -> str:
    """Resolve one of the _PED_SITE names from inside this module (module __getattr__
    does not fire for bare global lookups within the module itself)."""
    key, suffix = _PED_SITE[name]
    return site_config.get(key) + suffix

# --------------------------------------------------------------------------
# The 4 pedestrian scenarios in scope. class_name == route-XML <scenario type="..."> and the
# scenario_runner class name; subfolder == route dir; blueprint_attr == the route-XML attribute
# read by the scenario (all 4 use pedestrian_blueprint; ObstacleApproachingEgo would use
# obstacle_blueprint but is out of scope).
# --------------------------------------------------------------------------
PED_SCENARIOS = [
    {"class_name": "PedestrianCrossingModified",         "subfolder": "pedestrian_crossing",               "blueprint_attr": "pedestrian_blueprint"},
    {"class_name": "DynamicObjectCrossingModified",      "subfolder": "dynamic_object_crossing",           "blueprint_attr": "pedestrian_blueprint"},
    {"class_name": "ParkingCrossingPedestrianModified",  "subfolder": "parking_crossing_pedestrian",       "blueprint_attr": "pedestrian_blueprint"},
    {"class_name": "VehicleTurningRoutePedestrianModified", "subfolder": "vehicle_turning_route_pedestrian", "blueprint_attr": "pedestrian_blueprint"},
]
PED_SCENARIO_BY_CLASS = {s["class_name"]: s for s in PED_SCENARIOS}
DEFAULT_SCENARIOS = [s["class_name"] for s in PED_SCENARIOS]

# Resumable state. cook_ingest is OPTIONAL (only when the manifest sets cook: true — i.e. the
# walker is not yet cooked into the /media build); spawn_smoke + route_check are the test core.
PED_STAGE_ORDER = ["cook_ingest", "spawn_smoke", "route_check"]

BLUEPRINT_RE = re.compile(r"^walker\.pedestrian\.[A-Za-z0-9_]+$")


# --------------------------------------------------------------------------
# Naming. The registered walker id is whatever the manual import's Package.json
# `walkers[].name` was (lowercased) — e.g. folder delivery-robot -> deliveryrobot —
# so the blueprint_id is taken VERBATIM from the manifest, never derived from walker_name.
#   walker_name  CamelCase   -> labels, filenames base, runs/<walker_name>, state file
#   blueprint_id verbatim    -> walker.pedestrian.<id>   (validated live in Stage A)
#   walker_id    last segment-> <id>
#   slug         <id>.lower() -> route filename suffix + checkpoint json + save-dir grouping
# --------------------------------------------------------------------------
@dataclass
class WalkerNames:
    walker_name: str
    blueprint_id: str
    walker_id: str          # last segment of blueprint_id == Package.json walkers[].name
    slug: str               # walker_id.lower() — route filename suffix + checkpoint
    package_name: str       # the Content folder / `make package` package name (CamelCase)
    dist_tar: str           # <package_name>_0.9.15-dirty.tar.gz (cook output)


def walker_names_for(walker_name: str, blueprint_id: str, package_name: str = "") -> WalkerNames:
    wid = blueprint_id.split(".")[-1]
    pkg = package_name or walker_name
    return WalkerNames(
        walker_name=walker_name,
        blueprint_id=blueprint_id,
        walker_id=wid,
        slug=wid.lower(),
        package_name=pkg,
        dist_tar=f"{pkg}_{CARLA_VERSION_TAG}.tar.gz",
    )


def content_dir(package_name: str) -> str:
    """The UE-source Content folder for a package (where the user authored the walker)."""
    return os.path.join(sc.CARLA_CONTENT, package_name)


def package_json_path(package_name: str) -> str:
    return os.path.join(content_dir(package_name), "Config", f"{package_name}.Package.json")


def route_filename(template_route_id: str, slug: str) -> str:
    """route_<N>_template -> route_<N>_<slug>.xml (keeps the real benchmark route number)."""
    m = re.match(r"^(route_\d+)_template$", template_route_id)
    if not m:
        raise ValueError(f"template route id must look like route_<N>_template, got {template_route_id!r}")
    return f"{m.group(1)}_{slug}.xml"


def validate_blueprint_id(blueprint_id: str) -> None:
    if not BLUEPRINT_RE.match(blueprint_id):
        raise ValueError(
            f"blueprint_id must look like walker.pedestrian.<name> (alphanumerics/underscore), "
            f"got {blueprint_id!r}"
        )


# --------------------------------------------------------------------------
# Pedestrian test manifest (input contract — minimal, test-only).
# --------------------------------------------------------------------------
@dataclass
class WalkerManifest:
    walker_name: str
    blueprint_id: str
    shift_type: str = "visual"            # visual | geometric — labels/output only
    scenarios: Optional[List[str]] = None  # default = all 4 PED_SCENARIOS
    cook: bool = False                    # if true, cook+ingest the package before testing
    package_name: Optional[str] = None    # Content folder / make-package name (default = walker_name)
    path: Optional[str] = None             # filled by loader

    def validate(self) -> None:
        validate_camelcase(self.walker_name)
        validate_blueprint_id(self.blueprint_id)
        if self.shift_type not in ("visual", "geometric"):
            raise ValueError(f"shift_type must be visual|geometric, got {self.shift_type!r}")
        unknown = [s for s in self.scenario_list if s not in PED_SCENARIO_BY_CLASS]
        if unknown:
            raise ValueError(
                f"unknown scenario class(es): {unknown}; valid: {sorted(PED_SCENARIO_BY_CLASS)}"
            )
        if self.package_name is not None:
            validate_camelcase(self.package_name)
        # If cooking, the user must have authored the package in the UE editor already.
        if self.cook:
            pj = package_json_path(self.effective_package_name)
            if not os.path.isfile(pj):
                raise ValueError(
                    f"cook: true but the package config was not found: {pj} — author the walker "
                    f"in the UE editor first (Content/{self.effective_package_name}/ with a "
                    f"walkers-keyed Package.json), or set cook: false to test an already-cooked walker"
                )

    @property
    def scenario_list(self) -> List[str]:
        return list(self.scenarios) if self.scenarios else list(DEFAULT_SCENARIOS)

    @property
    def effective_package_name(self) -> str:
        return self.package_name or self.walker_name


def load_walker_manifest(path: str) -> WalkerManifest:
    with open(path) as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        import yaml  # the client interpreter has pyyaml
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    known = {f for f in WalkerManifest.__dataclass_fields__ if f != "path"}  # type: ignore[attr-defined]
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown manifest keys: {sorted(unknown)}; valid: {sorted(known)}")
    m = WalkerManifest(
        walker_name=data["walker_name"],
        blueprint_id=data["blueprint_id"],
        shift_type=data.get("shift_type", "visual"),
        scenarios=data.get("scenarios"),
        cook=bool(data.get("cook", False)),
        package_name=data.get("package_name"),
    )
    m.path = os.path.abspath(path)
    m.validate()
    return m


# --------------------------------------------------------------------------
# Per-walker / per-scenario path helpers.
# --------------------------------------------------------------------------
def ped_work_dir(walker_name: str) -> str:
    return os.path.join(_site("PED_RUNS_DIR"), walker_name)


def route_template_path(subfolder: str) -> str:
    return os.path.join(_site("PED_ROUTE_DATA_DIR"), subfolder, "route_template.xml")


def route_out_path(subfolder: str, fname: str) -> str:
    return os.path.join(_site("PED_ROUTE_DATA_DIR"), subfolder, fname)


def run_out_path(subfolder: str, slug: str) -> str:
    return os.path.join(_site("PED_RUN_SCRIPT_DIR"), subfolder, f"run_{slug}.sh")


def save_dir(subfolder: str) -> str:
    return os.path.join(_site("PED_SAVE_ROOT"), subfolder)


def checkpoint_path(subfolder: str, slug: str) -> str:
    return os.path.join(_site("PED_SAVE_ROOT"), subfolder, f"{slug}.json")


# --------------------------------------------------------------------------
# Resumable state file (separate dir from the static pipeline).
# --------------------------------------------------------------------------
def ped_state_path(walker_name: str) -> str:
    return os.path.join(_site("PED_STATE_DIR"), f"{walker_name}.state.json")


def load_or_init_ped_state(walker_name: str, manifest_path: str) -> dict:
    p = ped_state_path(walker_name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {
        "walker_name": walker_name,
        "manifest_path": manifest_path,
        "stages": {s: {"done": False} for s in PED_STAGE_ORDER},
    }


def save_ped_state(state: dict) -> None:
    os.makedirs(_site("PED_STATE_DIR"), exist_ok=True)
    p = ped_state_path(state["walker_name"])
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, p)


def ped_mark_stage(state: dict, stage: str, *, done: bool, verdict: dict | None = None,
                   artifacts: dict | None = None, approved: bool | None = None) -> dict:
    st = state["stages"].setdefault(stage, {})
    st["done"] = done
    if verdict is not None:
        st["verdict"] = verdict
    if artifacts is not None:
        st["artifacts"] = {**st.get("artifacts", {}), **artifacts}
    if approved is not None:
        st["approved"] = approved
    state["ts"] = time.time()
    save_ped_state(state)
    return state
