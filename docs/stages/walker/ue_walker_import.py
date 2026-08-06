"""
ue_walker_import.py -- WALKER IMPORT stage 4 (see the procedure document). Headless UE 4.26.

  UE4Editor-Cmd CarlaUE4.uproject -run=pythonscript \
    -script="ue_walker_import.py --asset_name <W> --sk_fbx <SK_<W>.fbx> \
             --anims_dir <clips_dir> --textures_json <texture_classify_or_resolved.json> \
             --dest_root /Game --out <verdict.json>" \
    -unattended -nosplash -nopause -nullrhi -stdout

Forked from import_automation/stages/ue_import_material_collision.py: keeps the FBX-import
scaffolding, verdict envelope and save-everything (grey-shader) discipline; DROPS the static
collision / Tier-1 half. Imports a skeletal mesh (+ new skeleton + physics asset), renames the
three to the gold convention, imports each armature-only clip as an AnimSequence on that
skeleton, imports the T_<W>_* textures with correct sRGB/compression, builds + assigns a
per-asset material M_<W>, then saves everything. Material may still cook grey until the human
fixes it at G2 (same UE-4.26 limitation as the static pipeline).

Success = the JSON verdict ok:true on disk, NOT the exit code (UE segfaults on teardown).
Self-contained (UE python 3.7; the runbook operator resolves names in the client interpreter and passes argv).

Spike-5 finding baked in: skeletal import auto-names the mesh `<dest_name>_<fbxNode>` and creates
`<that>_Skeleton` + `<that>_PhysicsAsset` siblings -> we rename all three to SK_<W> /
<W>_Skeleton / <W>_PhysicsAsset (== manual-procedure step 5).
"""
import argparse
import glob
import json
import os
import sys
import time
import traceback

import unreal


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset_name", required=True)
    ap.add_argument("--sk_fbx", required=True)
    ap.add_argument("--anims_dir", default=None)
    ap.add_argument("--anims_json", default=None, help="JSON list of clip FBX paths (overrides anims_dir)")
    ap.add_argument("--textures_json", default=None, help="texture_classify verdict (canonical_set)")
    ap.add_argument("--dest_root", default="/Game")
    ap.add_argument("--use_t0_as_ref_pose", default="false")
    ap.add_argument("--direct", action="store_true",
                    help="DIRECT mode: --sk_fbx is a single pre-animated FBX (e.g. the "
                         "peoplewithdisabilities pack); import mesh + baked takes + native "
                         "materials in one pass (no Blender, no separate clip FBXs).")
    ap.add_argument("--clip_name_map", default=None,
                    help="JSON mapping FBX take suffix -> clip stem, e.g. "
                         "{\"forward\":\"Walk\",\"idle\":\"Idle\",\"talking\":\"Talking\"}")
    ap.add_argument("--out", required=True)
    args, _ = ap.parse_known_args(sys.argv)
    return args


def write_verdict(out, ok, data, error=None):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        json.dump({"stage": "ue_import", "ok": bool(ok), "data": data,
                   "error": error, "ts": time.time()}, f, indent=2, default=str)


TC = unreal.TextureCompressionSettings
SLOT_TEX_SETTINGS = {
    "BaseColor": (True, TC.TC_DEFAULT),
    "Emissive": (True, TC.TC_DEFAULT),
    "Normal": (False, TC.TC_NORMALMAP),
    "Metallic": (False, TC.TC_GRAYSCALE),
    "Roughness": (False, TC.TC_GRAYSCALE),
    "AO": (False, TC.TC_GRAYSCALE),
}
_ST = unreal.MaterialSamplerType


def _sampler(*cands):
    for c in cands:
        if hasattr(_ST, c):
            return getattr(_ST, c)
    raise AttributeError("none of %s on MaterialSamplerType" % (cands,))


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


def _bool(s):
    return str(s).lower() in ("1", "true", "yes", "y")


