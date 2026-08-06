#!/bin/bash
# secondary_ingest.sh -- OPTIONAL: install a cooked package into a SECOND CARLA.
#
# Artifact version: v0.9 -- corresponds to arXiv v1 of the OOD-PerceptionBench paper.
#
# The cook installs into `carla_pkg`. If you evaluate on a different machine (a cluster, say),
# that build also needs the package: installing into one CARLA does NOT install into another,
# and a route run against a build that never received the tarball silently substitutes a
# fallback actor and still reports a plausible score.
#
# Configure `secondary_carla_pkg` (and, when it is on another host, `secondary_ssh_host` and
# optionally `secondary_submit_partitions`) in your site config. With none of them set this
# script is an inert no-op, which is the correct behaviour for a single-machine setup.
#
#   secondary_ingest.sh <PackageName> [--out verdict.json] [--dry-run] [--reuse-existing]
#                                     [--config <site_config.yaml>]
#
# Success = Content/<PackageName> present on the secondary build. NOT the tar/scheduler exit
# code: `tar --keep-newer-files` exits 2 when it skips shared Engine files, which is expected.
set -uo pipefail

PKG=""; OUT=""; DRY_RUN=0; REUSE=0; CFG=""
usage(){
  echo "Usage: $0 <PackageName> [--out verdict.json] [--dry-run] [--reuse-existing] [--config FILE]" >&2
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)            OUT="${2:-}"; shift 2 ;;
    --out=*)          OUT="${1#*=}"; shift ;;
    --config)         CFG="${2:-}"; shift 2 ;;
    --config=*)       CFG="${1#*=}"; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --reuse-existing) REUSE=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    -*)               echo "Unknown option: $1" >&2; usage; exit 2 ;;
    *) if [[ -z "$PKG" ]]; then PKG="$1"; shift; else echo "Unexpected arg: $1" >&2; usage; exit 2; fi ;;
  esac
done
[[ -n "$PKG" ]] || { usage; exit 2; }

STAGES_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -n "$CFG" ]] && export OODPB_SITE_CONFIG="$CFG"

# Every path comes from the site config; nothing here is hardcoded.
PY="${OODPB_CLIENT_PYTHON:-}"
if [[ -z "$PY" ]]; then
  PY="$(python3 - "$STAGES_ROOT" <<'PY' 2>/dev/null
import sys; sys.path.insert(0, sys.argv[1])
import site_config
print(site_config.get("client_python"))
PY
)" || { echo "cannot resolve client_python from the site config" >&2; exit 3; }
fi

read -r CARLA_DIST VERSION_TAG DEST_ROOT SSH_HOST PARTITIONS < <("$PY" - "$STAGES_ROOT" <<'PY'
import sys; sys.path.insert(0, sys.argv[1])
import site_config
dest = site_config.get_optional("secondary_carla_pkg")
print(site_config.get("carla_src") + "/Dist",
      "0.9.15-dirty",
      dest or "-",
      site_config.get_optional("secondary_ssh_host") or "-",
      site_config.get_optional("secondary_submit_partitions") or "-")
PY
) || { echo "failed to read the site config" >&2; exit 3; }

emit(){  # ok data_json error
  local ok="$1" data="$2" err="$3"
  local ts; ts=$("$PY" -c 'import time;print(repr(time.time()))')
  local json="{\"stage\":\"ingest_secondary\",\"ok\":${ok},\"data\":${data},\"error\":${err},\"ts\":${ts}}"
  if [[ -n "$OUT" ]]; then mkdir -p "$(dirname "$OUT")"; echo "$json" > "$OUT"; fi
  echo "$json"
}
fail(){ echo "[secondary_ingest] $1" >&2; emit false "{\"package\":\"${PKG}\"}" "\"$1\""; exit 1; }

if [[ "$DEST_ROOT" == "-" ]]; then
  echo "[secondary_ingest] secondary_carla_pkg is not configured; nothing to do."
  emit true "{\"package\":\"${PKG}\",\"skipped\":true,\"reason\":\"secondary_carla_pkg unset\"}" null
  exit 0
