"""E2E: ``examples/replay_eval_demo.py`` still demonstrates the replay-eval loop (ADR-0041).

``tests/integration/test_eval.py`` tests the *capability* — the report fields, the gate
verdicts, ``compare_runs``. This module tests the **example**: that the file a reader
downloads still runs offline, still reuses the prefix so an eval is cheap, and still prints
the four gate outcomes the cookbook page turns on.

The claims, in the order the example makes them:

1. a baseline run records five durable calls and their cost;
2. a sharper prompt re-runs only ``draft``, changes the output, and passes an
   ``expect_output="changed"`` gate while failing an ``expect_output="unchanged"`` one;
3. a cheaper model leaves the output unchanged and passes a "usd must not rise" gate;
4. a pricier model leaves the output unchanged but fails that same gate — the regression.

Observable outcomes only (ADR-0011): run statuses, the durable calls on each journal,
recorded usage, printed output. Never replay internals. And no network and no API key,
which is the example's own claim, checked here rather than assumed.
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
EXAMPLE = REPO_ROOT / "examples" / "replay_eval_demo.py"


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


async def _inspections(data_dir: Path) -> list[satay.RunInspection]:
    """Every run in ``data_dir``, read back through the public ``satay.inspect``."""
    store = SQLiteStore.open(db_path(data_dir))
    try:
        return [await satay.inspect(run_id, store=store) for run_id in await store.list_runs()]
    finally:
        store.close()


def _executed_here(inspection: satay.RunInspection) -> list[str]:
    """The durable-call identities whose body actually ran in this run (not copied prefix)."""
    boundary = 0
    if inspection.forked_from is not None:
        boundary = int(inspection.forked_from["fork_point_seq"])
    return [call.identity for call in inspection.calls if call.first_seq > boundary]


def test_example_runs_and_prints_all_four_gate_outcomes(tmp_path: Path) -> None:
    """The cookbook's four verdicts, plus the CI hint and the reuse meter, all print."""
    stdout = run_example(tmp_path)

    assert "output:      changed at .reply" in stdout
    assert "gate [expect_output=changed]: PASS" in stdout
    assert "gate [expect_output=unchanged]: FAIL" in stdout
    # The cheaper model passes the cost gate; the pricier one fails it.
    assert stdout.count("gate [max usd increase 0]: PASS") == 1
    assert stdout.count("gate [max usd increase 0]: FAIL") == 1
    assert "usage 'usd' increased by" in stdout
    # The CI entry point is named, and the reuse arithmetic is the whole cost argument.
    assert "satay eval" in stdout
    assert "made 8 model calls, not 20" in stdout


async def test_journal_is_a_baseline_plus_three_forks_each_reusing_the_prefix(
    tmp_path: Path,
) -> None:
    """Four completed runs: one started, three forked, and each fork re-ran only ``draft``."""
    run_example(tmp_path)
    inspections = await _inspections(tmp_path)

    assert len(inspections) == 4
    assert all(i.status == RunStatus.COMPLETED.value for i in inspections)

    baselines = [i for i in inspections if i.forked_from is None]
    forks = [i for i in inspections if i.forked_from is not None]
    assert len(baselines) == 1
    assert len(forks) == 3

    # The baseline made all five durable calls; each fork reused four and re-ran the draft.
    assert len(baselines[0].calls) == 5
    for fork in forks:
        assert _executed_here(fork) == ["draft:0"], fork.run_id


async def test_cost_moves_with_the_model_swap(tmp_path: Path) -> None:
    """A model swap moves the bill without touching the answer: one cheaper, one pricier."""
    run_example(tmp_path)
    inspections = await _inspections(tmp_path)

    baseline = next(i for i in inspections if i.forked_from is None)
    forks = [i for i in inspections if i.forked_from is not None]
    base_usd = baseline.usage["usd"]

    fork_usds = [fork.usage["usd"] for fork in forks]
    assert any(usd < base_usd for usd in fork_usds), "no fork came in cheaper than the baseline"
    assert any(usd > base_usd for usd in fork_usds), "no fork came in pricier than the baseline"


def test_the_example_needs_no_provider_and_adds_no_dependency() -> None:
    """Satay ships no model adapters (ADR-0016), so the seam and its fake live in the file."""
    source = EXAMPLE.read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            assert "anthropic" not in line and "openai" not in line, (
                f"provider SDK imported at module scope: {line!r}"
            )
    assert "        from anthropic import" in source, "the provider import must stay function-local"
