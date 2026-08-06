"""
ue_walker_clone.py -- WALKER ASSEMBLE stage 5 SCAFFOLD (design sections 4/5). Headless UE 4.26.

  UE4Editor-Cmd CarlaUE4.uproject -run=pythonscript \
    -script="ue_walker_clone.py --asset_name <W> --clone_from <TemplateWalker> \
             --dest_root /Game --out <verdict.json>" \
    -unattended -nosplash -nopause -nullrhi -stdout

Duplicates a template walker's three blueprints (BS_/ABP_/BP_) into /Game/<W>/Blueprints/ so a
human can then author them at gate G3. The duplication donates ONLY the correct naming, the
BP_Walker parent class and the generic EventGraph -- it does NOT retarget across skeletons.
duplicate_asset copies the source graph verbatim (spike finding): the rig-specific wiring
(skeleton repoint, BlendSpace samples, CDO mesh / anim-class / capsule / collision, and the
WalkerFactory GenerateDefinitions graph edit) is all G3 manual work, recorded verbatim in
data.needs_human. This stage is a SCAFFOLD: it never attempts a cross-skeleton repoint.

Success = the JSON verdict ok:true on disk, NOT the exit code (UE segfaults on teardown).
Self-contained (UE python 3.7; the runbook operator resolves names and passes argv).
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
    ap.add_argument("--asset_name", required=True, help="new walker name W")
    ap.add_argument("--clone_from", required=True, help="template walker name, e.g. Boar")
    ap.add_argument("--dest_root", default="/Game", help="root for the NEW walker /<W>/Blueprints")
    ap.add_argument("--clone_root", default=None,
                    help="root the TEMPLATE lives under (default = dest_root; real content is /Game)")
    ap.add_argument("--out", required=True)
    a, _ = ap.parse_known_args(sys.argv)
    return a


def write_verdict(out, ok, data, error=None):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        json.dump({"stage": "assemble", "ok": bool(ok), "data": data,
                   "error": error, "ts": time.time()}, f, indent=2, default=str)


# the three blueprints cloned, in dependency order (BlendSpace -> AnimBP -> Blueprint)
ROLES = ("bs", "abp", "bp")
PREFIX = {"bs": "BS", "abp": "ABP", "bp": "BP"}


def clone_blueprint(eal, role, src, dst):
    """Duplicate ONE blueprint if the source exists. duplicate_asset makes a clean copy already
    named after dst -- no post-rename needed. A missing source is recorded and skipped (never
    crash): the runbook operator / human decides whether the partial scaffold is usable."""
    exists = bool(eal.does_asset_exist(src))
    duplicated = False
    if exists:
        try:
            dup = eal.duplicate_asset(src, dst)
        except Exception:
            dup = None
        duplicated = bool(dup is not None) and bool(eal.does_asset_exist(dst))
    return {"role": role, "src": src, "dst": dst,
            "duplicated": duplicated, "src_exists": exists}


def build_needs_human(W):
    """The G3 manual build steps that duplication CANNOT do (cross-skeleton + graph edits)."""
    return [
        "BS_%s: set skeleton to %s_Skeleton; drop each A_%s_<clip> at its clip_speed_map Speed "
        "(axis Speed max 300 grid 300 Linear)" % (W, W, W),
        "ABP_%s: set TargetSkeleton=%s_Skeleton; AnimGraph ForwardSpeed -> "
        "BlendSpacePlayer(BS_%s) -> Output Pose; compile" % (W, W, W),
        "BP_%s: set SkeletalMesh=SK_%s, AnimClass=ABP_%s_C, materials; set mesh-component yaw for "
        "front_axis; type Capsule radius/half_height from sizing; set CarStopper/"
        "PedestrianDeathTrigger/PedestrianPropDeathTrigger box extents; MaxAcceleration=6048; "
        "compile+save; CLOSE editor" % (W, W, W),
        "WalkerFactory: add a PedestrianParameters element (Id/Class=BP_%s_C/Gender/Age/Speed/"
        "Generation) to the GenerateDefinitions graph; compile+save  (spike 3: this is a graph "
        "edit, not scriptable headlessly)" % W,
    ]


def main():
    a = parse_args()
    W = a.asset_name
    C = a.clone_from
    clone_root = a.clone_root or a.dest_root
    src_dir = "%s/%s/Blueprints" % (clone_root, C)
    dst_dir = "%s/%s/Blueprints" % (a.dest_root, W)
    content_root = "%s/%s" % (a.dest_root, W)
    data = {"asset_name": W, "clone_from": C,
            "src_blueprint_dir": src_dir, "dest_blueprint_dir": dst_dir}
    try:
        eal = unreal.EditorAssetLibrary

        # ---- duplicate BS_ / ABP_ / BP_ from the template into /Game/<W>/Blueprints/ ----
        items = []
        for role in ROLES:
            src = "%s/%s_%s" % (src_dir, PREFIX[role], C)
            dst = "%s/%s_%s" % (dst_dir, PREFIX[role], W)
            items.append(clone_blueprint(eal, role, src, dst))
        data["items"] = items
        duplicated_count = sum(1 for it in items if it["duplicated"])
        data["duplicated_count"] = duplicated_count

        # ---- save the freshly duplicated blueprints (dirty-dep / grey-cook defense) ----
        try:
            data["saved_directory"] = bool(
                eal.save_directory(content_root, only_if_is_dirty=False, recursive=True))
        except Exception as e:
            data["saved_directory"] = "err:%r" % e

        # ---- the G3 manual build that duplication cannot do (cross-skeleton + graph edits) ----
        data["needs_human"] = build_needs_human(W)

        # ---- verdict: ok only when all three blueprints duplicated cleanly ----
        ok = bool(duplicated_count == len(ROLES))
        err = None if ok else ("clone incomplete: only %d/%d blueprints duplicated (see data.items)"
                               % (duplicated_count, len(ROLES)))
        write_verdict(a.out, ok, data, err)
        unreal.log("UE_WALKER_CLONE_DONE ok=%s %d/%d -> %s" % (ok, duplicated_count, len(ROLES), dst_dir))
    except Exception as e:
        write_verdict(a.out, False, data, error="%r\n%s" % (e, traceback.format_exc()))
        unreal.log_error("UE_WALKER_CLONE_FAIL " + repr(e))


main()
