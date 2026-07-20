UV ?= uv
VENV := .venv/bin

.PHONY: sync check python-check viewer-check format

## Install both locked environments.
sync:
	$(UV) sync --no-editable --reinstall-package atlas-analyzer
	npm --prefix viewer ci

## Run every repository quality gate.
check: python-check viewer-check

python-check:
	$(VENV)/python scripts/generate_models.py --check
	$(VENV)/black --check analyzer scripts
	$(VENV)/ruff check analyzer scripts
	$(VENV)/pytest

viewer-check:
	npm --prefix viewer run check:generated
	npm --prefix viewer run typecheck
	npm --prefix viewer test
	npm --prefix viewer run build

## Apply formatting and safe lint fixes.
format:
	$(VENV)/black analyzer scripts
	$(VENV)/ruff check --fix analyzer scripts