# ------------------------------------------------------------------- skeletal import
def import_skeletal_mesh(fbx, dest_path, dest_name, use_t0):
    task = unreal.AssetImportTask()
    task.filename = fbx
    task.destination_path = dest_path
    task.destination_name = dest_name
    task.automated = True
    task.save = True
    task.replace_existing = True

    ui = unreal.FbxImportUI()
    ui.import_mesh = True
    ui.import_as_skeletal = True
    ui.import_materials = False        # we build + assign M_<W> ourselves
    ui.import_textures = False         # textures imported separately with explicit sRGB
    ui.create_physics_asset = True
    ui.mesh_type_to_import = unreal.FBXImportType.FBXIT_SKELETAL_MESH
    try:
        ui.skeleton = None             # NEW skeleton per walker (rigs are unique)
    except Exception:
        pass
    smid = ui.skeletal_mesh_import_data
    try:
        smid.set_editor_property("use_t0_as_ref_pose", use_t0)
    except Exception:
        pass
    task.options = ui
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    tools.import_asset_tasks([task])
    return list(task.imported_object_paths or [])


def _load_all(paths):
    eal = unreal.EditorAssetLibrary
    out = []
    seen = set()
    for p in paths:
        # keep full object path; load_asset accepts it. does_asset_exist needs the same form.
        if p in seen:
            continue
        seen.add(p)
        if eal.does_asset_exist(p):
            out.append((p, eal.load_asset(p)))
    return out


def _rename_loaded(tools, eal, asset, dest_path, new_name):
    """Rename a LOADED asset's object+package to dest_path/new_name via AssetTools.
    (EditorAssetLibrary.rename_asset takes package paths and fails when the object name
    differs from the package -- which is exactly the skeletal-import case, where the mesh
    object is `<pkg>_<fbxNode>`. AssetTools.rename_assets renames the loaded UObject itself.)"""
    target = "%s/%s" % (dest_path, new_name)
    if asset.get_name() == new_name and asset.get_path_name().split(".")[0] == target:
        return asset, target, "already"
    if eal.does_asset_exist(target):
        eal.delete_asset(target)
    try:
        rd = unreal.AssetRenameData(asset=asset, new_package_path=dest_path, new_name=new_name)
    except Exception:
        rd = unreal.AssetRenameData()
        rd.set_editor_property("asset", asset)
        rd.set_editor_property("new_package_path", dest_path)
        rd.set_editor_property("new_name", new_name)
    ok = tools.rename_assets([rd])
    a2 = eal.load_asset(target) if eal.does_asset_exist(target) else None
    return (a2 if a2 is not None else asset), target, ("renamed" if a2 is not None else "rename_failed:%s" % ok)


def import_and_rename_skeletal(fbx, dest_path, names, use_t0):
    """Import skeletal FBX, then rename SK / Skeleton / PhysicsAsset to the gold convention.

    Skeletal import auto-names the mesh `<dest>_<fbxNode>` (object != package) and creates
    `<that>_Skeleton` / `<that>_PhysicsAsset` siblings. We rename the loaded objects (not the
    packages) via AssetTools so the result is clean SK_<W> / <W>_Skeleton / <W>_PhysicsAsset."""
    eal = unreal.EditorAssetLibrary
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    imported = import_skeletal_mesh(fbx, dest_path, names["sk"], use_t0)
    # gather everything the import produced (imported paths + whole dest dir)
    listed = list(eal.list_assets(dest_path, recursive=True, include_folder=False) or [])
    loaded = _load_all(list(imported) + listed)
    report = {"imported_object_paths": imported, "created": []}
    sk = skel = phys = None
    for p, a in loaded:
        t = type(a).__name__
        report["created"].append({"path": p, "type": t, "name": a.get_name()})
        if isinstance(a, unreal.SkeletalMesh) and sk is None:
            sk = a
        elif isinstance(a, unreal.Skeleton) and skel is None:
            skel = a
        elif isinstance(a, unreal.PhysicsAsset) and phys is None:
            phys = a
    if sk is None:
        raise RuntimeError("no SkeletalMesh after import; created=%s" % report["created"])
    # rename physics + skeleton FIRST (before SK) so their names are final, then SK
    if phys is not None:
        phys, phys_path, st = _rename_loaded(tools, eal, phys, dest_path, names["physics"])
        report["physics_path"], report["physics_rename"] = phys_path, st
    if skel is None:
        skel = sk.get_editor_property("skeleton")
    if skel is not None:
        skel, skel_path, st = _rename_loaded(tools, eal, skel, dest_path, names["skeleton"])
        report["skeleton_path"], report["skeleton_rename"] = skel_path, st
    sk, sk_path, st = _rename_loaded(tools, eal, sk, dest_path, names["sk"])
    report["sk_path"], report["sk_rename"] = sk_path, st
    skel = sk.get_editor_property("skeleton") or skel
    return sk, skel, report


