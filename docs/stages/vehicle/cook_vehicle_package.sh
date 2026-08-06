#!/usr/bin/env bash
# =============================================================================
# cook_vehicle_package.sh -- optional Stage 0: cook + ingest a VEHICLE package.
#
# Vehicle analogue of pedestrian_check/stages/cook_walker_package.sh. Same shape:
# the Config/<Pkg>.Package.json is authored BY THE USER in the UE editor; this
# script does NOT write it -- it VERIFIES it, then cooks + ingests.
#
#   1. Pull the contract (paths/names) from vehicle_common.py.
#   2. VERIFY Content/<Pkg>/Config/<Pkg>.Package.json exists and has a NON-EMPTY
#      vehicles[] array (optionally containing --vehicle_entry <name>).
#   3. SAFETY: abort if a CARLA server/editor is running (cook + ingest need it OFF).
#   4. --clean (recommended after GUI authoring): drop the iterative cooked output
#      + stale ingest so freshly-compiled shaders are cooked (else grey material).
#   5. cd CARLA_UE_ROOT; activate the CARLA build environment; make package ARGS="--packages=<Pkg>".
#   6. Verify Dist/<Pkg>_0.9.15-dirty.tar.gz, copy into the /media server Import/,
#      run ImportAssets.sh, verify the server's Content/<Pkg> now exists.
#   7. Emit a make_verdict-shaped JSON to --out.
#
# Usage:
#   cook_vehicle_package.sh <PackageName> --blueprint_id vehicle.<make>.<model> \
#       [--vehicle_entry <Package.json vehicles[].name>] \
#       [--out verdict.json] [--reuse-existing] [--clean] [--dry-run]
#
# !!! THE ONE PLACE THIS DIFFERS FROM THE WALKER SCRIPT !!!
# For walkers, `walker.pedestrian.<Package.json walkers[].name>` holds, so that script
# asserts the name matches the blueprint id. For VEHICLES that assertion is FALSE:
#   Content/SUV_Import/Config -> vehicles[].name = "suv_import"
#   live blueprint            -> vehicle.ood.suv
# The registered id is `vehicle.<Make>.<Model>` from the asset's entry in the cooked
# base-content VehicleFactory, which this script cannot read. So we verify only that a
# vehicles[] entry EXISTS and defer id verification to Stage A's live registration check
# -- which is exactly why Stage A must never be skipped.
#
# Server lifecycle is runbook operator-owned.
# =============================================================================

set -euo pipefail

STAGE_NAME="cook_ingest"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The client interpreter comes from the site config -- never hardcoded. Override for one run
# with $OODPB_CLIENT_PYTHON, or point at a different config with $OODPB_SITE_CONFIG.
STAGES_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY_CLIENT="${OODPB_CLIENT_PYTHON:-}"
if [[ -z "$PY_CLIENT" ]]; then
  PY_CLIENT="$(python3 - "$STAGES_ROOT" <<'SITECFG'
import sys; sys.path.insert(0, sys.argv[1])
import site_config
print(site_config.get("client_python"))
SITECFG
)" || { echo "cannot resolve client_python from the site config (see site_config.example.yaml)" >&2; exit 3; }
fi
PY_CLIENT="$PY_CLIENT"

PKG=""
BLUEPRINT_ID=""
VEHICLE_ENTRY=""
OUT=""
REUSE_EXISTING=0
CLEAN=0
DRY_RUN=0

usage() {
  echo "Usage: $0 <PackageName> --blueprint_id vehicle.<make>.<model> [--vehicle_entry <name>] [--out verdict.json] [--reuse-existing] [--clean] [--dry-run]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --blueprint_id)    BLUEPRINT_ID="${2:-}"; shift 2 ;;
    --blueprint_id=*)  BLUEPRINT_ID="${1#*=}"; shift ;;
    --vehicle_entry)   VEHICLE_ENTRY="${2:-}"; shift 2 ;;
    --vehicle_entry=*) VEHICLE_ENTRY="${1#*=}"; shift ;;
    --out)             OUT="${2:-}"; shift 2 ;;
    --out=*)           OUT="${1#*=}"; shift ;;
    --reuse-existing)  REUSE_EXISTING=1; shift ;;
    --clean)           CLEAN=1; shift ;;
    --dry-run)         DRY_RUN=1; shift ;;
    -h|--help)         usage; exit 0 ;;
    -*)                echo "Unknown option: $1" >&2; usage; exit 2 ;;
    *)
      if [[ -z "$PKG" ]]; then PKG="$1"; shift
      else echo "Unexpected positional arg: $1" >&2; usage; exit 2; fi
      ;;
  esac
