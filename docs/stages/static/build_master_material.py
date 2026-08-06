"""
build_master_material.py — one-time builder for M_OODPropMaster (see the procedure document).

Run headless inside the CARLA UE editor:
  UE4Editor-Cmd CarlaUE4.uproject -run=pythonscript \
    -script="build_master_material.py --out <json> [--force]" -unattended -nosplash -nullrhi -stdout

Creates /Game/OODProps/M_OODPropMaster exposing 6 overridable TextureSampleParameter2D
params (BaseColor, Normal, Metallic, Roughness, AO, Emissive) wired to the matching
material outputs with correct sampler types. Per-prop MaterialInstanceConstants override
these params (see ue_import_material_collision.py). Writes a JSON verdict to --out.
"""
import json
import os
import sys

import unreal

MASTER_DIR = "/Game/OODProps"
MASTER_NAME = "M_OODPropMaster"
MASTER_PATH = MASTER_DIR + "/" + MASTER_NAME

# UE collision/material constants
MP = unreal.MaterialProperty
ST = unreal.MaterialSamplerType


def _sampler(*candidates):
    """Return the first MaterialSamplerType member that exists (UE enum names vary)."""
    for c in candidates:
        if hasattr(ST, c):
            return getattr(ST, c)
    raise AttributeError(f"none of {candidates} on MaterialSamplerType")


_COLOR = _sampler("SAMPLERTYPE_COLOR")
_NORMAL = _sampler("SAMPLERTYPE_NORMAL")
_LIN_GRAY = _sampler("SAMPLERTYPE_LINEAR_GRAYSCALE", "SAMPLERTYPE_GRAYSCALE", "SAMPLERTYPE_MASKS")

# (param_name, sampler_type, output_pin, material_property)
PARAMS = [
    ("BaseColor", _COLOR, "RGB", MP.MP_BASE_COLOR),
    ("Normal", _NORMAL, "RGB", MP.MP_NORMAL),
    ("Metallic", _LIN_GRAY, "R", MP.MP_METALLIC),
    ("Roughness", _LIN_GRAY, "R", MP.MP_ROUGHNESS),
    ("AO", _LIN_GRAY, "R", MP.MP_AMBIENT_OCCLUSION),
    ("Emissive", _COLOR, "RGB", MP.MP_EMISSIVE_COLOR),
]


def _out_path():
    av = sys.argv
    out = "/tmp/master_material.json"
    if "--out" in av:
        out = av[av.index("--out") + 1]
    return out, ("--force" in av)


def write(out, ok, data, error=None):
    import time
    with open(out, "w") as f:
        json.dump({"stage": "master_material", "ok": ok, "data": data,
                   "error": error, "ts": time.time()}, f, indent=2)


def main():
    out, force = _out_path()
    data = {"path": MASTER_PATH}
    try:
        eal = unreal.EditorAssetLibrary
        if eal.does_asset_exist(MASTER_PATH):
            if not force:
                data["existed"] = True
                write(out, True, data)
                unreal.log("MASTER_MATERIAL_EXISTS " + MASTER_PATH)
                return
            eal.delete_asset(MASTER_PATH)

        if not eal.does_directory_exist(MASTER_DIR):
            eal.make_directory(MASTER_DIR)

        tools = unreal.AssetToolsHelpers.get_asset_tools()
        mat = tools.create_asset(MASTER_NAME, MASTER_DIR, unreal.Material,
                                 unreal.MaterialFactoryNew())
        MEL = unreal.MaterialEditingLibrary

        created = []
        y = -400
        for (name, sampler, pin, prop) in PARAMS:
            node = MEL.create_material_expression(
                mat, unreal.MaterialExpressionTextureSampleParameter2D, -400, y)
            node.set_editor_property("parameter_name", name)
            node.set_editor_property("sampler_type", sampler)
            ok_conn = MEL.connect_material_property(node, pin, prop)
            created.append({"param": name, "pin": pin, "connected": bool(ok_conn)})
            y += 180

        MEL.recompile_material(mat)
        eal.save_asset(MASTER_PATH)
        data["created"] = created
        data["existed"] = False
        write(out, True, data)
        unreal.log("MASTER_MATERIAL_BUILT " + MASTER_PATH)
    except Exception as e:
        import traceback
        write(out, False, data, error=f"{e!r}\n{traceback.format_exc()}")
        unreal.log_error("MASTER_MATERIAL_FAIL " + repr(e))


main()
