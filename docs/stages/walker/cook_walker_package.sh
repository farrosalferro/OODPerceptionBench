#!/usr/bin/env bash
# =============================================================================
# cook_walker_package.sh -- optional Stage 0: cook + ingest a WALKER package.
#
# Walker analogue of import_automation/stages/cook_package.sh. KEY DIFFERENCE:
# a walker's Config/<Pkg>.Package.json is authored BY THE USER in the UE editor
# (walkers-keyed, pointing at BP_<Name> + a Maps entry). This script does NOT
# write it -- it VERIFIES it, then cooks + ingests:
#
#   1. Pull the contract (paths/names) from walker_common.py.
#   2. VERIFY Content/<Pkg>/Config/<Pkg>.Package.json exists and has a
#      walkers[] entry whose name == <walker_id> (the blueprint_id last segment).
#   3. SAFETY: abort if a CARLA server/editor is running (cook + ingest need it OFF).
#   4. --clean (recommended after GUI authoring): drop the iterative cooked output
#      + stale ingest so freshly-compiled shaders are cooked (else grey material).
#   5. cd CARLA_UE_ROOT; activate the CARLA build environment; make package ARGS="--packages=<Pkg>".
#   6. Verify Dist/<Pkg>_0.9.15-dirty.tar.gz, copy into the /media server Import/,
#      run ImportAssets.sh, verify the server's Content/<Pkg> now exists.
#   7. Emit a make_verdict-shaped JSON to --out.
#
# Usage:
#   cook_walker_package.sh <PackageName> --blueprint_id walker.pedestrian.<id> \
#       [--out verdict.json] [--reuse-existing] [--clean] [--dry-run]
#
# The resulting blueprint is walker.pedestrian.<id>; <id> must equal the
# Package.json walkers[].name (lowercased). Server lifecycle is runbook operator-owned.
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
OUT=""
REUSE_EXISTING=0
CLEAN=0
DRY_RUN=0

usage() {
  echo "Usage: $0 <PackageName> --blueprint_id walker.pedestrian.<id> [--out verdict.json] [--reuse-existing] [--clean] [--dry-run]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --blueprint_id)   BLUEPRINT_ID="${2:-}"; shift 2 ;;
    --blueprint_id=*) BLUEPRINT_ID="${1#*=}"; shift ;;
    --out)            OUT="${2:-}"; shift 2 ;;
    --out=*)          OUT="${1#*=}"; shift ;;
    --reuse-existing) REUSE_EXISTING=1; shift ;;
    --clean)          CLEAN=1; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    -*)               echo "Unknown option: $1" >&2; usage; exit 2 ;;
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

# --- pull the contract from walker_common (never hardcode paths) -------------
CONTRACT="$("$PY_CLIENT" - "$SCRIPT_DIR" "$PKG" "$BLUEPRINT_ID" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import walker_common as w
pkg, bid = sys.argv[2], sys.argv[3]
w.validate_camelcase(pkg)
w.validate_blueprint_id(bid)
n = w.walker_names_for(pkg, bid, package_name=pkg)
kv = {
    "PKG":              n.package_name,
    "WALKER_ID":        n.walker_id,
    "DIST_TAR":         n.dist_tar,
    "PACKAGE_JSON":     w.package_json_path(pkg),
    "CONTENT_DIR":      w.content_dir(pkg),
    "CARLA_UE_ROOT":    w.CARLA_UE_ROOT,
    "CARLA_DIST":       w.CARLA_DIST,
    "CARLA_IMPORT_DIR": w.CARLA_IMPORT_DIR,
    "IMPORT_ASSETS_SH": w.CARLA_IMPORT_ASSETS_SH,
    "SERVER_ROOT":      w.CARLA_SERVER_ROOT,
    "CONDA_ROOT":       w.CONDA_ROOT,
    "COOK_CONDA_ENV":   w.COOK_CONDA_ENV,
}
for k, v in kv.items():
    print("%s\t%s" % (k, v))
PY
)" || { echo "Failed to load contract from walker_common.py (invalid name/blueprint?)" >&2; exit 7; }

