#!/usr/bin/env python3
"""
texture_classify.py — Agent 1 core: texture role classify + packed-split (spec ).

Classifies the texture files in a textures dir into PBR roles (a case-insensitive
keyword table, priority-ordered), detects the authoring convention (glTF-packed vs
Substance separate-channel), and — for packed metallicRoughness/ORM maps — splits
them via PIL into the CANONICAL separate-channel set UE always consumes. This keeps
the UE side free of fragile in-material channel masks.

Role table (case-insensitive, first match wins, in priority order):
  metallicRoughness | orm    -> "packed"     (a packed map; must be split)
  basecolor|albedo|diffuse   -> "BaseColor"  srgb=True
  normal|nrm                 -> "Normal"     srgb=False  ue_compression="Normalmap"
  rough(ness)?               -> "Roughness"  srgb=False
  metal(lic|ness)?           -> "Metallic"   srgb=False
  ao|occlusion               -> "AO"         srgb=False
  emissi(ve|on)              -> "Emissive"   srgb=True
  curvature|(^|_)id(_|$)|mask -> "junk"      (dropped)
  else                       -> "unknown"    (runtime LLM fallback decides; no guessing here)

Packing layouts (detected by filename):
  metallicRoughness (glTF): G=Roughness, B=Metallic (R unused) -> _Roughness, _Metallic
  ORM                     : R=AO, G=Roughness, B=Metallic       -> _AO, _Roughness, _Metallic
Split outputs are 8-bit single-channel ("L") grayscale PNGs written to --out_dir.

CLI:
  texture_classify.py --textures_dir DIR --out_dir DIR --out verdict.json

Emits the common.make_verdict envelope to --out (via common.write_verdict) and a
copy to stdout. Exits non-zero on hard failure (bad dir, PIL error).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import make_verdict, write_verdict  # noqa: E402

STAGE = "manifest_texture"

# Image extensions we treat as textures.
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".bmp", ".exr")

# UE canonical slots, in a stable display order.
UE_SLOTS = ["BaseColor", "Normal", "Metallic", "Roughness", "AO", "Emissive"]

# Role table: (compiled regex, role, srgb, ue_compression). First match wins;
# order encodes priority. "packed" / "junk" / "unknown" srgb is irrelevant (None).
# NB: `orm` is anchored to token boundaries (start/_/end) so it does NOT match the
# substring inside `normal`/`nrm`; `metallicroughness` is distinctive enough as-is.
_ROLE_TABLE = [
    (re.compile(r"metallicroughness|(^|[_\-])orm([_\-]|$)", re.I), "packed", None, None),
    (re.compile(r"basecolor|albedo|diffuse", re.I),       "BaseColor", True,  None),
    (re.compile(r"normal|nrm", re.I),                     "Normal",    False, "Normalmap"),
    (re.compile(r"rough(ness)?", re.I),                   "Roughness", False, None),
    (re.compile(r"metal(lic|ness)?", re.I),               "Metallic",  False, None),
    (re.compile(r"ao|occlusion", re.I),                   "AO",        False, None),
    (re.compile(r"emissi(ve|on)", re.I),                  "Emissive",  True,  None),
    (re.compile(r"curvature|(^|_)id(_|$)|mask", re.I),    "junk",      None,  None),
]


# --- site config bootstrap (adds --config; no machine-specific defaults anywhere) ------------
_SC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)
import site_config as _site_config  # noqa: E402


def classify_name(filename: str):
    """Return (role, srgb, ue_compression) for a texture filename's stem.

    Matches against the stem (extension stripped) so e.g. `.tga` doesn't accidentally
    hit a keyword. Returns role="unknown" when the table can't resolve the name.
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    for rx, role, srgb, comp in _ROLE_TABLE:
        if rx.search(stem):
            return role, srgb, comp
    return "unknown", None, None


