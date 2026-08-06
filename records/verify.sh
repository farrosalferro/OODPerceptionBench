#!/usr/bin/env bash
# Run every records verification check. Usage: ./verify.sh <paper-repo> <scratch-dir>
set -euo pipefail
PAPER="${1:?usage: verify.sh <paper-repo> <scratch-dir>}"
WORK="${2:?usage: verify.sh <paper-repo> <scratch-dir>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
REC="$HERE/ood_perceptionbench_records_v0.9.csv"

echo "=== 1/4  meta.json describes the artifacts that actually ship ==="
"$PY" "$HERE/check_meta.py" --records "$REC"

echo; echo "=== 2/4  row-level vs the frozen paper CSVs ==="
"$PY" "$HERE/validate_against_frozen.py" --records "$REC" \
      --frozen-eval "$PAPER/eval" --max-examples 3

echo; echo "=== 3/4  coverage vs the canonical route manifest ==="
"$PY" "$HERE/reconcile_with_manifest.py" --records "$REC" \
      --manifest "$HERE/../routes/MANIFEST.tsv"

echo; echo "=== 4/4  ACCEPTANCE: Table 1 regenerates exactly ==="
"$PY" "$HERE/reproduce_table1.py" --records "$REC" \
      --paper-repo "$PAPER" --work-dir "$WORK" --python "$PY"

echo; echo "ALL CHECKS PASSED"