# ------------------------------------------------------------------- anims
def import_anim(fbx, dest_path, skel):
    task = unreal.AssetImportTask()
    task.filename = fbx
    task.destination_path = dest_path
    task.destination_name = os.path.splitext(os.path.basename(fbx))[0]  # A_<W>_<Clip>_01
    task.automated = True
    task.save = True
    task.replace_existing = True
    ui = unreal.FbxImportUI()
    ui.import_mesh = False
    ui.import_as_skeletal = True
    ui.import_animations = True
    ui.create_physics_asset = False
    ui.mesh_type_to_import = unreal.FBXImportType.FBXIT_ANIMATION
    ui.skeleton = skel
    task.options = ui
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    return list(task.imported_object_paths or [])


# ------------------------------------------------------------------- textures + material
def import_texture(path, dest_path, name):
    task = unreal.AssetImportTask()
    task.filename = path
    task.destination_path = dest_path
    task.destination_name = name
    task.automated = True
    task.save = True
    task.replace_existing = True
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    eal = unreal.EditorAssetLibrary
    tp = "%s/%s" % (dest_path, name)
    return eal.load_asset(tp) if eal.does_asset_exist(tp) else None


def build_material(asset_name, dest_path, tex_assets):
    eal = unreal.EditorAssetLibrary
    MEL = unreal.MaterialEditingLibrary
    mat_name = "M_%s" % asset_name
    mat_path = "%s/%s" % (dest_path, mat_name)
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
    return mat, mat_path, graph


def assign_skeletal_material(sk, mat):
    """Assign `mat` to every material slot of the skeletal mesh. Returns the slot count.

    Rebuild the array with FRESH SkeletalMaterial structs -- mutating the returned array's
    elements in place does not persist (UE returns struct copies), and the import may have
    auto-linked a same-named material from another package (e.g. the gold M_Cow) that we must
    override."""
    mats = sk.get_editor_property("materials")
    n = len(mats) if mats else 0
    count = max(n, 1)
    new = []
    for i in range(count):
        slot = unreal.SkeletalMaterial()
        slot.set_editor_property("material_interface", mat)
        if i < n:
            try:
                slot.set_editor_property("material_slot_name",
                                         mats[i].get_editor_property("material_slot_name"))
            except Exception:
                pass
        new.append(slot)
    sk.set_editor_property("materials", new)
    return count


def assigned_material_ok(sk):
    mats = sk.get_editor_property("materials")
    if not mats:
        return False, []
    out = []
    ok = True
    for m in mats:
        mi = m.get_editor_property("material_interface")
        out.append(mi.get_path_name().split(".")[0] if mi else None)
        if mi is None:
            ok = False
    return ok, out


# ------------------------------------------------------------------- direct mode
CLIP_DEFAULT_MAP = {"forward": "Walk", "idle": "Idle", "talking": "Talking",
                    "walk": "Walk", "run": "Run", "idlebreak": "IdleBreak", "eating": "Eating"}


