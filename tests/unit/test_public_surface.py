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
    }
    assert expected <= set(satay.__all__)
    for name in expected:
        assert hasattr(satay, name), name


def test_version_is_exposed() -> None:
    assert isinstance(satay.__version__, str)


def test_stubs_raise_not_implemented() -> None:
    async def dummy() -> None: ...

    with pytest.raises(NotImplementedError):
        satay.workflow(dummy)
    with pytest.raises(NotImplementedError):
        satay.task(retries=1)
    with pytest.raises(NotImplementedError):
        satay.start(dummy)


def test_config_data_dir_convention(temp_data_dir: object) -> None:
    from satay import config

    resolved = config.resolve_data_dir("/tmp/example")
    assert resolved.name == "example"
    assert config.db_path(resolved).name == config.DB_FILENAME
    assert config.SCHEMA_USER_VERSION == 0
