#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# OOD-PerceptionBench — overlay setup
#
# This repository contains only our own code. The simulation harness it patches
# lives upstream. This script:
#
#   1. clones autonomousvision/carla_garage at the SHA pinned in
#      patches/UPSTREAM.txt (Bench2Drive is vendored inside it — there is no
#      submodule and nothing else to clone),
#   2. applies every patch in patches/ in filename order,
#   3. verifies the result.
#
# It is IDEMPOTENT: re-running against an already-patched tree detects that and
# exits 0 without reapplying. It fails LOUDLY on any rejected hunk — that is the
# whole point of the overlay architecture.
#
# It does NOT install CARLA, create a conda environment, or download model
# weights. See README.md.
#
# Usage:
#   ./setup.sh --upstream-dir ./third_party/carla_garage
#   ./setup.sh --upstream-dir /somewhere/carla_garage --verify-only
#   ./setup.sh --upstream-dir ... --existing-checkout   # already cloned/pinned
#
# Environment variables (all optional, all override the defaults):
#   OODPB_UPSTREAM_DIR   same as --upstream-dir
#   GIT                  git binary to use (default: git)
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_DIR="$REPO_ROOT/patches"
UPSTREAM_FILE="$PATCH_DIR/UPSTREAM.txt"
GIT="${GIT:-git}"

UPSTREAM_DIR="${OODPB_UPSTREAM_DIR:-}"
VERIFY_ONLY=0
EXISTING_CHECKOUT=0

die()  { printf '\n[setup.sh] ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '[setup.sh] %s\n' "$*"; }

usage() {
  sed -n '2,34p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --upstream-dir)      UPSTREAM_DIR="${2:?--upstream-dir needs a value}"; shift 2 ;;
    --verify-only)       VERIFY_ONLY=1; shift ;;
    --existing-checkout) EXISTING_CHECKOUT=1; shift ;;
    -h|--help)           usage ;;
    *) die "unknown argument: $1  (try --help)" ;;
  esac
done

[[ -n "$UPSTREAM_DIR" ]] || die "--upstream-dir is required (there is deliberately no default)"
[[ -f "$UPSTREAM_FILE" ]] || die "missing $UPSTREAM_FILE"

read_pin() { awk -F'= *' -v k="$1" '$0 ~ "^"k {print $2}' "$UPSTREAM_FILE" | tr -d '[:space:]' | head -1; }
UPSTREAM_REPO="$(read_pin CARLA_GARAGE_REPO)"
UPSTREAM_SHA="$(read_pin CARLA_GARAGE_SHA)"
[[ -n "$UPSTREAM_REPO" && -n "$UPSTREAM_SHA" ]] || die "could not parse repo/SHA from $UPSTREAM_FILE"

info "upstream repo : $UPSTREAM_REPO"
info "upstream SHA  : $UPSTREAM_SHA"
info "target dir    : $UPSTREAM_DIR"

# --- 1. obtain the pinned upstream tree -----------------------------------
if [[ $EXISTING_CHECKOUT -eq 1 || $VERIFY_ONLY -eq 1 ]]; then
  [[ -d "$UPSTREAM_DIR/.git" ]] || die "$UPSTREAM_DIR is not a git checkout"
elif [[ -d "$UPSTREAM_DIR/.git" ]]; then
  info "reusing existing checkout at $UPSTREAM_DIR"
  ( cd "$UPSTREAM_DIR" && "$GIT" cat-file -e "${UPSTREAM_SHA}^{commit}" 2>/dev/null ) \
    || die "$UPSTREAM_DIR exists but does not contain $UPSTREAM_SHA. Remove it and re-run."
else
  info "cloning upstream (blobless clone — full history, blobs fetched on demand)"
  mkdir -p "$(dirname "$UPSTREAM_DIR")"
  "$GIT" clone --filter=blob:none --no-checkout "$UPSTREAM_REPO" "$UPSTREAM_DIR"
  ( cd "$UPSTREAM_DIR" && "$GIT" checkout --detach "$UPSTREAM_SHA" )
fi

cd "$UPSTREAM_DIR"
HEAD_SHA="$("$GIT" rev-parse HEAD)"
if [[ "$HEAD_SHA" != "$UPSTREAM_SHA" ]]; then
  if [[ $EXISTING_CHECKOUT -eq 1 || $VERIFY_ONLY -eq 1 ]]; then
    info "WARNING: HEAD is $HEAD_SHA, pinned SHA is $UPSTREAM_SHA"
  else
    die "HEAD is $HEAD_SHA but the pinned SHA is $UPSTREAM_SHA"
  fi
fi

