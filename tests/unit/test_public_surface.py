"""Smoke tests: the package imports cleanly and exposes its public surface."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

import pytest

import satay


def test_public_surface_is_exported() -> None:
    expected = {
        "workflow",
        "task",
        "start",
        "sleep",
        "wait_for_event",
        "send_event",
        "map",
        "gather",
        "start_child",
        "TaskContext",
        "RunHandle",
        # V2 public error types (N9/A10.2).
        "NondeterminismError",
        "EffectSafetyError",
        # V7 public error type (N17): version mismatch on resume under strict.
        "VersionMismatchError",
    }
    assert expected <= set(satay.__all__)
    for name in expected:
        assert hasattr(satay, name), name


def test_v2_error_types_are_public() -> None:
    """NondeterminismError/EffectSafetyError are public runtime errors (V2)."""
    assert issubclass(satay.NondeterminismError, RuntimeError)
    assert issubclass(satay.EffectSafetyError, RuntimeError)


def test_v7_version_mismatch_error_is_public() -> None:
    """VersionMismatchError is a public runtime error, mirroring the V2 policy errors."""
    assert issubclass(satay.VersionMismatchError, RuntimeError)


def test_version_is_exposed() -> None:
    assert isinstance(satay.__version__, str)


def _installed_version() -> str | None:
    """The version PyPI/pip report for the installed ``satay`` distribution, if any."""
    try:
        return importlib.metadata.version("satay")
    except importlib.metadata.PackageNotFoundError:
        return None


def _declared_version() -> str | None:
    """The version declared in the checkout's ``pyproject.toml``, if we are in one."""
    pyproject = Path(satay.__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.is_file():
        return None
    with pyproject.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    assert project["name"] == "satay"
    declared = project["version"]
    assert isinstance(declared, str)
    return declared


def test_version_tracks_the_single_source_of_truth() -> None:
    """``__version__`` is derived, not hand-maintained (KAN-447).

    The published ``0.1.0a1`` wheel reported ``0.0.0`` because the constant in
    ``satay/__init__.py`` was bumped independently of ``pyproject.toml`` — i.e. not at
    all. Whichever way Satay was imported, one authority for the version is available,
    so this asserts against that authority rather than skipping:

    * installed (wheel, sdist, editable, PyPI) — the distribution metadata, which the
      build backend generates from ``pyproject.toml``;
    * bare source checkout with nothing installed — ``pyproject.toml`` itself.
    """
    installed = _installed_version()
    declared = _declared_version()

    if installed is None and declared is None:  # pragma: no cover - defensive
        pytest.fail(
            "no version authority to check against: satay is neither installed as a "
            "distribution nor imported from a source checkout, so this test would be "
            "vacuous. Fix the environment rather than weakening the assertion."
        )

    if installed is not None:
        assert satay.__version__ == installed, (
            f"satay.__version__ ({satay.__version__!r}) disagrees with the installed "
            f"distribution metadata ({installed!r}); __version__ must be derived from "
            "metadata, not hard-coded."
        )
    else:
        assert satay.__version__ == declared, (
            f"satay.__version__ ({satay.__version__!r}) disagrees with pyproject.toml "
            f"({declared!r}) in a source checkout with no installed distribution."
        )


def test_installed_metadata_matches_pyproject() -> None:
    """The two authorities above must agree, so the assertion above cannot be gamed.

    Only checkable when both exist — the repo's own test environment, where ``satay``
    is installed from this very ``pyproject.toml``. A failure here usually means a
    stale install after a version bump: re-run ``uv sync --extra studio``.
    """
    installed = _installed_version()
    declared = _declared_version()
    if installed is None or declared is None:
        pytest.skip("needs both an installed distribution and a source checkout")
    assert installed == declared


def test_v1_decorators_are_live() -> None:
    """V1 implements @workflow/@task; they register rather than raise."""

    @satay.workflow
    async def wf(value: int) -> int:  # pragma: no cover - not driven here
        return value

    @satay.task(retries=1)
    async def tk(value: int) -> int:  # pragma: no cover - not driven here
        return value

    assert hasattr(wf, "__satay_workflow__")
    assert hasattr(tk, "__satay_task__")


async def test_v4_composition_primitives_are_live() -> None:
    """V4 implements map/gather/start_child; called outside a drive they guard, not stub."""
    import inspect

    assert inspect.iscoroutinefunction(satay.map)
    assert inspect.iscoroutinefunction(satay.gather)
    assert inspect.iscoroutinefunction(satay.start_child)

    @satay.workflow
    async def _noop(value: int) -> int:  # pragma: no cover - not driven here
        return value

    # Outside a workflow drive they raise a clear RuntimeError (not NotImplementedError).
    with pytest.raises(RuntimeError):
        await satay.start_child(_noop)
    with pytest.raises(RuntimeError):
        await satay.gather()


def test_v3_primitives_are_live() -> None:
    """V3 implements sleep/wait_for_event/send_event as coroutine functions."""
    import inspect

    assert inspect.iscoroutinefunction(satay.sleep)
    assert inspect.iscoroutinefunction(satay.wait_for_event)
    assert inspect.iscoroutinefunction(satay.send_event)


def test_config_data_dir_convention(temp_data_dir: object) -> None:
    from satay import config

    resolved = config.resolve_data_dir("/tmp/example")
    assert resolved.name == "example"
    assert config.db_path(resolved).name == config.DB_FILENAME
    assert config.SCHEMA_USER_VERSION == 0
