PYTHON ?= python3

.PHONY: help test lint ci

help:
	@echo "Targets:"
	@echo "  make test   Run the unittest suite (stdlib only, no network)"
	@echo "  make lint   Byte-compile every module"
	@echo "  make ci     lint + test"

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

lint:
	$(PYTHON) -m compileall -q upgrade_sync upgrade_core templates tests
	$(PYTHON) -m py_compile sync.py check-versions.py manage-backups.py

ci: lint test
