"""Integration tests for the ``satay runs show`` CLI read (U1).

These are synchronous tests: the CLI owns its own ``asyncio.run`` loop, so the test
seeds the store in a separate loop then invokes ``main`` directly.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from satay.cli.main import main
from satay.journal.events import Event, EventType, RunRecord, RunStatus
from satay.journal.store import SQLiteStore


async def _seed_journal(db: Path, run_id: str, *, with_resume: bool) -> None:
    store = SQLiteStore.open(db)
    await store.create_run(
        RunRecord(
            run_id=run_id,
            workflow_name="demo",
            status=RunStatus.COMPLETED,
            code_version="dev:test",
            created_at=datetime(2026, 7, 22, tzinfo=UTC),
        )
    )
    await store.append(
        Event(
            run_id=run_id,
            type=EventType.WORKFLOW_CREATED,
            payload={"workflow_name": "demo", "code_version": "dev:test"},
        )
    )
    await store.append(
        Event(
            run_id=run_id,
            type=EventType.TASK_SCHEDULED,
            payload={"task_name": "step_one", "ordinal": 0},
        )
    )
    await store.append(
        Event(
            run_id=run_id,
            type=EventType.TASK_COMPLETED,
            payload={"task_name": "step_one", "ordinal": 0, "output_ref": 2},
        )
    )
    if with_resume:
        await store.append(Event(run_id=run_id, type=EventType.WORKFLOW_RESUMED))
    await store.append(
        Event(run_id=run_id, type=EventType.WORKFLOW_COMPLETED, payload={"output_ref": 4})
    )
    store.close()


def _seed(db: Path, run_id: str, *, with_resume: bool) -> None:
    asyncio.run(_seed_journal(db, run_id, with_resume=with_resume))


def test_runs_show_renders_ordered_timeline_with_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    db = data_dir / "satay.db"
    _seed(db, "run-x", with_resume=True)

    code = main(["runs", "show", "run-x", "--data-dir", str(data_dir)])
    assert code == 0
    out = capsys.readouterr().out
    # Ordered by seq, and the resume point carries the ⚡ marker.
    assert out.index("WorkflowCreated") < out.index("TaskCompleted")
    resume_line = next(line for line in out.splitlines() if "WorkflowResumed" in line)
    assert resume_line.startswith("⚡")


def test_runs_show_missing_run_returns_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    db = data_dir / "satay.db"
    _seed(db, "run-x", with_resume=False)
    code = main(["runs", "show", "does-not-exist", "--data-dir", str(data_dir)])
    assert code == 1
    assert "not found" in capsys.readouterr().err


def test_dev_dispatches_to_the_studio_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`satay dev` forwards its --data-dir/--port to the studio Typer command (V8, ADR-0016)."""
    captured: dict[str, object] = {}

    def _fake_run_dev(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    # The Typer command calls run_dev in its own module namespace; patch it there.
    import satay.devstack.cli as devstack_cli

    monkeypatch.setattr(devstack_cli, "run_dev", _fake_run_dev)

    code = main(["dev", "--port", "9999", "--data-dir", str(tmp_path)])
    assert code == 0
    assert captured["port"] == 9999
    assert captured["data_dir"] == str(tmp_path)


def test_dev_without_studio_extra_prints_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the studio extra, `satay dev` fails with a clear install hint (ADR-0016)."""
    import importlib

    cli_main = importlib.import_module("satay.cli.main")

    def _raise_import_error() -> object:
        raise ImportError("no studio extra")

    monkeypatch.setattr(cli_main, "_load_dev_cli", _raise_import_error)
    code = main(["dev"])
    assert code == 2
    assert "satay[studio]" in capsys.readouterr().err
