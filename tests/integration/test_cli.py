"""Integration tests for the core ``satay`` CLI (U1): ``runs show``, ``dev``, ``--version``.

These are synchronous tests: the CLI owns its own ``asyncio.run`` loop, so the test
seeds the store in a separate loop then invokes ``main`` directly.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import satay
from satay.blobs import BlobStore
from satay.cli.main import main
from satay.config import blob_dir
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


def test_runs_delete_removes_a_terminal_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    db = data_dir / "satay.db"
    _seed(db, "run-x", with_resume=False)

    code = main(["runs", "delete", "run-x", "--data-dir", str(data_dir)])

    assert code == 0
    assert "deleted" in capsys.readouterr().out
    assert main(["runs", "show", "run-x", "--data-dir", str(data_dir)]) == 1


def test_runs_delete_rejects_a_non_terminal_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    db = data_dir / "satay.db"

    async def _seed_running() -> None:
        store = SQLiteStore.open(db)
        await store.create_run(
            RunRecord(
                run_id="run-running",
                workflow_name="demo",
                status=RunStatus.RUNNING,
                code_version="dev:test",
                created_at=datetime(2026, 7, 22, tzinfo=UTC),
            )
        )
        store.close()

    asyncio.run(_seed_running())

    code = main(["runs", "delete", "run-running", "--data-dir", str(data_dir)])

    assert code == 1
    assert "not terminal" in capsys.readouterr().err


def test_gc_dry_run_reports_reclaimable_blobs_without_deleting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    db = data_dir / "satay.db"
    _seed(db, "run-x", with_resume=False)
    blobs = BlobStore(blob_dir(data_dir))
    orphan_id = blobs.put(b"orphan" * 1000)
    path = blobs.directory / f"{orphan_id}.blob"
    old = path.stat().st_mtime - 3600
    os.utime(path, (old, old))

    code = main(["gc", "--data-dir", str(data_dir), "--grace-period-seconds", "60"])

    assert code == 0
    out = capsys.readouterr().out
    assert "would reclaim 1 blobs" in out
    assert path.exists()


def test_gc_apply_deletes_reclaimable_blobs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    db = data_dir / "satay.db"
    _seed(db, "run-x", with_resume=False)
    blobs = BlobStore(blob_dir(data_dir))
    orphan_id = blobs.put(b"orphan" * 1000)
    path = blobs.directory / f"{orphan_id}.blob"
    old = path.stat().st_mtime - 3600
    os.utime(path, (old, old))

    code = main(["gc", "--data-dir", str(data_dir), "--apply", "--grace-period-seconds", "60"])

    assert code == 0
    out = capsys.readouterr().out
    assert "reclaimed 1 blobs" in out
    assert not path.exists()


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


def _cli_version_output(capsys: pytest.CaptureFixture[str]) -> str:
    """Run ``satay --version`` through ``main`` and return the line it printed."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    # argparse's version action exits 0 — the flag is a successful query, not an error.
    assert exc_info.value.code == 0
    return capsys.readouterr().out.strip()


def test_version_flag_works_without_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    """``satay --version`` is how a user types it: bare, with no subcommand (KAN-459).

    The parser's subcommand is ``required=True``, so this asserts the version action
    fires *before* the "arguments are required: command" error, not after it.
    """
    assert _cli_version_output(capsys) == f"satay {satay.__version__}"


def test_version_flag_agrees_with_the_installed_distribution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The drift guard — compare against distribution metadata, never against a literal.

    This is the consumer ``__version__`` never had. KAN-447 shipped ``0.0.0`` in the
    ``0.1.0a1`` wheel because the value was a hard-coded constant and no code path or
    test ever compared it to the metadata built from ``pyproject.toml``; that bug would
    fail here. A literal expected version would recreate the original problem in test
    form, so there isn't one: the release bump moves both sides together.
    """
    try:
        installed = importlib.metadata.version("satay")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - bare source tree
        pytest.skip("satay is not installed; no distribution metadata to compare against")
    assert satay.__version__ == installed
    assert _cli_version_output(capsys) == f"satay {installed}"
