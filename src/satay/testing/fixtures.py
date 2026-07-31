"""Pytest fixtures for the primary test seam (ADR-0011).

Importable as a pytest plugin: add ``pytest_plugins = ["satay.testing.fixtures"]`` to a
``conftest.py``. These provide the temp-store scaffolding, the determinism controls
(manual clock, seeded RNG, fault injector) that the public-API E2E seam is driven with,
and ``drain`` — :func:`satay.testing.settle` for driving a run through backoff.

The temp-store fixtures hand out *paths* only (a temp-file path and the ``:memory:``
path) laid out per ADR-0017; tests open a real ``SQLiteStore`` on them, which creates and
migrates the schema on connect. This module imports ``pytest`` and so is not imported by
``satay.testing`` at package import time — the runtime affordances (clock, RNG, faults)
stay import-clean without pytest.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from satay.config import BLOB_DIR_NAME, DB_FILENAME
from satay.testing.clock import ManualClock
from satay.testing.faults import FaultInjector
from satay.testing.rng import SeededRng
from satay.testing.settle import settle

#: Default seed for the seeded RNG fixture, so backoff jitter is reproducible.
DEFAULT_TEST_SEED = 1234


@pytest.fixture
def temp_data_dir(tmp_path: Path) -> Path:
    """A project-local ``.satay``-style data dir under pytest's ``tmp_path`` (ADR-0017)."""
    data_dir = tmp_path / ".satay"
    (data_dir / BLOB_DIR_NAME).mkdir(parents=True)
    return data_dir


@pytest.fixture
def temp_db_path(temp_data_dir: Path) -> Path:
    """Path to a temp-file SQLite database; the store creates and migrates it on connect."""
    return temp_data_dir / DB_FILENAME


@pytest.fixture
def memory_db_path() -> str:
    """The in-memory SQLite path, for tests that do not need on-disk durability."""
    return ":memory:"


@pytest.fixture
def manual_clock() -> ManualClock:
    """A ``ManualClock`` for deterministic time control (ADR-0011)."""
    return ManualClock()


@pytest.fixture
def seeded_rng() -> SeededRng:
    """A reproducibly seeded RNG for deterministic backoff jitter (ADR-0011, Q46)."""
    return SeededRng(DEFAULT_TEST_SEED)


@pytest.fixture
def drain() -> Callable[..., Awaitable[Any]]:
    """:func:`satay.testing.settle`, as a fixture — drives an awaitable through backoff.

    Hands back the importable function rather than re-implementing the loop, so the
    fixture and the ``examples/`` scripts (which cannot use a fixture) cannot drift apart.
    The fixture keeps its original name because that is what the existing call sites say;
    new code can just ``from satay.testing import settle``.
    """
    return settle


@pytest.fixture
def fault_injector() -> Iterator[FaultInjector]:
    """A ``FaultInjector`` for crash/stall simulation; cleared on teardown (ADR-0011)."""
    injector = FaultInjector()
    yield injector
    injector.clear()
