PY ?= python3

.PHONY: run export validate test check check-ci hooks

# Dry-run the pipeline (no Google Sheet write), then validate the dataset.
run:
	$(PY) main.py --dry-run
	$(PY) tools/validate_dataset.py

# Full run (writes Google Sheet), then validate.
export:
	$(PY) main.py
	$(PY) tools/validate_dataset.py

# Dataset integrity check only (phantom EV, orphan markets, EV math, NaN, dupes).
validate:
	$(PY) tools/validate_dataset.py

# Unit tests.
test:
	$(PY) -m unittest discover -s tests

# Everything a CI/run gate should do: compile + tests + dataset integrity.
check:
	$(PY) -m py_compile $$(find src tools -name '*.py') main.py
	$(PY) -m unittest discover -s tests
	$(PY) tools/validate_dataset.py

# CI gate: same as check, but tolerance when no cached dataset exists (fresh checkout).
check-ci:
	$(PY) -m py_compile $$(find src tools -name '*.py') main.py
	$(PY) -m unittest discover -s tests
	$(PY) tools/validate_dataset.py --allow-missing

# Activate tracked git hooks (pre-commit integrity check).
hooks:
	bash tools/install-hooks.sh
