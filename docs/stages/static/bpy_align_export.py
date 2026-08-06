#!/usr/bin/env python3
"""
bpy_align_export.py — Agent 2 stage (see the procedure document). Run headless in Blender:

  blender --background --python bpy_align_export.py -- \
      --fbx <prop.fbx> --shift_type visual --target_mode match_anchor \
      --out_fbx <export.fbx> --out_blend <aligned.blend> --render_dir <dir> --out <verdict.json> \
      [--yaw 90] [--mirror x] [--front_axis -Y] [--iteration 1] [--allow_nonuniform]

Heuristic pre-align (ground + center + long-horizontal-axis -> anchor's) -> optional
yaw/mirror nudges (the runbook operator's K=3 vision loop drives these) -> UNIFORM scale
(visual: best-fit to anchor, hard-fail if not within 20% on all axes; geometric:
least-squares uniform fit to absolute target, halt if it lands in the visual band) ->
export with the anchor-pinned FBX preset -> round-trip render beside the anchor for GATE 1.

Verdict (common envelope) carries realized_LWH, the dimension_check verdict, scale, and render paths.
Exit codes: 0 ok; 10 visual-unfittable; 11 geometric-in-visual-band; 1 other hard failure.
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
from dimension_check import classify  # noqa: E402

COLLISION_PREFIXES = common.COLLISION_MESH_PREFIXES


# --------------------------------------------------------------------------- args
def parse_args():
    av = sys.argv
    args = av[av.index("--") + 1:] if "--" in av else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--fbx", required=True)
    ap.add_argument("--anchor", default=None,
                    help="anchor FBX; defaults to the site config's anchor_fbx")
    ap.add_argument("--shift_type", required=True, choices=["visual", "geometric"])
    ap.add_argument("--target_mode", required=True, choices=["match_anchor", "absolute"])
    ap.add_argument("--target_L", type=float)
    ap.add_argument("--target_W", type=float)
    ap.add_argument("--target_H", type=float)
    ap.add_argument("--out_fbx", required=True)
    ap.add_argument("--out_blend", required=True)
    ap.add_argument("--render_dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--yaw", type=float, default=0.0, help="extra yaw (deg) applied on top of heuristic")
    ap.add_argument("--mirror", choices=["x", "y", "none"], default="none")
    ap.add_argument("--front_axis", default=None, help="escape hatch e.g. -Y; sets base yaw")
    ap.add_argument("--iteration", type=int, default=0)
    ap.add_argument("--allow_nonuniform", action="store_true")
    ap.add_argument("--no_render", action="store_true")
    ap.add_argument("--textures_json", default=None,
                    help="texture_classify verdict JSON. When given, BUILD a Principled BSDF "
                         "material from data.canonical_set and export the FBX WITH textures "
                         "embedded, so UE imports the material natively (fixes grey-checkerboard). "
                         "When omitted, behavior is unchanged (mesh-only export).")
    parsed = ap.parse_args(args)
    if parsed.anchor is None:
        # resolved lazily so that --help works on a machine with no site config
        parsed.anchor = common.ANCHOR_FBX
    return parsed


# --------------------------------------------------------------------------- bpy helpers
def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _enable(addon):
    try:
        import addon_utils
        addon_utils.enable(addon)
    except Exception:
        pass


def import_fbx(path):
    """Import a source mesh by extension: .fbx via FBX, .gltf/.glb via glTF.
    (The Blender->UE EXPORT is always FBX; only the source import varies.)"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".gltf", ".glb"):
        op, addon = bpy.ops.import_scene.gltf, "io_scene_gltf2"
    else:
        op, addon = bpy.ops.import_scene.fbx, "io_scene_fbx"
    try:
        op(filepath=path)
    except (AttributeError, RuntimeError):
        _enable(addon)
        op(filepath=path)
    return [o for o in bpy.context.selected_objects] or list(bpy.context.scene.objects)


def is_collision_mesh(name):
    return any(name.startswith(p) for p in COLLISION_PREFIXES)


