#!/usr/bin/env bash
# =============================================================================
# cook_package.sh -- Agent 4 (see the procedure document): cook + package + ingest.
#
# Steps:
#   1. Write Content/<A>/Config/<A>.Package.json (maps:[], one prop, size Medium).
#   2. SAFETY: abort if a CarlaUE4 / UE4Editor server or editor is running --
#      cook + ingest MUST run with the server OFF.
#   3. cd CARLA_UE_ROOT; activate the CARLA build environment; make package ARGS="--packages=<A>".
#   4. Verify Dist/<A>_0.9.15-dirty.tar.gz exists.
#   5. Copy that tar into the standalone CARLA Import/, run ImportAssets.sh,
#      verify the server's CarlaUE4/Content/<A> now exists.
#   6. Emit a common.make_verdict-shaped JSON to --out.
#
# Usage:
#   cook_package.sh <AssetName> [--out verdict.json] [--reuse-existing] [--dry-run]
#
# Names/paths/anchors are pulled FROM the shared contract (common.py via
# names_for); nothing the contract already defines is hardcoded here.
#
# --reuse-existing : skip `make package` if Dist tar already present (cook is
#                    minutes; never redo needlessly -- spec sec 5).
# --dry-run        : write ONLY the Package.json to a temp path, print that path,
#                    and exit 0. (Used by the self-test; no cook, no ingest.)
# =============================================================================

set -euo pipefail

# --- locate self + the shared contract ---------------------------------------
STAGE_NAME="cook_package"
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

# --- arg parse ---------------------------------------------------------------
ASSET=""
OUT=""
REUSE_EXISTING=0
DRY_RUN=0
CLEAN=0

usage() {
  echo "Usage: $0 <AssetName> [--out verdict.json] [--reuse-existing] [--clean] [--dry-run]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)             OUT="${2:-}"; shift 2 ;;
    --out=*)           OUT="${1#*=}"; shift ;;
    --reuse-existing)  REUSE_EXISTING=1; shift ;;
    --clean)           CLEAN=1; shift ;;
    --dry-run)         DRY_RUN=1; shift ;;
    -h|--help)         usage; exit 0 ;;
    -*)                echo "Unknown option: $1" >&2; usage; exit 2 ;;
    *)
      if [[ -z "$ASSET" ]]; then ASSET="$1"; shift
      else echo "Unexpected positional arg: $1" >&2; usage; exit 2; fi
      ;;
  esac
done

if [[ -z "$ASSET" ]]; then
  usage; exit 2
fi

# --- derive everything from the shared contract (never hardcode) -------------
# Pull names_for(...) + path constants out of common.py in one shot. Tab-sep KEY\tVALUE.
CONTRACT="$("$PY_CLIENT" - "$SCRIPT_DIR" "$ASSET" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import common
n = common.names_for(sys.argv[2])
kv = {
    "ASSET":            n.asset_name,
    "SM_NAME":          n.sm_name,
    "SM_PACKAGE_PATH":  n.sm_package_path,
    "DIST_TAR":         n.dist_tar,
    "CARLA_UE_ROOT":    common.CARLA_UE_ROOT,
    "CARLA_DIST":       common.CARLA_DIST,
    "CARLA_CONTENT":    common.CARLA_CONTENT,
    "CARLA_IMPORT_DIR": common.CARLA_IMPORT_DIR,
    "IMPORT_ASSETS_SH": common.CARLA_IMPORT_ASSETS_SH,
    "SERVER_ROOT":      common.CARLA_SERVER_ROOT,
    "CONDA_ROOT":       common.CONDA_ROOT,
    "COOK_CONDA_ENV":   common.COOK_CONDA_ENV,
}
try:
    common.validate_asset_name(n.asset_name)
except Exception as e:
    sys.stderr.write("INVALID_ASSET_NAME: %s\n" % e)
    sys.exit(7)
for k, v in kv.items():
    print("%s\t%s" % (k, v))
PY
)" || { echo "Failed to load contract from common.py (invalid asset name?)" >&2; exit 7; }

# Parse the KEY\tVALUE pairs into shell vars.
while IFS=$'\t' read -r _k _v; do
  case "$_k" in
    ASSET)            ASSET="$_v" ;;
    SM_NAME)          SM_NAME="$_v" ;;
    SM_PACKAGE_PATH)  SM_PACKAGE_PATH="$_v" ;;
    DIST_TAR)         DIST_TAR="$_v" ;;
    CARLA_UE_ROOT)    CARLA_UE_ROOT="$_v" ;;
    CARLA_DIST)       CARLA_DIST="$_v" ;;
    CARLA_CONTENT)    CARLA_CONTENT="$_v" ;;
    CARLA_IMPORT_DIR) CARLA_IMPORT_DIR="$_v" ;;
    IMPORT_ASSETS_SH) IMPORT_ASSETS_SH="$_v" ;;
    SERVER_ROOT)      SERVER_ROOT="$_v" ;;
    CONDA_ROOT)       CONDA_ROOT="$_v" ;;
    COOK_CONDA_ENV)   COOK_CONDA_ENV="$_v" ;;
  esac
