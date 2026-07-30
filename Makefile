.DEFAULT_GOAL := help
.PHONY: help dev check lint type test ci install-hooks demo demo-clean

# Data directory for `make demo`. Kept separate from ./.satay so the demo never
# disturbs a real project journal you might be working against.
DEMO_DIR ?= .satay-demo

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

dev: ## Sync the dev environment (uv sync)
	uv sync

lint: ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

type: ## mypy --strict over src
	uv run mypy src

check: lint type ## Lint + type-check

test: ## Run unit tests
	uv run pytest tests/unit -q

ci: check test ## Everything CI runs (lint + type + unit tests + import-hygiene)
	uv run pytest tests/integration --collect-only -q

install-hooks: ## Install the pre-push git hook
	./scripts/install-hooks.sh

demo: ## Run the crash-recovery demo, then browse it in Satay Studio (Ctrl-C to stop)
	@rm -rf $(DEMO_DIR)
	@echo "==> crash-recovery demo (data dir: $(DEMO_DIR))"
	@SATAY_DATA_DIR=$(DEMO_DIR) uv run --extra studio python examples/crash_recovery_demo.py
	@echo
	@echo "==> starting Satay Studio — open the printed URL (the ?token= is required)"
	@echo "    the run above is on the run list; open it for the timeline and the tree"
	@SATAY_DATA_DIR=$(DEMO_DIR) uv run --extra studio satay dev

demo-clean: ## Delete the demo data directory
	rm -rf $(DEMO_DIR)
