.DEFAULT_GOAL := help
.PHONY: help dev dev-studio check lint type test test-all docs secrets ci install-hooks demo demo-clean

# Data directory for `make demo`. Kept separate from ./.satay so the demo never
# disturbs a real project journal you might be working against.
DEMO_DIR ?= .satay-demo

# Pinned to match .github/workflows/docs.yml — bump the two together, or the
# local docs gate stops predicting the one that gates the PR.
ZENSICAL_VERSION ?= 0.0.52

# Pinned to match .github/workflows/security.yml. Only used for the install hint
# in `make secrets`; this repo does not vendor the binary.
GITLEAKS_VERSION ?= 8.30.1

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

dev: ## Sync the dev environment (uv sync)
	uv sync

dev-studio: ## Sync the dev env WITH the studio extra — what every CI job installs
	uv sync --extra studio --frozen

lint: ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

# Depends on dev-studio deliberately. Without fastapi/uvicorn/typer installed,
# mypy reports 21 import-not-found and untyped-decorator errors in
# satay.control and satay.devstack — a red gate that says nothing about the code.
# CI's type job runs `uv sync --extra studio --frozen` for exactly this reason.
type: dev-studio ## mypy --strict over src (needs the studio extra)
	uv run mypy src

check: lint type ## Lint + type-check

test: ## Unit tests only — the fast inner-loop target
	uv run pytest tests/unit -q

# CI's job is *named* "Unit tests (py3.12/3.13)" only to keep the
# branch-protection required-check contract; it actually installs the studio
# extra and runs the WHOLE suite. `make test` running tests/unit alone left 235
# tests (427 vs 192) that never executed locally before a push (KAN-576).
test-all: dev-studio ## The FULL suite (unit + integration + e2e), as CI runs it
	uv run pytest tests/integration --collect-only -q
	uv run pytest -q

# The Docs workflow is a required check and has no path filters, because the
# site links out into docs/ — so renaming or deleting a file there breaks the
# published site without touching docsite/ at all.
docs: ## Build the docs site the way the Docs workflow does
	python3 docsite/check_repo_links.py
	cd docsite && uv tool run --from "zensical==$(ZENSICAL_VERSION)" zensical build --strict --clean

# The local mirror of the Security workflow's gitleaks job (KAN-578: it had no
# local entry point, and three authors have tripped it in CI). Not vendored: the
# release binary is platform-specific and CI verifies its checksum, which is not
# worth reimplementing here. CI remains the authoritative scan.
secrets: ## Secret scan over history + tree (needs gitleaks on PATH)
	@command -v gitleaks >/dev/null 2>&1 || { \
		echo "gitleaks is not on PATH. Install it, then re-run:"; \
		echo "  brew install gitleaks                  # macOS"; \
		echo "  https://github.com/gitleaks/gitleaks/releases  # v$(GITLEAKS_VERSION), matching CI"; \
		echo; \
		echo "CI's Security workflow scans full history + working tree regardless."; \
		exit 1; }
	gitleaks git --no-banner --redact --verbose .
	gitleaks dir --no-banner --redact --verbose .

ci: check test-all docs ## Everything CI gates on (lint + mypy + FULL suite + docs)

install-hooks: ## Install the pre-push git hook
	./scripts/install-hooks.sh

demo: ## Run the crash-recovery demo, then browse it in Satay Studio (Ctrl-C to stop)
	@rm -rf $(DEMO_DIR)
	@echo "==> crash-recovery demo (data dir: $(DEMO_DIR))"
	@SATAY_DATA_DIR=$(DEMO_DIR) uv run --extra studio python examples/crash_recovery_demo.py
	@echo
	@echo "==> starting Satay Studio — open the printed URL (the ?token= is required)"
	@echo "    the run above is on the run list; open it for the timeline and the tree"
	@SATAY_DATA_DIR=$(DEMO_DIR) uv run --extra studio satay dev --app satay.demo

demo-clean: ## Delete the demo data directory
	rm -rf $(DEMO_DIR)