shopt -s nullglob
PATCHES=( "$PATCH_DIR"/*.patch )
shopt -u nullglob
(( ${#PATCHES[@]} > 0 )) || die "no patches found in $PATCH_DIR"
info "found ${#PATCHES[@]} patches"

# --- 2. idempotency probe --------------------------------------------------
# A patch that reverse-applies cleanly is already in the tree.
already=0
for p in "${PATCHES[@]}"; do
  if "$GIT" apply --reverse --check --whitespace=nowarn "$p" >/dev/null 2>&1; then
    already=$((already + 1))
  fi
done

if [[ $already -eq ${#PATCHES[@]} ]]; then
  info "all ${#PATCHES[@]} patches are ALREADY applied — nothing to do"
  APPLY=0
elif [[ $already -gt 0 ]]; then
  die "tree is PARTIALLY patched ($already/${#PATCHES[@]} already applied).
       Refusing to guess. Reset the checkout and re-run:
         git -C \"$UPSTREAM_DIR\" checkout --force --detach $UPSTREAM_SHA && git -C \"$UPSTREAM_DIR\" clean -fd"
else
  APPLY=1
fi

if [[ $VERIFY_ONLY -eq 1 ]]; then
  [[ $already -eq ${#PATCHES[@]} ]] || die "verify-only: tree is not fully patched ($already/${#PATCHES[@]})"
  info "verify-only: OK"
  exit 0
fi

# --- 3. dry run, then apply -----------------------------------------------
if [[ ${APPLY:-0} -eq 1 ]]; then
  info "dry run (git apply --check) ..."
  failed=()
  for p in "${PATCHES[@]}"; do
    "$GIT" apply --check --whitespace=nowarn "$p" 2>/dev/null || failed+=( "$(basename "$p")" )
  done
  if (( ${#failed[@]} > 0 )); then
    printf '\n[setup.sh] ERROR: %d patch(es) do NOT apply to %s:\n' "${#failed[@]}" "$UPSTREAM_SHA" >&2
    printf '  - %s\n' "${failed[@]}" >&2
    cat >&2 <<'EOM'

This is patch rot: upstream moved, or the checkout is not at the pinned SHA.
Do NOT force it. Report the failing patch names in an issue — the pinned SHA
and the patch set are versioned together and must be updated together.
EOM
    exit 1
  fi

  info "applying ..."
  for p in "${PATCHES[@]}"; do
    "$GIT" apply --whitespace=nowarn "$p" || die "failed applying $(basename "$p") after a clean dry run"
    printf '  applied  %s\n' "$(basename "$p")"
  done
fi

# --- 4. post-conditions ----------------------------------------------------
info "verifying post-conditions ..."

SR="Bench2Drive/scenario_runner/srunner"
LB="Bench2Drive/leaderboard/leaderboard"

check_file() { [[ -f "$1" ]] || die "post-condition failed: missing $1"; }
check_grep() { grep -q "$2" "$1" || die "post-condition failed: '$2' not found in $1"; }

# 4a. the twelve scenario classes the 475 canonical routes instantiate
declare -A NEED=(
  [VehicleOpensDoorTwoWaysModified]="$SR/scenarios/vehicle_opens_door_modified.py"
  [PedestrianCrossingModified]="$SR/scenarios/pedestrian_crossing_modified.py"
  [ParkingCutInModified]="$SR/scenarios/parking_cut_in_modified.py"
  [ParkingCrossingPedestrianModified]="$SR/scenarios/parking_crossing_pedestrian_modified.py"
  [ParkedObstacleModified]="$SR/scenarios/parked_obstacle_modified.py"
  [ParkedObstacleTwoWaysModified]="$SR/scenarios/parked_obstacle_modified.py"
  [HardBreakRouteModified]="$SR/scenarios/hard_break_modified.py"
  [VehicleTurningRoutePedestrianModified]="$SR/scenarios/vehicle_turning_route_pedestrian_modified.py"
  [InvadingTurnModified]="$SR/scenarios/invading_turn_modified.py"
  [DynamicObjectCrossingModified]="$SR/scenarios/dynamic_object_crossing_modified.py"
  [ConstructionObstacleModified]="$SR/scenarios/construction_obstacle_two_ways_modified.py"
  [ConstructionObstacleTwoWaysModified]="$SR/scenarios/construction_obstacle_two_ways_modified.py"
)
for cls in "${!NEED[@]}"; do
  f="${NEED[$cls]}"
  check_file "$f"
  grep -q "^class ${cls}\b" "$f" || die "post-condition failed: class $cls not in $f"
done
info "  OK  12/12 scenario classes required by the canonical route set"

# 4b. the metrics plumbing
check_grep "$SR/scenariomanager/traffic_events.py" "TTR_DAR_MEASUREMENT"
check_grep "$SR/scenariomanager/scenarioatomics/atomic_criteria.py" "TTRDARCriterion"
check_grep "$LB/utils/statistics_manager.py" "TTR_DAR_MEASUREMENT"
check_grep "$LB/utils/checkpoint_tools.py" "_sanitize_floats"
check_grep "$SR/tools/scenario_helper.py" "apply_leading_edge_offset"
check_file "$LB/autoagents/autonomous_agent_local.py"
info "  OK  metrics + spawn-parity plumbing present"

# 4c. every patched python file still parses
PY=$(command -v python3 || command -v python || true)
if [[ -n "$PY" ]]; then
  mapfile -t PATCHED < <(
    for p in "${PATCHES[@]}"; do
      awk '/^\+\+\+ b\//{sub("^\\+\\+\\+ b/","");print;exit}' "$p"
    done | sort -u
  )
  bad=0
  for f in "${PATCHED[@]}"; do
    [[ "$f" == *.py ]] || continue
    "$PY" -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$f" \
      || { echo "  SYNTAX ERROR in $f" >&2; bad=1; }
  done
  [[ $bad -eq 0 ]] || die "patched tree contains a python syntax error"
  info "  OK  all patched python files parse"
else
  info "  SKIP python syntax check (no python interpreter found)"
fi

cat <<EOM

[setup.sh] done.

  upstream : $UPSTREAM_DIR  @ $HEAD_SHA
  patches  : ${#PATCHES[@]} applied

Next steps (none of these are done by this script):
  1. Install CARLA 0.9.15 and export CARLA_ROOT.
  2. Install the content pack from assets/ (see assets/INSTALL.md) — without it,
     OOD props silently fail to spawn and routes score plausibly but wrongly.
  3. Point the runner at your agent — see runner/README.md and config/.
EOM