done

[[ -n "$PKG" ]] || { usage; exit 2; }
[[ -n "$BLUEPRINT_ID" ]] || { echo "--blueprint_id is required" >&2; usage; exit 2; }

# Resolve --out to an ABSOLUTE path now: later steps cd into the UE/server dirs, so a
# relative --out would otherwise land in the wrong cwd.
if [[ -n "$OUT" ]]; then
  _od="$(dirname "$OUT")"; mkdir -p "$_od"
  OUT="$(cd "$_od" && pwd)/$(basename "$OUT")"
fi

# --- pull the contract from vehicle_common (never hardcode paths) ------------
CONTRACT="$("$PY_CLIENT" - "$SCRIPT_DIR" "$PKG" "$BLUEPRINT_ID" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import vehicle_common as v
pkg, bid = sys.argv[2], sys.argv[3]
v.validate_package_name(pkg)   # underscores allowed: Content/SUV_Import etc.
v.validate_blueprint_id(bid)
n = v.vehicle_names_for(pkg, bid, package_name=pkg)
kv = {
    "PKG":              n.package_name,
    "MAKE":             n.make,
    "MODEL":            n.model,
    "DIST_TAR":         n.dist_tar,
    "PACKAGE_JSON":     v.package_json_path(pkg),
    "CONTENT_DIR":      v.content_dir(pkg),
    "CARLA_UE_ROOT":    v.CARLA_UE_ROOT,
    "CARLA_DIST":       v.CARLA_DIST,
    "CARLA_IMPORT_DIR": v.CARLA_IMPORT_DIR,
    "IMPORT_ASSETS_SH": v.CARLA_IMPORT_ASSETS_SH,
    "SERVER_ROOT":      v.CARLA_SERVER_ROOT,
    "CONDA_ROOT":       v.CONDA_ROOT,
    "COOK_CONDA_ENV":   v.COOK_CONDA_ENV,
}
for k, val in kv.items():
    print("%s\t%s" % (k, val))
PY
)" || { echo "Failed to load contract from vehicle_common.py (invalid name/blueprint?)" >&2; exit 7; }

while IFS=$'\t' read -r _k _v; do
  case "$_k" in
    PKG)              PKG="$_v" ;;
    MAKE)             MAKE="$_v" ;;
    MODEL)            MODEL="$_v" ;;
    DIST_TAR)         DIST_TAR="$_v" ;;
    PACKAGE_JSON)     PACKAGE_JSON="$_v" ;;
    CONTENT_DIR)      CONTENT_DIR="$_v" ;;
    CARLA_UE_ROOT)    CARLA_UE_ROOT="$_v" ;;
    CARLA_DIST)       CARLA_DIST="$_v" ;;
    CARLA_IMPORT_DIR) CARLA_IMPORT_DIR="$_v" ;;
    IMPORT_ASSETS_SH) IMPORT_ASSETS_SH="$_v" ;;
    SERVER_ROOT)      SERVER_ROOT="$_v" ;;
    CONDA_ROOT)       CONDA_ROOT="$_v" ;;
    COOK_CONDA_ENV)   COOK_CONDA_ENV="$_v" ;;
  esac
done <<< "$CONTRACT"

