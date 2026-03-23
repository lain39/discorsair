UV ?= uv
PYTHON ?= python

.PHONY: sync lint static test build release-check check

sync:
	$(UV) sync --group dev

lint:
	$(UV) run --group dev ruff check src tests scripts

static:
	$(UV) run $(PYTHON) -m compileall src tests scripts

test:
	$(UV) run $(PYTHON) -m unittest discover -s tests

build:
	$(UV) run --group dev $(PYTHON) -m build --no-isolation
	$(UV) run --group dev $(PYTHON) -m twine check dist/*

release-check:
	$(UV) run $(PYTHON) scripts/release_guard.py $(if $(TAG),--tag $(TAG),)

check: lint static test build
