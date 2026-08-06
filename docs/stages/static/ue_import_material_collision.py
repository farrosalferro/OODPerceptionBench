"""
ue_import_material_collision.py — Agent 3 (see the procedure document). Run headless:

  UE4Editor-Cmd CarlaUE4.uproject -run=pythonscript \
    -script="ue_import_material_collision.py --asset_name <A> --fbx <export.fbx> \
             --textures_json <texture_classify.json> --master /Game/OODProps/M_OODPropMaster \
             --out <verdict.json>" -unattended -nosplash -nullrhi -stdout

Imports the Blender-exported FBX (auto-collision ON, skeletal OFF, no transform), renames
the SM to SM_<A>, imports the canonical separate-channel textures with correct sRGB/compression,
instances M_OODPropMaster as MI_<A> (texture params from texture_classify's canonical_set),
assigns it, sets collision response BlockAll + Query&Physics (with a fallback ladder if
auto-collision is empty), and runs Tier-1 verification. Writes a JSON verdict to --out.

Self-contained (no common.py import — UE python is 3.7); the runbook operator resolves names in
the client interpreter and passes them in. Verified against UE 4.26.2 API (see dev/ue_api.json).
"""
import argparse
import json
import os
import sys
import time

import unreal


# ----------------------------------------------------------------------- args / io
def parse_args():
    av = sys.argv
    # commandlet passes the whole "-script=..." as one arg list after the .py; argparse
    # over sys.argv works because UE forwards tokens after the script path.
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset_name", required=True)
    ap.add_argument("--fbx", required=True)
    ap.add_argument("--textures_json", required=True)
    ap.add_argument("--master", default="/Game/OODProps/M_OODPropMaster")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no_fallback", action="store_true")
    # FIX (B): when set, let UE's FBX importer CREATE the material(s)+texture(s) natively
    # (same provenance as the working reference prop DutchCrashAttenuator) instead of
    # building a material programmatically via MaterialEditingLibrary (which cooks to an
    # invalid shader -> grey checkerboard). --master is ignored in this mode.
    ap.add_argument("--native_material", action="store_true")
    # ignore unknown UE flags
    args, _ = ap.parse_known_args(av)
    return args


def write_verdict(out, ok, data, error=None):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        json.dump({"stage": "ue_import", "ok": bool(ok), "data": data,
                   "error": error, "ts": time.time()}, f, indent=2)


# slot -> (srgb, compression)
TC = unreal.TextureCompressionSettings
SLOT_TEX_SETTINGS = {
    "BaseColor": (True, TC.TC_DEFAULT),
    "Emissive": (True, TC.TC_DEFAULT),
    "Normal": (False, TC.TC_NORMALMAP),
    "Metallic": (False, TC.TC_GRAYSCALE),
    "Roughness": (False, TC.TC_GRAYSCALE),
    "AO": (False, TC.TC_GRAYSCALE),
}

# Self-contained material graph (built per-prop, in-package). slot -> (sampler, output pin, material property).
_ST = unreal.MaterialSamplerType


def _sampler(*cands):
    for c in cands:
        if hasattr(_ST, c):
            return getattr(_ST, c)
    raise AttributeError(f"none of {cands} on MaterialSamplerType")


_COLOR = _sampler("SAMPLERTYPE_COLOR")
_NORMAL = _sampler("SAMPLERTYPE_NORMAL")
_LING = _sampler("SAMPLERTYPE_LINEAR_GRAYSCALE", "SAMPLERTYPE_GRAYSCALE", "SAMPLERTYPE_MASKS")
_MP = unreal.MaterialProperty
SLOT_GRAPH = {
    "BaseColor": (_COLOR, "RGB", _MP.MP_BASE_COLOR),
    "Normal": (_NORMAL, "RGB", _MP.MP_NORMAL),
    "Metallic": (_LING, "R", _MP.MP_METALLIC),
    "Roughness": (_LING, "R", _MP.MP_ROUGHNESS),
    "AO": (_LING, "R", _MP.MP_AMBIENT_OCCLUSION),
    "Emissive": (_COLOR, "RGB", _MP.MP_EMISSIVE_COLOR),
}


