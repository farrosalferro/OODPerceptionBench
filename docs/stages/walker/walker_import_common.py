"""
walker_import_common.py -- shared contract for the WALKER IMPORT stage scripts, the walker
analogue of the static-prop stage package.

It does NOT fork the static common.py or the validation-side walker_common.py; it imports both
and re-exports what it needs, then adds the import-specific manifest, naming, stage order and
resumable state. Keep it pure stdlib + optional PyYAML. It must NEVER be imported from inside a
UE-python stage script -- resolve names here and pass resolved argv to UE.

Artifact version: v0.9 -- corresponds to arXiv v1 of the OOD-PerceptionBench paper.
Machine-specific locations resolve through ``site_config``; there are no defaults.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional, List

# --- locate the sibling contracts ------------------------------------------------------
_THIS = os.path.dirname(os.path.abspath(__file__))          # .../stages/walker
_STAGES_ROOT = os.path.dirname(_THIS)                       # .../stages
_STATIC_STAGES = os.path.join(_STAGES_ROOT, "static")
for _p in (_STAGES_ROOT, _STATIC_STAGES, _THIS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import site_config             # noqa: E402
import common as sc            # noqa: E402  static common.py (envelope, state helpers, paths)
import walker_common as wc     # noqa: E402  validation-side walker contract

# re-export the location-independent helpers the import stages need ---------------------
make_verdict = sc.make_verdict
write_verdict = sc.write_verdict
read_verdict = sc.read_verdict
validate_camelcase = sc.validate_asset_name
CARLA_VERSION_TAG = sc.CARLA_VERSION_TAG

# Site-dependent names delegate LAZILY to the static contract (see __getattr__ below), so
# importing this module for its pure helpers never requires a configured site.
_DELEGATED = (
    "PY_CLIENT", "BLENDER", "UE4_EDITOR_CMD", "UPROJECT",
    "CARLA_UE_ROOT", "CARLA_CONTENT", "CARLA_DIST", "COOK_CONDA_ENV",
    "CARLA_IMPORT_DIR", "CARLA_IMPORT_ASSETS_SH", "CARLA_SERVER_ROOT", "REPO_ROOT",
    "SECONDARY_CARLA_ROOT", "SECONDARY_SSH_HOST", "SECONDARY_PARTITIONS",
)

# shared naming helpers from the validation-side contract
walker_names_for = wc.walker_names_for
content_dir = wc.content_dir
package_json_path = wc.package_json_path
validate_blueprint_id = wc.validate_blueprint_id

# --- import-pipeline directories, resolved from site_config ----------------------------
WALKER_FACTORY_PATH = "/Game/Carla/Blueprints/Walkers/WalkerFactory"

_WI_SITE = {
    "WI_MANIFEST_DIR": ("work_root", "/manifests/walker"),
    "WI_STATE_DIR":    ("work_root", "/state/walker-import"),
    "WI_RUNS_DIR":     ("work_root", "/runs/walker-import"),   # + /<W>/import/
}


def __getattr__(name):  # PEP 562
    if name in _WI_SITE:
        key, suffix = _WI_SITE[name]
        return site_config.get(key) + suffix
    if name in _DELEGATED:
        return getattr(sc, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__():
    return sorted(list(globals()) + list(_WI_SITE) + list(_DELEGATED))


def _site(name: str) -> str:
    """Resolve one of the _WI_SITE names from inside this module (module __getattr__ does
    not fire for bare global lookups within the module itself)."""
    key, suffix = _WI_SITE[name]
    return site_config.get(key) + suffix

# --- stage order (see the procedure document) ----------------------------------------------------
WI_STAGE_ORDER = [
    "manifest_texture",   # 1 scripted  (REUSE texture_classify.py)
    "sizing",             # 2 scripted  (walker_sizing.py)
    "blender",            # 3 scripted -> G1
    "ue_import",          # 4 scripted -> G2
    "assemble",           # 5 G3 (manual BlendSpace + Anim BP + BP)
    "finalize",           # 6 scripted (runs after G3, editor closed)
    "cook_ingest",        # 7 scripted (cook_walker_package.sh -> /media server)
    "ingest_secondary",   # 8 scripted, OPTIONAL -> secondary_carla_pkg
    "movement_check",     # 9 G4 (visual)
]
WI_GATE_STAGES = {"blender": "G1", "ue_import": "G2", "assemble": "G3", "movement_check": "G4"}


# --- names -----------------------------------------------------------------------------
@dataclass
class WalkerImportNames:
    walker_name: str            # CamelCase (folder / package)
    blueprint_id: str
    walker_id: str              # blueprint_id last segment == Package.json walkers[].name
    package_name: str
    dist_tar: str
    content_root: str           # /Game/<W>
    static_dir: str             # /Game/<W>/Static/Other/<W>
    anim_dir: str               # /Game/<W>/Animations
    blueprint_dir: str          # /Game/<W>/Blueprints
    maps_dir: str               # /Game/<W>/Maps
    sk: str                     # SK_<W>
    skeleton: str               # <W>_Skeleton
    physics: str                # <W>_PhysicsAsset
    material: str               # M_<W>
    bs: str                     # BS_<W>
    abp: str                    # ABP_<W>
    bp: str                     # BP_<W>
    map_name: str               # <W>Map
    sk_object_path: str         # /Game/<W>/Static/Other/<W>/SK_<W>
    bp_object_path: str         # /Game/<W>/Blueprints/BP_<W>.BP_<W>  (Package.json walkers[].path)
    map_object_path: str        # /Game/<W>/Maps/<W>Map

    def anim_name(self, clip_stem: str) -> str:
        """Imported AnimSequence name for a source clip FBX stem (already A_<W>_<Clip>_01)."""
        return clip_stem

    def texture_name(self, slot: str) -> str:
        return "T_{}_{}".format(self.walker_name, slot)


def walker_import_names_for(walker_name: str, blueprint_id: str,
                            package_name: str = "") -> WalkerImportNames:
    validate_camelcase(walker_name)
    validate_blueprint_id(blueprint_id)
    base = walker_names_for(walker_name, blueprint_id, package_name=package_name or walker_name)
    W = base.package_name
    content_root = "/Game/{}".format(W)
    static_dir = "{}/Static/Other/{}".format(content_root, W)
    blueprint_dir = "{}/Blueprints".format(content_root)
    maps_dir = "{}/Maps".format(content_root)
    return WalkerImportNames(
        walker_name=base.walker_name,
        blueprint_id=base.blueprint_id,
        walker_id=base.walker_id,
        package_name=W,
        dist_tar=base.dist_tar,
        content_root=content_root,
        static_dir=static_dir,
        anim_dir="{}/Animations".format(content_root),
        blueprint_dir=blueprint_dir,
        maps_dir=maps_dir,
        sk="SK_{}".format(W),
        skeleton="{}_Skeleton".format(W),
        physics="{}_PhysicsAsset".format(W),
        material="M_{}".format(W),
        bs="BS_{}".format(W),
        abp="ABP_{}".format(W),
        bp="BP_{}".format(W),
        map_name="{}Map".format(W),
        sk_object_path="{}/SK_{}".format(static_dir, W),
        bp_object_path="{}/BP_{}.BP_{}".format(blueprint_dir, W, W),
        map_object_path="{}/{}Map".format(maps_dir, W),
    )


# --- manifest (see the procedure document) -------------------------------------------------------
_GENDERS = ("Other", "Female", "Male")
_AGES = ("Child", "Teenager", "Adult", "Elderly")


@dataclass
class WalkerImportManifest:
    # identity
    walker_name: str
    blueprint_id: str
    shift_type: str                                  # visual | geometric
    # blender inputs
    source_fbx: str
    textures_dir: str
    target_dims_m: dict                              # {L,W,H} meters
    package_name: Optional[str] = None               # default walker_name
    animations_dir: Optional[str] = None             # per-clip FBX dir
    animations: Optional[List[str]] = None           # OR explicit list of clip FBX paths
    front_axis: Optional[str] = None                 # blender-frame forward (sanity)
    blender_export_script: Optional[str] = None      # BESPOKE per-rig export (blender stage is
                                                     # per-folder, like Cow's export_fbx.py); if
                                                     # unset the blender stage is a manual G1 prep
    import_mode: str = "blender"                     # "blender" = separate SK + per-clip FBXs
                                                     # (Cow); "direct" = one pre-animated FBX
                                                     # (peoplewithdisabilities pack) -> skip Blender,
                                                     # ue_walker_import --direct on source_fbx
    # sizing
    capsule_override: Optional[dict] = None          # {radius_cm, half_height_cm}
    # blendspace / clip->speed  (consumed by human at G3; asserted subset of imported anims)
    clip_speed_map: Optional[dict] = None
    blendspace_axis: Optional[dict] = None
    # assemble template
    clone_from: Optional[str] = None                 # nearest body-plan walker (e.g. Boar)
    # WalkerFactory params
    gender: str = "Other"
    age: str = "Adult"
    factory_speed: Optional[List[float]] = None      # default [0.0, 1.0, 2.5]
    # cook + downstream
    cook: bool = True
    ingest_secondary: bool = False                    # also install the cooked tar into secondary_carla_pkg
                                                     # server and ImportAssets it there (via srun)
    scenarios: Optional[List[str]] = None
    # filled by loader
    path: Optional[str] = None

    def validate(self) -> None:
        validate_camelcase(self.walker_name)
        validate_blueprint_id(self.blueprint_id)
        if self.walker_id != self.blueprint_id.split(".")[-1]:
            raise ValueError("internal: walker_id mismatch")
        if self.shift_type not in ("visual", "geometric"):
            raise ValueError("shift_type must be visual|geometric, got %r" % self.shift_type)
        if not os.path.isfile(self.source_fbx):
            raise ValueError("source_fbx not found: %s" % self.source_fbx)
        if not os.path.isdir(self.textures_dir):
            raise ValueError("textures_dir not found: %s" % self.textures_dir)
        if self.animations_dir is None and not self.animations:
            raise ValueError("provide animations_dir or an explicit animations: list")
        if self.animations_dir is not None and not os.path.isdir(self.animations_dir):
            raise ValueError("animations_dir not found: %s" % self.animations_dir)
        td = self.target_dims_m or {}
        for k in ("L", "W", "H"):
            v = td.get(k)
            if not isinstance(v, (int, float)) or v <= 0:
                raise ValueError("target_dims_m.%s must be positive meters" % k)
        if self.gender not in _GENDERS:
            raise ValueError("gender must be one of %s" % (_GENDERS,))
        if self.age not in _AGES:
            raise ValueError("age must be one of %s" % (_AGES,))
        if self.front_axis is not None and self.front_axis not in (
                "+X", "-X", "+Y", "-Y", "+Z", "-Z", "X", "Y", "Z"):
            raise ValueError("front_axis must be a signed axis like -Y, got %r" % self.front_axis)

    @property
    def walker_id(self) -> str:
        return self.blueprint_id.split(".")[-1]

    @property
    def effective_package_name(self) -> str:
        return self.package_name or self.walker_name

    @property
    def factory_speed_or_default(self) -> List[float]:
        return self.factory_speed if self.factory_speed is not None else [0.0, 1.0, 2.5]

    def clip_stems(self) -> List[str]:
        """Source clip FBX basenames without extension (== imported AnimSequence names)."""
        stems = []
        if self.animations:
            paths = self.animations
        else:
            paths = [os.path.join(self.animations_dir, f)
                     for f in sorted(os.listdir(self.animations_dir))
                     if f.lower().endswith(".fbx")]
        for p in paths:
            stems.append(os.path.splitext(os.path.basename(p))[0])
        return stems

    def assert_clip_speed_subset(self) -> None:
        """Design section 6 gotcha 3: clip_speed_map keys must be a subset of imported anim names."""
        if not self.clip_speed_map:
            return
        imported = set(self.clip_stems())
        missing = set(self.clip_speed_map) - imported
        if missing:
            raise ValueError(
                "clip_speed_map references clips not among the animation FBXs %s "
                "(imported=%s)" % (sorted(missing), sorted(imported)))


def load_walker_import_manifest(path: str) -> WalkerImportManifest:
    with open(path) as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        import yaml
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    known = {fld for fld in WalkerImportManifest.__dataclass_fields__}  # type: ignore[attr-defined]
    known.discard("path")
    unknown = set(data) - known
    if unknown:
        raise ValueError("unknown manifest keys: %s" % sorted(unknown))
    m = WalkerImportManifest(
        walker_name=data["walker_name"],
        blueprint_id=data["blueprint_id"],
        shift_type=data["shift_type"],
        source_fbx=data["source_fbx"],
        textures_dir=data["textures_dir"],
        target_dims_m=data["target_dims_m"],
        package_name=data.get("package_name"),
        animations_dir=data.get("animations_dir"),
        animations=data.get("animations"),
        front_axis=data.get("front_axis"),
        blender_export_script=data.get("blender_export_script"),
        import_mode=data.get("import_mode", "blender"),
        capsule_override=data.get("capsule_override"),
        clip_speed_map=data.get("clip_speed_map"),
        blendspace_axis=data.get("blendspace_axis"),
        clone_from=data.get("clone_from"),
        gender=data.get("gender", "Other"),
        age=data.get("age", "Adult"),
        factory_speed=data.get("factory_speed"),
        cook=bool(data.get("cook", True)),
        ingest_secondary=bool(data.get("ingest_secondary", False)),
        scenarios=data.get("scenarios"),
    )
    m.path = os.path.abspath(path)
    m.validate()
    return m


# --- resumable state (see the procedure document) ------------------------------------------------
def wi_state_path(walker_name: str) -> str:
    return os.path.join(_site("WI_STATE_DIR"), "%s.import.json" % walker_name)


def load_or_init_wi_state(walker_name: str, manifest_path: str) -> dict:
    p = wi_state_path(walker_name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {
        "walker_name": walker_name,
        "manifest_path": manifest_path,
        "stages": {s: {"done": False} for s in WI_STAGE_ORDER},
    }


def save_wi_state(state: dict) -> None:
    os.makedirs(_site("WI_STATE_DIR"), exist_ok=True)
    p = wi_state_path(state["walker_name"])
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, p)


def wi_mark_stage(state: dict, stage: str, *, done: bool, verdict: dict = None,
                  artifacts: dict = None, approved: bool = None) -> dict:
    st = state["stages"].setdefault(stage, {})
    st["done"] = done
    if verdict is not None:
        st["verdict"] = verdict
    if artifacts is not None:
        st["artifacts"] = {**st.get("artifacts", {}), **artifacts}
    if approved is not None:
        st["approved"] = approved
    state["ts"] = time.time()
    save_wi_state(state)
    return state


def stage_is_satisfied(state: dict, stage: str) -> bool:
    """A stage counts as complete only when done AND (if it is a gate) approved.
    This is the design section 6 gotcha 1 fix: resume must not march past an unapproved gate."""
    st = state["stages"].get(stage, {})
    if not st.get("done"):
        return False
    if stage in WI_GATE_STAGES:
        return bool(st.get("approved"))
    return True


def resume_from(state: dict) -> Optional[str]:
    for s in WI_STAGE_ORDER:
        if not stage_is_satisfied(state, s):
            return s
    return None


# --- Package.json (see the procedure document) ---------------------------------------------------
def walker_package_json(names: WalkerImportNames) -> dict:
    """The walkers-keyed Package.json (verified == the gold Cow.Package.json).
    NB (spike 3): the walkers[] entry is INERT at runtime -- registration is the WalkerFactory
    graph edit -- but the cook script asserts walkers[].name == blueprint_id last segment, so
    we still emit it, and it documents the map that cooks the WalkerFactory into the package."""
    return {
        "props": [],
        "maps": [{
            "name": names.map_name,
            "path": names.maps_dir,
            "use_carla_materials": False,
        }],
        "walkers": [{
            "name": names.walker_id,
            "path": names.bp_object_path,
        }],
    }
