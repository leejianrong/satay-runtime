#!/usr/bin/env python3
"""Verify that every repository file the docs site links to actually exists.

Zensical's ``--strict`` build validates links *between* pages of the site. It cannot
validate a link that leaves the site, and the Decisions page is made almost entirely of
those: it points at ``docs/adr/*.md`` and the other planning documents on GitHub rather
than duplicating them into ``docsite/docs/``.

Without this check, renaming or deleting an ADR would silently leave the published site
with a wall of 404s. So: pull every ``github.com/leejianrong/satay-runtime`` blob and tree
URL out of the Markdown sources, map it back to a path in this checkout, and fail if the
path is missing.

    python docsite/check_repo_links.py

Exits 0 when every reference resolves, 1 otherwise. Stdlib only, so it runs anywhere the
docs build runs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_SOURCE = Path(__file__).resolve().parent / "docs"

#: Matches a link into this repository at a branch ref, capturing the in-repo path.
#: e.g. https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0001-x.md
REPO_LINK = re.compile(
    r"https://github\.com/leejianrong/satay-runtime/(?:blob|tree)/[^/]+/([^)\s#]+)"
)


def main() -> int:
    missing: list[tuple[Path, int, str]] = []
    checked = 0

    for page in sorted(DOCS_SOURCE.rglob("*.md")):
        for lineno, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
            for match in REPO_LINK.finditer(line):
                target = match.group(1).rstrip("/")
                checked += 1
                if not (REPO_ROOT / target).exists():
                    missing.append((page.relative_to(REPO_ROOT), lineno, target))

    if missing:
        print(f"{len(missing)} broken repository link(s):", file=sys.stderr)
        for page, lineno, target in missing:
            print(f"  {page}:{lineno} -> {target} does not exist", file=sys.stderr)
        return 1

    print(f"{checked} repository link(s) resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
