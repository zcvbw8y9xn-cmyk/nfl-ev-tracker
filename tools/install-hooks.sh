#!/usr/bin/env bash
# Activate the tracked git hooks in .githooks (currently: pre-commit integrity check).
# Run once after `git init` (or after cloning).
set -euo pipefail
cd "$(dirname "$0")/.."
chmod +x .githooks/pre-commit
git config core.hooksPath .githooks
echo "git hooks activated (core.hooksPath=.githooks)."
echo "pre-commit will run tools/validate_dataset.py and block regressions."