def import_static_mesh(fbx, dest_path, sm_name):
    task = unreal.AssetImportTask()
    task.filename = fbx
    task.destination_path = dest_path
    task.destination_name = sm_name
    task.automated = True
    task.save = True
    task.replace_existing = True

    ui = unreal.FbxImportUI()
    ui.import_mesh = True
    ui.import_as_skeletal = False
    ui.import_materials = False        # we assign our own master-material instance
    ui.import_textures = False         # textures imported separately with explicit sRGB
    ui.create_physics_asset = False
    ui.mesh_type_to_import = unreal.FBXImportType.FBXIT_STATIC_MESH
    smid = ui.static_mesh_import_data
    smid.set_editor_property("auto_generate_collision", True)
    smid.set_editor_property("combine_meshes", True)
    smid.set_editor_property("import_uniform_scale", 1.0)
    task.options = ui

    tools = unreal.AssetToolsHelpers.get_asset_tools()
    tools.import_asset_tasks([task])
    return list(task.imported_object_paths or [])


def import_static_mesh_native(fbx, dest_path, sm_name):
    """FIX (B): native FBX import that lets UE's importer CREATE the material(s) and
    texture(s) from the FBX (import_materials/import_textures=True) and auto-assign the
    material to the SM. Same provenance as the reference prop that renders correctly.
    Everything else matches import_static_mesh (static, no skeletal, auto-collision,
    combine_meshes). Returns the list of imported object paths (SM + materials + textures)."""
    task = unreal.AssetImportTask()
    task.filename = fbx
    task.destination_path = dest_path
    task.destination_name = sm_name
    task.automated = True
    task.save = True
    task.replace_existing = True

    ui = unreal.FbxImportUI()
    ui.import_mesh = True
    ui.import_as_skeletal = False
    ui.import_materials = True          # UE creates + auto-assigns the material(s)
    ui.import_textures = True           # UE creates the texture asset(s) from the FBX
    ui.create_physics_asset = False
    ui.mesh_type_to_import = unreal.FBXImportType.FBXIT_STATIC_MESH
    smid = ui.static_mesh_import_data
    smid.set_editor_property("auto_generate_collision", True)
    smid.set_editor_property("combine_meshes", True)
    smid.set_editor_property("import_uniform_scale", 1.0)
    task.options = ui

    tools = unreal.AssetToolsHelpers.get_asset_tools()
    tools.import_asset_tasks([task])
    return list(task.imported_object_paths or [])


def _texture_role(name):
    """Best-effort role from an imported texture asset name (matched case-insensitively).
    Returns one of: 'base', 'normal', 'gray', or None (leave UE's import defaults)."""
    n = (name or "").lower()
    if any(k in n for k in ("normal", "_norm", "_nrm", "_n")):
        return "normal"
    if any(k in n for k in ("basecolor", "base_color", "albedo", "diffuse",
                            "_color", "_col", "_diff", "_basecol", "_bc")):
        return "base"
    if any(k in n for k in ("metallic", "metalness", "metal", "roughness", "_rough",
                            "_metrough", "_mr", "_orm", "ambientocclusion", "_ao",
                            "occlusion", "specular", "_spec", "mask")):
        return "gray"
    return None


# role -> (srgb, compression). For native-imported textures we VERIFY/CORRECT only.
ROLE_TEX_SETTINGS = {
    "base": (True, TC.TC_DEFAULT),
    "normal": (False, TC.TC_NORMALMAP),
    "gray": (False, TC.TC_GRAYSCALE),
}