def render_mesh_objects(objs):
    return [o for o in objs if o.type == "MESH" and not is_collision_mesh(o.name)]


def world_bbox(objs):
    mins = [1e18] * 3
    maxs = [-1e18] * 3
    for o in objs:
        for c in o.bound_box:
            w = o.matrix_world @ mathutils.Vector(c)
            for i in range(3):
                mins[i] = min(mins[i], w[i])
                maxs[i] = max(maxs[i], w[i])
    dims = [maxs[i] - mins[i] for i in range(3)]
    return mins, maxs, dims


def select_only(objs):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    if objs:
        bpy.context.view_layer.objects.active = objs[0]


def join_into_one(objs, name):
    """Join render meshes into a single object; returns it."""
    meshes = [o for o in objs if o.type == "MESH"]
    if not meshes:
        raise RuntimeError("no mesh objects to join")
    select_only(meshes)
    if len(meshes) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    obj.name = name
    return obj


def apply_transforms(obj):
    select_only([obj])
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


# --------------------------------------------------------------------------- alignment + scale
def ground_and_center(obj):
    apply_transforms(obj)
    mins, maxs, dims = world_bbox([obj])
    cx = (mins[0] + maxs[0]) / 2.0
    cy = (mins[1] + maxs[1]) / 2.0
    obj.location.x -= cx
    obj.location.y -= cy
    obj.location.z -= mins[2]
    apply_transforms(obj)


def flatten_imported():
    """Collapse an import hierarchy into world-space, single-user meshes.

    glTF importers parent meshes under an empty carrying the Y-up->Z-up conversion
    rotation AND set rotation_mode='QUATERNION'; both break local-frame rotation. After
    this, every mesh is unparented, single-user, and has its transform baked in, so
    local frame == world frame and matrix-based yaw bakes correctly. Harmless for FBX
    (flat hierarchy)."""
    bpy.ops.object.select_all(action="SELECT")
    try:
        bpy.ops.object.make_single_user(type="SELECTED_OBJECTS", object=True, obdata=True)
    except RuntimeError:
        pass
    try:
        bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")
    except RuntimeError:
        pass
    for o in list(bpy.context.scene.objects):
        if o.type != "MESH":
            bpy.data.objects.remove(o, do_unlink=True)
    bpy.ops.object.select_all(action="SELECT")
    if bpy.context.selected_objects:
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def rotate_z(obj, deg):
    if abs(deg) < 1e-6:
        return
    select_only([obj])
    # Rotate via matrix_world (mode-agnostic): setting rotation_euler is silently
    # ignored when rotation_mode == 'QUATERNION' (e.g. glTF imports).
    obj.matrix_world = mathutils.Matrix.Rotation(math.radians(deg), 4, "Z") @ obj.matrix_world
    apply_transforms(obj)


def mirror_axis(obj, axis):
    if axis == "none":
        return
    select_only([obj])
    s = obj.scale
    if axis == "x":
        obj.scale = (-s.x, s.y, s.z)
    elif axis == "y":
        obj.scale = (s.x, -s.y, s.z)
    apply_transforms(obj)
    # negative scale flips normals; recalc outside
    select_only([obj])
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")


def heuristic_long_axis(obj):
    """Rotate so the broader horizontal extent lands on Y (anchor is broader in Y: W>L)."""
    _, _, dims = world_bbox([obj])
    ex, ey = dims[0], dims[1]
    if ex > ey:
        rotate_z(obj, 90.0)


FRONT_AXIS_YAW = {"+X": 0, "X": 0, "-X": 180, "+Y": 90, "Y": 90, "-Y": 270, "+Z": 0, "-Z": 0, "Z": 0}


def uniform_scale_visual(dims, anchor):
    """Minimize the max relative deviation across axes -> balance smallest & largest ratio."""
    ratios = [dims[i] / anchor[i] for i in range(3)]
    s = 1.0 / math.sqrt(min(ratios) * max(ratios))
    return s


