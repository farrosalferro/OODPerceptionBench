"""
vehicle_common.py — shared contract for the VEHICLE validation pipeline.

This is the vehicle analogue of walker_common.py (which is itself the analogue of the static
pipeline's common.py). It does NOT fork either file: it imports the genuinely-shared,
location-independent helpers plus the site-resolved toolchain paths from the static contract and
adds only the vehicle-specific naming, paths, the 6-scenario map, the test manifest, and a
separate resumable state file.

Artifact version: v0.9 — corresponds to arXiv v1 of the OOD-PerceptionBench paper.

VALIDATION scope: this pipeline assumes the vehicle is ALREADY cooked + registered as
`vehicle.<make>.<model>`. It does not author the asset — that is the manual procedure in
import_procedure_vehicle.md.

Machine-specific locations resolve through ``site_config``; there are no defaults.
Pure stdlib + optional PyYAML (for the manifest).

------------------------------------------------------------------------------------------------
WHY THIS IS NOT JUST "walker_common WITH THE STRINGS SWAPPED" — three real vehicle-only facts:

 1. The blueprint id is NOT derivable from the package name. For walkers,
    `walker.pedestrian.<Package.json walkers[].name>` holds. For vehicles it does NOT: the id is
    `vehicle.<Make>.<Model>` taken from the asset's entry in the cooked base-content
    VehicleFactory, while Package.json `vehicles[].name` is something else entirely
    (Content/SUV_Import/Config: name "suv_import"  ->  live blueprint "vehicle.ood.suv").
    So `blueprint_id` is ALWAYS verbatim from the manifest and is only ever validated LIVE.

 2. A registered vehicle can still silently fall back. `CarlaDataProvider.create_blueprint`
    filters by `attribute_filter` BEFORE choosing; if the filter leaves the list empty,
    `_rng.choice([])` raises ValueError -> the same `except` branch that handles an unknown model
    fires -> category "car" -> **vehicle.tesla.model3**. VehicleOpensDoorTwoWaysModified filters
    `{"has_dynamic_doors": True}`, and OOD vehicles typically carry base_type/special_type EMPTY
    with generation 0. So SCENARIO_ATTR_FILTERS below is a load-bearing part of the contract, not
    documentation.

 3. "Any vehicle fallback is ours" is FALSE. Unlike the pedestrian scenarios (where the only
    walker requested is ours), several vehicle scenarios legitimately request `vehicle.*`
    blockers. The fallback grep must match our blueprint id EXACTLY.
------------------------------------------------------------------------------------------------
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

# --------------------------------------------------------------------------
# Reuse the static pipeline's shared contract (don't fork it).
# stages/vehicle/vehicle_common.py -> stages/ -> stages/static
# --------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))                 # .../stages/vehicle
_STAGES_ROOT = os.path.dirname(_HERE)                              # .../stages
_STATIC_STAGES = os.path.join(_STAGES_ROOT, "static")
for _p in (_STAGES_ROOT, _STATIC_STAGES):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import site_config  # noqa: E402
import common as sc  # noqa: E402  (static common.py)

# Re-export the location-independent helpers the vehicle stages need.
make_verdict = sc.make_verdict
write_verdict = sc.write_verdict
read_verdict = sc.read_verdict
validate_camelcase = sc.validate_asset_name        # CamelCase-alphanumerics validator
CARLA_VERSION_TAG = sc.CARLA_VERSION_TAG           # "0.9.15-dirty"

# Site-dependent names delegate LAZILY to the static contract (see __getattr__ below), so that
# importing this module for its pure helpers never requires a configured site.
#
# SECONDARY_* is the optional second CARLA to install into. It exists because cooking + installing
# into one build does NOT register anything in another: running a route against a build that never
# received the tarball means create_blueprint falls back and the route silently scores a Tesla.
# That is the whole reason this validation pipeline exists.
_DELEGATED = (
    "PY_CLIENT", "CONDA_ROOT", "COOK_CONDA_ENV",
    "CARLA_SERVER_ROOT", "CARLA_SERVER_SH", "CARLA_IMPORT_DIR", "CARLA_IMPORT_ASSETS_SH",
    "CARLA_UE_ROOT", "CARLA_DIST", "CARLA_CONTENT", "REPO_ROOT", "B2D",
    "SECONDARY_CARLA_ROOT", "SECONDARY_SSH_HOST", "SECONDARY_PARTITIONS",
)

# --------------------------------------------------------------------------
# Vehicle path names, resolved from site_config (see site_config.example.yaml).
# --------------------------------------------------------------------------
VEH_STAGES_DIR = _HERE

_VEH_SITE = {
    "VEH_RUNS_DIR":             ("work_root", "/runs/vehicle"),
    "VEH_STATE_DIR":            ("work_root", "/state/vehicle"),
    "VEH_ROUTE_DATA_DIR":       ("work_root", "/routes/vehicle"),        # + /<subfolder>/...
    "VEH_RUN_SCRIPT_DIR":       ("work_root", "/runs/vehicle"),          # + /<subfolder>/run_<slug>.sh
    "VEH_RUN_TEMPLATE":         ("bench_root", "/routes/templates/vehicle/run_template.sh"),
    "VEH_SAVE_ROOT":            ("results_root", "/vehicle"),            # + /<subfolder>/
    "CANONICAL_VEHICLE_ROUTES": ("bench_root", "/routes/vehicle"),       # reference only
}


def __getattr__(name):  # PEP 562
    if name in _VEH_SITE:
        key, suffix = _VEH_SITE[name]
        return site_config.get(key) + suffix
    if name in _DELEGATED:
        return getattr(sc, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__():
    return sorted(list(globals()) + list(_VEH_SITE) + list(_DELEGATED))


def _site(name: str) -> str:
    """Resolve one of the _VEH_SITE names from inside this module (module __getattr__ does not
    fire for bare global lookups within the module itself)."""
    key, suffix = _VEH_SITE[name]
    return site_config.get(key) + suffix


# --------------------------------------------------------------------------
# The 6 vehicle scenarios in scope — exactly the 6 subfolders of the canonical
# OOD-v2 perception `vehicle/` route tree (the vehicle analogue of the pedestrian
# pipeline's 4). class_name == route-XML <scenario type="..."> == the scenario_runner
# class name; subfolder == route dir.
#
# blueprint_attr is NOT uniform here (it is for pedestrians — all 4 use
# `pedestrian_blueprint`). Four different attribute names carry the vehicle id, so the
# generator substitutes on the VALUE token, never on the attribute name.
#
# attr_filter is the `attribute_filter` dict CarlaDataProvider applies to OUR asset in that
# scenario — verified by reading each scenario's request_new_actor call (see module docstring
# fact #2). `{}` means "no filtering" (the `for key, value in {}.items()` loop is a no-op);
# a non-empty dict is a REAL precondition the blueprint must satisfy or it silently becomes
# a Tesla. `None` means the scenario passes no attribute_filter at all.
# --------------------------------------------------------------------------
VEH_SCENARIOS = [
    {
        "class_name": "HardBreakRouteModified",
        "subfolder": "hard_break",
        "blueprint_attr": "front_vehicle_model",
        "attr_filter": None,
        # NB not route_24330 (the shortest hard_break route): that one is a NIGHT route
        # (sun_altitude_angle=-90, cloudiness=100). Every template here is deliberately a
        # daylight, low-fog variant — a canary should fail because of the asset, not the weather.
        "template_route": "route_26406",
        "town": "Town04",
        "src": "hard_break_modified.py",
    },
    {
        "class_name": "InvadingTurnModified",
        "subfolder": "invading_turn",
        "blueprint_attr": "blueprint_name",
        # spawned by the InvadingActorFlowModified behavior, whose _attribute_filter is
        # hardcoded to {} (atomic_behaviors.py) — NOT the SingleInvadingActor default.
        "attr_filter": {},
        "template_route": "route_2790",
        "town": "Town12",
        "src": "invading_turn_modified.py",
    },
    {
        "class_name": "ParkedObstacleModified",
        "subfolder": "parked_obstacle",
        "blueprint_attr": "parked_vehicle_model",
        "attr_filter": {},
        "template_route": "route_24757",
        "town": "Town06",
        "src": "parked_obstacle_modified.py",
    },
    {
        "class_name": "ParkedObstacleTwoWaysModified",
        "subfolder": "parked_obstacle_two_ways",
        "blueprint_attr": "parked_vehicle_model",
        # subclasses ParkedObstacleModified and does not override _spawn_obstacle
        "attr_filter": {},
        "template_route": "route_25865",
        "town": "Town11",
        "src": "parked_obstacle_modified.py",
    },
    {
        "class_name": "ParkingCutInModified",
        "subfolder": "parking_cut_in",
        "blueprint_attr": "cut_in_vehicle_model",
        # attribute_filter=self._bp_attributes, which is initialised to {}
        "attr_filter": {},
        "template_route": "route_24759",
        "town": "Town05",
        "src": "parking_cut_in_modified.py",
    },
    {
        "class_name": "VehicleOpensDoorTwoWaysModified",
        "subfolder": "vehicle_opens_door_two_ways",
        "blueprint_attr": "parked_vehicle_model",
        # THE ONE REAL PRECONDITION among the 6. A vehicle whose blueprint lacks this
        # attribute (or has it False) is filtered out -> empty list -> ValueError ->
        # silent vehicle.tesla.model3. Stage A hard-fails on this.
        "attr_filter": {"has_dynamic_doors": True},
        "template_route": "route_25928",
        "town": "Town11",
        "src": "vehicle_opens_door_modified.py",
    },
]
VEH_SCENARIO_BY_CLASS = {s["class_name"]: s for s in VEH_SCENARIOS}
DEFAULT_SCENARIOS = [s["class_name"] for s in VEH_SCENARIOS]

# Convenience view used by the probe's precondition gate.
SCENARIO_ATTR_FILTERS = {s["class_name"]: s["attr_filter"] for s in VEH_SCENARIOS}

# The blueprint CarlaDataProvider falls back to for actor_category "car" (its
# _actor_blueprint_categories table). Every one of the 6 scenarios uses the default category.
FALLBACK_BLUEPRINT = "vehicle.tesla.model3"

# Resumable state. cook_ingest is OPTIONAL (only when the manifest sets cook: true — i.e. the
# vehicle is not yet cooked into the /media build); spawn_smoke + route_check are the test core.
VEH_STAGE_ORDER = ["cook_ingest", "spawn_smoke", "route_check"]

# Lowercase-only on purpose: CARLA lowercases Make/Model when it builds the id, so an
# uppercase manifest entry (`vehicle.Tohoku.SUV`) can NEVER match a live blueprint. Verified:
# all 52 vehicle ids in data/blueprint_dimensions.csv are lowercase. Catching it here turns a
# confusing Stage A "not registered" into an instant manifest error.
BLUEPRINT_RE = re.compile(r"^vehicle\.[a-z0-9_]+\.[a-z0-9_]+$")

# --------------------------------------------------------------------------
# Shift classification anchor for vehicles (record.md, 2026-05-13 "Final Obstacle
# Selection"). Unlike the static pipeline's single-anchor 20% rule and the walker
# pipeline's relative-Δ-vs-adult, vehicles are classified by Z-score against the
# `vehicle_car` cluster (base_type=car, generation=2) over the 220-blueprint
# data/blueprint_dimensions.csv extraction:
#     visual     if max_d Z_d <= 2.0
#     geometric  if any  Z_d  > 3.0
# The band in between is ambiguous (no benchmark asset lands there).
# Reported by the probe as INFORMATIONAL — it never gates. It exists so the operator
# can catch "I declared visual but this thing is a dump truck" at the visual gate.
# --------------------------------------------------------------------------
VEHICLE_CAR_CLUSTER = {          # dim -> (mean_m, stdev_m)
    "L": (4.53, 0.72),
    "W": (1.95, 0.16),
    "H": (1.57, 0.17),
}
VISUAL_Z_MAX = 2.0
GEOMETRIC_Z_MIN = 3.0


def classify_shift(L: float, W: float, H: float) -> dict:
    """Z-score classify realized dims against the vehicle_car cluster. Informational."""
    z = {}
    for dim, val in (("L", L), ("W", W), ("H", H)):
        mu, sigma = VEHICLE_CAR_CLUSTER[dim]
        z[dim] = round(abs(val - mu) / sigma, 3) if sigma else None
    zs = [v for v in z.values() if v is not None]
    z_max = max(zs) if zs else None
    if z_max is None:
        implied = "unknown"
    elif z_max > GEOMETRIC_Z_MIN:
        implied = "geometric"
    elif z_max <= VISUAL_Z_MAX:
        implied = "visual"
    else:
        implied = "ambiguous"
    return {"z_per_dim": z, "z_max": z_max, "implied_shift": implied}


# --------------------------------------------------------------------------
# Naming. The registered vehicle id comes from the asset's VehicleFactory entry
# (`vehicle.<Make>.<Model>`) and is NOT derivable from the UE package name — e.g.
# Content/SUV_Import (Package.json vehicles[].name "suv_import") registers as
# `vehicle.ood.suv`. So blueprint_id is taken VERBATIM from the manifest and
# validated LIVE in Stage A; nothing here ever reconstructs it.
#   vehicle_name  CamelCase        -> labels, runs/<vehicle_name>, state file
#   blueprint_id  verbatim         -> vehicle.<make>.<model>
#   make / model  split of the id  -> reporting
#   slug          model.lower()    -> route filename suffix + checkpoint json + save-dir grouping
#                                     (matches the canonical tree: vehicle.ood.roadroller ->
#                                      route_2790_roadroller.xml)
# --------------------------------------------------------------------------
@dataclass
class VehicleNames:
    vehicle_name: str
    blueprint_id: str
    make: str               # middle segment of blueprint_id
    model: str              # last segment of blueprint_id
    slug: str               # model.lower() — route filename suffix + checkpoint
    package_name: str       # the Content folder / `make package` package name (CamelCase)
    dist_tar: str           # <package_name>_0.9.15-dirty.tar.gz (cook output)


def vehicle_names_for(vehicle_name: str, blueprint_id: str, package_name: str = "") -> VehicleNames:
    parts = blueprint_id.split(".")
    make, model = parts[1], parts[2]
    pkg = package_name or vehicle_name
    return VehicleNames(
        vehicle_name=vehicle_name,
        blueprint_id=blueprint_id,
        make=make,
        model=model,
        slug=model.lower(),
        package_name=pkg,
        dist_tar=f"{pkg}_{CARLA_VERSION_TAG}.tar.gz",
    )


def content_dir(package_name: str) -> str:
    """The UE-source Content folder for a package (where the user authored the vehicle)."""
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
    if not BLUEPRINT_RE.fullmatch(blueprint_id):
        hint = ""
        if blueprint_id.lower() != blueprint_id and BLUEPRINT_RE.fullmatch(blueprint_id.lower()):
            hint = (f" — ids are lowercase in CARLA; did you mean {blueprint_id.lower()!r}?")
        raise ValueError(
            f"blueprint_id must look like vehicle.<make>.<model> (lowercase alphanumerics/"
            f"underscore), got {blueprint_id!r}{hint}"
        )


# UE *vehicle* package folders in this project legitimately contain underscores
# (Content/SUV_Import, Content/HatchBack_Import, Content/Sedane_Import, Content/BOV_VP),
# unlike the static/walker packages. `validate_camelcase` (= the static pipeline's
# validate_asset_name) forbids them, so package_name gets its own, looser validator.
# vehicle_name stays strict CamelCase — it is only a label / runs-dir name.
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def validate_package_name(package_name: str) -> None:
    if not PACKAGE_NAME_RE.match(package_name):
        raise ValueError(
            f"package_name must be alphanumerics/underscore starting with a letter "
            f"(it is the UE Content/<Pkg> folder, e.g. SUV_Import), got {package_name!r}"
        )


# --------------------------------------------------------------------------
# Vehicle test manifest (input contract — minimal, test-only).
# --------------------------------------------------------------------------
@dataclass
class VehicleManifest:
    vehicle_name: str
    blueprint_id: str
    shift_type: str = "visual"            # visual | geometric — labels/output only
    scenarios: Optional[List[str]] = None  # default = all 6 VEH_SCENARIOS
    cook: bool = False                    # if true, cook+ingest the package before testing
    package_name: Optional[str] = None    # Content folder / make-package name (default = vehicle_name)
    path: Optional[str] = None             # filled by loader

    def validate(self) -> None:
        validate_camelcase(self.vehicle_name)
        validate_blueprint_id(self.blueprint_id)
        if self.shift_type not in ("visual", "geometric"):
            raise ValueError(f"shift_type must be visual|geometric, got {self.shift_type!r}")
        unknown = [s for s in self.scenario_list if s not in VEH_SCENARIO_BY_CLASS]
        if unknown:
            raise ValueError(
                f"unknown scenario class(es): {unknown}; valid: {sorted(VEH_SCENARIO_BY_CLASS)}"
            )
        if self.package_name is not None:
            validate_package_name(self.package_name)
        # If cooking, the user must have authored the package in the UE editor already.
        if self.cook:
            pj = package_json_path(self.effective_package_name)
            if not os.path.isfile(pj):
                raise ValueError(
                    f"cook: true but the package config was not found: {pj} — author the vehicle "
                    f"in the UE editor first (Content/{self.effective_package_name}/ with a "
                    f"vehicles-keyed Package.json AND a VehicleFactory entry), or set cook: false "
                    f"to test an already-cooked vehicle"
                )

    @property
    def scenario_list(self) -> List[str]:
        return list(self.scenarios) if self.scenarios else list(DEFAULT_SCENARIOS)

    @property
    def effective_package_name(self) -> str:
        return self.package_name or self.vehicle_name


def load_vehicle_manifest(path: str) -> VehicleManifest:
    with open(path) as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        import yaml  # the client interpreter has pyyaml
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    known = {f for f in VehicleManifest.__dataclass_fields__ if f != "path"}  # type: ignore[attr-defined]
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown manifest keys: {sorted(unknown)}; valid: {sorted(known)}")
    m = VehicleManifest(
        vehicle_name=data["vehicle_name"],
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
# Per-vehicle / per-scenario path helpers.
# --------------------------------------------------------------------------
def veh_work_dir(vehicle_name: str) -> str:
    return os.path.join(_site("VEH_RUNS_DIR"), vehicle_name)


def route_template_path(subfolder: str) -> str:
    return os.path.join(_site("VEH_ROUTE_DATA_DIR"), subfolder, "route_template.xml")


def route_out_path(subfolder: str, fname: str) -> str:
    return os.path.join(_site("VEH_ROUTE_DATA_DIR"), subfolder, fname)


def run_out_path(subfolder: str, slug: str) -> str:
    return os.path.join(_site("VEH_RUN_SCRIPT_DIR"), subfolder, f"run_{slug}.sh")


def save_dir(subfolder: str) -> str:
    return os.path.join(_site("VEH_SAVE_ROOT"), subfolder)


def checkpoint_path(subfolder: str, slug: str) -> str:
    return os.path.join(_site("VEH_SAVE_ROOT"), subfolder, f"{slug}.json")


# --------------------------------------------------------------------------
# Resumable state file (separate dir from the static + pedestrian pipelines).
# --------------------------------------------------------------------------
def veh_state_path(vehicle_name: str) -> str:
    return os.path.join(_site("VEH_STATE_DIR"), f"{vehicle_name}.state.json")


def load_or_init_veh_state(vehicle_name: str, manifest_path: str) -> dict:
    p = veh_state_path(vehicle_name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {
        "vehicle_name": vehicle_name,
        "manifest_path": manifest_path,
        "stages": {s: {"done": False} for s in VEH_STAGE_ORDER},
    }


def save_veh_state(state: dict) -> None:
    os.makedirs(_site("VEH_STATE_DIR"), exist_ok=True)
    p = veh_state_path(state["vehicle_name"])
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, p)


def veh_mark_stage(state: dict, stage: str, *, done: bool, verdict: dict | None = None,
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
    save_veh_state(state)
    return state
