"""The docsite quotes a release version in seven shapes; they must move together (KAN-878).

``0.1.0`` shipped with seven cookbook pages still pinned to ``v0.1.0a3``. The agentic-DAG
page documented collect mode while its own ``Source:`` link and its ``curl`` line fetched a
file from before collect mode existed — a recipe pointing a new user at a version of itself
that contradicted the page around it. The flip was a manual sweep across five pages, so it
got skipped.

``docsite/sync_docs_version.py`` makes it a command and ``--check`` makes it a gate. What
follows pins the three things that are easy to get wrong:

- **What counts as a version reference.** Only anchored contexts and backticked bare
  versions equal to the site's current one. A Zensical pin or a Python version in the same
  paragraph must be untouchable, or the script becomes too dangerous to run unattended.
- **The tag requirement.** The docs may never quote a version that has no tag, because
  that is precisely the pin that 404s.
- **What ``--check`` does *not* demand.** ``docs/RELEASING.md`` §3 flips the docsite in a
  PR *after* the tag is pushed. A check that required the newest tag would turn ``main``
  red for the length of that window, reporting a failure for the documented process
  working correctly.

The script is not importable as a module (``docsite`` is not a package and must not become
one — it holds a static site, not library code), so it is loaded by path, the same way
``test_check_repo_links.py`` loads its neighbour.
"""

from __future__ import annotations

import importlib.util
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "docsite" / "sync_docs_version.py"