def uniform_scale_absolute(dims, target):
    """Least-squares uniform fit: minimize sum((s*d - t)^2)."""
    num = sum(dims[i] * target[i] for i in range(3))
    den = sum(dims[i] * dims[i] for i in range(3))
    return num / den if den else 1.0


def apply_uniform_scale(obj, s):
    select_only([obj])
    obj.scale = (obj.scale.x * s, obj.scale.y * s, obj.scale.z * s)
    apply_transforms(obj)


# --------------------------------------------------------------------------- material (fix B)
# slot -> (blender image colorspace, principled input or special). Mirrors
# bpy_textured_render.py's mapping so the EXPORTED material matches the gate-2 preview
# and the UE-side wiring. BaseColor+Normal are the priority (the FBX material model is
# limited; baseColor and normal reliably survive the round-trip).
SLOT_INPUT = {
    "BaseColor": ("sRGB", "Base Color"),
    "Emissive": ("sRGB", "Emission"),
    "Metallic": ("Non-Color", "Metallic"),
    "Roughness": ("Non-Color", "Roughness"),
    "Normal": ("Non-Color", "__normal__"),
    "AO": ("Non-Color", "__ao__"),
}


def build_material_from_canonical(canonical):
    """Build a clean Principled BSDF material from the texture_classify canonical_set and
    return (material, applied_dict). canonical maps UE slot -> absolute texture path.

      BaseColor -> Base Color (sRGB)
      Normal    -> Normal Map node -> Normal input (Non-Color)
      Metallic  -> Metallic input (Non-Color)
      Roughness -> Roughness input (Non-Color)
      (Emissive/AO optional; skipped if absent.)
    """
    mat = bpy.data.materials.new("OOD_Material")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    x = -800
    applied = {}
    for slot, path in canonical.items():
        if slot not in SLOT_INPUT or not path or not os.path.exists(path):
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
            if "Normal" in bsdf.inputs:
                nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
        elif target == "__ao__":
            # AO has no dedicated Principled input; skip (UE handles AO on its side).
            continue
        elif target in bsdf.inputs:
            # single-channel (Non-Color) maps drive scalar inputs via the Color output's grey value.
            nt.links.new(tex.outputs["Color"], bsdf.inputs[target])
        else:
            continue
        applied[slot] = path
    return mat, applied


def assign_material(obj, mat):
    """Replace the object's material slots with the single built material."""
    obj.data.materials.clear()
    obj.data.materials.append(mat)


# --------------------------------------------------------------------------- export
def export_fbx(obj, path, embed_textures=False):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    select_only([obj])
    kwargs = dict(
        filepath=path,
        use_selection=True,
        object_types={"MESH"},
        axis_forward="X",
        axis_up="Z",
        mesh_smooth_type="FACE",
        add_leaf_bones=False,
        bake_anim=False,
        apply_unit_scale=True,
        global_scale=1.0,
        use_mesh_modifiers=True,
    )
    if embed_textures:
        # Export materials + textures so UE imports them natively (import_materials/textures=True),
        # giving the material the same provenance as an FBX-importer-built one (fix B).
        kwargs["path_mode"] = "COPY"
        kwargs["embed_textures"] = True
    bpy.ops.export_scene.fbx(**kwargs)


# --------------------------------------------------------------------------- render (gate 1)
def setup_render_engine():
    scene = bpy.context.scene
    # Prefer Workbench (fast, reliable headless for shape/orientation); Cycles CPU fallback.
    try:
        scene.render.engine = "BLENDER_WORKBENCH"
        scene.display.shading.light = "STUDIO"
        # per-object flat color -> prop vs anchor are visually distinct in the gate render,
        # and we avoid pulling (absent) textures off the FBX.
        scene.display.shading.color_type = "OBJECT"
        scene.world = bpy.data.worlds.new("W") if bpy.data.worlds.get("W") is None else scene.world
        return "BLENDER_WORKBENCH"
    except Exception:
        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = 16
        return "CYCLES"


def add_sun():
    light = bpy.data.lights.new("Sun", type="SUN")
    light.energy = 3.0
    lo = bpy.data.objects.new("Sun", light)
    bpy.context.collection.objects.link(lo)
    lo.rotation_euler = (math.radians(50), math.radians(20), math.radians(30))