done <<< "$CONTRACT"

# Derived on-disk locations.
CONTENT_FOLDER="${CARLA_CONTENT}/${ASSET}"
CONFIG_DIR="${CONTENT_FOLDER}/Config"
PACKAGE_JSON="${CONFIG_DIR}/${ASSET}.Package.json"
DIST_TAR_PATH="${CARLA_DIST}/${DIST_TAR}"
DIST_BUILD_DIR="${DIST_TAR_PATH%.tar.gz}"   # uncooked iterative output dir the cook reuses with -iterate
IMPORTED_TAR_PATH="${CARLA_IMPORT_DIR}/${DIST_TAR}"
SERVER_CONTENT="${SERVER_ROOT}/CarlaUE4/Content/${ASSET}"
CONDA_SH="${CONDA_ROOT}/etc/profile.d/conda.sh"

# --- verdict emitter (bash heredoc, no python; common.make_verdict shape) ----
# {stage, ok, data:{...}, error, ts}.  data_json is a pre-built JSON object.
emit_verdict() {
  local ok="$1" error="$2" data_json="$3"
  [[ -z "$OUT" ]] && return 0
  local ts; ts="$(date +%s.%N)"
  mkdir -p "$(dirname "$OUT")"
  if [[ "$error" == "null" ]]; then
    printf '{\n  "stage": "%s",\n  "ok": %s,\n  "data": %s,\n  "error": null,\n  "ts": %s\n}\n' \
      "$STAGE_NAME" "$ok" "$data_json" "$ts" > "$OUT"
  else
    # JSON-escape the error string (quotes + backslashes).
    local esc="${error//\\/\\\\}"; esc="${esc//\"/\\\"}"
    printf '{\n  "stage": "%s",\n  "ok": %s,\n  "data": %s,\n  "error": "%s",\n  "ts": %s\n}\n' \
      "$STAGE_NAME" "$ok" "$data_json" "$esc" "$ts" > "$OUT"
  fi
}

data_blob() {
  # Stable data object reflecting the derived plan/results.
  printf '{"asset": "%s", "sm_name": "%s", "package_json": "%s", "sm_package_path": "%s", "dist_tar": "%s", "imported_tar": "%s", "server_content": "%s", "reuse_existing": %s}' \
    "$ASSET" "$SM_NAME" "$PACKAGE_JSON" "$SM_PACKAGE_PATH" "$DIST_TAR_PATH" "$IMPORTED_TAR_PATH" "$SERVER_CONTENT" "$REUSE_EXISTING"
}

fail() {
  local msg="$1"
  echo "[cook_package] ERROR: $msg" >&2
  emit_verdict false "$msg" "$(data_blob)"
  exit 1
}

# =============================================================================
# Step 1: write the Package.json (always; this is the cook input).
# =============================================================================
write_package_json() {
  local dest="$1"
  mkdir -p "$(dirname "$dest")"
  # Exactly: maps:[], one prop {name, path, size:Medium}. Matches the committed
  # DutchCrashAttenuator.Package.json semantically (same name/path/size).
  cat > "$dest" <<EOF
{
    "maps": [
    ],
    "props": [
        {
            "name": "${ASSET}",
            "path": "${SM_PACKAGE_PATH}",
            "size": "Medium"
        }
    ]
}
EOF
}

# --- dry-run short circuit: write ONLY the Package.json to a temp path, exit ---
if [[ "$DRY_RUN" -eq 1 ]]; then
  TMP_PKG="$(mktemp -t "${ASSET}.Package.XXXXXX.json")"
  write_package_json "$TMP_PKG"
  echo "$TMP_PKG"
  exit 0
fi

echo "[cook_package] asset=${ASSET}"

