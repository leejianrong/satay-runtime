.DEFAULT_GOAL := help
.PHONY: help dev check lint type test ci install-hooks

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