def look_at(cam_obj, target):
    direction = target - cam_obj.location
    rot = direction.to_track_quat("-Z", "Y")
    cam_obj.rotation_euler = rot.to_euler()


def render_views(render_dir, iteration, extent):
    os.makedirs(render_dir, exist_ok=True)
    setup_render_engine()
    add_sun()
    scene = bpy.context.scene
    scene.render.resolution_x = 1280
    scene.render.resolution_y = 720
    scene.render.film_transparent = False

    cam_data = bpy.data.cameras.new("Cam")
    cam = bpy.data.objects.new("Cam", cam_data)
    bpy.context.collection.objects.link(cam)
    scene.camera = cam

    target = mathutils.Vector((0, 0, extent * 0.4))
    r = extent * 2.6
    views = {
        "front": (0, -r, extent * 0.6),
        "side": (r, 0, extent * 0.6),
        "threequarter": (r * 0.8, -r * 0.8, extent * 0.9),
        "top": (0.01, -0.01, r * 1.2),
    }
    paths = {}
    for name, (x, y, z) in views.items():
        cam.location = mathutils.Vector((x, y, z))
        look_at(cam, target)
        p = os.path.join(render_dir, f"gate1_iter{iteration}_{name}.png")
        scene.render.filepath = p
        bpy.ops.render.render(write_still=True)
        paths[name] = p
    return paths


