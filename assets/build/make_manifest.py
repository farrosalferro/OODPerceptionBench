#!/usr/bin/env python3
"""Emit SHA256SUMS (tarball level) and MANIFEST.tsv (file level) for the asset pack."""
import argparse
import hashlib
import os

ASSET_META = {
    # content_dir -> (blueprint_id, category, level, author, license, spdx_or_url)
    "ConcreteRoadBarrier": (
        "static.prop.concreteroadbarrier", "static", "geometric_shift",
        "widthRider", "CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
    "RoadClosedBarricade": (
        "static.prop.roadclosedbarricade", "static", "geometric_shift",
        "exiS7-Gs", "CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
    "Astronaut": (
        "walker.pedestrian.astronaut", "pedestrian", "visual_shift",
        "Antropik", "CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
    "DeliveryRobot": (
        "walker.pedestrian.deliveryrobot", "pedestrian", "geometric_shift",
        "Bento (gostbento)", "CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
    "Boar": (
        "walker.pedestrian.boar", "pedestrian", "geometric_shift",
        "AnimalMesh 3D", "CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
    "Firefighter": (
        "walker.pedestrian.firefighter", "pedestrian", "visual_shift",
        "KIFIR", "CC BY-NC 4.0", "https://creativecommons.org/licenses/by-nc/4.0/"),
    "Carla": (
        "(base content: WalkerFactory)", "-", "-",
        "CARLA Simulator authors", "CC BY 4.0 (CARLA assets)",
        "https://github.com/carla-simulator/carla"),
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--dist", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--version", default="v0.9")
    a = ap.parse_args()

    # ---- tarball-level SHA256SUMS (this is what a user verifies after download)
    tars = sorted(f for f in os.listdir(a.dist) if f.endswith(".tar.gz"))
    lines = [f"{sha256(os.path.join(a.dist, t))}  {t}" for t in tars]
    with open(os.path.join(a.out, "SHA256SUMS"), "w") as fh:
        fh.write("\n".join(lines) + "\n")

    # ---- file-level MANIFEST.tsv
    rows = []
    for grp in sorted(os.listdir(a.stage)):
        gdir = os.path.join(a.stage, grp)
        if not os.path.isdir(gdir):
            continue
        tar = next((t for t in tars if f"-{grp}-" in t), "")
        for root, _, files in os.walk(gdir):
            for f in sorted(files):
                p = os.path.join(root, f)
                rel = os.path.relpath(p, gdir)                      # CarlaUE4/Content/...
                parts = rel.split(os.sep)
                content_dir = parts[2] if len(parts) > 2 else "-"
                bp, cat, lvl, author, lic, licurl = ASSET_META.get(
                    content_dir, ("-", "-", "-", "-", "-", "-"))
                rows.append((tar, rel, sha256(p), str(os.path.getsize(p)),
                             content_dir, bp, cat, lvl, author, lic, licurl))
    rows.sort(key=lambda r: (r[0], r[1]))

    hdr = ("tarball", "path_in_carla_root", "sha256", "bytes", "content_dir",
           "blueprint_id", "category", "level", "author", "license", "license_url")
    with open(os.path.join(a.out, "MANIFEST.tsv"), "w") as fh:
        fh.write(f"# OOD-PerceptionBench asset pack {a.version} — file manifest\n")
        fh.write("# path_in_carla_root is relative to the CARLA build root, i.e. the path\n")
        fh.write("# the file occupies after ImportAssets.sh has run.\n")
        fh.write("\t".join(hdr) + "\n")
        for r in rows:
            fh.write("\t".join(r) + "\n")

    total = sum(int(r[3]) for r in rows)
    tot_tar = sum(os.path.getsize(os.path.join(a.dist, t)) for t in tars)
    print(f"  files={len(rows)}  installed={total/1e6:.1f} MB  "
          f"download={tot_tar/1e6:.1f} MB")
    for t in tars:
        print(f"    {t}  {os.path.getsize(os.path.join(a.dist,t))/1e6:.1f} MB")


if __name__ == "__main__":
    main()
