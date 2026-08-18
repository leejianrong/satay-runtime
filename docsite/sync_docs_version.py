#!/usr/bin/env python3
"""Keep every version the docs site quotes on one released version, and flip it in one command.

The site quotes a version in seven different shapes across five pages — a pinned ``blob/``
link, a pinned ``raw.githubusercontent`` URL a reader is told to ``curl``, a
``pip install 'satay[studio]==…'`` pin, the ``Successfully installed satay-…`` line of an
install transcript, a ``pip list`` column, and bare backticked prose ("`0.1.0a3` is the
current release"). Flipping those by hand after a tag is how ``0.1.0`` shipped with seven
cookbook pages still pointing at ``v0.1.0a3``: the cookbook page for the agentic DAG
documented a feature while its own ``Source:`` link fetched a file from before that
feature existed.

So this script owns the flip::

    python docsite/sync_docs_version.py            # rewrite the docs to the newest tag
    python docsite/sync_docs_version.py --to 0.2.0 # …or to a version you name
    python docsite/sync_docs_version.py --check    # CI/`make docs`: assert, do not rewrite

**What ``--check`` enforces is an invariant, not a schedule.** It asserts two things: every
version reference on the site agrees with every other, and the version they agree on has a
``v<version>`` tag in this checkout. It deliberately does *not* demand the site sit on the
**newest** tag, because ``docs/RELEASING.md`` is explicit that the docsite is flipped in a
PR **after** the tag is pushed, never in the same PR as the version bump. A check that
demanded newest-tag would turn ``main`` red for the length of that window and would be
telling the truth about nothing — the docs pointing at the previous release for ten
minutes is the documented process working. A newer tag is reported as a note instead, so
the reminder is loud without being a failure.

The tag requirement is the half that has teeth: it is what makes it impossible to publish
a page pinned to a tag that does not exist yet. ``check_repo_links.py`` then takes it one
step further and proves each pinned *path* resolves at that ref (KAN-489). The two run
together in ``make docs``.

Only literals that are already the site's current version are ever rewritten, and only in
the anchored contexts below. A bare ``0.0.52`` (Zensical) or ``3.13`` (Python) in the same
paragraph is not a candidate and cannot be touched by accident. Links pinned to ``main``
are left alone: pointing at ``main`` is a content decision (the Decisions page links ADRs
that keep moving), not a stale pin.

Stdlib only, so it runs wherever the docs build runs; ``git`` is invoked only to list tags.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_SOURCE = Path(__file__).resolve().parent / "docs"

#: A PEP 440 release number in the shapes this project ships: ``0.1.0``, ``0.1.0a3``.
VERSION = r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?"

#: Every place the docs quote a version, anchored so the match cannot be an unrelated
#: number. Each pattern captures the version in the group named ``version``; everything
#: else is context that is preserved verbatim on rewrite.
#:
#: Bare backticked versions ("`0.1.0a3` is the current release") are handled separately in
#: :func:`bare_occurrences`, because on their own they are indistinguishable from any
#: other version-shaped token on the page. They are rewritten only when they exactly match
#: the version the anchored patterns below have already established as the site's.
ANCHORED = (
    # https://github.com/leejianrong/satay-runtime/blob/v0.1.0/examples/x.py — and the
    # raw.githubusercontent.com form the cookbook's `curl` lines use.
    re.compile(rf"satay-runtime/(?:blob|tree|raw)/v(?P<version>{VERSION})/"),
    re.compile(rf"satay-runtime/v(?P<version>{VERSION})/"),
    # pip install 'satay[studio]==0.1.0'
    re.compile(rf"satay(?:\[[a-z,]+\])?==(?P<version>{VERSION})"),
    # Successfully installed satay-0.1.0
    re.compile(rf"satay-(?P<version>{VERSION})\b"),
    # `satay 0.1.0` in prose, and the `satay   0.1.0` column of a `pip list` transcript.
    re.compile(rf"satay(?P<gap>[ \t]+)(?P<version>{VERSION})\b"),
)

#: A version on its own inside backticks, optionally tag-shaped: ``0.1.0a3``, ``v0.1.0a3``.
BARE = re.compile(rf"`(?P<v>v?)(?P<version>{VERSION})`")

#: Environment variables that point git at a repository of its own choosing. Git hooks
#: export them and ``make docs`` runs from the pre-push hook, so leaving them in place
#: would list the tags of whatever repository invoked us. Same reasoning, same list, as
#: ``check_repo_links.py``.
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


def sort_key(version: str) -> tuple[int, int, int, int, int]:
    """Order versions the way PEP 440 does, so ``0.1.0`` sorts above ``0.1.0a3``.

    Hand-rolled rather than pulled from ``packaging``: this script has to run under a bare
    ``python3`` with nothing installed, the same as ``check_repo_links.py``.
    """
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?", version)
    if match is None:  # pragma: no cover - callers filter on VERSION first
        raise ValueError(f"not a release number: {version}")
    major, minor, patch, phase, phase_number = match.groups()
    # A final release outranks every pre-release of the same number, so it sorts last.
    rank = {"a": 0, "b": 1, "rc": 2, None: 3}[phase]
    return (int(major), int(minor), int(patch), rank, int(phase_number or 0))


def release_tags(repo_root: Path) -> list[str]:
    """Every ``v<release>`` tag in this checkout, oldest first. Empty if git cannot run."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "tag", "--list", "v*"],
            capture_output=True,
            check=False,
            text=True,
            env=git_env(),
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    versions = [
        line[1:]
        for line in completed.stdout.split()
        if re.fullmatch(rf"v{VERSION}", line) is not None
    ]
    return [f"v{version}" for version in sorted(versions, key=sort_key)]


