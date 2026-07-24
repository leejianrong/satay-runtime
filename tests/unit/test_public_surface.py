"""Smoke tests: the package imports cleanly and exposes its public surface."""

from __future__ import annotations

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
    }
    assert expected <= set(satay.__all__)
    for name in expected:
        assert hasattr(satay, name), name


def test_v2_error_types_are_public() -> None:
    """NondeterminismError/EffectSafetyError are public runtime errors (V2)."""
    assert issubclass(satay.NondeterminismError, RuntimeError)
    assert issubclass(satay.EffectSafetyError, RuntimeError)


def test_version_is_exposed() -> None:
    assert isinstance(satay.__version__, str)


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


async def test_deferred_composition_primitives_still_raise() -> None:
    """Composition primitives landing in V4 still raise NotImplementedError."""

    @satay.workflow
    async def _noop(value: int) -> int:  # pragma: no cover - not driven here
        return value

    with pytest.raises(NotImplementedError):
        await satay.start_child(_noop)


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
