"""E2E: ``examples/durable_agent_demo.py`` still shows a durable agent loop (ADR-0043).

``tests/integration/test_agent.py`` tests the *capability*; this tests the **example** — that
the file a reader downloads still runs offline, still answers via tools, still survives a
crash mid-loop, still bounds a runaway model, and still bridges into ``replay_eval``.

Observable outcomes only (ADR-0011): run statuses, journal identities, printed output. No
network and no API key, which is the example's own claim.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import satay
from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.journal.events import RunStatus
from satay.journal.store import SQLiteStore

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "examples" / "durable_agent_demo.py"


def run_example(data_dir: Path) -> str:
    """Run the example as a subprocess with **no** provider credentials in the environment."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1", DATA_DIR_ENV_VAR: str(data_dir)}
    for leaked in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SATAY_DEMO_MODEL"):
        env.pop(leaked, None)
    proc = subprocess.run(
        [sys.executable, str(EXAMPLE)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        f"exited {proc.returncode}\n--- stdout ---\n{proc.stdout[-4000:]}\n"
        f"--- stderr ---\n{proc.stderr[-4000:]}"
    )
    return proc.stdout


def test_example_runs_and_prints_the_loop_the_crash_and_the_budget(tmp_path: Path) -> None:
    """The four sections the cookbook turns on all print, with the expected verdicts."""
    stdout = run_example(tmp_path)

    # 1) the loop answers via tools.
    assert "comes to $17" in stdout
    assert "stop: final" in stdout
    # 2) durability: at least the first recorded turn replayed as a hit.
    assert "replayed as a hit and were not re-run" in stdout
    # 3) the budget stopped a runaway model.
    assert "stop: max_steps" in stdout
    # 4) the bridge into the eval wedge ran.
    assert "usd delta on the replayed turn" in stdout


async def test_the_agent_run_journal_is_two_turns_and_a_tool(tmp_path: Path) -> None:
    """The answering run recorded exactly its two model turns and the one tool call."""
    run_example(tmp_path)
    store = SQLiteStore.open(db_path(tmp_path))
    try:
        answering = None
        for run_id in await store.list_runs():
            inspection = await satay.inspect(run_id, store=store)
            identities = [call.identity for call in inspection.calls]
            if identities == ["model_turn:0", "price:0", "shipping:0", "model_turn:1"]:
                answering = inspection
                break
        assert answering is not None, "the four-call answering run is not on the journal"
        assert answering.status == RunStatus.COMPLETED.value
    finally:
        store.close()


def test_the_example_needs_no_provider_and_adds_no_dependency() -> None:
    """Satay ships no model adapters (ADR-0016); the seam and its fake live in the file."""
    source = EXAMPLE.read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            assert "anthropic" not in line and "openai" not in line, (
                f"provider SDK imported at module scope: {line!r}"
            )