_spec = importlib.util.spec_from_file_location("sync_docs_version", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


# --------------------------------------------------------------------------------------
# Version ordering: a final release outranks its own pre-releases.
# --------------------------------------------------------------------------------------


def test_a_final_release_sorts_above_its_own_prereleases() -> None:
    """The whole reason 0.1.0 is "newer" than 0.1.0a3 — and a plain string sort disagrees."""
    versions = ["0.1.0", "0.1.0a3", "0.2.0", "0.1.0b1", "0.1.0a1", "0.1.0rc1", "0.10.0"]

    assert sorted(versions, key=sync.sort_key) == [
        "0.1.0a1",
        "0.1.0a3",
        "0.1.0b1",
        "0.1.0rc1",
        "0.1.0",
        "0.2.0",
        "0.10.0",
    ]
    assert sorted(versions) != sorted(versions, key=sync.sort_key), "string sort would do"


# --------------------------------------------------------------------------------------
# Discovery: what is a version reference, and what only looks like one.
# --------------------------------------------------------------------------------------


def test_every_shape_the_docs_actually_use_is_discovered() -> None:
    """One case per shape found on the real site, so a regex edit cannot silently drop one."""
    page = "\n".join(
        (
            "Source: [`x`](https://github.com/leejianrong/satay-runtime/blob/v0.1.0a3/examples/x.py)",
            "curl -O https://raw.githubusercontent.com/leejianrong/satay-runtime/v0.1.0a3/examples/x.py",
            "pip install 'satay[studio]==0.1.0a3'",
            "Successfully installed satay-0.1.0a3",
            "satay   0.1.0a3",
            "`satay 0.1.0a3` is the current release",
        )
    )

    assert sync.anchored_versions(page) == {"0.1.0a3"}
    assert sync.anchored_occurrences(page, "0.1.0a3") == 6


def test_an_unrelated_version_number_is_not_a_candidate() -> None:
    """Zensical's pin and Python's version share a page with the release number."""
    page = (
        "Pinned to `zensical==0.0.52`, and Satay needs Python **3.12** or **3.13**.\n"
        "The site is built with 0.0.52 and the wheel is satay-0.1.0a3.\n"
    )

    assert sync.anchored_versions(page) == {"0.1.0a3"}


def test_a_bare_backticked_version_is_rewritten_only_when_it_is_the_current_one() -> None:
    """The guard that makes prose safe to touch at all.

    A backticked version on its own is indistinguishable from any other version-shaped
    token, so it is rewritten only on an exact match with the version the anchored
    contexts have already established. ``0.0.52`` in the same sentence survives.
    """
    page = "`0.1.0a3` is current; `v0.1.0a3` is the tag; Zensical is pinned to `0.0.52`.\n"

    updated, moved = sync.rewrite(page, "0.1.0a3", "0.1.0")

    assert updated == "`0.1.0` is current; `v0.1.0` is the tag; Zensical is pinned to `0.0.52`.\n"
    assert moved == 2


def test_links_pinned_to_main_are_left_alone() -> None:
    """Pointing at ``main`` is a content decision (the ADR index), not a stale pin."""
    page = "[ADRs](https://github.com/leejianrong/satay-runtime/tree/main/docs/adr/)\n"

    updated, moved = sync.rewrite(page, "0.1.0a3", "0.1.0")

    assert updated == page
    assert moved == 0


# --------------------------------------------------------------------------------------
# The rewrite, end to end, against a throwaway site.
# --------------------------------------------------------------------------------------


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A miniature docsite on ``v0.1.0a3``, in a repo that has ``v0.1.0`` too."""
    root = tmp_path / "repo"
    docs = root / "docsite" / "docs" / "cookbook"
    docs.mkdir(parents=True)

    (docs.parent / "index.md").write_text(
        "`satay 0.1.0a3` is the current release.\n\nSuccessfully installed satay-0.1.0a3\n"
    )
    (docs / "recipe.md").write_text(
        "Source: [`x`](https://github.com/leejianrong/satay-runtime/blob/v0.1.0a3/examples/x.py)\n"
        "curl -O https://raw.githubusercontent.com/leejianrong/satay-runtime/v0.1.0a3/examples/x.py\n"
        "`pip install 'satay[studio]==0.1.0a3'`\n"
    )

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, env=sync.git_env()
        )

    subprocess.run(
        ["git", "init", "-b", "main", str(root)],
        check=True,
        capture_output=True,
        env=sync.git_env(),
    )
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    git("add", "-A")
    git("commit", "-m", "site")
    git("tag", "v0.1.0a3")
    git("tag", "v0.1.0")

    monkeypatch.setattr(sync, "REPO_ROOT", root)
    monkeypatch.setattr(sync, "DOCS_SOURCE", root / "docsite" / "docs")
    yield root


def test_sync_moves_the_whole_site_to_the_newest_tag(site: Path) -> None:
    """The release step, in one command: no argument means "the newest tag you have"."""
    assert sync.main([]) == 0

    for page in sync.pages():
        text = page.read_text()
        assert "0.1.0a3" not in text
        assert "0.1.0" in text


def test_sync_is_idempotent(site: Path) -> None:
    assert sync.main([]) == 0
    before = {page: page.read_text() for page in sync.pages()}

    assert sync.main([]) == 0

    assert {page: page.read_text() for page in sync.pages()} == before


def test_sync_refuses_a_version_with_no_tag(site: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The docsite is flipped only AFTER the tag exists (docs/RELEASING.md §3)."""
    assert sync.main(["--to", "0.9.0"]) == 1
    assert "no `v0.9.0` tag" in capsys.readouterr().err
    assert "0.1.0a3" in (sync.DOCS_SOURCE / "cookbook" / "recipe.md").read_text()


# --------------------------------------------------------------------------------------
# `--check`: the gate `make docs` and the Docs workflow run.
# --------------------------------------------------------------------------------------


def test_check_passes_on_a_consistent_site_even_when_a_newer_tag_exists(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The window between pushing a tag and merging the flip PR must not be a red main.

    The site here sits on ``v0.1.0a3`` while ``v0.1.0`` is tagged — exactly the state
    RELEASING.md §3 prescribes between the two steps. That is the process working, so it
    is a note, not a failure.
    """
    assert sync.main(["--check"]) == 0

    out = capsys.readouterr().out
    assert "consistently on 0.1.0a3" in out
    assert "v0.1.0 is newer" in out


def test_check_fails_when_the_pages_disagree(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The 0.1.0 bug itself: one page flipped, the rest left behind."""
    page = sync.DOCS_SOURCE / "cookbook" / "recipe.md"
    page.write_text(page.read_text().replace("blob/v0.1.0a3/", "blob/v0.1.0/"))

    assert sync.main(["--check"]) == 1

    err = capsys.readouterr().err
    assert "0.1.0, 0.1.0a3" in err
    assert "cookbook/recipe.md" in err, "the failure has to name the page that disagrees"


def test_check_fails_when_the_docs_quote_an_untagged_version(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pin to a tag that does not exist is the link that is guaranteed to 404."""
    for page in sync.pages():
        page.write_text(page.read_text().replace("0.1.0a3", "0.4.0"))

    assert sync.main(["--check"]) == 1
    assert "no `v0.4.0` tag" in capsys.readouterr().err


def test_check_and_to_together_are_rejected(site: Path) -> None:
    """``--check`` rewrites nothing, so a target for it would be a silent no-op."""
    with pytest.raises(SystemExit):
        sync.main(["--check", "--to", "0.1.0"])


# --------------------------------------------------------------------------------------
# The real site, which is what actually ships.
# --------------------------------------------------------------------------------------


def test_the_real_docsite_is_consistent_and_tagged() -> None:
    """`make docs` runs this; asserting it here means a bad flip fails the fast suite too."""
    if not sync.release_tags(REPO_ROOT):
        pytest.skip("this checkout has no release tags (a shallow clone)")

    assert sync.main(["--check"]) == 0
