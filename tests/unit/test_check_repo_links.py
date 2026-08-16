"""The docs link checker must honour the ref a link pins, not just the path (KAN-489).

``docsite/check_repo_links.py`` used to capture the in-repo path out of a GitHub URL and
throw the ref away, checking every link against the working tree. So a cookbook page
linking to ``blob/v0.1.0a1/examples/elt_pipeline_demo.py`` passed the checker while being
a live 404 — that file did not exist at that tag (KAN-488). The checker gave false
confidence on exactly the class of link it exists to protect, and the 0.1.0 release notes
will be made of those links.

Most of what follows runs against a **throwaway git repository** built in ``tmp_path``
with the same shape as the real bug: a first tag with one example, a second tag that adds
another. That keeps the proof independent of which tags happen to be present in the
checkout running the suite — CI's test job clones shallow with no tags, and a test that
silently skipped there would leave the regression unguarded. One case at the end does pin
the real URL from the card, and it is the only one that skips.

The script is not importable as a module (``docsite`` is not a package and must not
become one — it holds a static site, not library code), so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = REPO_ROOT / "docsite" / "check_repo_links.py"

_spec = importlib.util.spec_from_file_location("check_repo_links", CHECKER_PATH)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)

#: The example that landed after ``v0.1.0a1`` — the card's concrete case.
LATE_EXAMPLE = "examples/elt_pipeline_demo.py"
#: The example that was already there at ``v0.1.0a1``.
EARLY_EXAMPLE = "examples/crash_recovery_demo.py"


def _git(repo: Path, *args: str) -> str:
    """Run git against ``repo``, with the hook environment kept out of the way.

    ``git_env()`` matters here for the same reason it matters in the script: the pre-push
    hook exports ``GIT_DIR`` and friends, so without stripping them these commands build
    their throwaway repo inside *this* one.
    """
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=checker.git_env(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo tagged like this one was: ``v0.1.0a1``, then a tag that adds a file."""
    root = tmp_path / "repo"
    (root / "examples").mkdir(parents=True)
    _git(root.parent, "init", "-b", "main", str(root))
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")

    (root / EARLY_EXAMPLE).write_text("# early\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "first")
    _git(root, "tag", "v0.1.0a1")

    (root / LATE_EXAMPLE).write_text("# late\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "second")
    _git(root, "tag", "v0.1.0a3")
    return root


# --------------------------------------------------------------------------------------
# Parsing: the ref has to survive the regex before anything can honour it.
# --------------------------------------------------------------------------------------


def test_scan_captures_the_ref_alongside_the_path() -> None:
    text = (
        "Source: [`x`](https://github.com/leejianrong/satay-runtime/blob/v0.1.0a1/"
        "examples/elt_pipeline_demo.py)\n"
        "and the [ADRs](https://github.com/leejianrong/satay-runtime/tree/main/docs/adr/)\n"
        "and a raw one https://github.com/leejianrong/satay-runtime/raw/abc1234/README.md\n"
    )
    assert list(checker.scan_text(text)) == [
        (1, "v0.1.0a1", LATE_EXAMPLE),
        (2, "main", "docs/adr"),
        (3, "abc1234", "README.md"),
    ]


# --------------------------------------------------------------------------------------
# The bug itself.
# --------------------------------------------------------------------------------------


def test_a_pinned_link_to_a_file_absent_at_that_tag_is_flagged(repo: Path) -> None:
    """The KAN-489 headline: present in the tree, absent at the tag, so the link 404s."""
    assert (repo / LATE_EXAMPLE).exists(), "fixture is wrong: the file should be in the tree"

    reason = checker.check_link(repo, "v0.1.0a1", LATE_EXAMPLE)

    assert reason is not None, "the working tree must not rescue a link pinned at a tag"
    assert "v0.1.0a1" in reason


def test_a_pinned_link_to_a_file_present_at_that_tag_passes(repo: Path) -> None:
    assert checker.check_link(repo, "v0.1.0a3", LATE_EXAMPLE) is None
    assert checker.check_link(repo, "v0.1.0a1", EARLY_EXAMPLE) is None


def test_a_pinned_link_resolves_at_a_commit_sha_too(repo: Path) -> None:
    """Release notes pin tags; anything else that pins is a SHA, and it must work."""
    first = _git(repo, "rev-parse", "v0.1.0a1")

    assert checker.check_link(repo, first, EARLY_EXAMPLE) is None
    assert checker.check_link(repo, first, LATE_EXAMPLE) is not None


def test_a_ref_this_checkout_does_not_have_fails_rather_than_skipping(repo: Path) -> None:
    """The card's open question, pinned as behaviour: unverifiable is not the same as fine.

    Skipping an unknown ref would rebuild the original bug quietly, so this fails — and
    because the script never reaches the network, the message has to say how to fix it
    locally. CI is kept honest instead by cloning with ``fetch-depth: 0``.
    """
    reason = checker.check_link(repo, "v9.9.9", EARLY_EXAMPLE)

    assert reason is not None
    assert "git fetch --tags" in reason


# --------------------------------------------------------------------------------------
# `main` links keep their old meaning.
# --------------------------------------------------------------------------------------


def test_main_links_resolve_against_the_working_tree(repo: Path) -> None:
    """A PR may add a file and link to it at ``main`` in the same commit, uncommitted even."""
    (repo / "examples" / "brand_new_demo.py").write_text("# new\n")

    assert checker.check_link(repo, "main", "examples/brand_new_demo.py") is None
    assert checker.check_link(repo, "main", "examples/never_existed.py") is not None


# --------------------------------------------------------------------------------------
# The real URL from the card, when the checkout has the tag to prove it with.
# --------------------------------------------------------------------------------------


@pytest.mark.skipif(
    not checker.ref_exists(REPO_ROOT, "v0.1.0a1") or not checker.ref_exists(REPO_ROOT, "v0.1.0a3"),
    reason="this checkout has no release tags (a shallow clone); the tmp-repo cases cover it",
)
def test_the_exact_url_from_kan_488_is_flagged_in_this_repo() -> None:
    assert checker.check_link(REPO_ROOT, "v0.1.0a1", LATE_EXAMPLE) is not None
    assert checker.check_link(REPO_ROOT, "v0.1.0a3", LATE_EXAMPLE) is None