def direct_import_main(a):
    """DIRECT mode: one pre-animated FBX (peoplewithdisabilities pack etc.) -> mesh + baked takes
    + native materials, in a single import (matches the working CaneMan asset). No Blender. The
    Max-biped orientation, the multi-armature crutch/cane props and the animations all come across
    correctly because UE's own FBX importer handles them -- re-exporting through Blender is what
    broke them. We only rename to the CARLA convention and move the takes into Animations/."""
    A = a.asset_name
    dest = "%s/%s/Static/Other/%s" % (a.dest_root, A, A)
    anim_dest = "%s/%s/Animations" % (a.dest_root, A)
    content_root = "%s/%s" % (a.dest_root, A)
    data = {"asset_name": A, "mode": "direct", "dest": dest}
    try:
        eal = unreal.EditorAssetLibrary
        tools = unreal.AssetToolsHelpers.get_asset_tools()
        clip_map = json.loads(a.clip_name_map) if a.clip_name_map else CLIP_DEFAULT_MAP

        task = unreal.AssetImportTask()
        task.filename = a.sk_fbx
        task.destination_path = dest
        task.automated = True
        task.save = True
        task.replace_existing = True
        ui = unreal.FbxImportUI()
        ui.import_mesh = True
        ui.import_as_skeletal = True
        ui.import_animations = True     # bring the baked takes in as AnimSequences
        ui.import_materials = True      # native FBX materials (peoplePal / disaProps ...)
        ui.import_textures = True
        ui.create_physics_asset = True
        ui.mesh_type_to_import = unreal.FBXImportType.FBXIT_SKELETAL_MESH
        task.options = ui
        tools.import_asset_tasks([task])
        imported = list(task.imported_object_paths or [])
        data["imported_object_paths"] = imported

        # force the asset registry to pick up the freshly-imported siblings (list_assets lags)
        try:
            unreal.AssetRegistryHelpers.get_asset_registry().scan_paths_synchronous([dest], True)
        except Exception as e:
            data["registry_scan_err"] = repr(e)

        sk = skel = phys = None
        anims = []
        seen = set()
        # use the FULL object path (skeletal import names the object != package, e.g.
        # crutches_woman_ani.crutches_woman_ani_crutches_woman -> splitting to the package loses it)
        candidates = imported + list(eal.list_assets(dest, recursive=True, include_folder=False) or [])
        for p in candidates:
            if p in seen or not eal.does_asset_exist(p):
                continue
            seen.add(p)
            asset = eal.load_asset(p)
            if isinstance(asset, unreal.SkeletalMesh) and sk is None:
                sk = asset
            elif isinstance(asset, unreal.Skeleton) and skel is None:
                skel = asset
            elif isinstance(asset, unreal.PhysicsAsset) and phys is None:
                phys = asset
            elif isinstance(asset, unreal.AnimSequence):
                anims.append(asset)
        if sk is None:
            raise RuntimeError("no SkeletalMesh after direct import; imported=%s" % imported)

        rename = {}
        if phys is not None:
            _, rename["physics"], _ = _rename_loaded(tools, eal, phys, dest, "%s_PhysicsAsset" % A)
        if skel is None:
            skel = sk.get_editor_property("skeleton")
        if skel is not None:
            _, rename["skeleton"], _ = _rename_loaded(tools, eal, skel, dest, "%s_Skeleton" % A)
        sk, sk_path, _ = _rename_loaded(tools, eal, sk, dest, "SK_%s" % A)
        rename["sk"] = sk_path
        data["rename"] = rename
        sksk = sk.get_editor_property("skeleton")
        data["skeleton_name"] = sksk.get_name() if sksk else None

        anim_names = []
        for anim in anims:
            suffix = anim.get_name().rsplit("_", 1)[-1].lower()   # forward / idle / talking
            clip = clip_map.get(suffix, suffix.capitalize())
            new = "A_%s_%s_01" % (A, clip)
            _rename_loaded(tools, eal, anim, anim_dest, new)
            anim_names.append(new)
        data["anim_names"] = anim_names

        mat_ok, assigned = assigned_material_ok(sk)
        data["assigned_material_paths"] = assigned
        data["native_materials"] = True
        try:
            b = sk.get_bounds().box_extent
            data["sk_box_extent"] = [round(b.x, 2), round(b.y, 2), round(b.z, 2)]
            data["standing"] = (2 * b.z / 100.0) > 1.4
        except Exception as e:
            data["bounds_err"] = repr(e)

        eal.save_directory(content_root, only_if_is_dirty=False, recursive=True)
        ok = bool(sk is not None and skel is not None and anim_names and mat_ok)
        write_verdict(a.out, ok, data, None if ok else "direct import incomplete (see data)")
        unreal.log("UE_WALKER_IMPORT_DIRECT_DONE ok=%s %s" % (ok, sk_path))
    except Exception as e:
        write_verdict(a.out, False, data, error="%r\n%s" % (e, traceback.format_exc()))
        unreal.log_error("UE_WALKER_IMPORT_DIRECT_FAIL " + repr(e))


