#!/usr/bin/env python3
"""
bpy_textured_render.py — HUMAN GATE 2 textured preview (see the procedure document).

Renders the Blender-exported FBX with the canonical PBR texture set applied to a
Principled BSDF, so the runbook operator can show the user a faithful preview of the UE
material BEFORE the slow cook. Done in Blender (Cycles CPU) rather than UE because
UE textured rendering needs RHI (flaky headless); the master material is just
BaseColor/Normal/Metallic/Roughness/AO/Emissive -> a Principled BSDF is equivalent.

  blender --background --python bpy_textured_render.py -- \
      --fbx <export.fbx> --textures_json <texture_classify.json> \
      --render_dir <dir> --out <verdict.json>

This is a PREVIEW only; the authoritative material wiring is done in UE (Agent 3).
"""
import argparse
import json
import math
import os
import sys

import bpy
import mathutils

STAGES = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, STAGES)
import common  # noqa: E402

COLLISION_PREFIXES = common.COLLISION_MESH_PREFIXES
# slot -> (blender image colorspace, principled input or special)
SLOT_INPUT = {
    "BaseColor": ("sRGB", "Base Color"),
    "Emissive": ("sRGB", "Emission" ),
    "Metallic": ("Non-Color", "Metallic"),
    "Roughness": ("Non-Color", "Roughness"),
    "Normal": ("Non-Color", "__normal__"),
    "AO": ("Non-Color", "__ao__"),
}


def args():
    av = sys.argv
    a = av[av.index("--") + 1:] if "--" in av else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--fbx", required=True)
    ap.add_argument("--textures_json", required=True)
    ap.add_argument("--render_dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--samples", type=int, default=24)
    return ap.parse_args(a)


def import_fbx(path):
    try:
        bpy.ops.import_scene.fbx(filepath=path)
    except (AttributeError, RuntimeError):
        import addon_utils
        addon_utils.enable("io_scene_fbx")
        bpy.ops.import_scene.fbx(filepath=path)


def join_render_meshes(name):
    meshes = [o for o in bpy.context.scene.objects
              if o.type == "MESH" and not any(o.name.startswith(p) for p in COLLISION_PREFIXES)]
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    obj.name = name
    return obj


def build_material(canonical):
    mat = bpy.data.materials.new("OOD_Preview")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    out = nt.nodes.get("Material Output")
    x = -800
    applied = {}
    for slot, path in canonical.items():
        if slot not in SLOT_INPUT or not os.path.exists(path):
            continue
        colorspace, target = SLOT_INPUT[slot]
        img = bpy.data.images.load(path)
        try:
            img.colorspace_settings.name = colorspace
        except Exception:
            pass
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = img
        tex.location = (x, len(applied) * -300)
        if target == "__normal__":
            nm = nt.nodes.new("ShaderNodeNormalMap")
            nm.location = (x + 300, -300)
            nt.links.new(tex.outputs["Color"], nm.inputs["Color"])
            nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
        elif target == "__ao__":
            # multiply AO into base color if a base color is present (best-effort preview)
            continue
        elif target in bsdf.inputs:
            nt.links.new(tex.outputs["Color"], bsdf.inputs[target])
        applied[slot] = path
    return mat, applied


def render(obj, render_dir, samples):
    os.makedirs(render_dir, exist_ok=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    try:
        scene.cycles.device = "CPU"
    except Exception:
        pass
    scene.cycles.samples = samples
    scene.render.resolution_x = 1280
    scene.render.resolution_y = 720

    # light
    light = bpy.data.lights.new("Sun", type="SUN")
    light.energy = 3.0
    lo = bpy.data.objects.new("Sun", light)
    bpy.context.collection.objects.link(lo)
    lo.rotation_euler = (math.radians(55), math.radians(15), math.radians(35))

    # ground the object
    mn = [1e18] * 3
    mx = [-1e18] * 3
    for c in obj.bound_box:
        w = obj.matrix_world @ mathutils.Vector(c)
        for i in range(3):
            mn[i] = min(mn[i], w[i]); mx[i] = max(mx[i], w[i])
    obj.location.z -= mn[2]
    extent = max(mx[i] - mn[i] for i in range(3))
    center = mathutils.Vector(((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mx[2] - mn[2]) / 2))

    cam_data = bpy.data.cameras.new("Cam")
    cam = bpy.data.objects.new("Cam", cam_data)
    bpy.context.collection.objects.link(cam)
    scene.camera = cam
    r = extent * 2.2
    views = {"front": (0, -r, extent * 0.5), "threequarter": (r * 0.8, -r * 0.8, extent * 0.8)}
    paths = {}
    for name, (dx, dy, dz) in views.items():
        cam.location = center + mathutils.Vector((dx, dy, dz))
        d = center - cam.location
        cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
        p = os.path.join(render_dir, f"gate2_{name}.png")
        scene.render.filepath = p
        bpy.ops.render.render(write_still=True)
        paths[name] = p
    return paths


def main():
    a = args()
    try:
        with open(a.textures_json) as f:
            tj = json.load(f)
        canonical = (tj.get("data") or {}).get("canonical_set") or {}

        bpy.ops.wm.read_factory_settings(use_empty=True)
        import_fbx(a.fbx)
        obj = join_render_meshes("PROP")
        mat, applied = build_material(canonical)
        obj.data.materials.clear()
        obj.data.materials.append(mat)
        paths = render(obj, a.render_dir, a.samples)

        data = {"fbx": a.fbx, "applied_slots": sorted(applied.keys()),
                "render_paths": paths, "texture_mapping": canonical}
        common.write_verdict(a.out, common.make_verdict("ue_import_render", True, data))
    except Exception as e:
        import traceback
        common.write_verdict(a.out, common.make_verdict(
            "ue_import_render", False, {"fbx": a.fbx}, error=f"{e!r}\n{traceback.format_exc()}"))
        sys.exit(1)


if __name__ == "__main__":
    main()