def _packing_for(filename: str):
    """Return "ORM" or "MR" for a packed map, by filename. ORM checked first
    (an `orm` name should never be read as metallicRoughness)."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    if re.search(r"(^|[_\-])orm([_\-]|$)", stem, re.I):
        return "ORM"
    return "MR"  # metallicroughness


def _save_channel(img, band_index: int, dst_path: str) -> None:
    """Extract a single band as an 8-bit grayscale ("L") PNG."""
    rgb = img.convert("RGB")  # guarantees >=3 bands in fixed R,G,B order
    chan = rgb.split()[band_index]  # already mode "L" (8-bit, single channel)
    chan.save(dst_path, format="PNG")


def split_packed(src_path: str, out_dir: str):
    """Split a packed map into canonical separate-channel grayscale PNGs.

    glTF metallicRoughness: G=Roughness, B=Metallic (R unused).
    ORM                   : R=AO, G=Roughness, B=Metallic.
    Returns a list of {file, role, srgb, ue_compression} for the produced PNGs.
    """
    from PIL import Image  # the client interpreter has Pillow; imported lazily so --help needs no PIL

    packing = _packing_for(src_path)
    stem = os.path.splitext(os.path.basename(src_path))[0]
    os.makedirs(out_dir, exist_ok=True)

    with Image.open(src_path) as im:
        produced = []
        # (band_index, role) pairs per packing convention.
        if packing == "ORM":
            plan = [(0, "AO"), (1, "Roughness"), (2, "Metallic")]
        else:  # MR
            plan = [(1, "Roughness"), (2, "Metallic")]
        for band_index, role in plan:
            dst = os.path.join(out_dir, f"{stem}_{role}.png")
            _save_channel(im, band_index, dst)
            produced.append({
                "file": os.path.abspath(dst),
                "role": role,
                "srgb": False,  # all linear data channels
                "ue_compression": None,
                "derived_from": os.path.abspath(src_path),
            })
    return packing, produced


def list_textures(textures_dir: str):
    """Sorted list of texture file paths in textures_dir (non-recursive)."""
    if not os.path.isdir(textures_dir):
        raise NotADirectoryError(f"textures_dir does not exist: {textures_dir}")
    files = []
    for name in sorted(os.listdir(textures_dir)):
        p = os.path.join(textures_dir, name)
        if os.path.isfile(p) and os.path.splitext(name)[1].lower() in IMAGE_EXTS:
            files.append(p)
    return files


def run(textures_dir: str, out_dir: str) -> dict:
    """Classify + normalize. Returns the verdict.data dict."""
    files = list_textures(textures_dir)

    textures = []          # every input file's classification record
    derived = []           # canonical PNGs produced by splitting packed maps
    dropped = []           # junk (file paths)
    unknown = []           # unresolved (file paths) — runtime LLM fallback decides
    has_packed = False

    for path in files:
        role, srgb, comp = classify_name(path)
        rec = {
            "file": os.path.abspath(path),
            "role": role,
            "srgb": srgb,
            "ue_compression": comp,
        }
        textures.append(rec)
        if role == "packed":
            has_packed = True
            packing, produced = split_packed(path, out_dir)
            rec["packing"] = packing
            rec["split_into"] = [d["file"] for d in produced]
            derived.extend(produced)
        elif role == "junk":
            dropped.append(os.path.abspath(path))
        elif role == "unknown":
            unknown.append(os.path.abspath(path))

    convention = "gltf-packed" if has_packed else "substance-separate"

    # Build the canonical UE-slot -> absolute-path map from normalized textures.
    # Source = the original separate-channel records (in place) + derived split PNGs.
    # Packed/junk/unknown source records do not occupy a slot directly.
    canonical_set = {}
    normalized = [r for r in textures if r["role"] in UE_SLOTS] + derived
    for r in normalized:
        slot = r["role"]
        if slot in UE_SLOTS and slot not in canonical_set:
            canonical_set[slot] = r["file"]

    # Stable slot ordering for readability.
    canonical_set = {s: canonical_set[s] for s in UE_SLOTS if s in canonical_set}

    return {
        "textures_dir": os.path.abspath(textures_dir),
        "out_dir": os.path.abspath(out_dir),
        "convention": convention,
        "textures": textures,
        "derived": derived,
        "canonical_set": canonical_set,
        "dropped": dropped,
        "unknown": unknown,
    }


def main():
    ap = argparse.ArgumentParser(description="Classify textures into PBR roles + split packed maps.")
    _site_config.add_config_arg(ap)
    ap.add_argument("--textures_dir", required=True, help="dir holding the source texture files")
    ap.add_argument("--out_dir", required=True, help="dir for normalized/derived PNGs (created if absent)")
    ap.add_argument("--out", required=True, help="path to write the JSON verdict envelope")
    args = ap.parse_args()
    _site_config.apply_config_arg(args)

    try:
        os.makedirs(args.out_dir, exist_ok=True)
        data = run(args.textures_dir, args.out_dir)
        verdict = make_verdict(STAGE, True, data=data)
    except Exception as e:  # hard failure -> ok=False envelope + non-zero exit
        verdict = make_verdict(STAGE, False, data={"textures_dir": args.textures_dir},
                               error=f"{type(e).__name__}: {e}")
        write_verdict(args.out, verdict)
        print(json.dumps(verdict, indent=2))
        sys.exit(1)

    write_verdict(args.out, verdict)
    print(json.dumps(verdict, indent=2))


if __name__ == "__main__":
    main()
