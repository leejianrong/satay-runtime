"""Read-view builders: the journal-derived JSON contract V6/Studio consumes (N16).

Pure functions over a :class:`~satay.journal.Store`. Reads go **direct to the store**
and never touch live worker state (ADR-0009/0012); the views reconstruct everything
from the append-only journal plus the V4 tree linkage and the V2 usage slot.

The contract is **additive and forward-compatible** (ADR-0018): each view emits a
stable set of fields, and consumers assert on the fields they need while tolerating
extras that V2/V4/V7 layer on (the usage slot, tree linkage, a future
version-mismatch field, ``RunForked`` lineage). Nothing here imports FastAPI — the
builders sit below the HTTP surface so the core-dependency boundary holds.

Every builder raises :class:`RunNotFoundError` for an unknown run so the HTTP layer
can map it to a 404. Redaction is applied *on top* of these raw views by
:class:`satay.control.api.ReadAPI`; the builders themselves return unredacted data so
they can be unit-tested for structure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from satay.journal import Store
from satay.journal.codec import decode
from satay.journal.events import Event, EventType, RunRecord, RunStatus

#: Task-lifecycle event types that carry a durable-call identity in their payload.
_TASK_EVENTS = frozenset(
    {
        EventType.TASK_SCHEDULED,
        EventType.TASK_ATTEMPT_STARTED,
        EventType.TASK_ATTEMPT_FAILED,
        EventType.TASK_COMPLETED,
    }
)


class RunNotFoundError(LookupError):
    """Raised by a read view when the requested run id is unknown (HTTP 404)."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id!r} not found")
        self.run_id = run_id


def call_identity(payload: Mapping[str, Any]) -> str:
    """Return the stable identity string for a task-lifecycle event payload.

    Keyed fan-out items identify by ``{task_name}:key:{key}``; ordinary call-site
    calls by ``{task_name}:{ordinal}``. This is the token used in the
    ``/tasks/{identity}`` path and as the compare-alignment key. Task names and keys
    never contain ``:`` in practice (function names / ``item-N`` keys), so the encoding
    is unambiguous and URL-path-safe (no ``#`` fragment character).
    """
    task_name = payload["task_name"]
    if "key" in payload and payload.get("key") is not None:
        return f"{task_name}:key:{payload['key']}"
    return f"{task_name}:{payload.get('ordinal')}"


def _version_mismatch(record: RunRecord, current_version: str) -> dict[str, Any]:
    """The additive version-mismatch field the U8 banner reads (N17, ADR-0018).

    Compares a run's *stamped* code version against the *current* process version; the
    banner renders when ``mismatch`` is true. Additive and tolerated by existing
    view-models (ADR-0018).
    """
    from satay.versioning import is_version_mismatch

    return {
        "stamped": record.code_version,
        "current": current_version,
        "mismatch": is_version_mismatch(record.code_version, current_version),
    }


def _fork_lineage(events: Sequence[Event]) -> dict[str, Any] | None:
    """The run's own fork record, or ``None`` if it was not forked (N15, ADR-0004).

    A run's *own* lineage is the ``RunForked`` event with the greatest ``seq``: a
    fork-of-a-fork copies its ancestor's ``RunForked`` into its seeded prefix (lower
    ``seq``), while the fork operation appends this run's own record last, so following
    ``source_run_id`` from the max-``seq`` ``RunForked`` walks a correct lineage chain.
    """
    forked = [e for e in events if e.type is EventType.RUN_FORKED]
    if not forked:
        return None
    own = max(forked, key=lambda e: e.seq)
    return {
        "source_run_id": own.payload.get("source_run_id"),
        "fork_point_seq": own.payload.get("fork_point_seq"),
    }


