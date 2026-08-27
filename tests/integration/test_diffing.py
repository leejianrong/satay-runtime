"""``satay.diff``: call-by-call compare of two runs (ADR-0034).

Driven through the public API against a temp SQLite store (ADR-0011). The scenario that
matters is the ADR-0025 wedge: fork a run under a changed input, drive the fork, and read
off exactly which call diverged and which field of it.
"""

from __future__ import annotations

import pytest

import satay
from satay.journal.store import SQLiteStore
from satay.redaction import REDACTED, Redactor
from satay.valuediff import ROOT


@satay.task()
async def dif_research(topic: str) -> dict[str, object]:
    return {"topic": topic, "findings": ["a", "b"]}


@satay.task()
async def dif_synthesize(found: dict[str, object], style: str) -> dict[str, object]:
    return {"summary": f"[{style}] report", "count": len(found["findings"])}  # type: ignore[arg-type]


@satay.workflow
async def dif_dossier(brief: dict[str, str]) -> dict[str, object]:
    found = await dif_research(brief["topic"])
    return await dif_synthesize(found, brief["style"])


async def test_forking_under_a_changed_input_shows_which_field_diverged() -> None:
    """The headline: not *that* the fork differs, but where.

    ``dif_research`` is replayed off the journal and must be identical; ``dif_synthesize``
    re-executes under the new style, so its second positional argument and the ``summary``
    field of its output differ — while ``count``, which the change does not touch, does not.
    """
    store = SQLiteStore.open(":memory:")
    source = satay.start(dif_dossier, {"topic": "acme", "style": "dry"}, store=store)
    await source.result()

    forked = await satay.fork(
        source.run_id,
        before_task="dif_synthesize",
        workflow_input={"topic": "acme", "style": "sceptical"},
        store=store,
    )
    await forked.result()

    result = await satay.diff(source.run_id, forked.run_id, store=store)
    assert result.a_run_id == source.run_id
    assert result.b_run_id == forked.run_id

    by_identity = {call.identity: call for call in result.calls}
    replayed = by_identity["dif_research:0"]
    assert replayed.changed is False
    assert replayed.aligned is True
    assert replayed.args is not None and replayed.args.changed is False

    diverged = by_identity["dif_synthesize:0"]
    assert diverged.changed is True
    assert diverged.args is not None
    assert diverged.args.paths == ("[1]",), "the style is the second positional argument"
    assert diverged.output is not None
    assert diverged.output.paths == (".summary",), "count is unchanged and must not appear"

    assert [call.identity for call in result.changed] == ["dif_synthesize:0"]
    store.close()


@satay.task()
async def dif_extra(value: int) -> int:
    return value


async def test_a_call_only_one_run_made_is_unaligned_and_uncomparable() -> None:
    """An absent side is the difference, and there is nothing to diff against."""
    store = SQLiteStore.open(":memory:")

    @satay.workflow
    async def dif_one(value: int) -> int:
        return await dif_extra(value)

    @satay.workflow
    async def dif_two(value: int) -> int:
        first = await dif_extra(value)
        return await dif_extra(first)

    a = satay.start(dif_one, 1, store=store)
    await a.result()
    b = satay.start(dif_two, 1, store=store)
    await b.result()

    result = await satay.diff(a.run_id, b.run_id, store=store)
    by_identity = {call.identity: call for call in result.calls}

    shared = by_identity["dif_extra:0"]
    assert shared.aligned is True
    assert shared.changed is False

    only_b = by_identity["dif_extra:1"]
    assert only_b.aligned is False
    assert only_b.changed is True
    assert only_b.a is None
    assert only_b.b is not None
    assert only_b.args is None, "nothing to compare against, so no value diff"
    assert only_b.output is None
    store.close()


async def test_diffing_a_run_against_itself_reports_no_change() -> None:
    store = SQLiteStore.open(":memory:")
    handle = satay.start(dif_dossier, {"topic": "acme", "style": "dry"}, store=store)
    await handle.result()

    result = await satay.diff(handle.run_id, handle.run_id, store=store)
    assert result.calls
    assert all(call.changed is False for call in result.calls)
    assert result.changed == ()
    store.close()


async def test_unknown_run_raises_lookup_error() -> None:
    store = SQLiteStore.open(":memory:")
    handle = satay.start(dif_dossier, {"topic": "a", "style": "b"}, store=store)
    await handle.result()
    with pytest.raises(LookupError):
        await satay.diff(handle.run_id, "no-such-run", store=store)
    with pytest.raises(LookupError):
        await satay.diff("no-such-run", handle.run_id, store=store)
    store.close()


