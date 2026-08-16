"""The missing-extra gate's decision table (KAN-460).

These cover the predicate in isolation — which is the only way to test the *absent*-extra
branches from a session that has the extra installed. The wiring (conftest hook, exit
code, real message on a real session) is proved separately in
``tests/integration/test_extra_guard_session.py`` with a child interpreter that really
cannot import FastAPI.
"""

from __future__ import annotations

import importlib
from argparse import Namespace
from pathlib import Path

from _extra_guard import (
    ALLOW_MISSING_EXTRA_ENV_VAR,
    STUDIO_EXTRA_MODULES,
    TESTS_ROOT,
    covers_whole_suite,
    evaluate,
    is_narrowed,
    missing_modules,
    opted_out,
)

REPO_ROOT = TESTS_ROOT.parent

#: A module name no environment will ever provide, standing in for an uninstalled extra.
ABSENT = "satay_definitely_not_installed_xyz"

#: An option namespace with no narrowing switch set — the default for a plain `pytest`.
WIDE_OPEN = Namespace(keyword="", markexpr="", deselect=None, lf=False, stepwise=False)


def test_studio_extra_modules_match_the_pyproject_extra() -> None:
    # The gate is only as good as the names it checks; drift here is a silent hole.
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    studio_block = text.split("studio = [", 1)[1].split("]", 1)[0]
    declared = {line.strip().strip('",').split(">=")[0] for line in studio_block.splitlines()}
    assert set(STUDIO_EXTRA_MODULES) == {name for name in declared if name}


def test_missing_modules_reports_only_the_absent_ones() -> None:
    assert missing_modules(["json", "pathlib"]) == ()
    assert missing_modules(["json", ABSENT]) == (ABSENT,)


def test_the_probe_agrees_with_a_real_import() -> None:
    # `find_spec` is a stand-in for "can this be imported"; if the two ever disagree the
    # gate is reading a different world from the `importorskip` calls it exists to
    # protect. Asserted as an equivalence rather than as "the extra is installed", so it
    # holds on a dev-only environment too — a check that only passes with the extra would
    # break `make test`, which is the fast inner loop this whole design protects.
    absent = missing_modules()
    for name in STUDIO_EXTRA_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            importable = False
        else:
            importable = True
        assert importable is (name not in absent), name


def test_whole_suite_is_recognised_from_the_default_testpaths() -> None:
    # `pytest -q` with no path argument arrives as config.args == ["tests"].
    assert covers_whole_suite(["tests"], REPO_ROOT)


def test_repo_root_and_absolute_tests_root_also_count_as_whole_suite() -> None:
    assert covers_whole_suite(["."], REPO_ROOT)
    assert covers_whole_suite([str(TESTS_ROOT)], Path("/nowhere"))


def test_a_tier_a_file_or_a_node_id_is_not_the_whole_suite() -> None:
    assert not covers_whole_suite(["tests/unit"], REPO_ROOT)
    assert not covers_whole_suite(["tests/integration", "tests/e2e"], REPO_ROOT)
    assert not covers_whole_suite(["tests/e2e/test_studio_serving.py::test_x"], REPO_ROOT)


def test_narrowing_switches_are_detected() -> None:
    assert not is_narrowed(WIDE_OPEN)
    assert is_narrowed(Namespace(keyword="clock"))
    assert is_narrowed(Namespace(markexpr="slow"))
    assert is_narrowed(Namespace(deselect=["tests/unit/test_clock.py"]))
    assert is_narrowed(Namespace(lf=True))
    assert is_narrowed(Namespace(stepwise=True))


def test_opt_out_is_explicit_and_typo_resistant() -> None:
    for value in ("1", "true", "TRUE", " yes ", "on"):
        assert opted_out(environ={ALLOW_MISSING_EXTRA_ENV_VAR: value})
    for value in ("", "0", "no", "ture", "please"):
        assert not opted_out(environ={ALLOW_MISSING_EXTRA_ENV_VAR: value})
    assert not opted_out(environ={})


def test_no_verdict_when_the_extra_is_present() -> None:
    assert evaluate(["tests"], REPO_ROOT, WIDE_OPEN, modules=["json"], environ={}) is None


def test_whole_suite_without_the_extra_is_fatal() -> None:
    verdict = evaluate(["tests"], REPO_ROOT, WIDE_OPEN, modules=[ABSENT], environ={})
    assert verdict is not None
    assert verdict.fatal
    assert verdict.missing == (ABSENT,)
    # The failure has to name both fixes, the way KAN-408's bundle gate does.
    assert "make dev-studio" in verdict.message
    assert ALLOW_MISSING_EXTRA_ENV_VAR in verdict.message


def test_a_narrowed_run_without_the_extra_only_warns() -> None:
    # This is what keeps `make test` — the documented fast inner loop on a dev-only
    # environment — green. Breaking that would be worse than the bug being fixed.
    tier = evaluate(["tests/unit"], REPO_ROOT, WIDE_OPEN, modules=[ABSENT], environ={})
    assert tier is not None and not tier.fatal

    filtered = evaluate(
        ["tests"], REPO_ROOT, Namespace(keyword="clock"), modules=[ABSENT], environ={}
    )
    assert filtered is not None and not filtered.fatal


def test_the_explicit_opt_out_downgrades_the_whole_suite_verdict() -> None:
    verdict = evaluate(
        ["tests"],
        REPO_ROOT,
        WIDE_OPEN,
        modules=[ABSENT],
        environ={ALLOW_MISSING_EXTRA_ENV_VAR: "1"},
    )
    assert verdict is not None and not verdict.fatal