def _assets_in_dir(dest_path):
    """Object paths of every asset under dest_path. UE's native FBX import creates the
    material + textures as SIBLING packages of the SM, but does NOT list them in
    task.imported_object_paths (only the primary SM is returned). So we enumerate the
    whole prop directory to find the material/textures UE generated."""
    eal = unreal.EditorAssetLibrary
    try:
        return list(eal.list_assets(dest_path, recursive=True, include_folder=False) or [])
    except Exception:
        return []


def fix_imported_textures(imported_paths, dest_path=None):
    """Verify/correct sRGB + compression on the textures UE created during native import,
    inferring the role from the asset name. UE usually sets these correctly on import; we
    only override when our role mapping disagrees. Saves any asset we touch.
    Returns a report list and the list of texture asset object paths.

    `dest_path`, when given, is scanned for texture assets too — UE does NOT report the
    materials/textures it auto-creates in task.imported_object_paths, so relying on
    imported_paths alone misses every native-import texture."""
    eal = unreal.EditorAssetLibrary
    report = []
    tex_paths = []
    seen = set()
    candidates = list(imported_paths)
    if dest_path:
        candidates += _assets_in_dir(dest_path)
    for p in candidates:
        obj_path = p.split(".")[0]
        if obj_path in seen:
            continue
        seen.add(obj_path)
        if not eal.does_asset_exist(obj_path):
            continue
        asset = eal.load_asset(obj_path)
        if not isinstance(asset, unreal.Texture):
            continue
        tex_paths.append(obj_path)
        role = _texture_role(asset.get_name())
        entry = {"texture": obj_path, "name": asset.get_name(), "role": role}
        if role is None:
            entry["action"] = "left_as_imported"
            report.append(entry)
            continue
        want_srgb, want_comp = ROLE_TEX_SETTINGS[role]
        changed = False
        try:
            cur_srgb = bool(asset.get_editor_property("srgb"))
            if cur_srgb != want_srgb:
                asset.set_editor_property("srgb", want_srgb)
                changed = True
            cur_comp = asset.get_editor_property("compression_settings")
            if cur_comp != want_comp:
                asset.set_editor_property("compression_settings", want_comp)
                changed = True
            entry.update({"srgb": want_srgb, "compression": str(want_comp), "changed": changed})
            if changed:
                eal.save_loaded_asset(asset)
        except Exception as e:
            entry["err"] = repr(e)
        report.append(entry)
    return report, tex_paths


def assigned_material_paths(sm):
    """Object paths of the materials currently assigned to the SM's material slots."""
    out = []
    try:
        static_materials = sm.get_editor_property("static_materials")
        for smat in (static_materials or []):
            try:
                mi = smat.get_editor_property("material_interface")
            except Exception:
                mi = None
            if mi is not None:
                try:
                    out.append(mi.get_path_name().split(".")[0])
                except Exception:
                    out.append(str(mi))
            else:
                out.append(None)
    except Exception:
        pass
    return out


def find_static_mesh(imported_paths, dest_path, sm_name):
    eal = unreal.EditorAssetLibrary
    target = f"{dest_path}/{sm_name}"
    if eal.does_asset_exist(target):
        a = eal.load_asset(target)
        if isinstance(a, unreal.StaticMesh):
            return a, target
    # else hunt among imported paths
    for p in imported_paths:
        p = p.split(".")[0]
        if eal.does_asset_exist(p):
            a = eal.load_asset(p)
            if isinstance(a, unreal.StaticMesh):
                if a.get_name() != sm_name:
                    eal.rename_asset(p, target)
                    a = eal.load_asset(target)
                    return a, target
                return a, p
    return None, target


def import_texture(path, dest_path, name):
    task = unreal.AssetImportTask()
    task.filename = path
    task.destination_path = dest_path
    task.destination_name = name
    task.automated = True
    task.save = True
    task.replace_existing = True
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    tools.import_asset_tasks([task])
    eal = unreal.EditorAssetLibrary
    tp = f"{dest_path}/{name}"
    return eal.load_asset(tp) if eal.does_asset_exist(tp) else None