# --------------------------------------------------------------------------- main
def main():
    a = parse_args()
    try:
        # ---- align/scale the prop in a working scene ----
        reset_scene()
        import_fbx(a.fbx)
        # collapse any import hierarchy (glTF Y-up parent + QUATERNION mode) so yaw works;
        # re-query the scene afterwards (flatten deletes the import's empty objects).
        flatten_imported()
        render_objs = render_mesh_objects(list(bpy.context.scene.objects))
        if not render_objs:
            raise RuntimeError(f"no render meshes found in {a.fbx}")
        # drop UE collision meshes (UE auto-generates collision on import)
        for o in list(bpy.context.scene.objects):
            if o.type == "MESH" and is_collision_mesh(o.name):
                bpy.data.objects.remove(o, do_unlink=True)
        obj = join_into_one(render_mesh_objects(list(bpy.context.scene.objects)),
                            name="PROP")
        ground_and_center(obj)

        # orientation: front_axis escape hatch sets base yaw, else heuristic; then nudges
        if a.front_axis:
            rotate_z(obj, FRONT_AXIS_YAW.get(a.front_axis, 0))
        else:
            heuristic_long_axis(obj)
        rotate_z(obj, a.yaw)
        mirror_axis(obj, a.mirror)
        ground_and_center(obj)

        _, _, native_dims = world_bbox([obj])

        # scale (uniform only)
        anchor = list(common.ANCHOR_LWH)
        if a.target_mode == "match_anchor":
            s = uniform_scale_visual(native_dims, anchor)
        else:
            target = [a.target_L, a.target_W, a.target_H]
            if None in target:
                raise RuntimeError("absolute mode requires --target_L/W/H")
            s = uniform_scale_absolute(native_dims, target)
        apply_uniform_scale(obj, s)
        ground_and_center(obj)

        _, _, realized = world_bbox([obj])
        realized_LWH = [round(realized[0], 4), round(realized[1], 4), round(realized[2], 4)]

        # classify + gate (hard-fail on band mismatch)
        cls = classify(realized_LWH[0], realized_LWH[1], realized_LWH[2])
        verdict_cls = cls["verdict"]
        band_ok = (verdict_cls == a.shift_type)
        gate_error = None
        exit_code = 0
        if not band_ok:
            if a.shift_type == "visual":
                gate_error = ("visual shape unfittable: uniform scale lands at delta_max="
                              f"{cls['deltas']['delta_max']:.3f} (>0.20). Shape too different "
                              "to be a visual shift; swap prop or reclassify.")
                exit_code = 10
            else:
                gate_error = ("geometric target lands inside the visual band "
                              f"(delta_max={cls['deltas']['delta_max']:.3f} <=0.20). "
                              "Pick a more distinct real-world size or accept artificial size (human).")
                exit_code = 11

        # ---- (fix B) optionally BUILD + ASSIGN a PBR material from canonical_set, just
        # before export, on the already-aligned/scaled/joined single object. When given,
        # the FBX is exported WITH textures embedded so UE imports the material natively
        # (same provenance as DutchCrashAttenuator -> no grey checkerboard). ----
        material_applied = None
        embed = False
        if a.textures_json:
            with open(a.textures_json) as f:
                tj = json.load(f)
            canonical = (tj.get("data") or {}).get("canonical_set") or {}
            mat, material_applied = build_material_from_canonical(canonical)
            assign_material(obj, mat)
            embed = True

        # export (even on gate fail we keep artifacts for inspection, but only mark ok if band_ok)
        export_fbx(obj, a.out_fbx, embed_textures=embed)
        os.makedirs(os.path.dirname(os.path.abspath(a.out_blend)), exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=a.out_blend)

        # ---- round-trip render: re-import EXPORTED fbx beside the anchor ----
        render_paths = {}
        roundtrip_dims = None
        if not a.no_render:
            reset_scene()
            rt = import_fbx(a.out_fbx)
            rt_obj = join_into_one(render_mesh_objects(list(bpy.context.scene.objects)), "PROP_RT")
            _, _, roundtrip_dims = world_bbox([rt_obj])
            roundtrip_dims = [round(d, 4) for d in roundtrip_dims]
            # place prop and anchor side by side
            import_fbx(a.anchor)
            # drop UE collision meshes (e.g. anchor's UCX_*) so they don't render as stray blobs
            for o in list(bpy.context.scene.objects):
                if o.type == "MESH" and is_collision_mesh(o.name):
                    bpy.data.objects.remove(o, do_unlink=True)
            anc = join_into_one([o for o in bpy.context.scene.objects
                                 if o.type == "MESH" and o is not rt_obj], "ANCHOR")
            # ground both, separate along X
            ground_and_center(rt_obj)
            ground_and_center(anc)
            rt_obj.color = (0.85, 0.25, 0.20, 1.0)   # prop = red
            anc.color = (0.25, 0.55, 0.90, 1.0)      # anchor = blue
            sep = (max(realized_LWH[:2]) + max(anchor[:2])) * 0.8 + 1.0
            rt_obj.location.x = -sep / 2
            anc.location.x = sep / 2
            extent = max(max(realized_LWH), max(anchor)) + sep
            render_paths = render_views(a.render_dir, a.iteration, extent)

        data = {
            "fbx_in": a.fbx,
            "out_fbx": a.out_fbx,
            "out_blend": a.out_blend,
            "native_dims_XYZ": [round(d, 4) for d in native_dims],
            "scale_factor": round(s, 6),
            "realized_LWH": realized_LWH,
            "roundtrip_dims_XYZ": roundtrip_dims,
            "applied": {"yaw": a.yaw, "mirror": a.mirror, "front_axis": a.front_axis,
                        "iteration": a.iteration, "heuristic_long_axis": not bool(a.front_axis)},
            "shift_declared": a.shift_type,
            "shift_check": cls,
            "band_ok": band_ok,
            "render_paths": render_paths,
            "textures_json": a.textures_json,
            "embedded_textures": embed,
            "material_slots_applied": sorted(material_applied.keys()) if material_applied else None,
        }
        verdict = common.make_verdict("blender", ok=band_ok, data=data, error=gate_error)
        common.write_verdict(a.out, verdict)
        sys.exit(exit_code)

    except SystemExit:
        raise
    except Exception as e:
        import traceback
        v = common.make_verdict("blender", ok=False, data={"fbx_in": a.fbx},
                                error=f"{e!r}\n{traceback.format_exc()}")
        try:
            common.write_verdict(a.out, v)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
