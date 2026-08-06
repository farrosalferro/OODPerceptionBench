"""
walker_sizing.py -- WALKER IMPORT stage 2 (see the procedure document). Scripted (the client interpreter).

Derives the UE capsule (radius_cm, half_height_cm) from the manifest target_dims_m and
classifies the walker's size shift (visual | geometric | ambiguous) using the SAME math as
perception/pedestrian_dimension_checker.ipynb (union of the walker_adult relative-difference
mold and the walker_child Z-score mold). The category is cross-checked against the
human-declared manifest shift_type -- a mismatch is surfaced (the runbook operator treats it like the
static band mismatch: a halt for review, not a silent pass).

  walker_sizing.py --manifest <import/Cow.yaml> --out <verdict.json>
  walker_sizing.py --target_dims_m '{"L":1.95,"W":0.80,"H":1.64}' --shift_type geometric --out ...

Capsule derivation (a STARTING point; the human tunes it at G3, cross-checking the notebook):
  radius_cm      = W * 100 / 2     (capsule diameter == body width; matches the gold Cow: 40cm)
  half_height_cm = H * 100 / 2     (full capsule height; L/front-back is captured by the G3
                                    death-trigger boxes, not the vertical capsule)
"""
import argparse
import json
import os
import sys

_THIS = os.path.dirname(os.path.abspath(__file__))
if _THIS not in sys.path:
    sys.path.insert(0, _THIS)
import walker_import_common as wi

STAGE = "sizing"

# --- constants copied verbatim from pedestrian_dimension_checker.ipynb -----------------
ADULT_MU = {"L": 0.375400, "W": 0.375400, "H": 1.860000}          # sigma == 0
CHILD = {"L": (0.453275, 0.064487),
         "W": (0.453275, 0.064487),
         "H": (1.175000, 0.103510)}
Z_VISUAL = 2.0
Z_GEOMETRIC = 3.0
DELTA_VISUAL_WALKER = 0.20
HEIGHT_SATURATION = 2.0


# --- site config bootstrap (adds --config; no machine-specific defaults anywhere) ------------
_SC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)
import site_config as _site_config  # noqa: E402


def carla_bbox_from_capsule(radius_cm, half_height_cm):
    return {"L": 2.0 * radius_cm / 100.0,
            "W": 2.0 * radius_cm / 100.0,
            "H": 2.0 * half_height_cm / 100.0}


def _eval_adult(bbox):
    deltas = {}
    for dim in ("L", "W", "H"):
        deltas[dim] = abs(bbox[dim] - ADULT_MU[dim]) / ADULT_MU[dim]
    dmax = max(deltas, key=deltas.get)
    shift = "level_1_visual" if deltas[dmax] <= DELTA_VISUAL_WALKER else "level_2_geometric"
    return deltas, dmax, shift


def _eval_child(bbox):
    zs = {}
    for dim in ("L", "W", "H"):
        mu, sigma = CHILD[dim]
        if dim == "H" and mu > HEIGHT_SATURATION and bbox[dim] > HEIGHT_SATURATION:
            zs[dim] = 0.0
        else:
            zs[dim] = abs(bbox[dim] - mu) / sigma
    zmaxd = max(zs, key=zs.get)
    zmax = zs[zmaxd]
    if zmax <= Z_VISUAL:
        shift = "level_1_visual"
    elif zmax > Z_GEOMETRIC:
        shift = "level_2_geometric"
    else:
        shift = "ambiguous"
    return zs, zmaxd, shift


def classify(bbox):
    a_deltas, a_dim, a_shift = _eval_adult(bbox)
    c_zs, c_dim, c_shift = _eval_child(bbox)
    if a_shift == "level_1_visual" or c_shift == "level_1_visual":
        combined = "level_1_visual"
    elif a_shift == "level_2_geometric" and c_shift == "level_2_geometric":
        combined = "level_2_geometric"
    else:
        combined = "ambiguous"
    return {
        "category": combined,
        "adult": {"deltas_pct": {k: round(v * 100, 2) for k, v in a_deltas.items()},
                  "dmax_dim": a_dim, "verdict": a_shift},
        "child": {"zscores": {k: round(v, 3) for k, v in c_zs.items()},
                  "zmax_dim": c_dim, "verdict": c_shift},
    }


CATEGORY_TO_SHIFT = {"level_1_visual": "visual", "level_2_geometric": "geometric",
                     "ambiguous": "ambiguous"}


def derive_capsule(target_dims_m, capsule_override=None):
    W = float(target_dims_m["W"])
    H = float(target_dims_m["H"])
    radius_cm = round(W * 100.0 / 2.0, 1)
    half_height_cm = round(H * 100.0 / 2.0, 1)
    ov = capsule_override or {}
    if ov.get("radius_cm") is not None:
        radius_cm = float(ov["radius_cm"])
    if ov.get("half_height_cm") is not None:
        half_height_cm = float(ov["half_height_cm"])
    return radius_cm, half_height_cm


def run(target_dims_m, shift_type, capsule_override=None):
    radius_cm, half_height_cm = derive_capsule(target_dims_m, capsule_override)
    bbox = carla_bbox_from_capsule(radius_cm, half_height_cm)
    cls = classify(bbox)
    category = cls["category"]
    derived_shift = CATEGORY_TO_SHIFT[category]
    matches = (shift_type == derived_shift)
    return {
        "target_dims_m": target_dims_m,
        "radius_cm": radius_cm,
        "half_height_cm": half_height_cm,
        "derived_bbox_m": {k: round(v, 4) for k, v in bbox.items()},
        "category": category,
        "derived_shift_type": derived_shift,
        "declared_shift_type": shift_type,
        "shift_type_matches": matches,
        "classification": cls,
        "note": ("capsule radius derives from W (body width); the full L (front-back) and the "
                 "death-trigger/CarStopper box extents are set by the human at G3."),
    }


def main():
    ap = argparse.ArgumentParser()
    _site_config.add_config_arg(ap)
    ap.add_argument("--manifest")
    ap.add_argument("--target_dims_m", help="JSON e.g. {\"L\":1.95,\"W\":0.8,\"H\":1.64}")
    ap.add_argument("--shift_type")
    ap.add_argument("--capsule_override", help="JSON {radius_cm, half_height_cm}")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    _site_config.apply_config_arg(a)
    try:
        if a.manifest:
            m = wi.load_walker_import_manifest(a.manifest)
            target = m.target_dims_m
            shift_type = m.shift_type
            override = m.capsule_override
        else:
            target = json.loads(a.target_dims_m)
            shift_type = a.shift_type
            override = json.loads(a.capsule_override) if a.capsule_override else None
        data = run(target, shift_type, override)
        ok = bool(data["shift_type_matches"]) and data["category"] != "ambiguous"
        err = None
        if data["category"] == "ambiguous":
            err = "sizing category is AMBIGUOUS -- review manually (see classification)."
        elif not data["shift_type_matches"]:
            err = ("declared shift_type=%s but derived=%s -- halt for review (see the procedure document)."
                   % (shift_type, data["derived_shift_type"]))
        verdict = wi.make_verdict(STAGE, ok, data, err)
        wi.write_verdict(a.out, verdict)
        print(json.dumps(verdict, indent=2))
        sys.exit(0 if ok else 1)
    except Exception as e:
        import traceback
        verdict = wi.make_verdict(STAGE, False, {"target_dims_m": a.target_dims_m},
                                  "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
        wi.write_verdict(a.out, verdict)
        print(json.dumps(verdict, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
