#!/usr/bin/env bash
# Install Satay's git hooks. One-liner:  ./scripts/install-hooks.sh
# (or: make install-hooks)
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
hook_src="$repo_root/scripts/hooks/pre-push"
hook_dst="$repo_root/.git/hooks/pre-push"

install -m 0755 "$hook_src" "$hook_dst"
echo "Installed pre-push hook -> $hook_dst"
echo "Bypass any time with: git push --no-verify"