while IFS=$'\t' read -r _k _v; do
  case "$_k" in
    PKG)              PKG="$_v" ;;
    WALKER_ID)        WALKER_ID="$_v" ;;
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
BLUEPRINT="walker.pedestrian.${WALKER_ID}"

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
  printf '{"package": "%s", "blueprint_id": "%s", "walker_id": "%s", "package_json": "%s", "dist_tar": "%s", "imported_tar": "%s", "server_content": "%s", "clean": %s, "reuse_existing": %s}' \
    "$PKG" "$BLUEPRINT" "$WALKER_ID" "$PACKAGE_JSON" "$DIST_TAR_PATH" "$IMPORTED_TAR_PATH" "$SERVER_CONTENT" "$CLEAN" "$REUSE_EXISTING"
}

fail() {
  echo "[cook_walker] ERROR: $1" >&2
  emit_verdict false "$1" "$(data_blob)"
  exit 1
}

# =============================================================================
# Step 2: VERIFY the user-authored walkers Package.json (do NOT write it).
# =============================================================================
echo "[cook_walker] package=${PKG} blueprint=${BLUEPRINT}"
echo "[cook_walker] Step 2: verifying ${PACKAGE_JSON}"
[[ -f "$PACKAGE_JSON" ]] || fail "Package.json not found: ${PACKAGE_JSON} (author the walker in the UE editor first)"
"$PY_CLIENT" - "$PACKAGE_JSON" "$WALKER_ID" <<'PY' || fail "Package.json has no walkers[] entry matching the blueprint id (see stderr)"
import json, sys
pj, wid = sys.argv[1], sys.argv[2]
d = json.load(open(pj))
walkers = d.get("walkers") or []
names = [w.get("name") for w in walkers]
if wid not in names:
    sys.stderr.write("[cook_walker] Package.json walkers names %s do not include %r\n" % (names, wid))
    sys.exit(1)
PY

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[cook_walker] --dry-run: verified Package.json only; no cook, no ingest."
  emit_verdict true null "$(data_blob)"
  exit 0
fi

# =============================================================================
# Step 3: SAFETY -- abort if a CARLA server/editor is running (cook needs it OFF).
# =============================================================================
echo "[cook_walker] Step 3: checking no CARLA server/editor is running"
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
# (else the walker can render grey). Removes the iterative output + stale ingest.
# =============================================================================
if [[ "$CLEAN" -eq 1 ]]; then
  echo "[cook_walker] Step 4: --clean -> removing iterative cooked output + stale ingest"
  rm -rf "$DIST_BUILD_DIR" "$DIST_TAR_PATH" "$SERVER_CONTENT" "$IMPORTED_TAR_PATH"
fi

# =============================================================================
# Step 5: cook -- cd CARLA_UE_ROOT; activate the CARLA build environment; make package.
# =============================================================================
if [[ "$REUSE_EXISTING" -eq 1 && -f "$DIST_TAR_PATH" ]]; then
  echo "[cook_walker] Step 5: --reuse-existing and Dist tar present -> skipping 'make package'"
else
  echo "[cook_walker] Step 5: cooking with 'make package ARGS=\"--packages=${PKG}\"'"
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
echo "[cook_walker] Step 6: verifying ${DIST_TAR_PATH}"
[[ -f "$DIST_TAR_PATH" ]] || fail "cook produced no Dist tar: $DIST_TAR_PATH"

echo "[cook_walker] Step 6: ingesting into ${SERVER_ROOT}"
[[ -d "$CARLA_IMPORT_DIR" ]] || fail "Import dir not found: $CARLA_IMPORT_DIR"
[[ -f "$IMPORT_ASSETS_SH" ]] || fail "ImportAssets.sh not found: $IMPORT_ASSETS_SH"
cp -f "$DIST_TAR_PATH" "$IMPORTED_TAR_PATH" || fail "failed to copy tar into $CARLA_IMPORT_DIR"
cd "$SERVER_ROOT"
bash "$IMPORT_ASSETS_SH" || fail "ImportAssets.sh failed"
[[ -d "$SERVER_CONTENT" ]] || fail "ingest did not create server Content dir: $SERVER_CONTENT"

echo "[cook_walker] OK: ${PKG} cooked + ingested -> ${SERVER_CONTENT} (blueprint ${BLUEPRINT})"
emit_verdict true null "$(data_blob)"
exit 0