def setup_collision(sm, allow_fallback):
    esml = unreal.EditorStaticMeshLibrary
    ladder = []

    def counts():
        return (esml.get_simple_collision_count(sm), esml.get_convex_collision_count(sm))

    simple, convex = counts()
    ladder.append({"step": "auto_import", "simple": simple, "convex": convex})

    if allow_fallback and simple + convex == 0:
        try:
            esml.set_convex_decomposition_collisions(sm, 8, 32, 100000)
        except Exception as e:
            ladder.append({"step": "convex_decomp_err", "err": repr(e)})
        simple, convex = counts()
        ladder.append({"step": "convex_decomp(8)", "simple": simple, "convex": convex})

    if allow_fallback and simple + convex == 0:
        try:
            esml.set_convex_decomposition_collisions(sm, 1, 32, 100000)
        except Exception as e:
            ladder.append({"step": "single_hull_err", "err": repr(e)})
        simple, convex = counts()
        ladder.append({"step": "single_hull", "simple": simple, "convex": convex})

    if allow_fallback and simple + convex == 0:
        try:
            esml.add_simple_collisions(sm, unreal.ScriptingCollisionShapeType.BOX)
        except Exception as e:
            ladder.append({"step": "box_err", "err": repr(e)})
        simple, convex = counts()
        ladder.append({"step": "box", "simple": simple, "convex": convex})

    # response/enabled on the body instance
    try:
        bs = sm.get_editor_property("body_setup")
        di = bs.get_editor_property("default_instance")
        di.set_editor_property("collision_profile_name", "BlockAll")
        di.set_editor_property("collision_enabled", unreal.CollisionEnabled.QUERY_AND_PHYSICS)
        bs.set_editor_property("default_instance", di)
        bs.set_editor_property("collision_trace_flag", unreal.CollisionTraceFlag.CTF_USE_DEFAULT)
        sm.set_editor_property("body_setup", bs)
    except Exception as e:
        ladder.append({"step": "set_response_err", "err": repr(e)})

    return ladder, simple, convex


def tier1_coverage(sm):
    """Best-effort collision-vs-render bbox coverage. KConvexElem has no vertex_data in
    4.26; attempt elem_box, else return None (Tier-2 probe is the behavioral safety net)."""
    out = {"render_full_cm": None, "collision_full_cm": None, "coverage": None, "method": None}
    try:
        ext = sm.get_bounds().box_extent
        render_full = [2 * ext.x, 2 * ext.y, 2 * ext.z]
        out["render_full_cm"] = [round(v, 2) for v in render_full]
    except Exception as e:
        out["render_err"] = repr(e)
        return out
    try:
        bs = sm.get_editor_property("body_setup")
        agg = bs.get_editor_property("agg_geom")
        for elems_name, kind in (("convex_elems", "convex"), ("box_elems", "box"),
                                 ("sphere_elems", "sphere"), ("sphyl_elems", "sphyl")):
            try:
                elems = agg.get_editor_property(elems_name)
            except Exception:
                elems = None
            if elems:
                box = None
                try:
                    box = elems[0].get_editor_property("elem_box")  # convex hull aabb
                except Exception:
                    box = None
                if box is not None:
                    cd = [box.max.x - box.min.x, box.max.y - box.min.y, box.max.z - box.min.z]
                    out["collision_full_cm"] = [round(v, 2) for v in cd]
                    cov = [cd[i] / render_full[i] if render_full[i] else 0 for i in range(3)]
                    out["coverage"] = [round(c, 3) for c in cov]
                    out["method"] = f"{kind}.elem_box"
                return out
    except Exception as e:
        out["collision_err"] = repr(e)
    return out


