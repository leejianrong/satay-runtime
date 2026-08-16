"""End-to-end acceptance tests for write-time redaction (KAN-653, ADR-0029).

Driven through the primary seam (ADR-0011): the public ``satay.start`` / ``send_event``
API against a temp-file ``SQLiteStore``, with the ``FaultInjector`` crash hook for the
resume case. The distinguishing assertion of this slice is made against the **raw SQLite
file** through a separate connection — a read through the store or the read API cannot
tell you whether the secret is in the store, only whether it came back out.

Reuse-versus-execution is proven by an execution-count marker and the journal, never by
spying on the engine.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from satay.api.decorators import task, workflow
from satay.api.fork import fork
from satay.api.primitives import map as durable_map
from satay.api.primitives import send_event, sleep, start, wait_for_event
from satay.api.run_handle import PARKED, WorkflowFailedError
from satay.config import WRITE_REDACTION_ENV_VAR
from satay.control.api import ReadAPI
from satay.journal.events import EventType, RunStatus
from satay.journal.store import SQLiteStore
from satay.redaction import REDACTED, Redactor
from satay.replay.failures import TaskFailedError
from satay.testing.clock import ManualClock
from satay.testing.faults import FaultInjector, SimulatedCrash
from satay.timers import TimerEventWorker

SECRET = "sk-live-super-secret-value"

#: Execution-count marker: the observable proof of reuse-versus-re-execution (ADR-0011).
EXECUTIONS: Counter[str] = Counter()


@pytest.fixture(autouse=True)
def _reset_marker() -> None:
    EXECUTIONS.clear()


# -- workflows under test ---------------------------------------------------------


@task()
async def wr_issue_credentials(label: str) -> dict[str, Any]:
    """Returns a structure with a sensitive field name — the redactable output slot."""
    EXECUTIONS["wr_issue_credentials"] += 1
    return {"api_key": SECRET, "label": label}


@task()
async def wr_use_credentials(creds: dict[str, Any]) -> str:
    EXECUTIONS["wr_use_credentials"] += 1
    return f"used:{creds['label']}"


@workflow
async def wr_two_step(label: str) -> str:
    creds = await wr_issue_credentials(label)
    return await wr_use_credentials(creds)


@task()
async def wr_draft(item: dict[str, Any]) -> dict[str, Any]:
    EXECUTIONS[f"wr_draft:{item['id']}"] += 1
    return {"id": item["id"], "token": SECRET}


@workflow
async def wr_fan_out(_: Any = None) -> list[str]:
    items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    # Sequential so a crash after the first item's TaskCompleted is deterministic.
    results = await durable_map(wr_draft, items, key=lambda i: str(i["id"]), concurrency=1)
    return [r["id"] for r in results]


@workflow
async def wr_seeded(payload: dict[str, Any]) -> str:
    """The resume-seed case: a parked run is re-entered from its *recorded* input.

    The first drive holds the caller's live argument; the wake after the park rehydrates
    ``WorkflowCreated.input_ref`` from the journal instead, which is where a write-time
    redaction of the workflow input becomes visible to the workflow body.
    """
    await sleep(60.0)
    return await wr_use_credentials({"label": payload["api_key"]})


@dataclass
class WrApproval:
    api_key: str
    note: str


@workflow
async def wr_await_approval(_: Any = None) -> str:
    approval = await wait_for_event(WrApproval, key="wr-1")
    return f"{approval.api_key}|{approval.note}"


@task()
async def wr_maybe_fail(item: dict[str, Any]) -> dict[str, Any]:
    EXECUTIONS[f"wr_maybe_fail:{item['id']}"] += 1
    if item["id"] == "bad":
        raise ValueError("could not reach the provider")
    return {"id": item["id"], "api_key": SECRET}


@workflow
async def wr_collect(_: Any = None) -> list[str]:
    """Collect mode (ADR-0027): the failure is *recorded* as `TaskFailed`, not raised."""
    results = await durable_map(
        wr_maybe_fail,
        [{"id": "ok"}, {"id": "bad"}],
        key=lambda i: str(i["id"]),
        concurrency=1,
        return_exceptions=True,
    )
    return [r.error_type if isinstance(r, TaskFailedError) else str(r["id"]) for r in results]


@workflow
async def wr_from_input(payload: dict[str, Any]) -> str:
    used = await wr_use_credentials({"label": payload["label"]})
    return f"{used}|{payload['api_key']}"


# -- the type discriminator under redaction (KAN-520, ADR-0031) --------------------


@dataclass
class WrApproved:
    reason: str


@dataclass
class WrRejected:
    """Structurally identical to :class:`WrApproved`: only the discriminator separates them."""

    reason: str


@task()
async def wr_classify(flag: bool) -> WrApproved | WrRejected:
    EXECUTIONS["wr_classify"] += 1
    return WrApproved(reason="clean") if flag else WrRejected(reason="blocked")


@task()
async def wr_record(verdict: str) -> str:
    EXECUTIONS["wr_record"] += 1
    return f"recorded:{verdict}"


@workflow
async def wr_union_two_step(flag: bool) -> str:
    outcome = await wr_classify(flag)
    return await wr_record(type(outcome).__name__)


@task()
async def wr_bulk(_: str) -> dict[str, str]:
    EXECUTIONS["wr_bulk"] += 1
    return {"api_key": "S" * 300_000}  # over SPILL_THRESHOLD_BYTES once encoded


@workflow
async def wr_spilling(_: Any = None) -> int:
    result = await wr_bulk("x")
    return len(result["api_key"])


# -- helpers ----------------------------------------------------------------------


def raw_payloads(db: Path) -> str:
    """Every stored payload, straight from the file, bypassing the store entirely."""
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT payload_json FROM events ORDER BY run_id, seq").fetchall()
        inbox = conn.execute("SELECT payload_ref FROM event_inbox").fetchall()
    finally:
        conn.close()
    return "".join(r[0] for r in rows) + "".join(r[0] for r in inbox)


def run_payloads(db: Path, run_id: str) -> str:
    """Every stored payload of one run, straight from the file."""
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT payload_json FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
    finally:
        conn.close()
    return "".join(r[0] for r in rows)


def blob_bytes(data_dir: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted((data_dir / "blobs").glob("*.blob")))


# -- the mode is off by default ---------------------------------------------------


async def test_write_redaction_is_off_by_default(temp_db_path: Path) -> None:
    """Read-time stays the local default: the store keeps the raw value (ADR-0009)."""
    store = SQLiteStore.open(temp_db_path)
    assert store.write_redaction_enabled is False

    handle = start(wr_two_step, "acct-1", store=store)
    assert await handle.result() == "used:acct-1"
    store.close()

    assert SECRET in raw_payloads(temp_db_path)


async def test_read_time_redaction_is_unchanged_when_the_mode_is_off(
    temp_db_path: Path,
) -> None:
    """The read API still scrubs the response even though the store holds the value."""
    store = SQLiteStore.open(temp_db_path)
    handle = start(wr_two_step, "acct-1", store=store)
    await handle.result()

    view = json.dumps(await ReadAPI(store).timeline(handle.run_id))
    assert SECRET not in view
    assert REDACTED in view
    store.close()

    assert SECRET in raw_payloads(temp_db_path)  # ...but only the response was protected


# -- the mode is on ---------------------------------------------------------------


async def test_the_secret_never_reaches_the_store(temp_db_path: Path) -> None:
    """The headline: with the mode on, the value is not in `satay.db` at all."""
    store = SQLiteStore.open(temp_db_path, write_redaction="on")
    assert store.write_redaction_enabled is True

    handle = start(wr_two_step, "acct-1", store=store)
    assert await handle.result() == "used:acct-1"
    store.close()

    stored = raw_payloads(temp_db_path)
    assert SECRET not in stored
    assert REDACTED in stored


async def test_the_env_var_turns_it_on_for_every_store(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(WRITE_REDACTION_ENV_VAR, "on")
    store = SQLiteStore.open(temp_db_path)
    assert store.write_redaction_enabled is True

    handle = start(wr_two_step, "acct-1", store=store)
    await handle.result()
    store.close()

    assert SECRET not in raw_payloads(temp_db_path)


async def test_structural_fields_survive_so_the_journal_is_still_readable(
    temp_db_path: Path,
) -> None:
    """Redaction is slot-scoped: identity and bookkeeping fields are untouched."""
    store = SQLiteStore.open(temp_db_path, write_redaction="on")
    handle = start(wr_two_step, "acct-1", store=store)
    await handle.result()

    events = list(await store.read_events(handle.run_id))
    scheduled = [e for e in events if e.type is EventType.TASK_SCHEDULED]
    assert [e.payload["task_name"] for e in scheduled] == [
        "wr_issue_credentials",
        "wr_use_credentials",
    ]
    assert [e.payload["ordinal"] for e in scheduled] == [0, 0]
    created = events[0]
    assert created.payload["workflow_name"] == "wr_two_step"
    assert created.payload["code_version"]
    # The value slot is what changed.
    completed = next(
        e
        for e in events
        if e.type is EventType.TASK_COMPLETED and e.payload["task_name"] == "wr_issue_credentials"
    )
    assert completed.payload["output_ref"] == {"api_key": REDACTED, "label": "acct-1"}
    store.close()


# -- replay: the part that must not break -----------------------------------------


async def test_crash_and_resume_against_a_write_redacted_journal(temp_db_path: Path) -> None:
    """A run crashes mid-flight and resumes correctly off a redacted journal.

    Nondeterminism detection is strict by default (ADR-0022), so a resume that resolved a
    different call than the journal recorded would raise rather than reach the assertion
    below. Reuse is proven by the execution-count marker and the single ``TaskCompleted``.
    """
    store = SQLiteStore.open(temp_db_path, write_redaction="on")
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")  # die right after the first task commits

    handle = start(wr_two_step, "acct-1", store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    assert EXECUTIONS["wr_issue_credentials"] == 1
    assert EXECUTIONS["wr_use_credentials"] == 0

    resumed = start(wr_two_step, "acct-1", run_id=handle.run_id, store=store)
    assert await resumed.result() == "used:acct-1"

    # First task reused (not re-executed) from the redacted record; second one ran.
    assert EXECUTIONS["wr_issue_credentials"] == 1
    assert EXECUTIONS["wr_use_credentials"] == 1

    events = list(await store.read_events(handle.run_id))
    completions = [
        e
        for e in events
        if e.type is EventType.TASK_COMPLETED and e.payload["task_name"] == "wr_issue_credentials"
    ]
    assert len(completions) == 1
    assert any(e.type is EventType.WORKFLOW_RESUMED for e in events)
    store.close()

    assert SECRET not in raw_payloads(temp_db_path)


async def test_fan_out_identity_survives_a_pattern_set_aimed_at_it(temp_db_path: Path) -> None:
    """The slot-scoping guarantee, end to end: ``key`` is identity, never a value.

    A whole-payload redactor with these patterns would mask every map item's ``key`` to
    the same placeholder, collapsing three durable identities into one. Slot scoping means
    the run completes, and resumes mid-fan-out reusing exactly the finished items.
    """
    # A custom set *replaces* the defaults, so "token" is carried over deliberately —
    # the rest are aimed squarely at the fields durable identity is derived from.
    hostile = Redactor(patterns=["token", "key", "name", "identity"])
    store = SQLiteStore.open(temp_db_path, write_redaction="on", redactor=hostile)
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")

    handle = start(wr_fan_out, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    assert [EXECUTIONS[f"wr_draft:{i}"] for i in ("a", "b", "c")] == [1, 0, 0]

    resumed = start(wr_fan_out, run_id=handle.run_id, store=store)
    assert await resumed.result() == ["a", "b", "c"]  # input order, all three items

    # Every item executed exactly once across the crash: identities matched on resume.
    assert [EXECUTIONS[f"wr_draft:{i}"] for i in ("a", "b", "c")] == [1, 1, 1]

    events = await store.read_events(handle.run_id)
    keys = sorted(
        e.payload["key"]
        for e in events
        if e.type is EventType.TASK_COMPLETED and "key" in e.payload
    )
    assert keys == ["a", "b", "c"]  # three distinct keys, none of them the placeholder
    store.close()

    assert SECRET not in raw_payloads(temp_db_path)


async def test_a_redacted_workflow_input_warns_and_the_run_resumes_from_the_placeholder(
    temp_db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The documented sharp edge (ADR-0029 decisions 4 and 5): the seed is redacted too.

    The first drive still sees the caller's live value — redaction is about the record.
    The wake after the park re-enters the workflow from the *journal*, and that is the
    redacted form, so the run genuinely resumes against the placeholder.
    """
    clock = ManualClock()
    store = SQLiteStore.open(temp_db_path, write_redaction="on")
    with caplog.at_level(logging.WARNING, logger="satay"):
        handle = start(wr_seeded, {"api_key": SECRET}, store=store, clock=clock)
        assert await handle.result() is PARKED  # parked on the sleep
    assert await handle.status() == RunStatus.WAITING.value

    warnings = [r.getMessage() for r in caplog.records if "write_redaction" in r.getMessage()]
    assert len(warnings) == 1
    assert handle.run_id in warnings[0]

    clock.advance(120.0)
    assert await TimerEventWorker(store=store, clock=clock).tick() == 1
    assert await handle.result() == f"used:{REDACTED}"
    store.close()

    assert SECRET not in raw_payloads(temp_db_path)


