#!/usr/bin/env python3
"""Cross-check NOTICE against the private asset-licence audit.

MAINTAINER TOOL. The audit TSV is private — it records marketplace URLs, purchase
state, and licence analysis — so this cannot run in public CI. Run it by hand
before tagging a release.

It enforces both directions of the obligation:

  * every asset marked `ship` in the audit is attributed in NOTICE, with its
    author and its licence, and
  * no asset marked `replace` appears anywhere in NOTICE's attribution section
    or in assets/ — a non-redistributable asset leaking into the pack is the
    expensive failure.

Usage:
    python3 tools/dev/check_notice_assets.py --assets-tsv /path/to/ASSETS.tsv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_audit(path: str):
    ship, replace = [], []
    with open(path, encoding="utf-8") as fh:
        rows = [ln for ln in fh if not ln.lstrip().startswith("#") and ln.strip()]
    reader = csv.DictReader(rows, delimiter="\t")
    for row in reader:
        bp = (row.get("blueprint_current") or "").strip()
        action = (row.get("action") or "").strip().lower()
        author = (row.get("author_seller") or "").strip()
        lic = (row.get("license_stated") or "").strip()
        if not bp:
            continue
        (ship if action == "ship" else replace).append((bp, author, lic))
    return ship, replace


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets-tsv", required=True, help="private asset audit TSV")
    ap.add_argument("--notice", default=os.path.join(REPO_ROOT, "NOTICE"))
    args = ap.parse_args()

    ship, replace = load_audit(args.assets_tsv)
    notice = open(args.notice, encoding="utf-8").read()

    print(f"audit: {len(ship)} ship, {len(replace)} replace")
    ok = True

    # Direction 1 — everything shippable must be attributed.
    for bp, author, lic in ship:
        short = bp.rsplit(".", 1)[-1]
        if short not in notice:
            print(f"FAIL  ship asset not attributed in NOTICE: {bp}")
            ok = False
            continue
        # Author surname / handle should appear too.
        key = author.split("(")[0].strip()
        if key and key.split()[0] not in notice:
            print(f"FAIL  ship asset {bp}: author '{author}' not credited in NOTICE")
            ok = False
        if "NonCommercial" in lic or "NC" in lic.replace("NCommercial", ""):
            if "CC BY-NC 4.0" not in notice:
                print(f"FAIL  {bp} is NonCommercial but NOTICE never states 'CC BY-NC 4.0'")
                ok = False

    # Direction 2 — nothing non-redistributable may be attributed or shipped.
    attribution_section = notice.split("2. REDISTRIBUTED 3D ASSETS")[-1] \
                                .split("3. NOT REDISTRIBUTED")[0]
    for bp, _author, _lic in replace:
        short = bp.rsplit(".", 1)[-1]
        if short in attribution_section:
            print(f"FAIL  non-redistributable asset appears in NOTICE attributions: {bp}")
            ok = False

    assets_dir = os.path.join(REPO_ROOT, "assets")
    for dirpath, _dirs, files in os.walk(assets_dir):
        for name in files:
            low = name.lower()
            for bp, _a, _l in replace:
                if bp.rsplit(".", 1)[-1] in low:
                    rel = os.path.relpath(os.path.join(dirpath, name), REPO_ROOT)
                    print(f"FAIL  non-redistributable asset file present: {rel}")
                    ok = False

    # NC term must be prominent, not only tabulated.
    if "NON-COMMERCIAL COMPONENT" not in notice:
        print("FAIL  NOTICE must flag the NonCommercial component prominently")
        ok = False

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