DIST_TAR_PATH="${CARLA_DIST}/${DIST_TAR}"
DIST_BUILD_DIR="${DIST_TAR_PATH%.tar.gz}"
IMPORTED_TAR_PATH="${CARLA_IMPORT_DIR}/${DIST_TAR}"
SERVER_CONTENT="${SERVER_ROOT}/CarlaUE4/Content/${PKG}"
CONDA_SH="${CONDA_ROOT}/etc/profile.d/conda.sh"
BLUEPRINT="${BLUEPRINT_ID}"

# --- verdict emitter (make_verdict shape) ------------------------------------
emit_verdict() {
  local ok="$1" error="$2" data_json="$3"
  [[ -z "$OUT" ]] && return 0
  local ts; ts="$(date +%s.%N)"
  mkdir -p "$(dirname "$OUT")"
  if [[ "$error" == "null" ]]; then
    printf '{\n  "stage": "%s",\n  "ok": %s,\n  "data": %s,\n  "error": null,\n  "ts": %s\n}\n' \
      "$STAGE_NAME" "$ok" "$data_json" "$ts" > "$OUT"
  else
    local esc="${error//\\/\\\\}"; esc="${esc//\"/\\\"}"
    printf '{\n  "stage": "%s",\n  "ok": %s,\n  "data": %s,\n  "error": "%s",\n  "ts": %s\n}\n' \
      "$STAGE_NAME" "$ok" "$data_json" "$esc" "$ts" > "$OUT"
  fi
}

data_blob() {
  printf '{"package": "%s", "blueprint_id": "%s", "make": "%s", "model": "%s", "vehicle_entry": "%s", "package_json": "%s", "dist_tar": "%s", "imported_tar": "%s", "server_content": "%s", "clean": %s, "reuse_existing": %s, "id_verified_here": false}' \
    "$PKG" "$BLUEPRINT" "$MAKE" "$MODEL" "$VEHICLE_ENTRY" "$PACKAGE_JSON" "$DIST_TAR_PATH" "$IMPORTED_TAR_PATH" "$SERVER_CONTENT" "$CLEAN" "$REUSE_EXISTING"
}

fail() {
  echo "[cook_vehicle] ERROR: $1" >&2
  emit_verdict false "$1" "$(data_blob)"
  exit 1
}

# =============================================================================
# Step 2: VERIFY the user-authored vehicles Package.json (do NOT write it).
# NOTE: we can only assert an entry EXISTS -- vehicles[].name is NOT the blueprint
# model (see the header). Stage A's live filter() is the real id check.
# =============================================================================
echo "[cook_vehicle] package=${PKG} blueprint=${BLUEPRINT} (make=${MAKE} model=${MODEL})"
echo "[cook_vehicle] Step 2: verifying ${PACKAGE_JSON}"
[[ -f "$PACKAGE_JSON" ]] || fail "Package.json not found: ${PACKAGE_JSON} (author the vehicle in the UE editor first)"
"$PY_CLIENT" - "$PACKAGE_JSON" "$VEHICLE_ENTRY" <<'PY' || fail "Package.json has no usable vehicles[] entry (see stderr)"
import json, sys
pj, want = sys.argv[1], sys.argv[2]
d = json.load(open(pj))
vehicles = d.get("vehicles") or []
names = [v.get("name") for v in vehicles]
if not vehicles:
    sys.stderr.write(
        "[cook_vehicle] Package.json has no 'vehicles' entries (keys present: %s). A vehicle "
        "package must be vehicles-keyed and point at its BP_<Name>.\n" % sorted(d)
    )
    sys.exit(1)
if want and want not in names:
    sys.stderr.write("[cook_vehicle] vehicles names %s do not include requested entry %r\n" % (names, want))
    sys.exit(1)
missing = [v for v in vehicles if not v.get("path")]
if missing:
    sys.stderr.write("[cook_vehicle] vehicles entries missing a 'path': %s\n" % missing)
    sys.exit(1)
sys.stderr.write(
    "[cook_vehicle] NOTE: vehicles[].name %s is NOT the blueprint model. The live id comes from "
    "the VehicleFactory Make/Model and is verified in Stage A.\n" % names
)
PY

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[cook_vehicle] --dry-run: verified Package.json only; no cook, no ingest."
  emit_verdict true null "$(data_blob)"
  exit 0
