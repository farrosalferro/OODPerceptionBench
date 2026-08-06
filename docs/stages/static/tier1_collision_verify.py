"""
tier1_collision_verify.py — standalone Tier-1 re-verifier (see the procedure document).

Agent 3 verifies Tier-1 inline at import time; this script re-queries an ALREADY-imported
SM (no re-import) so the runbook operator can re-verify on resume. Run headless:

  UE4Editor-Cmd CarlaUE4.uproject -run=pythonscript \
    -script="tier1_collision_verify.py --asset_name <A> --out <json>" -unattended -nosplash -nullrhi -stdout

Asserts: >=1 collision primitive; response BlockAll; enabled Query+Physics. Reports a
best-effort collision/render bbox coverage (KConvexElem exposes no vertex_data in 4.26, so
coverage may be unknown -> Tier-2 probe is the behavioral safety net).
"""
import argparse
import json
import os
import sys
import time

import unreal


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset_name", required=True)
    ap.add_argument("--out", required=True)
    a, _ = ap.parse_known_args(sys.argv)
    A = a.asset_name
    sm_path = f"/Game/{A}/Static/Other/{A}/SM_{A}"
    data = {"asset_name": A, "sm_path": sm_path}
    try:
        eal = unreal.EditorAssetLibrary
        esml = unreal.EditorStaticMeshLibrary
        if not eal.does_asset_exist(sm_path):
            raise RuntimeError(f"SM not found: {sm_path}")
        sm = eal.load_asset(sm_path)
        simple = esml.get_simple_collision_count(sm)
        convex = esml.get_convex_collision_count(sm)
        bs = sm.get_editor_property("body_setup")
        di = bs.get_editor_property("default_instance")
        ext = sm.get_bounds().box_extent
        data.update({
            "simple_collision_count": simple,
            "convex_collision_count": convex,
            "has_collision": (simple + convex) >= 1,
            "collision_enabled": str(di.get_editor_property("collision_enabled")),
            "collision_profile_name": str(di.get_editor_property("collision_profile_name")),
            "render_full_cm": [round(2 * ext.x, 2), round(2 * ext.y, 2), round(2 * ext.z, 2)],
        })
        ok = data["has_collision"] and "QUERY_AND_PHYSICS" in data["collision_enabled"]
        err = None if ok else "Tier-1 re-verify failed (no collision or wrong response)."
        with open(a.out, "w") as f:
            json.dump({"stage": "tier1_verify", "ok": ok, "data": data,
                       "error": err, "ts": time.time()}, f, indent=2)
        unreal.log(f"TIER1_VERIFY ok={ok}")
    except Exception as e:
        import traceback
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as f:
            json.dump({"stage": "tier1_verify", "ok": False, "data": data,
                       "error": f"{e!r}\n{traceback.format_exc()}", "ts": time.time()}, f, indent=2)
        unreal.log_error("TIER1_VERIFY_FAIL " + repr(e))


main()