def pages() -> list[Path]:
    """Every Markdown source of the site, in a stable order."""
    return sorted(DOCS_SOURCE.rglob("*.md"))


def anchored_versions(text: str) -> set[str]:
    """Every version quoted in an unambiguous context on one page."""
    return {match.group("version") for pattern in ANCHORED for match in pattern.finditer(text)}


def anchored_occurrences(text: str, current: str) -> int:
    """How many anchored references on the page name the site's current version."""
    return sum(
        1
        for pattern in ANCHORED
        for match in pattern.finditer(text)
        if match.group("version") == current
    )


def bare_occurrences(text: str, current: str) -> int:
    """How many backticked bare versions on the page are the site's current version."""
    return sum(1 for match in BARE.finditer(text) if match.group("version") == current)


def rewrite(text: str, current: str, target: str) -> tuple[str, int]:
    """``text`` with every reference to ``current`` moved to ``target``, and how many moved."""
    moved = 0

    def swap_anchored(match: re.Match[str]) -> str:
        nonlocal moved
        if match.group("version") != current:
            return match.group(0)
        moved += 1
        return match.group(0).replace(current, target)

    def swap_bare(match: re.Match[str]) -> str:
        nonlocal moved
        if match.group("version") != current:
            return match.group(0)
        moved += 1
        return f"`{match.group('v')}{target}`"

    for pattern in ANCHORED:
        text = pattern.sub(swap_anchored, text)
    return BARE.sub(swap_bare, text), moved


def survey() -> tuple[dict[Path, set[str]], set[str]]:
    """Which versions each page quotes in an anchored context, and the union across pages."""
    per_page = {page: anchored_versions(page.read_text(encoding="utf-8")) for page in pages()}
    found = {version for versions in per_page.values() for version in versions}
    return {page: versions for page, versions in per_page.items() if versions}, found


