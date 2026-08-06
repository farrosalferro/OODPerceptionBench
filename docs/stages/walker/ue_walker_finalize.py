"""
ue_walker_finalize.py -- WALKER IMPORT stage 6 (see the procedure document). Runs AFTER G3, editor closed.

Scripts the two headless-safe pieces of registration:
  (1) the `<W>Map` level -- spawns BP_<W> + the shared WalkerFactory actor; this map is the
      vehicle that cooks the (G3-edited) WalkerFactory into the package.
  (2) the walkers-keyed `Config/<W>.Package.json`.

It does NOT edit the WalkerFactory itself: spike 3 proved the Walkers[] list is baked into the
WalkerFactory `GenerateDefinitions` blueprint graph (not a reflected CDO property), so adding the
walker is a G3 human step. The Package.json `walkers[]` entry is inert at runtime (no code reads
it) -- kept only because cook_walker_package.sh asserts walkers[].name == blueprint_id last segment.

  UE4Editor-Cmd CarlaUE4.uproject -run=pythonscript \
    -script="ue_walker_finalize.py --asset_name <W> --walker_id <id> --dest_root /Game \
             --package_json_path <Content/<W>/Config/<W>.Package.json> --out <verdict.json>" \
    -unattended -nosplash -nopause -nullrhi -stdout

Success = verdict ok:true on disk (server + editor must be OFF for this stage). Self-contained.
"""
import argparse
import json
import os
import sys
import time
import traceback

import unreal


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset_name", required=True)
    ap.add_argument("--walker_id", required=True, help="blueprint_id last segment (Package.json walkers[].name)")
    ap.add_argument("--dest_root", default="/Game")
    ap.add_argument("--package_json_path", required=True, help="disk path for Config/<W>.Package.json")
    ap.add_argument("--walker_factory", default="/Game/Carla/Blueprints/Walkers/WalkerFactory")
    ap.add_argument("--game_root", default=None,
                    help="/Game root used for Package.json path fields (default = dest_root)")
    ap.add_argument("--out", required=True)
    a, _ = ap.parse_known_args(sys.argv)
    return a


def write_verdict(out, ok, data, error=None):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        json.dump({"stage": "finalize", "ok": bool(ok), "data": data,
                   "error": error, "ts": time.time()}, f, indent=2, default=str)


def author_level(map_path, bp_object, wf_path):
    eal = unreal.EditorAssetLibrary
    ell = unreal.EditorLevelLibrary
    r = {"map_path": map_path}
    # new_level fails if the map already exists, and delete_asset can't remove Map/Level assets
    # (EditorAssetLibrary treats them specially). So: try new_level; on failure, load the existing
    # level and clear its actors -> idempotent re-run.
    made = ell.new_level(map_path)
    r["new_level"] = bool(made)
    r["level_ready"] = bool(made)
    if not made:
        loaded = ell.load_level(map_path)
        r["loaded_existing"] = bool(loaded)
        r["level_ready"] = bool(loaded)
        if loaded:
            for ac in (ell.get_all_level_actors() or []):
                for meth in ("destroy_actor",):
                    try:
                        getattr(ell, meth)(ac)
                        break
                    except Exception:
                        try:
                            ac.destroy_actor()
                        except Exception:
                            pass
    bp = eal.load_asset(bp_object)
    r["bp_loaded"] = bp is not None
    a1 = ell.spawn_actor_from_object(bp, unreal.Vector(0, 0, 0)) if bp is not None else None
    r["spawned_bp"] = a1 is not None
    wf = eal.load_asset(wf_path)
    r["wf_loaded"] = wf is not None
    a2 = ell.spawn_actor_from_object(wf, unreal.Vector(300, 0, 0)) if wf is not None else None
    r["spawned_wf"] = a2 is not None
    r["saved"] = bool(ell.save_current_level())
    r["ok"] = bool(r["level_ready"] and r["spawned_bp"] and r["spawned_wf"] and r["saved"])
    return r


def write_package_json(path, walker_id, map_name, maps_dir_game, bp_package_path):
    d = {
        "props": [],
        "maps": [{"name": map_name, "path": maps_dir_game, "use_carla_materials": False}],
        "walkers": [{"name": walker_id, "path": bp_package_path}],
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(d, f, indent=4)
    return d


def main():
    a = parse_args()
    W = a.asset_name
    game_root = a.game_root or a.dest_root
    bp_object = "%s/%s/Blueprints/BP_%s" % (a.dest_root, W, W)
    map_path = "%s/%s/Maps/%sMap" % (a.dest_root, W, W)
    map_name = "%sMap" % W
    maps_dir_game = "%s/%s/Maps" % (game_root, W)
    bp_package_path = "%s/%s/Blueprints/BP_%s.BP_%s" % (game_root, W, W, W)
    data = {"asset_name": W, "bp_object": bp_object, "map_path": map_path}
    try:
        data["level"] = author_level(map_path, bp_object, a.walker_factory)
        data["package_json"] = write_package_json(
            a.package_json_path, a.walker_id, map_name, maps_dir_game, bp_package_path)
        data["package_json_path"] = a.package_json_path
        ok = bool(data["level"]["ok"] and os.path.isfile(a.package_json_path))
        err = None if ok else "finalize incomplete (see data.level)"
        write_verdict(a.out, ok, data, err)
        unreal.log("UE_WALKER_FINALIZE_DONE ok=%s %s" % (ok, map_path))
    except Exception as e:
        write_verdict(a.out, False, data, error="%r\n%s" % (e, traceback.format_exc()))
        unreal.log_error("UE_WALKER_FINALIZE_FAIL " + repr(e))


main()
