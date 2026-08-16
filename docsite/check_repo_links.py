#!/usr/bin/env python3
"""Verify that every repository file the docs site links to actually exists.

Zensical's ``--strict`` build validates links *between* pages of the site. It cannot
validate a link that leaves the site, and the Decisions page is made almost entirely of
those: it points at ``docs/adr/*.md`` and the other planning documents on GitHub rather
than duplicating them into ``docsite/docs/``.

Without this check, renaming or deleting an ADR would silently leave the published site
with a wall of 404s. So: pull every ``github.com/leejianrong/satay-runtime`` blob, raw and
tree URL out of the Markdown sources, and confirm the path resolves **at the ref the URL
names**.

That last part is the whole point (KAN-489). This script used to capture the path and
throw the ref away, checking every link against the working tree. A cookbook page linking
to ``blob/v0.1.0a1/examples/elt_pipeline_demo.py`` therefore passed while being a live
404, because that file did not exist at that tag — the checker gave false confidence on
exactly the class of link it exists to protect. A pinned ref is now resolved with
``git cat-file -e <ref>:<path>``.

Links at ``main`` keep resolving against the working tree, and that is deliberate: on a
pull request the tree is what ``main`` is about to become, so a page and the file it links
to can land in the same commit.

A pinned ref that this checkout does not have is a **failure**, not a skip. Skipping would
be the KAN-489 bug in a quieter voice: the link nobody can verify is precisely the link
most likely to 404. Nothing here touches the network — ``make docs`` runs in the pre-push
hook and has to work offline — so the fix is local (``git fetch --tags``) and the error
message says so. The Docs workflow checks out with ``fetch-depth: 0`` so every tag and
commit a doc can pin is present in CI.

    python docsite/check_repo_links.py

Exits 0 when every reference resolves, 1 otherwise. Stdlib only, so it runs anywhere the
docs build runs; ``git`` is invoked only when a link actually pins a ref.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_SOURCE = Path(__file__).resolve().parent / "docs"

#: Refs that name "the tree being built" rather than a pinned snapshot. A link at one of
#: these is checked against the working tree, so a PR may add a file and link to it at
#: once. Anything else — a tag, a SHA — is a pin and is resolved at that ref.
WORKING_TREE_REFS = frozenset({"main"})

#: Matches a link into this repository, capturing the ref and the in-repo path.
#: e.g. https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0001-x.md
REPO_LINK = re.compile(
    r"https://github\.com/leejianrong/satay-runtime/(?:blob|tree|raw)/([^/]+)/([^)\s#]+)"
)


def scan_text(text: str) -> Iterator[tuple[int, str, str]]:
    """Yield ``(line number, ref, path)`` for every repository link in ``text``."""
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in REPO_LINK.finditer(line):
            yield lineno, match.group(1), match.group(2).rstrip("/")


#: Environment variables that point git at a repository of their own choosing, overriding
#: the ``-C`` below. Git *hooks* export them, and ``make docs`` runs from the pre-push
#: hook, so leaving them in place would have this resolving refs against whatever
#: repository invoked us rather than the checkout the docs are being built from.
_GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def git_env() -> dict[str, str]:
    """The ambient environment with git's repository-location overrides stripped out."""
    return {key: value for key, value in os.environ.items() if key not in _GIT_LOCATION_VARS}


def _git_status(repo_root: Path, *args: str) -> int | None:
    """Exit status of a git command, or ``None`` if git could not be run at all."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            check=False,
            env=git_env(),
        )
    except OSError:
        return None
    return completed.returncode


@cache
def ref_exists(repo_root: Path, ref: str) -> bool | None:
    """Whether ``ref`` resolves in this checkout. ``None`` when git is unavailable."""
    status = _git_status(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return None if status is None else status == 0


def check_link(repo_root: Path, ref: str, path: str) -> str | None:
    """``None`` when the link resolves, otherwise a sentence saying why it does not."""
    if ref in WORKING_TREE_REFS:
        if (repo_root / path).exists():
            return None
        return f"{path} does not exist in this checkout"

    known = ref_exists(repo_root, ref)
    if known is None:
        return f"{path} is pinned at {ref} and git is not runnable here, so it cannot be checked"
    if not known:
        return (
            f"{path} is pinned at {ref}, which this checkout does not have — run "
            "`git fetch --tags` (unverifiable is not the same as fine: see KAN-489)"
        )
    if _git_status(repo_root, "cat-file", "-e", f"{ref}:{path}") == 0:
        return None
    return f"{path} does not exist at {ref} — that link is a live 404"


def main() -> int:
    problems: list[str] = []
    checked = 0
    pinned = 0

    for page in sorted(DOCS_SOURCE.rglob("*.md")):
        for lineno, ref, target in scan_text(page.read_text(encoding="utf-8")):
            checked += 1
            if ref not in WORKING_TREE_REFS:
                pinned += 1
            reason = check_link(REPO_ROOT, ref, target)
            if reason is not None:
                problems.append(f"  {page.relative_to(REPO_ROOT)}:{lineno} -> {reason}")

    if problems:
        print(f"{len(problems)} broken repository link(s):", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1

    print(f"{checked} repository link(s) resolve ({pinned} at a pinned ref).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