async def test_no_warning_when_the_workflow_input_holds_nothing_sensitive(
    temp_db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = SQLiteStore.open(temp_db_path, write_redaction="on")
    with caplog.at_level(logging.WARNING, logger="satay"):
        handle = start(wr_two_step, "acct-1", store=store)
        await handle.result()
    assert not [r for r in caplog.records if "write_redaction" in r.getMessage()]
    store.close()


async def test_a_collected_failure_keeps_its_error_intact_across_replay(
    temp_db_path: Path,
) -> None:
    """`TaskFailed`'s error is not a value slot, and must not become one (ADR-0027).

    A collect-mode failure is recorded and then read back on replay, so the recorded
    `error_type` is what a workflow branching on the collected error sees on every pass.
    Redacting it would manufacture a first-pass-versus-replay divergence — the exact bug
    ADR-0027 exists to prevent — so the error rides through untouched even under a
    pattern set aimed at its field names.
    """
    hostile = Redactor(patterns=["api_key", "type", "message", "traceback"])
    store = SQLiteStore.open(temp_db_path, write_redaction="on", redactor=hostile)
    injector = FaultInjector()
    injector.crash_after("TaskFailed")  # die once the failure is durably recorded

    handle = start(wr_collect, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()

    resumed = start(wr_collect, run_id=handle.run_id, store=store)
    assert await resumed.result() == ["ok", "ValueError"]  # the replayed error, verbatim
    # Both items ran exactly once: the completion and the failure were both replay hits.
    assert [EXECUTIONS[f"wr_maybe_fail:{i}"] for i in ("ok", "bad")] == [1, 1]

    events = await store.read_events(handle.run_id)
    failed = next(e for e in events if e.type is EventType.TASK_FAILED)
    assert failed.payload["key"] == "bad"  # identity untouched
    assert failed.payload["error"]["type"] == "ValueError"  # ...and so is the error
    assert failed.payload["error"]["message"] == "could not reach the provider"
    # The task's *output* slot is still redacted for the item that succeeded.
    completed = next(e for e in events if e.type is EventType.TASK_COMPLETED)
    assert completed.payload["output_ref"] == {"id": "ok", "api_key": REDACTED}
    store.close()

    assert SECRET not in raw_payloads(temp_db_path)


async def test_a_union_typed_result_resolves_to_the_right_arm_across_a_redacted_resume(
    temp_db_path: Path,
) -> None:
    """The KAN-520 discriminator survives the default write-time pattern set.

    The recorded ``type`` qualname lives *inside* an ``output_ref`` value slot, so
    ``redact_value_slots`` does walk over it — but it is matched by **field name**, and
    neither ``$satay``, ``type`` nor ``fields`` is in the default pattern list. The
    resumed run therefore rehydrates the same arm the first pass produced, for two arms
    that nothing structural can tell apart.
    """
    store = SQLiteStore.open(temp_db_path, write_redaction="on")
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")

    handle = start(wr_union_two_step, False, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    assert EXECUTIONS["wr_classify"] == 1

    resumed = start(wr_union_two_step, False, run_id=handle.run_id, store=store)
    assert await resumed.result() == "recorded:WrRejected"  # not WrApproved, not a dict
    assert EXECUTIONS["wr_classify"] == 1  # reused from the journal, not re-run
    store.close()


async def test_a_pattern_set_that_masks_the_discriminator_fails_loudly_on_resume(
    temp_db_path: Path,
) -> None:
    """A masked discriminator must raise, never silently pick the other arm.

    ``Redactor(["type"])`` is hostile on purpose: it reaches inside the ``output_ref``
    slot and replaces the recorded qualname with the placeholder. That is a real (if
    unusual) configuration, and the ADR-0029 rule for it is the ADR-0027 rule inverted —
    the discriminator is *preferred*, never required, so losing it costs exactness, not
    correctness. With two indistinguishable arms there is no exact answer left, so the
    resume fails naming the cause instead of returning a ``WrApproved`` where the first
    pass produced a ``WrRejected``.
    """
    hostile = Redactor(patterns=["type"])
    store = SQLiteStore.open(temp_db_path, write_redaction="on", redactor=hostile)
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")

    handle = start(wr_union_two_step, False, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()

    resumed = start(wr_union_two_step, False, run_id=handle.run_id, store=store)
    with pytest.raises(WorkflowFailedError) as excinfo:
        await resumed.result()
    assert "DecodeError" in str(excinfo.value)
    assert "masked by write-time redaction" in str(excinfo.value)
    assert EXECUTIONS["wr_record"] == 0  # nothing downstream ran on a guessed value

    record = await store.get_run(handle.run_id)
    assert record is not None and record.status is RunStatus.FAILED
    store.close()


# -- fork (ADR-0028) --------------------------------------------------------------


async def test_forking_a_write_redacted_run_stays_redacted(temp_db_path: Path) -> None:
    store = SQLiteStore.open(temp_db_path, write_redaction="on")
    source = start(wr_two_step, "acct-1", store=store)
    assert await source.result() == "used:acct-1"

    forked = await fork(source.run_id, before_task="wr_use_credentials", store=store)
    assert await forked.result() == "used:acct-1"
    store.close()

    assert SECRET not in raw_payloads(temp_db_path)


async def test_a_fork_input_override_is_redacted_on_the_way_in(
    temp_db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`workflow_input=` is written into the fork's journal, so it goes through redaction.

    Also pins `RunForked.source_input_ref` — the input the override replaced. It is a
    value slot by the suffix rule and would have been missed by a hand-maintained list,
    which is the whole reason the rule is a suffix.
    """
    other_secret = "sk-live-a-different-secret"

    # The source run predates the mode, so its journal holds the raw value.
    plain = SQLiteStore.open(temp_db_path)
    source = start(wr_from_input, {"api_key": SECRET, "label": "a"}, store=plain)
    assert await source.result() == f"used:a|{SECRET}"
    plain.close()
    assert SECRET in raw_payloads(temp_db_path)

    # Reopen with the mode on and fork it under a new input.
    store = SQLiteStore.open(temp_db_path, write_redaction="on")
    with caplog.at_level(logging.WARNING, logger="satay"):
        forked = await fork(
            source.run_id,
            before_task="wr_use_credentials",
            workflow_input={"api_key": other_secret, "label": "b"},
            store=store,
        )
        # The fork re-enters from its own recorded input, which is now the placeholder.
        assert await forked.result() == f"used:b|{REDACTED}"

    warnings = [r.getMessage() for r in caplog.records if "write_redaction" in r.getMessage()]
    assert len(warnings) == 1
    assert forked.run_id in warnings[0]

    events = list(await store.read_events(forked.run_id))
    created = next(e for e in events if e.type is EventType.WORKFLOW_CREATED)
    assert created.payload["input_ref"] == {"api_key": REDACTED, "label": "b"}
    lineage = next(e for e in events if e.type is EventType.RUN_FORKED)
    assert lineage.payload["input_overridden"] is True
    assert lineage.payload["source_input_ref"] == {"api_key": REDACTED, "label": "a"}
    store.close()

    # Neither secret is anywhere in the fork's own events.
    forked_payloads = run_payloads(temp_db_path, forked.run_id)
    assert SECRET not in forked_payloads
    assert other_secret not in forked_payloads


# -- the other two write paths ----------------------------------------------------


async def test_a_delivered_event_is_redacted_in_the_inbox_and_the_journal(
    temp_db_path: Path,
) -> None:
    """``send_event`` writes its own column; it is a value slot like any other."""
    store = SQLiteStore.open(temp_db_path, write_redaction="on")
    await send_event(WrApproval(api_key=SECRET, note="ok"), key="wr-1", store=store)

    handle = start(wr_await_approval, store=store)
    assert await handle.result() == f"{REDACTED}|ok"
    store.close()

    stored = raw_payloads(temp_db_path)  # covers both `events` and `event_inbox`
    assert SECRET not in stored
    assert REDACTED in stored


async def test_redaction_runs_before_spill_so_no_blob_holds_the_secret(tmp_path: Path) -> None:
    """Order matters: encode → redact → spill (ADR-0029 decision 3).

    A payload over ``SPILL_THRESHOLD_BYTES`` normally lands in a content-addressed blob
    file. Redacting after the spill would leave the real bytes on disk under a hash, out
    of reach of anything the read path filters — so redaction runs first, and the
    placeholder is small enough that the value never spills at all.
    """
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    off = SQLiteStore.open(plain_dir / "satay.db")
    assert await start(wr_spilling, store=off).result() == 300_000
    off.close()
    assert b"S" * 300_000 in blob_bytes(plain_dir)  # baseline: spilled, verbatim

    redacted_dir = tmp_path / "redacted"
    redacted_dir.mkdir()
    on = SQLiteStore.open(redacted_dir / "satay.db", write_redaction="on")
    handle = start(wr_spilling, store=on)
    # The *first* execution still computes from the live value — redaction is about the
    # record, not the running program.
    assert await handle.result() == 300_000
    events = await on.read_events(handle.run_id)
    completed = next(e for e in events if e.type is EventType.TASK_COMPLETED)
    assert completed.payload["output_ref"] == {"api_key": REDACTED}
    on.close()

    assert blob_bytes(redacted_dir) == b""  # nothing spilled: no blob to leak
    assert "blobref" not in raw_payloads(redacted_dir / "satay.db")