fi

# =============================================================================
# Step 3: SAFETY -- abort if a CARLA server/editor is running (cook needs it OFF).
# =============================================================================
echo "[cook_vehicle] Step 3: checking no CARLA server/editor is running"
SELF_PIDS="^($$|${PPID:-0})\$"
RUNNING=""
check_proc() {
  local label="$1" pat="$2" pids
  pids="$(pgrep -f "$pat" 2>/dev/null | grep -vE "$SELF_PIDS" || true)"
  [[ -n "$pids" ]] && RUNNING="${RUNNING}${RUNNING:+, }${label}[$(echo $pids | tr '\n' ' ')]"
  return 0
}
check_proc "standalone-server" "${SERVER_ROOT}/CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping"
check_proc "source-editor"     "${CARLA_UE_ROOT}/Unreal/CarlaUE4/Binaries/Linux/CarlaUE4 "
[[ -n "$RUNNING" ]] && fail "A CARLA server/editor is running ($RUNNING). Close it before cooking + ingesting."

# =============================================================================
# Step 4 (--clean): force a fresh cook so freshly-GUI-compiled shaders are cooked
# (else the vehicle can render grey). Removes the iterative output + stale ingest.
# =============================================================================
if [[ "$CLEAN" -eq 1 ]]; then
  echo "[cook_vehicle] Step 4: --clean -> removing iterative cooked output + stale ingest"
  rm -rf "$DIST_BUILD_DIR" "$DIST_TAR_PATH" "$SERVER_CONTENT" "$IMPORTED_TAR_PATH"
fi

# =============================================================================
# Step 5: cook -- cd CARLA_UE_ROOT; activate the CARLA build environment; make package.
# =============================================================================
if [[ "$REUSE_EXISTING" -eq 1 && -f "$DIST_TAR_PATH" ]]; then
  echo "[cook_vehicle] Step 5: --reuse-existing and Dist tar present -> skipping 'make package'"
else
  echo "[cook_vehicle] Step 5: cooking with 'make package ARGS=\"--packages=${PKG}\"'"
  [[ -d "$CARLA_UE_ROOT" ]] || fail "CARLA_UE_ROOT not found: $CARLA_UE_ROOT"
  [[ -f "$CONDA_SH" ]]      || fail "conda.sh not found: $CONDA_SH"
  cd "$CARLA_UE_ROOT"
  # shellcheck disable=SC1090
  set +u
  source "$CONDA_SH"
  conda activate "$COOK_CONDA_ENV"
  set -u
  make package ARGS="--packages=${PKG}" || fail "'make package' failed for ${PKG}"
fi

# =============================================================================
# Step 6: verify Dist tar, ingest into the /media server, verify Content.
# =============================================================================
echo "[cook_vehicle] Step 6: verifying ${DIST_TAR_PATH}"
[[ -f "$DIST_TAR_PATH" ]] || fail "cook produced no Dist tar: $DIST_TAR_PATH"

echo "[cook_vehicle] Step 6: ingesting into ${SERVER_ROOT}"
[[ -d "$CARLA_IMPORT_DIR" ]] || fail "Import dir not found: $CARLA_IMPORT_DIR"
[[ -f "$IMPORT_ASSETS_SH" ]] || fail "ImportAssets.sh not found: $IMPORT_ASSETS_SH"
cp -f "$DIST_TAR_PATH" "$IMPORTED_TAR_PATH" || fail "failed to copy tar into $CARLA_IMPORT_DIR"
cd "$SERVER_ROOT"
bash "$IMPORT_ASSETS_SH" || fail "ImportAssets.sh failed"
[[ -d "$SERVER_CONTENT" ]] || fail "ingest did not create server Content dir: $SERVER_CONTENT"

echo "[cook_vehicle] OK: ${PKG} cooked + ingested -> ${SERVER_CONTENT}"
echo "[cook_vehicle] Expected blueprint ${BLUEPRINT} -- NOT verified here; Stage A checks it live."
emit_verdict true null "$(data_blob)"
exit 0