# ------------------------------------------------------------------- main
def main():
    a = parse_args()
    if a.direct:
        return direct_import_main(a)
    A = a.asset_name
    dest_path = "%s/%s/Static/Other/%s" % (a.dest_root, A, A)
    anim_dest = "%s/%s/Animations" % (a.dest_root, A)
    content_root = "%s/%s" % (a.dest_root, A)
    names = {"sk": "SK_%s" % A, "skeleton": "%s_Skeleton" % A, "physics": "%s_PhysicsAsset" % A}
    data = {"asset_name": A, "dest_path": dest_path, "anim_dest": anim_dest, "names": names}
    try:
        eal = unreal.EditorAssetLibrary

        # ---- skeletal mesh (+ skeleton + physics), renamed to gold convention ----
        sk, skel, skreport = import_and_rename_skeletal(a.sk_fbx, dest_path, names, _bool(a.use_t0_as_ref_pose))
        data["skeletal"] = skreport
        if skel is None:
            skel = sk.get_editor_property("skeleton")
        data["skeleton_name"] = skel.get_name() if skel else None
        try:
            b = sk.get_bounds().box_extent
            data["sk_box_extent"] = [round(b.x, 2), round(b.y, 2), round(b.z, 2)]
        except Exception as e:
            data["sk_box_extent_err"] = repr(e)

        # ---- animations (one AnimSequence per armature-only clip) ----
        if a.anims_json:
            clip_fbxs = json.loads(a.anims_json)
        elif a.anims_dir:
            clip_fbxs = sorted(glob.glob(os.path.join(a.anims_dir, "*.fbx")))
        else:
            clip_fbxs = []
        anim_report = []
        anim_names = []
        for fbx in clip_fbxs:
            paths = import_anim(fbx, anim_dest, skel)
            nm = os.path.splitext(os.path.basename(fbx))[0]
            ok = bool(paths)
            anim_report.append({"clip": nm, "imported": ok, "paths": paths})
            if ok:
                anim_names.append(nm)
        data["animations"] = anim_report
        data["anim_names"] = anim_names

        # ---- textures + material ----
        canonical = {}
        if a.textures_json and os.path.exists(a.textures_json):
            with open(a.textures_json) as f:
                tj = json.load(f)
            canonical = (tj.get("data") or {}).get("canonical_set") or tj.get("canonical_set") or {}
        data["canonical_slots"] = sorted(canonical.keys())
        tex_assets = {}
        tex_report = []
        for slot, src in canonical.items():
            if slot not in SLOT_TEX_SETTINGS:
                tex_report.append({"slot": slot, "skipped": "unknown slot"})
                continue
            tname = "T_%s_%s" % (A, slot)
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

        mat, mat_path, graph = build_material(A, dest_path, tex_assets)
        data["material_path"] = mat_path
        data["material_graph"] = graph
        nslots = assign_skeletal_material(sk, mat)
        data["material_slots_set"] = nslots
        eal.save_loaded_asset(sk)
        mat_ok, assigned = assigned_material_ok(sk)
        data["assigned_material_paths"] = assigned
        if not mat_ok:
            raise RuntimeError("a skeletal material slot has no material_interface after assign: %s" % assigned)

        # ---- save everything (grey-shader defense; cook can't find dirty-unsaved deps) ----
        # save the assets we hold refs to by loaded-object (robust to object!=package names),
        # then flush the whole package tree (catches anims + any dependent package).
        refs = [sk] + ([skel] if skel is not None else []) + list(tex_assets.values()) + [mat]
        saved = 0
        for asset in refs:
            try:
                eal.save_loaded_asset(asset)
                saved += 1
            except Exception:
                pass
        try:
            data["saved_directory"] = bool(eal.save_directory(content_root, only_if_is_dirty=False, recursive=True))
        except Exception as e:
            data["saved_directory"] = "err:%r" % e
        data["saved_assets_count"] = saved

        # ---- verdict ----
        ok = bool(sk is not None and skel is not None and anim_names and mat_ok)
        err = None if ok else "ue_walker_import incomplete (see data)"
        write_verdict(a.out, ok, data, err)
        unreal.log("UE_WALKER_IMPORT_DONE ok=%s %s" % (ok, skreport.get("sk_path")))
    except Exception as e:
        write_verdict(a.out, False, data, error="%r\n%s" % (e, traceback.format_exc()))
        unreal.log_error("UE_WALKER_IMPORT_FAIL " + repr(e))


main()