def main():
    a = parse_args()
    A = a.asset_name
    dest_path = f"/Game/{A}/Static/Other/{A}"
    sm_name = f"SM_{A}"
    mi_name = f"MI_{A}"
    data = {"asset_name": A, "dest_path": dest_path, "sm_name": sm_name,
            "native_material": bool(a.native_material)}
    try:
        eal = unreal.EditorAssetLibrary
        MEL = unreal.MaterialEditingLibrary

        # ---- textures from texture_classify canonical_set ----
        with open(a.textures_json) as f:
            tj = json.load(f)
        canonical = (tj.get("data") or {}).get("canonical_set") or {}
        data["canonical_slots"] = sorted(canonical.keys())

        if a.native_material:
            # =============================================================== FIX (B)
            # Let UE's FBX importer build + auto-assign the material and create the
            # textures (same provenance as the working reference prop). No
            # MaterialEditingLibrary, no separate texture import, no M_<A>.
            imported = import_static_mesh_native(a.fbx, dest_path, sm_name)
            sm, sm_path = find_static_mesh(imported, dest_path, sm_name)
            if sm is None:
                raise RuntimeError(f"static mesh not found after import; imported={imported}")
            data["sm_path"] = sm_path
            data["imported_object_paths"] = imported

            # verify/correct sRGB+compression on the textures UE created. UE does NOT
            # report auto-created materials/textures in task.imported_object_paths, so we
            # scan the prop directory (dest_path) to discover them.
            tex_report, tex_paths = fix_imported_textures(imported, dest_path=dest_path)
            data["textures"] = tex_report
            data["imported_texture_paths"] = tex_paths

            # CRITICAL: persist the material + textures UE created during native import.
            # import_asset_tasks(save=True) only reliably saves the PRIMARY asset (the SM);
            # the dependent material/texture packages are left dirty-but-unsaved. If we only
            # save the SM, the cook can't find /Game/.../OOD_Material on disk -> the SM
            # falls back to WorldGridMaterial (grey checkerboard) in the cooked server.
            # save_directory flushes every dirty package under the prop dir to disk.
            mat_paths = assigned_material_paths(sm)
            saved_report = {}
            for obj_path in sorted(set(p for p in (mat_paths + tex_paths) if p)):
                if eal.does_asset_exist(obj_path):
                    try:
                        saved_report[obj_path] = bool(eal.save_asset(obj_path, only_if_is_dirty=False))
                    except Exception as e:
                        saved_report[obj_path] = f"err:{e!r}"
            # belt-and-suspenders: save the whole prop directory (catches any dependent
            # package not reachable via the SM's slots, e.g. nested texture parameters).
            try:
                data["saved_directory"] = bool(eal.save_directory(dest_path, only_if_is_dirty=False, recursive=True))
            except Exception as e:
                data["saved_directory"] = f"err:{e!r}"
            eal.save_loaded_asset(sm)
            data["saved_assets"] = saved_report
            data["assigned_material_paths"] = mat_paths
            data["material_path"] = mat_paths[0] if mat_paths else None
            data["material_slots_set"] = len(mat_paths)
            # verify the assigned material(s) are actually on disk now (the bug was a
            # silently-missing material .uasset). Surface it in the verdict so a missing
            # material is a HARD failure, not a false success that re-cooks grey.
            missing_mats = [p for p in mat_paths if p and not eal.does_asset_exist(p)]
            data["missing_material_assets"] = missing_mats
            if missing_mats:
                raise RuntimeError(
                    "assigned material(s) not saved to disk after native import: "
                    f"{missing_mats}. Cook would fall back to WorldGridMaterial (grey).")
        else:
            # ---- import SM ----
            imported = import_static_mesh(a.fbx, dest_path, sm_name)
            sm, sm_path = find_static_mesh(imported, dest_path, sm_name)
            if sm is None:
                raise RuntimeError(f"static mesh not found after import; imported={imported}")
            data["sm_path"] = sm_path
            data["imported_object_paths"] = imported

            # ---- import textures + set sRGB/compression ----
            tex_assets = {}
            tex_report = []
            for slot, src in canonical.items():
                if slot not in SLOT_TEX_SETTINGS:
                    tex_report.append({"slot": slot, "skipped": "unknown slot"})
                    continue
                tname = f"T_{A}_{slot}"
                tex = import_texture(src, dest_path, tname)
                if tex is None:
                    tex_report.append({"slot": slot, "src": src, "imported": False})
                    continue
                srgb, comp = SLOT_TEX_SETTINGS[slot]
                tex.set_editor_property("srgb", srgb)
                tex.set_editor_property("compression_settings", comp)
                eal.save_loaded_asset(tex)
                tex_assets[slot] = tex
                tex_report.append({"slot": slot, "src": src, "imported": True,
                                   "srgb": srgb, "compression": str(comp)})
            data["textures"] = tex_report

            # ---- self-contained per-prop material (in-package; NOT a shared master) ----
            # CARLA cooks each prop as its own package. A cross-package master
            # (/Game/OODProps/M_OODPropMaster) does NOT resolve in the cooked prop -> the SM
            # falls back to the engine default (grey checkerboard) in CARLA. So build the
            # material graph INSIDE the prop package with textures wired as the TextureSample
            # node defaults, matching the self-contained reference props (e.g. DutchCrashAttenuator).
            # (--master is now vestigial.)
            mat_name = f"M_{A}"
            mat_path = f"{dest_path}/{mat_name}"
            if eal.does_asset_exist(mat_path):
                eal.delete_asset(mat_path)
            tools = unreal.AssetToolsHelpers.get_asset_tools()
            mat = tools.create_asset(mat_name, dest_path, unreal.Material, unreal.MaterialFactoryNew())
            graph = []
            y = -450
            for slot, tex in tex_assets.items():
                if slot not in SLOT_GRAPH:
                    continue
                sampler, pin, prop = SLOT_GRAPH[slot]
                node = MEL.create_material_expression(mat, unreal.MaterialExpressionTextureSample, -450, y)
                node.set_editor_property("texture", tex)
                node.set_editor_property("sampler_type", sampler)
                connected = MEL.connect_material_property(node, pin, prop)
                graph.append({"slot": slot, "pin": pin, "connected": bool(connected)})
                y += 230
            MEL.recompile_material(mat)
            eal.save_loaded_asset(mat)
            data["material_path"] = mat_path
            data["material_graph"] = graph

            # ---- assign the in-package material to all SM material slots ----
            static_materials = sm.get_editor_property("static_materials")
            nslots = len(static_materials) if static_materials else 0
            for i in range(max(nslots, 1)):
                try:
                    sm.set_material(i, mat)
                except Exception:
                    break
            data["material_slots_set"] = max(nslots, 1)

        # ---- collision ----
        ladder, simple, convex = setup_collision(sm, allow_fallback=not a.no_fallback)
        eal.save_loaded_asset(sm)
        data["collision_ladder"] = ladder
        data["simple_collision_count"] = simple
        data["convex_collision_count"] = convex

        # ---- Tier-1 verify ----
        bs = sm.get_editor_property("body_setup")
        di = bs.get_editor_property("default_instance")
        tier1 = {
            "has_collision": (simple + convex) >= 1,
            "collision_enabled": str(di.get_editor_property("collision_enabled")),
            "collision_profile_name": str(di.get_editor_property("collision_profile_name")),
        }
        tier1.update(tier1_coverage(sm))
        cov = tier1.get("coverage")
        coverage_ok = (cov is None) or (min(cov) >= 0.85)
        tier1["coverage_ok_or_unknown"] = coverage_ok
        tier1["pass"] = bool(tier1["has_collision"] and coverage_ok)
        data["tier1"] = tier1

        ok = tier1["pass"]
        err = None if ok else "Tier-1 collision verification failed (see data.tier1)."
        write_verdict(a.out, ok, data, err)
        unreal.log(f"UE_IMPORT_DONE ok={ok} {sm_path}")
    except Exception as e:
        import traceback
        write_verdict(a.out, False, data, error=f"{e!r}\n{traceback.format_exc()}")
        unreal.log_error("UE_IMPORT_FAIL " + repr(e))


main()
