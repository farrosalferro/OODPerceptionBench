#!/usr/bin/env bash
# =============================================================================
# OOD-PerceptionBench v0.9 — asset pack builder
#
# Assembles the SIX redistributable cooked CARLA assets into three tarballs that
# install with `ImportAssets.sh` over an official CARLA 0.9.15 Linux build.
#
# Reads a CARLA build READ-ONLY. Writes only into --out.
#
# Usage:
#   ./build_asset_pack.sh --carla-root /path/to/carla --out /path/to/assets
#
# There are no defaults for --carla-root; supply the build explicitly.
# =============================================================================
set -euo pipefail

CARLA_ROOT=""
OUT=""
VERSION="v0.9"

while [ $# -gt 0 ]; do
  case "$1" in
    --carla-root) CARLA_ROOT="$2"; shift 2 ;;
    --out)        OUT="$2";        shift 2 ;;
    --version)    VERSION="$2";    shift 2 ;;
    -h|--help)    sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[ -n "$CARLA_ROOT" ] || { echo "ERROR: --carla-root is required" >&2; exit 2; }
[ -n "$OUT" ]        || { echo "ERROR: --out is required" >&2; exit 2; }

CONTENT="$CARLA_ROOT/CarlaUE4/Content"
[ -d "$CONTENT" ] || { echo "ERROR: no CarlaUE4/Content under $CARLA_ROOT" >&2; exit 2; }

STAGE="$OUT/build/.stage"
DIST="$OUT/dist"
rm -rf "$STAGE"
mkdir -p "$STAGE" "$DIST"

# --- group -> content dirs ---------------------------------------------------
PROPS="ConcreteRoadBarrier RoadClosedBarricade"
WALKERS_CCBY="Astronaut DeliveryRobot Boar"
WALKERS_NC="Firefighter"

# --- exclusions (verified unreferenced; see build/EXCLUSIONS.tsv) -------------
EXCLUDES=(
  "Boar/Animations/Boar__000_SK_Boar_LOD0_Anim_Armature_alerted.uasset"
  "Boar/Animations/Boar__000_SK_Boar_LOD0_Anim_Armature_digging_feeding.uasset"
  "Boar/Animations/Boar__000_SK_Boar_LOD0_Anim_Armature_observing.uasset"
  "Boar/Animations/Boar__000_SK_Boar_LOD0_Anim_Armature_sniffing.uasset"
  "Boar/Animations/Boar__000_SK_Boar_LOD0_Anim_Armature_trot.uasset"
  "Boar/Animations/Boar__000_SK_Boar_LOD0_Anim_Armature_wake_up.uasset"
  "Boar/Animations/Boar__000_SK_Boar_LOD0_Anim_Armature_wound.uasset"
  "Firefighter/Blueprints/BP_FireFighter.uasset"
  "Firefighter/Blueprints/BP_FireFighter.uexp"
)
is_excluded() {
  local rel="$1"
  for e in "${EXCLUDES[@]}"; do [ "$rel" = "$e" ] && return 0; done
  return 1
}

# Copy one Content/<Name> dir into the stage, honouring the exclusion list.
# NOTE: `cp -p` preserves mtime. mtime is load-bearing — ImportAssets.sh runs
# `tar --keep-newer-files`, which SKIPS any member older than the file already
# on disk. Source mtimes (2025/2026) are newer than stock CARLA 0.9.15
# (2023-11-10), so extraction proceeds. Do not normalise mtimes to the epoch.
stage_dir() {
  local name="$1" grp="$2" kept=0 skipped=0
  while IFS= read -r -d '' f; do
    local rel="${f#$CONTENT/}"
    if is_excluded "$rel"; then skipped=$((skipped+1)); continue; fi
    local dst="$STAGE/$grp/CarlaUE4/Content/$rel"
    mkdir -p "$(dirname "$dst")"
    cp -p "$f" "$dst"
    kept=$((kept+1))
  done < <(find "$CONTENT/$name" -type f -print0 | sort -z)
  echo "  $name: kept=$kept excluded=$skipped"
}

echo "== staging =="
for n in $PROPS;        do stage_dir "$n" props;        done
for n in $WALKERS_CCBY; do stage_dir "$n" walkers-ccby; done
for n in $WALKERS_NC;   do stage_dir "$n" walkers-ccbync; done

# --- WalkerFactory -----------------------------------------------------------
# The four walkers do NOT self-register from their own Package.json. Their
# blueprint IDs live in the cooked base-content WalkerFactory, so it must ship.
# See ../WALKERFACTORY_DECISION.md for the full analysis and the measured
# consequence of shipping it (and of not shipping it).
WF_SRC="$CONTENT/Carla/Blueprints/Walkers"
WF_DST="$STAGE/walkers-ccby/CarlaUE4/Content/Carla/Blueprints/Walkers"
mkdir -p "$WF_DST"
cp -p "$WF_SRC/WalkerFactory.uasset" "$WF_SRC/WalkerFactory.uexp" "$WF_DST/"
echo "  WalkerFactory -> walkers-ccby"

# --- normalise directory mtimes ---------------------------------------------
# `cp -p` preserves FILE mtimes, but `mkdir -p` stamps staged DIRECTORIES with the build
# time, which would make the tarballs differ on every rebuild. Pin them to a fixed date.
# It must stay newer than stock CARLA 0.9.15 content (2023-11-10) so that
# `tar --keep-newer-files` does not skip directory metadata on extraction.
DIR_MTIME="2026-07-07 12:00:00"
find "$STAGE" -type d -exec touch -d "$DIR_MTIME" {} +

# Permission bits also leak the build environment (umask, parent-directory defaults), so
# pin them too. These are inert data files; 644/755 is what they should be.
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +

# --- tarballs ----------------------------------------------------------------
# Deterministic: sorted member order, no uid/gid, gzip without a timestamp header.
# Members are rooted at CarlaUE4/ so that `tar -xf` from the CARLA build root
# (which is what ImportAssets.sh does) lands them in the right place.
echo "== packing =="
declare -A TARS=(
  [props]="ood-perceptionbench-props-${VERSION}.tar.gz"
  [walkers-ccby]="ood-perceptionbench-walkers-ccby-${VERSION}.tar.gz"
  [walkers-ccbync]="ood-perceptionbench-walkers-ccbync-${VERSION}.tar.gz"
)
for grp in props walkers-ccby walkers-ccbync; do
  out="$DIST/${TARS[$grp]}"
  tar --sort=name --owner=0 --group=0 --numeric-owner \
      -C "$STAGE/$grp" -cf - CarlaUE4 | gzip -9 -n > "$out"
  echo "  $(basename "$out")  $(stat -c%s "$out") bytes"
done

# --- manifest ----------------------------------------------------------------
echo "== manifest =="
python3 "$(dirname "$0")/make_manifest.py" \
  --stage "$STAGE" --dist "$DIST" --out "$OUT" --version "$VERSION"

echo "== done =="
