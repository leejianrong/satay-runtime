"""Import-hygiene guard (Epic 0 CI step).

Proves the runtime core imports without any infrastructure and without pulling the
studio extra (FastAPI/uvicorn/Pydantic/Typer). Collected with ``--collect-only`` in CI
to assert the integration tier imports cleanly; the assertions also run under pytest.

The no-studio-dependency assertion runs in a **fresh subprocess** (V5): the studio extra
is installed in the same CI job that runs the HTTP-surface tests, and some sibling test
modules import FastAPI, so scanning the *current* interpreter's ``sys.modules`` would be
contaminated by other tests. A clean child interpreter imports only the core modules and
proves none of the forbidden packages were pulled — a strictly stronger guard than the
in-process scan it replaces (the boundary is enforced, never weakened).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap

CORE_MODULES = [
    "satay",
    "satay.config",
    "satay.api",
    "satay.replay",
    "satay.journal",
    "satay.executor",
    "satay.timers",
    "satay.control",
    "satay.versioning",
    "satay.blobs",
    "satay.devstack",
    "satay.testing",
    "satay.cli",
]

FORBIDDEN_IN_CORE = ["fastapi", "uvicorn", "pydantic", "typer", "click"]


def test_core_modules_import_cleanly() -> None:
    for name in CORE_MODULES:
        importlib.import_module(name)


def test_core_import_pulls_no_studio_dependency() -> None:
    program = textwrap.dedent(
        f"""
        import importlib, sys
        for name in {CORE_MODULES!r}:
            importlib.import_module(name)
        forbidden = {FORBIDDEN_IN_CORE!r}
        pulled = sorted(
            n for n in sys.modules if n.split(".")[0] in forbidden
        )
        if pulled:
            sys.stdout.write("PULLED:" + ",".join(pulled))
            raise SystemExit(1)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"core import pulled a studio-only dependency: {result.stdout} {result.stderr}"
    )


def test_run_app_drives_a_parked_run_with_no_studio_dependency() -> None:
    """``satay.run_app`` is core, and this proves it by *using* it (KAN-491, ADR-0030).

    Importing ``satay`` is not enough to make the claim. ``run_app`` imports the store and
    the worker **lazily, inside the function**, so the import-time scan above would stay
    green even if entering the block pulled FastAPI. So this child interpreter actually
    opens the journal, starts the poll loop, drives a workflow that parks on a durable
    sleep through to its result, and only then scans ``sys.modules``.

    A regression here means a plain ``pip install satay`` can no longer run a workflow that
    sleeps or waits for an event — which is the whole reason ADR-0030 put this in the core
    rather than in ``satay[studio]``.
    """
    program = textwrap.dedent(
        f"""
        import asyncio, sys, tempfile
        import satay

        @satay.workflow
        async def _hygiene_naps(value: int) -> int:
            await satay.sleep(0.01)
            return value + 1

        async def main() -> None:
            with tempfile.TemporaryDirectory() as data_dir:
                async with satay.run_app(data_dir=data_dir, interval=0.01) as store:
                    handle = satay.start(_hygiene_naps, 1, store=store)
                    outcome = await handle.result()
                    assert outcome == 2, outcome
                    assert outcome is not satay.PARKED

        asyncio.run(main())
        pulled = sorted(
            n for n in sys.modules if n.split(".")[0] in {FORBIDDEN_IN_CORE!r}
        )
        if pulled:
            sys.stdout.write("PULLED:" + ",".join(pulled))
            raise SystemExit(1)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"satay.run_app needed a studio-only dependency: {result.stdout} {result.stderr}"
    )


def test_inspect_reads_a_run_with_no_studio_dependency() -> None:
    """``satay.inspect`` is core, and this proves it by *using* it (KAN-477).

    ``inspect`` reaches the read-view assembly in ``satay.control.views`` through a lazy
    import, the same arrangement ``fork`` and ``RunHandle.cancel`` already use. That is
    exactly the shape the import-time scan above cannot see: ``satay.control`` is pure
    Python today, but it is also the package the FastAPI stack lives in, and one
    module-level ``import fastapi`` added there would put a studio-only dependency behind
    a core public function without failing any import-time check.

    So this child interpreter drives a real workflow, reads it back through the public
    name, asserts it got the recorded call, and only then scans ``sys.modules``.
    """
    program = textwrap.dedent(
        f"""
        import asyncio, sys
        import satay
        from satay.journal.store import SQLiteStore

        @satay.task()
        async def _hygiene_double(value: int) -> int:
            return value * 2

        @satay.workflow
        async def _hygiene_reads(value: int) -> int:
            return await _hygiene_double(value)

        async def main() -> None:
            store = SQLiteStore.open(":memory:")
            handle = satay.start(_hygiene_reads, 21, store=store)
            assert await handle.result() == 42
            inspection = await satay.inspect(handle.run_id, store=store)
            assert [c.identity for c in inspection.calls] == ["_hygiene_double:0"], inspection
            assert inspection.calls[0].output == 42, inspection
            store.close()

        asyncio.run(main())
        pulled = sorted(
            n for n in sys.modules if n.split(".")[0] in {FORBIDDEN_IN_CORE!r}
        )
        if pulled:
            sys.stdout.write("PULLED:" + ",".join(pulled))
            raise SystemExit(1)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"satay.inspect needed a studio-only dependency: {result.stdout} {result.stderr}"
    )
