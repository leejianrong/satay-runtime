"""Shared read/view-layer for rendering a run's journal as a text timeline.

This is the **single** place the ⚡ interruption/resume marker is computed (Q42,
corrected by Q52): the ``satay runs show`` CLI consumes it in V1 and Studio consumes
the same computation in V6, so the two can never disagree. Per the ADR-0009 H4
refinement the marker is simply the **presence of a ``WorkflowResumed`` event** — the
worker writes it only when re-driving a run interrupted mid-execution (not durably
parked), so its presence *is* the interruption.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from satay.journal.events import Event, EventType

#: The marker prefixed to a resume point in the rendered timeline.
INTERRUPTION_MARKER = "⚡"


def interruption_seqs(events: Sequence[Event]) -> set[int]:
    """Return the ``seq`` of every event that renders a ⚡ interruption marker.

    A ``WorkflowResumed`` event marks recovery from an interruption (ADR-0009/Q52).
    """
    return {e.seq for e in events if e.type is EventType.WORKFLOW_RESUMED}


def model_usage(events: Sequence[Event]) -> list[dict[str, Any]]:
    """Return every recorded model-usage entry across a run's ``TaskCompleted`` events.

    The read path for the generic usage slot written by ``ctx.record_model_usage``
    (N14); Studio renders these in V6. Empty when no task self-reported usage.
    """
    entries: list[dict[str, Any]] = []
    for event in events:
        if event.type is EventType.TASK_COMPLETED:
            entries.extend(event.payload.get("usage", []))
    return entries


def _summarise_payload(event: Event) -> str:
    """Render the key payload fields for one event type as a compact string."""
    p = event.payload
    if event.type is EventType.WORKFLOW_CREATED:
        return f"workflow={p.get('workflow_name')} code_version={p.get('code_version')}"
    if event.type in (
        EventType.TASK_SCHEDULED,
        EventType.TASK_ATTEMPT_STARTED,
        EventType.TASK_ATTEMPT_FAILED,
        EventType.TASK_COMPLETED,
    ):
        parts = [f"task={p.get('task_name')}", f"ordinal={p.get('ordinal')}"]
        if event.type is EventType.TASK_ATTEMPT_STARTED:
            parts.append(f"attempt={p.get('attempt')}")
        if event.type is EventType.TASK_ATTEMPT_FAILED:
            error = p.get("error", {})
            parts.append(f"attempt={p.get('attempt')}")
            parts.append(f"error={error.get('type')}: {error.get('message')}")
            next_delay = p.get("next_delay")
            if next_delay is not None:
                parts.append(f"next_delay={next_delay:.3f}s")
        return " ".join(parts)
    if event.type is EventType.WORKFLOW_FAILED:
        error = p.get("error", {})
        return f"error={error.get('type')}: {error.get('message')}"
    return ""


def render_timeline(events: Sequence[Event], *, run_id: str) -> str:
    """Render a run's ordered journal as a text timeline.

    One line per event (``seq``, ``ts``, ``type``, key payload fields); a ⚡ marker
    prefixes each ``WorkflowResumed`` resume point. A recorded ``WorkflowFailed``
    traceback is printed beneath its line.
    """
    marked = interruption_seqs(events)
    lines = [f"Run {run_id} — {len(events)} event(s)"]
    for event in events:
        marker = f"{INTERRUPTION_MARKER} " if event.seq in marked else "  "
        summary = _summarise_payload(event)
        ts = event.ts.isoformat()
        line = f"{marker}{event.seq:>3}  {ts}  {event.type.value}"
        if summary:
            line += f"  {summary}"
        lines.append(line)
        if event.type is EventType.WORKFLOW_FAILED:
            tb = event.payload.get("error", {}).get("traceback", "")
            for tb_line in tb.rstrip().splitlines():
                lines.append(f"        {tb_line}")
    return "\n".join(lines)
