"""The missing-extra gate really fires, proved on a session that really lacks the extra.

A guard nobody proved fires is exactly the failure class KAN-460 is about, and the
awkward part is that this process *has* ``satay[studio]`` — asserting on the absent case
from inside it is impossible. So each case runs the real ``pytest`` against this real
repository in a **child interpreter** whose import system genuinely cannot see FastAPI,
the same clean-child technique ``test_import_hygiene.py`` (KAN-491) uses for the
core-dependency boundary.

The block is a ``sitecustomize.py`` on ``PYTHONPATH``: Python imports it at startup, and
it installs a meta-path finder that raises ``ModuleNotFoundError`` for the named
top-level packages. That reproduces an uninstalled extra faithfully — both
``importlib.util.find_spec`` (what the guard probes with) and the ``pytest.importorskip``
calls at the top of the studio test modules see exactly what they would see on a
``uv sync`` environment with no ``--extra studio``.

The children run ``--collect-only`` because the guard fires at collection finish, before
any test executes: the exit code is the whole assertion, and skipping the run keeps four
child sessions to a couple of seconds.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from _extra_guard import ALLOW_MISSING_EXTRA_ENV_VAR, STUDIO_EXTRA_MODULES

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Block the whole extra, not just FastAPI. A partial block is not the situation being
#: reproduced: ``tests/unit/test_devstack_cli_app.py`` gates on Typer and then imports a
#: module that needs FastAPI, so blocking one and leaving the other installed produces a
#: collection *error* that no real environment ever sees. Taken from the guard's own list
#: so the two cannot drift apart.
STUDIO = ",".join(STUDIO_EXTRA_MODULES)

#: Installed into a temp dir and put on PYTHONPATH; ``site`` imports it at startup.
_SITECUSTOMIZE = '''
"""Make the named top-level packages unimportable, as if they were never installed."""

import os
import sys
from importlib.abc import MetaPathFinder

_BLOCKED = frozenset(n for n in os.environ.get("SATAY_BLOCK_MODULES", "").split(",") if n)


class _Blocker(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _BLOCKED:
            raise ModuleNotFoundError(f"blocked by the KAN-460 test: {fullname}", name=fullname)
        return None


if _BLOCKED:
    sys.meta_path.insert(0, _Blocker())
'''


def _run_pytest(
    *args: str, blocked: str = "", tmp_path: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Collect the real suite in a child interpreter, optionally with packages blocked."""
    block_dir = tmp_path / "blockdir"
    block_dir.mkdir(exist_ok=True)
    (block_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")

    env = dict(os.environ)
    env.pop(ALLOW_MISSING_EXTRA_ENV_VAR, None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(block_dir), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    env["SATAY_BLOCK_MODULES"] = blocked
    env.update(extra_env or {})

    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_blocking_the_extra_really_removes_the_studio_modules(tmp_path: Path) -> None:
    # The premise of every case below, and a restatement of the bug: asked for the studio
    # serving file alone with the extra blocked, pytest reports "no tests ran" — not a
    # failure — and the KAN-408 bundle gate inside it never executes.
    result = _run_pytest("tests/e2e/test_studio_serving.py", blocked=STUDIO, tmp_path=tmp_path)
    combined = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.NO_TESTS_COLLECTED, combined
    assert "could not import 'fastapi'" in combined


def test_whole_suite_without_the_extra_fails_instead_of_skipping(tmp_path: Path) -> None:
    result = _run_pytest(blocked=STUDIO, tmp_path=tmp_path)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, combined
    assert "satay[studio] extra is not installed" in combined
    assert "make dev-studio" in combined
    # And it names the file that would otherwise have vanished silently.
    assert "test_studio_serving.py" in combined


def test_whole_suite_with_the_extra_present_is_untouched(tmp_path: Path) -> None:
    # The control: same command, nothing blocked. A guard that fires when the extra is
    # installed would break every green run, so this is the half that must stay quiet.
    result = _run_pytest(blocked="", tmp_path=tmp_path)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "satay[studio] extra is not installed" not in combined


def test_the_unit_tier_stays_green_without_the_extra(tmp_path: Path) -> None:
    # `make test` is the documented fast inner loop and runs on a dev-only environment.
    # It must warn, not fail — a guard that breaks the inner loop is worse than the bug.
    result = _run_pytest("tests/unit", blocked=STUDIO, tmp_path=tmp_path)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "satay[studio] extra is not installed" in combined
    assert "warning rather than an error" in combined


def test_the_explicit_opt_out_downgrades_the_whole_suite_to_a_warning(tmp_path: Path) -> None:
    result = _run_pytest(
        blocked=STUDIO,
        tmp_path=tmp_path,
        extra_env={ALLOW_MISSING_EXTRA_ENV_VAR: "1"},
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
