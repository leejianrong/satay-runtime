"""End-to-end: ``satay.control.run_app`` really does let something outside the process
steer a run parked on ``wait_for_event`` (ADR-0046).

This is the capability cuttlefish-agent's steerable chat needs: a task is running
inside one blocking ``cuttlefish run``-shaped invocation, parked on
``satay.wait_for_event``, and a *separate* HTTP client (standing in for a second CLI
invocation, or a browser) delivers the steering message over the real control API —
the same demo ``tests/e2e/test_control_http.py`` already proves for ``DevStack``, now
proven for the narrower, block-scoped shape cuttlefish actually embeds.

Requires the ``satay[studio]`` extra; skips cleanly without it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
pytest.importorskip("httpx")

import httpx

import satay
import satay.control


@dataclass(frozen=True)
class SteerMessage:
    """A stand-in for cuttlefish's own steering event type."""

    text: str


@satay.task()
async def _echo(value: str) -> str:
    return value


@satay.workflow
async def steerable_task(_: None) -> str:
    """Parks on an event wait until steered, then finishes — the exact shape of a
    delegation loop racing a steering wait against its own progress."""
    message = await satay.wait_for_event(SteerMessage, key="task-1", timeout=30)
    assert message is not None
    return await _echo(f"steered: {message.text}")


async def test_an_external_http_client_wakes_a_run_parked_on_wait_for_event(
    tmp_path: Path,
) -> None:
    async with satay.control.run_app(data_dir=tmp_path / ".satay", interval=0.02) as app:
        handle = satay.start(steerable_task, None, store=app.store)

        # Sent right away, with no wait for the run to actually reach the event wait
        # first: an event sits buffered in the inbox until matched (D22/ADR-0021), the
        # same "may arrive before the wait" contract satay.send_event itself documents
        # (tests/e2e/test_run_app.py's own send_event demo relies on the same thing).
        async with httpx.AsyncClient(base_url=app.base_url) as client:
            resp = await client.post(
                f"/runs/{handle.run_id}/events",
                headers={"x-satay-token": app.token},
                json={
                    "event_type": "test_control_run_app_steering.SteerMessage",
                    "key": "task-1",
                    "payload": {"text": "focus on the auth module"},
                },
            )
            assert resp.status_code == 202

        assert await handle.result() == "steered: focus on the auth module"
        assert await handle.status() == "completed"


async def test_the_read_api_shows_the_steering_event_in_the_timeline(
    tmp_path: Path,
) -> None:
    """The steering message is part of the real record, not a side channel invisible
    to the same journal a human or another tool reads back (no parallel transcript)."""
    async with satay.control.run_app(data_dir=tmp_path / ".satay", interval=0.02) as app:
        handle = satay.start(steerable_task, None, store=app.store)
        await satay.send_event(SteerMessage(text="hi"), key="task-1", store=app.store)
        await handle.result()

        async with httpx.AsyncClient(base_url=app.base_url) as client:
            resp = await client.get(
                f"/runs/{handle.run_id}/timeline", headers={"x-satay-token": app.token}
            )
            assert resp.status_code == 200
            event_types = [e["type"] for e in resp.json()["events"]]
            assert "ExternalEventReceived" in event_types