def _run_summary(
    record: RunRecord,
    current_version: str,
    *,
    forked_from: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The run-list / compare-side summary fields for a run record.

    Carries the V7 additive fields (``version_mismatch``, ``forked_from``) alongside the
    V1 summary; consumers read only what they need and tolerate the extras (ADR-0018).
    """
    return {
        "run_id": record.run_id,
        "workflow_name": record.workflow_name,
        "status": record.status.value,
        "code_version": record.code_version,
        "created_at": record.created_at.isoformat(),
        "idempotency_key": record.idempotency_key,
        "version_mismatch": _version_mismatch(record, current_version),
        "forked_from": forked_from,
    }


async def run_list(store: Store) -> dict[str, Any]:
    """``GET /runs`` — id, status, code version, start time, and V7 lineage per run."""
    from satay import versioning

    current = versioning.current_code_version()
    runs: list[dict[str, Any]] = []
    for run_id in await store.list_runs():
        record = await store.get_run(run_id)
        if record is not None:
            events = await store.read_events(run_id)
            runs.append(_run_summary(record, current, forked_from=_fork_lineage(events)))
    return {"runs": runs}


async def _require_run(store: Store, run_id: str) -> RunRecord:
    record = await store.get_run(run_id)
    if record is None:
        raise RunNotFoundError(run_id)
    return record


def _is_interruption(event: Event) -> bool:
    """A ``WorkflowResumed`` marks recovery from an interruption (⚡, ADR-0009/Q52)."""
    return event.type is EventType.WORKFLOW_RESUMED


async def timeline(store: Store, run_id: str) -> dict[str, Any]:
    """``GET /runs/{id}/timeline`` — the ordered event stream as JSON.

    Carries the V7 additive fields ``version_mismatch`` (the U8 banner's data source)
    and ``forked_from`` (this run's lineage) alongside the V3 event stream (ADR-0018).
    """
    from satay import versioning

    record = await _require_run(store, run_id)
    events = await store.read_events(run_id)
    return {
        "run_id": run_id,
        "workflow_name": record.workflow_name,
        "status": record.status.value,
        "interrupted": any(_is_interruption(e) for e in events),
        "version_mismatch": _version_mismatch(record, versioning.current_code_version()),
        "forked_from": _fork_lineage(events),
        "events": [
            {
                "seq": e.seq,
                "event_id": e.event_id,
                "type": e.type.value,
                "ts": e.ts.isoformat(),
                "is_interruption": _is_interruption(e),
                "payload": dict(e.payload),
            }
            for e in events
        ],
    }


def _task_status(
    identity: str,
    completed: set[str],
    exhausted_failures: set[str],
    run_failed: bool,
) -> str:
    if identity in completed:
        return "completed"
    if identity in exhausted_failures or (run_failed and identity not in completed):
        return "failed"
    return "running"


def _scan_tasks(events: Sequence[Event]) -> dict[str, dict[str, Any]]:
    """Group task-lifecycle events by durable-call identity, in first-seen order."""
    tasks: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.type not in _TASK_EVENTS:
            continue
        identity = call_identity(e.payload)
        info = tasks.get(identity)
        if info is None:
            info = {
                "identity": identity,
                "task_name": e.payload["task_name"],
                "map_group": None,
                "first_seq": e.seq,
                "attempts": 0,
                "completed": False,
                "exhausted": False,
            }
            if "key" in e.payload and e.payload.get("key") is not None:
                info["key"] = e.payload["key"]
            else:
                info["ordinal"] = e.payload.get("ordinal")
            tasks[identity] = info
        if e.type is EventType.TASK_SCHEDULED and e.payload.get("map_group") is not None:
            info["map_group"] = e.payload["map_group"]
        if e.type is EventType.TASK_ATTEMPT_STARTED:
            info["attempts"] = max(info["attempts"], int(e.payload.get("attempt", 1)))
        if e.type is EventType.TASK_ATTEMPT_FAILED:
            info["attempts"] = max(info["attempts"], int(e.payload.get("attempt", 1)))
            if e.payload.get("next_delay") is None:
                info["exhausted"] = True
        if e.type is EventType.TASK_COMPLETED:
            info["completed"] = True
    return tasks


async def tree(store: Store, run_id: str) -> dict[str, Any]:
    """``GET /runs/{id}/tree`` — parent/child + map-item structure (V4 linkage)."""
    record = await _require_run(store, run_id)
    return await _build_tree(store, run_id, record)


async def _build_tree(store: Store, run_id: str, record: RunRecord) -> dict[str, Any]:
    events = await store.read_events(run_id)
    run_failed = record.status.value == "failed"
    tasks = _scan_tasks(events)

    completed = {i for i, t in tasks.items() if t["completed"]}
    exhausted = {i for i, t in tasks.items() if t["exhausted"]}

    def task_node(info: dict[str, Any]) -> dict[str, Any]:
        node: dict[str, Any] = {
            "kind": "task",
            "identity": info["identity"],
            "task_name": info["task_name"],
            "status": _task_status(info["identity"], completed, exhausted, run_failed),
            "attempts": info["attempts"],
        }
        if "key" in info:
            node["key"] = info["key"]
        else:
            node["ordinal"] = info["ordinal"]
        return node

    # Order every logical call (standalone task, map group, or child) by first seq.
    ordered: list[tuple[int, dict[str, Any]]] = []
    map_nodes: dict[str, dict[str, Any]] = {}

    for info in tasks.values():
        group = info["map_group"]
        if group is None:
            ordered.append((info["first_seq"], task_node(info)))
        else:
            node = map_nodes.get(group)
            if node is None:
                node = {
                    "kind": "map",
                    "group": group,
                    "task_name": info["task_name"],
                    "items": [],
                    "_first_seq": info["first_seq"],
                }
                map_nodes[group] = node
                ordered.append((info["first_seq"], node))
            node["items"].append(task_node(info))

    for node in map_nodes.values():
        node.pop("_first_seq", None)
        node["items"].sort(key=lambda item: str(item.get("key", "")))
        statuses = {item["status"] for item in node["items"]}
        node["status"] = (
            "failed"
            if "failed" in statuses
            else "completed"
            if statuses == {"completed"}
            else "running"
        )

    # Child workflows: recurse into the linked child's own tree (V4 linkage).
    for e in events:
        if e.type is EventType.CHILD_WORKFLOW_SCHEDULED:
            child_run_id = e.payload["child_run_id"]
            child_record = await store.get_run(child_run_id)
            child_node: dict[str, Any] = {
                "kind": "child",
                "identity": call_identity(e.payload) if "task_name" in e.payload else child_run_id,
                "workflow_name": e.payload.get("workflow_name"),
                "child_run_id": child_run_id,
                "status": child_record.status.value if child_record is not None else "unknown",
            }
            if child_record is not None:
                child_node["tree"] = await _build_tree(store, child_run_id, child_record)
            ordered.append((e.seq, child_node))

    ordered.sort(key=lambda pair: pair[0])
    return {
        "run_id": run_id,
        "workflow_name": record.workflow_name,
        "status": record.status.value,
        "nodes": [node for _, node in ordered],
    }


async def task_detail(store: Store, run_id: str, identity: str) -> dict[str, Any]:
    """``GET /runs/{id}/tasks/{identity}`` — a logical task and its physical attempts.

    Groups the attempts of one logical task with its input, output, per-attempt error /
    retry-delay / duration / usage, and — when the run failed — the native traceback
    recorded on ``WorkflowFailed``.

    The recorded model-usage slot (V2) appears twice, deliberately: on each attempt that
    reported any, and totalled in ``usage`` for the logical task. The total includes
    **failed** attempts, since the provider billed those too, so a task that never
    completed still prices itself (KAN-479).
    """
    record = await _require_run(store, run_id)
    events = await store.read_events(run_id)

    matching = [
        e for e in events if e.type in _TASK_EVENTS and call_identity(e.payload) == identity
    ]
    if not matching:
        raise RunNotFoundError(f"{run_id}/{identity}")

    head = matching[0].payload
    detail: dict[str, Any] = {
        "run_id": run_id,
        "identity": identity,
        "task_name": head["task_name"],
        "status": "running",
        "input": None,
        "output": None,
        "usage": [],
        "attempts": [],
    }
    if "key" in head and head.get("key") is not None:
        detail["key"] = head["key"]
    else:
        detail["ordinal"] = head.get("ordinal")

    attempts: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for e in matching:
        p = e.payload
        if e.type is EventType.TASK_SCHEDULED and "input_ref" in p:
            detail["input"] = decode(p["input_ref"])
        elif e.type is EventType.TASK_ATTEMPT_STARTED:
            current = {
                "attempt": int(p.get("attempt", 1)),
                "status": "running",
                "started_at": e.ts.isoformat(),
                "ended_at": None,
                "duration_seconds": None,
            }
            attempts.append(current)
        elif e.type is EventType.TASK_ATTEMPT_FAILED and current is not None:
            current["status"] = "failed"
            current["error"] = dict(p.get("error", {}))
            current["next_delay"] = p.get("next_delay")
            current["ended_at"] = e.ts.isoformat()
            current["duration_seconds"] = _duration(current["started_at"], e.ts.isoformat())
            _bill(detail, current, p)
        elif e.type is EventType.TASK_COMPLETED and current is not None:
            current["status"] = "completed"
            current["ended_at"] = e.ts.isoformat()
            current["duration_seconds"] = _duration(current["started_at"], e.ts.isoformat())
            if "output_ref" in p:
                detail["output"] = decode(p["output_ref"])
            _bill(detail, current, p)
            detail["status"] = "completed"

    detail["attempts"] = attempts
    if (
        detail["status"] != "completed"
        and record.status.value == "failed"
        and any(a["status"] == "failed" for a in attempts)
    ):
        detail["status"] = "failed"

    # Native stack trace: surface the run-level WorkflowFailed traceback when the run
    # failed (the per-attempt error record carries only type + message).
    for e in reversed(events):
        if e.type is EventType.WORKFLOW_FAILED:
            detail["error"] = dict(e.payload.get("error", {}))
            break

    return detail


async def compare(store: Store, run_id: str, other_run_id: str) -> dict[str, Any]:
    """``GET /runs/{id}/compare?to={other}`` — two runs aligned by durable-call identity.

    Every durable-call identity present in either run becomes one row; each side shows
    that call's status and recorded output (or ``null`` when the identity is absent on
    that side), so a diverging run is read off directly.
    """
    from satay import versioning

    record_a = await _require_run(store, run_id)
    record_b = await _require_run(store, other_run_id)
    current = versioning.current_code_version()

    side_a = await _compare_side(store, run_id, record_a, current)
    side_b = await _compare_side(store, other_run_id, record_b, current)

    identities = sorted(set(side_a["calls"]) | set(side_b["calls"]))
    rows: list[dict[str, Any]] = []
    for identity in identities:
        a = side_a["calls"].get(identity)
        b = side_b["calls"].get(identity)
        rows.append(
            {
                "identity": identity,
                "task_name": (a or b or {}).get("task_name"),
                "a": a,
                "b": b,
                "aligned": a is not None and b is not None,
            }
        )
    return {
        "a": side_a["summary"],
        "b": side_b["summary"],
        "rows": rows,
    }


def _calls_view(events: Sequence[Event], record: RunRecord) -> dict[str, dict[str, Any]]:
    """Assemble every durable call of one run, keyed by identity, in schedule order.

    Pure over the journal, so both the two-run compare and the single-run
    :func:`run_calls` share one assembly rather than two that can drift. Ordering is
    ``_scan_tasks``'s first-seen order, i.e. the order the calls were scheduled — note
    that :func:`compare` deliberately re-sorts its rows by identity for stable alignment,
    while :func:`run_calls` keeps this order.
    """
    tasks = _scan_tasks(events)
    completed = {i for i, t in tasks.items() if t["completed"]}
    exhausted = {i for i, t in tasks.items() if t["exhausted"]}
    run_failed = record.status is RunStatus.FAILED

    # Per-call input / output / timing, so the side-by-side view can mark exactly what a
    # change did (inputs, outputs, attempts, or duration). Additive to the compare
    # contract; the V5 alignment (identity → row, aligned flag) is unchanged.
    inputs: dict[str, Any] = {}
    outputs: dict[str, Any] = {}
    first_start: dict[str, Any] = {}
    last_end: dict[str, Any] = {}
    for e in events:
        if e.type not in _TASK_EVENTS:
            continue
        identity = call_identity(e.payload)
        if e.type is EventType.TASK_SCHEDULED and "input_ref" in e.payload:
            inputs[identity] = decode(e.payload["input_ref"])
        elif e.type is EventType.TASK_ATTEMPT_STARTED:
            first_start.setdefault(identity, e.ts)
        if e.type in (EventType.TASK_COMPLETED, EventType.TASK_ATTEMPT_FAILED):
            last_end[identity] = e.ts
        if e.type is EventType.TASK_COMPLETED and "output_ref" in e.payload:
            outputs[identity] = decode(e.payload["output_ref"])

    calls: dict[str, dict[str, Any]] = {}
    for identity, info in tasks.items():
        duration: float | None = None
        if identity in first_start and identity in last_end:
            duration = (last_end[identity] - first_start[identity]).total_seconds()
        calls[identity] = {
            "task_name": info["task_name"],
            "status": _task_status(identity, completed, exhausted, run_failed),
            "input": inputs.get(identity),
            "output": outputs.get(identity),
            "attempts": info["attempts"],
            "duration_seconds": duration,
        }
    return calls


def _run_outcome(events: Sequence[Event]) -> tuple[Any, dict[str, Any] | None]:
    """The run-level ``(output, error)`` recorded in the journal, neither one raising.

    The runtime's own :func:`satay.api.runner._outcome_from_events` *raises* a failed
    run's recorded error, which is right for ``await handle.result()`` and wrong for a
    read: a reader wants to be told about the failure, not to be interrupted by it.
    """
    for e in reversed(events):
        if e.type is EventType.WORKFLOW_COMPLETED:
            return decode(e.payload["output_ref"]), None
        if e.type is EventType.WORKFLOW_FAILED:
            return None, dict(e.payload["error"])
    return None, None


async def run_calls(store: Store, run_id: str) -> dict[str, Any]:
    """One run's durable calls with their recorded inputs and outputs, in schedule order.

    The single-run half of :func:`compare`, which until now could only be reached by
    supplying a second run id. Backs the Python-level read API (``satay.inspect``,
    KAN-477); it emits raw, **unredacted** data like every other builder here, so its
    callers are responsible for applying a :class:`~satay.redaction.Redactor` exactly as
    :class:`satay.control.api.ReadAPI` does for the HTTP reads.

    Adds the identity fields (``ordinal`` xor ``key``, ``map_group``, ``first_seq``) that
    the compare rows leave out, and the run-level ``output``/``error``.

    Covers tasks **and** child workflows, ordered together by the sequence in which the
    parent scheduled them.

    ``calls`` is a **list** carrying ``identity`` as a field, not a dict keyed by
    identity, and that is load-bearing rather than stylistic. A ``Redactor`` matches
    *field names* by substring, so keying by identity would let a task merely **named**
    ``fetch_secret`` collide with the ``secret`` pattern and have its entire call record
    masked. :func:`compare` is safe from this only incidentally — it flattens each side's
    calls into ``a``/``b`` values before the redactor ever sees them.
    """
    from satay import versioning

    record = await _require_run(store, run_id)
    events = await store.read_events(run_id)
    scanned = _scan_tasks(events)
    calls = []
    for identity, call in _calls_view(events, record).items():
        info = scanned[identity]
        entry = {"identity": identity, **call, "first_seq": info["first_seq"]}
        entry["map_group"] = info["map_group"]
        if "key" in info:
            entry["key"] = info["key"]
        else:
            entry["ordinal"] = info["ordinal"]
        calls.append(entry)

    # Child workflows are durable calls too, and `_scan_tasks` cannot see them: only the
    # four TASK_* events carry a task identity, so a `start_child` call would be silently
    # missing from a read whose whole job is showing what a run recorded. The child's own
    # result lives in the child's journal, not the parent's, so it is read from there —
    # the same linkage `tree` follows.
    for e in events:
        if e.type is not EventType.CHILD_WORKFLOW_SCHEDULED:
            continue
        child_run_id = e.payload["child_run_id"]
        child_record = await store.get_run(child_run_id)
        child_output = None
        if child_record is not None:
            child_output, _ = _run_outcome(await store.read_events(child_run_id))
        child: dict[str, Any] = {
            "identity": call_identity(e.payload) if "task_name" in e.payload else child_run_id,
            "task_name": e.payload.get("workflow_name"),
            "status": child_record.status.value if child_record is not None else "unknown",
            # Wrapped in a list to match the task convention, where `input_ref` holds
            # `encode(list(args))`. A child's `input_ref` is the single input value, and a
            # value that happens to *be* a list would otherwise read back as N arguments.
            "input": [decode(e.payload["input_ref"])] if "input_ref" in e.payload else None,
            "output": child_output,
            # The parent records no attempts for a child; retries belong to the child's own
            # tasks. One scheduling is what the parent's journal actually attests to.
            "attempts": 1,
            "duration_seconds": None,
            "first_seq": e.seq,
            "child_run_id": child_run_id,
            "map_group": None,
        }
        if "key" in e.payload and e.payload.get("key") is not None:
            child["key"] = e.payload["key"]
        else:
            child["ordinal"] = e.payload.get("ordinal")
        calls.append(child)

    calls.sort(key=lambda call: call["first_seq"])
    output, error = _run_outcome(events)
    return {
        "summary": _run_summary(
            record, versioning.current_code_version(), forked_from=_fork_lineage(events)
        ),
        "calls": calls,
        "output": output,
        "error": error,
    }


async def _compare_side(
    store: Store, run_id: str, record: RunRecord, current_version: str
) -> dict[str, Any]:
    events = await store.read_events(run_id)
    return {
        "summary": _run_summary(record, current_version, forked_from=_fork_lineage(events)),
        "calls": _calls_view(events, record),
    }


def _bill(detail: dict[str, Any], attempt: dict[str, Any], payload: Mapping[str, Any]) -> None:
    """Attach an attempt's flushed usage to the attempt and to the logical-task total."""
    usage = list(payload.get("usage", []))
    attempt["usage"] = usage
    detail["usage"].extend(usage)


def _duration(start_iso: str, end_iso: str) -> float:
    from datetime import datetime

    return (datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)).total_seconds()


__all__ = [
    "RunNotFoundError",
    "call_identity",
    "compare",
    "run_list",
    "task_detail",
    "timeline",
    "tree",
]