# =============================================================================
# Step 2: SAFETY -- abort if the CARLA server/editor is running.
#   Cook + ingest must run with the server OFF (see the procedure document).
#   Runs BEFORE any filesystem mutation so an abort leaves the tree untouched.
#   Detection is PATH-ANCHORED to the real binaries (not generic substrings like
#   "UE4Editor"/"CarlaUE4 ", which false-positive on sibling agents, log tails, and
#   this script's own command line) and excludes our own / parent PID.
# =============================================================================
echo "[cook_package] Step 2: checking no CARLA server/editor is running"
SELF_PIDS="^($$|${PPID:-0})\$"
RUNNING=""
check_proc() {  # $1=label  $2=path-anchored full-cmdline pattern
  local label="$1" pat="$2" pids
  pids="$(pgrep -f "$pat" 2>/dev/null | grep -vE "$SELF_PIDS" || true)"
  if [[ -n "$pids" ]]; then
    RUNNING="${RUNNING}${RUNNING:+, }${label}[$(echo $pids | tr '\n' ' ')]"
  fi
  return 0   # never let an empty match (set -e) abort the script
}
# the standalone server we ingest into, and a GUI editor open on the source project
check_proc "standalone-server" "${SERVER_ROOT}/CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping"
check_proc "source-editor"     "${CARLA_UE_ROOT}/Unreal/CarlaUE4/Binaries/Linux/CarlaUE4 "
if [[ -n "$RUNNING" ]]; then
  fail "A CARLA server/editor is running ($RUNNING). Stop it before cooking + ingesting (cook and ImportAssets must run with the server OFF)."
fi

# =============================================================================
# Step 1: write the Package.json (cook input) -- only after the safety check.
# =============================================================================
echo "[cook_package] Step 1: writing ${PACKAGE_JSON}"
write_package_json "$PACKAGE_JSON"

# =============================================================================
# Step 1b (--clean): force a fresh cook. CARLA's cook uses `-iterate` and reuses
# the cooked output dir AND cached shaders; after authoring a material in the UE
# GUI you MUST clean so the freshly-compiled shaders are cooked (else the prop
# stays grey/WorldGridMaterial). Removes the Dist output + tar + stale ingest.
# =============================================================================
if [[ "$CLEAN" -eq 1 ]]; then
  echo "[cook_package] Step 1b: --clean -> removing iterative cooked output + stale ingest"
  rm -rf "$DIST_BUILD_DIR" "$DIST_TAR_PATH" "$SERVER_CONTENT" "$IMPORTED_TAR_PATH"
fi

# =============================================================================
# Step 3: cook -- cd CARLA_UE_ROOT; activate the CARLA build environment; make package.
#   conda activate trips `set -u`; unset -u around it (repo convention).
# =============================================================================
if [[ "$REUSE_EXISTING" -eq 1 && -f "$DIST_TAR_PATH" ]]; then
  echo "[cook_package] Step 3: --reuse-existing and Dist tar present -> skipping 'make package'"
else
  echo "[cook_package] Step 3: cooking with 'make package ARGS=\"--packages=${ASSET}\"'"
  [[ -d "$CARLA_UE_ROOT" ]] || fail "CARLA_UE_ROOT not found: $CARLA_UE_ROOT"
  [[ -f "$CONDA_SH" ]]      || fail "conda.sh not found: $CONDA_SH"
  cd "$CARLA_UE_ROOT"
  # shellcheck disable=SC1090
  set +u
  source "$CONDA_SH"
  conda activate "$COOK_CONDA_ENV"
  set -u
  make package ARGS="--packages=${ASSET}" || fail "'make package' failed for ${ASSET}"
fi

# =============================================================================
# Step 4: verify the Dist tarball exists.
# =============================================================================
echo "[cook_package] Step 4: verifying ${DIST_TAR_PATH}"
[[ -f "$DIST_TAR_PATH" ]] || fail "cook produced no Dist tar: $DIST_TAR_PATH"

# =============================================================================
# Step 5: copy tar into the standalone CARLA Import/, ingest, verify Content.
# =============================================================================
echo "[cook_package] Step 5: ingesting into standalone CARLA at ${SERVER_ROOT}"
[[ -d "$CARLA_IMPORT_DIR" ]] || fail "Import dir not found: $CARLA_IMPORT_DIR"
[[ -f "$IMPORT_ASSETS_SH" ]] || fail "ImportAssets.sh not found: $IMPORT_ASSETS_SH"

cp -f "$DIST_TAR_PATH" "$IMPORTED_TAR_PATH" || fail "failed to copy tar into $CARLA_IMPORT_DIR"

# ImportAssets.sh untars everything under Import/ relative to SERVER_ROOT.
cd "$SERVER_ROOT"
bash "$IMPORT_ASSETS_SH" || fail "ImportAssets.sh failed"

[[ -d "$SERVER_CONTENT" ]] || fail "ingest did not create server Content dir: $SERVER_CONTENT"

# =============================================================================
# Step 6: success verdict.
# =============================================================================
echo "[cook_package] OK: ${ASSET} cooked + ingested -> ${SERVER_CONTENT}"
emit_verdict true null "$(data_blob)"
exit 0