def report_disagreement(per_page: dict[Path, set[str]], found: set[str]) -> None:
    """Print, per page, which version each one claims — the useful half of the failure."""
    print(
        f"the docs quote {len(found)} different versions ({', '.join(sorted(found))}); "
        "they must agree on one",
        file=sys.stderr,
    )
    for page, versions in sorted(per_page.items()):
        print(f"  {page.relative_to(REPO_ROOT)}: {', '.join(sorted(versions))}", file=sys.stderr)
    print(
        "\nrun `make docs-version` (or `python docsite/sync_docs_version.py`) to put them "
        "all on the newest tag",
        file=sys.stderr,
    )


def check(tags: list[str]) -> int:
    """Assert the site quotes one version and that version is tagged. 0 when it does."""
    per_page, found = survey()

    if not found:
        print("no version references found in the docs — the patterns are broken", file=sys.stderr)
        return 1
    if len(found) > 1:
        report_disagreement(per_page, found)
        return 1

    current = found.pop()
    sources = [page.read_text(encoding="utf-8") for page in pages()]
    pins = sum(anchored_occurrences(text, current) for text in sources)
    bare = sum(bare_occurrences(text, current) for text in sources)

    if not tags:
        print(
            f"the docs quote {current}, but no release tags are visible here — run "
            "`git fetch --tags` (unverifiable is not the same as fine: see KAN-489)",
            file=sys.stderr,
        )
        return 1
    if f"v{current}" not in tags:
        print(
            f"the docs quote {current}, which has no `v{current}` tag in this checkout.\n"
            "The docsite is flipped only AFTER the tag is pushed (docs/RELEASING.md §3) — "
            "either the tag is missing locally (`git fetch --tags`) or the docs were "
            "flipped early.",
            file=sys.stderr,
        )
        return 1

    print(f"docs are consistently on {current} ({pins} pinned reference(s), {bare} in prose).")
    if tags[-1] != f"v{current}":
        print(
            f"note: {tags[-1]} is newer. If it is released, flip the docs with "
            "`make docs-version` in a PR of its own (docs/RELEASING.md §3)."
        )
    return 0


def sync(target: str | None, tags: list[str]) -> int:
    """Move every version reference onto ``target`` (default: the newest tag). 0 on success."""
    if target is None:
        if not tags:
            print(
                "no release tags visible, so there is nothing to sync to — pass --to, or "
                "run `git fetch --tags`",
                file=sys.stderr,
            )
            return 1
        target = tags[-1][1:]

    if re.fullmatch(VERSION, target) is None:
        print(f"--to {target} is not a release number (expected e.g. 0.1.0)", file=sys.stderr)
        return 1
    if tags and f"v{target}" not in tags:
        print(
            f"there is no `v{target}` tag in this checkout. The docsite is flipped only "
            "AFTER the tag is pushed (docs/RELEASING.md §3); run `git fetch --tags` if you "
            "expect it to be there.",
            file=sys.stderr,
        )
        return 1

    per_page, found = survey()
    if not found:
        print("no version references found in the docs — the patterns are broken", file=sys.stderr)
        return 1
    if len(found) > 1:
        report_disagreement(per_page, found)
        return 1

    current = found.pop()
    if current == target:
        print(f"docs are already on {target}; nothing to do.")
        return 0

    touched = 0
    moved = 0
    for page in pages():
        text = page.read_text(encoding="utf-8")
        updated, count = rewrite(text, current, target)
        if updated != text:
            page.write_text(updated, encoding="utf-8")
            touched += 1
            moved += count

    print(f"{current} -> {target}: {moved} reference(s) across {touched} page(s).")
    print("Read the diff: a version in prose may need a sentence changed, not just a number.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="assert the docs quote one version and that version is tagged; rewrite nothing",
    )
    parser.add_argument(
        "--to",
        metavar="VERSION",
        help="the release to move the docs to (default: the newest tag in this checkout)",
    )
    args = parser.parse_args(argv)

    if args.check and args.to:
        parser.error("--check does not rewrite anything, so --to means nothing with it")

    tags = release_tags(REPO_ROOT)
    return check(tags) if args.check else sync(args.to, tags)


if __name__ == "__main__":
    raise SystemExit(main())