fi

TAR="${CARLA_DIST}/${PKG}_${VERSION_TAG}.tar.gz"
DEST_TAR="${DEST_ROOT}/Import/${PKG}_${VERSION_TAG}.tar.gz"
CONTENT="${DEST_ROOT}/CarlaUE4/Content/${PKG}"
LOG="${DEST_ROOT}/${PKG}_ingest.log"

[[ -f "$TAR" ]] || fail "cooked tar not found: ${TAR} (run the cook stage first)"

if [[ "$DRY_RUN" == 1 ]]; then
  emit true "{\"package\":\"${PKG}\",\"src_tar\":\"${TAR}\",\"dest_tar\":\"${DEST_TAR}\",\"dest_content\":\"${CONTENT}\",\"dry_run\":true}" null
  exit 0
fi

# 1. stage the tar into the secondary build's Import/
if [[ "$REUSE" == 1 && -f "$DEST_TAR" && "$(stat -c %s "$TAR")" == "$(stat -c %s "$DEST_TAR" 2>/dev/null)" ]]; then
  echo "[secondary_ingest] reusing the existing tar in Import/"
else
  echo "[secondary_ingest] copying ${PKG} tar into ${DEST_ROOT}/Import/ (this is large)..."
  mkdir -p "${DEST_ROOT}/Import" || fail "cannot create ${DEST_ROOT}/Import"
  cp -f "$TAR" "$DEST_TAR" || fail "copy to ${DEST_TAR} failed"
fi

# 2. run ImportAssets.sh where the secondary build lives.
#    ImportAssets.sh must run on a host where that filesystem is native.
if [[ "$SSH_HOST" == "-" ]]; then
  echo "[secondary_ingest] running ImportAssets.sh locally in ${DEST_ROOT} ..."
  ( cd "$DEST_ROOT" && bash ImportAssets.sh > "$LOG" 2>&1; echo "RC=$?" >> "$LOG" ) \
    || echo "[secondary_ingest] note: ImportAssets returned non-zero (tar exits 2 on --keep-newer-files; verifying on disk)"
elif [[ "$PARTITIONS" == "-" ]]; then
  echo "[secondary_ingest] running ImportAssets.sh on ${SSH_HOST} ..."
  ssh -o BatchMode=yes -o ConnectTimeout=20 "$SSH_HOST" \
    "cd ${DEST_ROOT} && bash ImportAssets.sh > ${LOG} 2>&1; echo RC=\$? >> ${LOG}" \
    || echo "[secondary_ingest] note: ImportAssets returned non-zero; verifying on disk"
else
  echo "[secondary_ingest] submitting ImportAssets.sh via the scheduler on ${SSH_HOST} ..."
  ssh -o BatchMode=yes -o ConnectTimeout=20 "$SSH_HOST" \
    "srun --partition=${PARTITIONS} --cpus-per-task=1 --mem=4G --time=00:30:00 -J ${PKG}_ingest \
         --chdir=${DEST_ROOT} bash -c 'bash ImportAssets.sh > ${LOG} 2>&1; echo RC=\$? >> ${LOG}'" \
    || echo "[secondary_ingest] note: the job returned non-zero; verifying on disk"
fi

# 3. success = Content/<PKG> present on the secondary build (never the exit code)
if [[ -d "$CONTENT" ]]; then
  NFILES=$(find "$CONTENT" -type f 2>/dev/null | wc -l)
  emit true "{\"package\":\"${PKG}\",\"dest_content\":\"${CONTENT}\",\"n_files\":${NFILES},\"dest_tar\":\"${DEST_TAR}\",\"log\":\"${LOG}\"}" null
  echo "[secondary_ingest] OK: ${CONTENT} (${NFILES} files)"
  echo "[secondary_ingest] NOTE: content on disk is necessary but not sufficient — confirm with a"
  echo "[secondary_ingest]       live blueprint-library query against a server started from ${DEST_ROOT}."
else
  fail "Content/${PKG} not present on the secondary build after ingest (see ${LOG})"
fi