async def test_sides_carry_the_recorded_call_so_values_are_readable() -> None:
    """`CallDiff.a`/`.b` reuse `RecordedCall`, so a diff is also a read."""
    store = SQLiteStore.open(":memory:")
    handle = satay.start(dif_dossier, {"topic": "acme", "style": "dry"}, store=store)
    await handle.result()

    result = await satay.diff(handle.run_id, handle.run_id, store=store)
    call = next(c for c in result.calls if c.identity == "dif_research:0")
    assert call.a is not None
    assert call.a.args == ("acme",)
    assert call.a.output == {"topic": "acme", "findings": ["a", "b"]}
    assert call.a.status == "completed"
    store.close()


# --- redaction: the headline correctness claim -----------------------------------------


@satay.task()
async def dif_issue(user: str) -> dict[str, str]:
    return {"session_token": f"sk-live-{user}", "user": user}


@satay.workflow
async def dif_issues(user: str) -> dict[str, str]:
    return await dif_issue(user)


async def test_two_different_secrets_are_reported_as_differing_though_both_come_back_masked() -> (
    None
):
    """Why the diff is computed before redaction rather than after.

    Both runs' outputs carry a ``session_token``, and both come back ``***REDACTED***`` —
    so a diff computed *after* redaction would compare two identical sentinels and report
    the calls identical, which is false. Computing before redaction and emitting only the
    *path* gets the right answer without disclosing either value.
    """
    store = SQLiteStore.open(":memory:")
    a = satay.start(dif_issues, "ada", store=store)
    await a.result()
    b = satay.start(dif_issues, "grace", store=store)
    await b.result()

    result = await satay.diff(a.run_id, b.run_id, store=store)
    (call,) = [c for c in result.calls if c.identity == "dif_issue:0"]

    assert call.output is not None
    assert call.output.changed is True
    assert ".session_token" in call.output.paths, "the differing secret must be located"
    assert call.output.redacted is False, "read-time masking does not make equality unknown"

    # ...and the values themselves are still masked in what the caller receives.
    assert call.a is not None and call.b is not None
    assert call.a.output["session_token"] == REDACTED
    assert call.b.output["session_token"] == REDACTED
    store.close()


async def test_a_journal_masked_value_is_reported_as_unknown_not_identical() -> None:
    """Write-time redaction (ADR-0029) puts the sentinel in the journal itself.

    Then the cleartext is gone at every layer and no comparison is possible, so the diff
    says so via ``redacted`` instead of quietly claiming the two runs agree.
    """
    store = SQLiteStore.open(":memory:", write_redaction="on")
    a = satay.start(dif_issues, "ada", store=store)
    await a.result()
    b = satay.start(dif_issues, "grace", store=store)
    await b.result()

    result = await satay.diff(a.run_id, b.run_id, store=store)
    (call,) = [c for c in result.calls if c.identity == "dif_issue:0"]

    assert call.output is not None
    assert call.output.redacted is True
    assert ".session_token" not in call.output.paths
    store.close()


async def test_a_caller_supplied_redactor_does_not_change_the_paths() -> None:
    """Paths are computed before any redactor runs, so they are redactor-independent."""
    store = SQLiteStore.open(":memory:")
    a = satay.start(dif_issues, "ada", store=store)
    await a.result()
    b = satay.start(dif_issues, "grace", store=store)
    await b.result()

    wide = await satay.diff(a.run_id, b.run_id, store=store, redactor=Redactor(patterns=["user"]))
    (call,) = [c for c in wide.calls if c.identity == "dif_issue:0"]
    assert call.output is not None
    assert ".session_token" in call.output.paths
    assert ".user" in call.output.paths
    assert call.a is not None
    assert call.a.output["user"] == REDACTED
    store.close()


# --- child workflows ------------------------------------------------------------------


@satay.workflow
async def dif_child(value: int) -> int:
    return await dif_extra(value)


@satay.workflow
async def dif_parent(value: int) -> int:
    handle = await satay.start_child(dif_child, value)
    result: int = await handle.result()
    return result


async def test_child_workflow_calls_appear_in_the_diff() -> None:
    """Compare was blind to ``start_child`` entirely, so a diverging child read as no change.

    ``_scan_tasks`` sees only the four ``TASK_*`` events, so the compare view never carried
    a child call. Two parents whose children received different inputs must now differ.
    """
    store = SQLiteStore.open(":memory:")
    a = satay.start(dif_parent, 1, store=store)
    await a.result()
    b = satay.start(dif_parent, 2, store=store)
    await b.result()

    result = await satay.diff(a.run_id, b.run_id, store=store)
    child_calls = [c for c in result.calls if c.a is not None and c.a.child_run_id is not None]
    assert child_calls, "no child workflow call in the diff"

    (child,) = child_calls
    assert child.changed is True
    assert child.args is not None
    assert child.args.paths == ("[0]",), "the child's single input is argument 0"
    assert child.output is not None and child.output.paths == (ROOT,)
    store.close()
