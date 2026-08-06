#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# regenerate_patches.sh — MAINTAINER TOOL, not part of the user-facing flow.
#
# Regenerates patches/*.patch from a working carla_garage checkout that
# contains the OOD-PerceptionBench changes.
#
# It emits ONE patch per file listed in tools/dev/patch_manifest.tsv, diffed
# against the pinned upstream SHA in patches/UPSTREAM.txt.
#
# Files in the manifest may be tracked-and-modified, tracked-and-new, OR
# UNTRACKED in the source worktree. `git diff` alone cannot see the untracked
# ones, which is why this script stages every selected path into a scratch
# index seeded from the upstream tree before diffing. Seven of the twenty
# scenario dependencies of the canonical route set are untracked; a plain
# `git diff > patches.patch` silently drops them.
#
# Usage:
#   tools/dev/regenerate_patches.sh --source-root /path/to/carla_garage
#
# There is deliberately NO default for --source-root.
# ---------------------------------------------------------------------------
set -euo pipefail

SOURCE_ROOT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-root) SOURCE_ROOT="${2:?--source-root needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$SOURCE_ROOT" ]] || { echo "ERROR: --source-root is required" >&2; exit 2; }
[[ -d "$SOURCE_ROOT/.git" ]] || { echo "ERROR: $SOURCE_ROOT is not a git checkout" >&2; exit 2; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$REPO_ROOT/tools/dev/patch_manifest.tsv"
PATCH_DIR="$REPO_ROOT/patches"
UPSTREAM_FILE="$PATCH_DIR/UPSTREAM.txt"

BASE_SHA="$(awk -F'= *' '/^CARLA_GARAGE_SHA/ {print $2}' "$UPSTREAM_FILE" | tr -d '[:space:]')"
[[ -n "$BASE_SHA" ]] || { echo "ERROR: no CARLA_GARAGE_SHA in $UPSTREAM_FILE" >&2; exit 2; }

cd "$SOURCE_ROOT"
git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null || {
  echo "ERROR: $SOURCE_ROOT does not contain base commit $BASE_SHA" >&2; exit 2; }

rm -f "$PATCH_DIR"/*.patch
SCRATCH_INDEX="$(mktemp -t oodpb-index.XXXXXX)"
trap 'rm -f "$SCRATCH_INDEX"' EXIT

emitted=0
while IFS=$'\t' read -r order path tier gitstate reason; do
  [[ -z "${order:-}" || "$order" == \#* || "$order" == "order" ]] && continue
  [[ -f "$path" ]] || { echo "ERROR: missing source file $path" >&2; exit 1; }

  slug="$(printf '%s' "$path" | tr '/' '_' | sed 's/\.py$//')"
  out="$PATCH_DIR/${order}-${slug}.patch"

  # Scratch index seeded from the upstream tree, then the worktree copy of THIS
  # path force-added. `git add -f` bypasses .gitignore and works for untracked
  # files, which is the whole point of the scratch index.
  rm -f "$SCRATCH_INDEX"
  GIT_INDEX_FILE="$SCRATCH_INDEX" git read-tree "$BASE_SHA"
  GIT_INDEX_FILE="$SCRATCH_INDEX" git add -f -- "$path"
  GIT_INDEX_FILE="$SCRATCH_INDEX" git diff --cached --binary --no-color \
      --src-prefix=a/ --dst-prefix=b/ "$BASE_SHA" -- "$path" > "$out"

  [[ -s "$out" ]] || { echo "ERROR: empty patch for $path (identical to upstream?)" >&2; exit 1; }
  emitted=$((emitted + 1))
  printf '  %-4s %-9s %s\n' "$order" "$tier" "$path"
done < "$MANIFEST"

echo "regenerated $emitted patches in $PATCH_DIR against $BASE_SHA"
