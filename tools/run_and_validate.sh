#!/usr/bin/env bash
# Run the NFL +EV tracker pipeline and then validate the resulting dataset.
# Exits non-zero if the dataset integrity check fails, so a parser change that
# reintroduces phantom EV (the Covers "DIFFERENCE %" bug) or an orphan market/team
# is caught before anything is trusted or exported to Google Sheets.
#
# Usage:
#   tools/run_and_validate.sh                 # dry-run + validate
#   tools/run_and_validate.sh --export        # full run (writes Google Sheet) + validate
#   tools/run_and_validate.sh --spreadsheet-id ID
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root

PY="${PYTHON:-python3}"

# Decide dry-run vs export.
DRY="--dry-run"
ARGS=()
for a in "$@"; do
  if [[ "$a" == "--export" ]]; then
    DRY=""           # real run
  else
    ARGS+=("$a")
  fi
done

echo ">>> Running pipeline (${DRY:-EXPORT})..."
# shellcheck disable=SC2086
if [[ ${#ARGS[@]} -gt 0 ]]; then
  "$PY" main.py ${DRY:-} "${ARGS[@]}"
else
  "$PY" main.py ${DRY:-}
fi

echo
echo ">>> Validating dataset integrity..."
if ! "$PY" tools/validate_dataset.py; then
  echo "=========================================================="
  echo "REGRESSION: dataset integrity check FAILED. Do NOT trust the"
  echo "output or export to Google Sheets until this is fixed."
  echo "=========================================================="
  exit 1
fi

echo ">>> OK: pipeline + integrity check passed."
