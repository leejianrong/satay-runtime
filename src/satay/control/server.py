"""FastAPI assembly of the control + read API (A7/A8) — **satay[studio] only**.

This is the *only* module in :mod:`satay.control` that imports FastAPI/uvicorn, and it
is **never imported at core import time**: ``satay.control.__init__`` reaches it through
a lazy factory, and nothing in the runtime core imports it. That keeps the
core-dependency boundary intact (ADR-0013) — importing the core pulls none of the
studio stack — while ``import satay.control.server`` (studio present) wires the HTTP
surface over the shared store.

Reads go through :class:`~satay.control.api.ReadAPI` (redaction enforced on every
response, N18); writes go through :class:`~satay.control.api.ControlAPI` onto the
command queue the worker drains (single writer, ADR-0012). Every request passes the
ADR-0014 guard (per-session token + ``Origin``/``Host`` allow-list); the server refuses
a non-loopback bind.
"""

from __future__ import annotations

from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from satay.control.api import ControlAPI, ReadAPI
from satay.control.commands import CommandQueue, ForkValidationError
from satay.control.redaction import Redactor
from satay.control.security import (
    TOKEN_HEADER,
    AuthError,
    SecurityPolicy,
    ensure_loopback_bind,
    generate_token,
)
from satay.control.views import RunNotFoundError
from satay.journal import Store


class StartBody(BaseModel):
    """``POST /runs`` request body."""

    workflow: str
    input: Any = None
    idempotency_key: str | None = None
    run_id: str | None = None


class SendEventBody(BaseModel):
    """``POST /runs/{id}/events`` request body (delivered into the V3 inbox)."""

    event_type: str
    key: str | None = None
    payload: Any = None


class ForkBody(BaseModel):
    """``POST /runs/{id}/fork`` request body (validated here; execution is V7)."""

    fork_point_seq: int


def create_app(
    *,
    store: Store,
    command_queue: CommandQueue,
    security: SecurityPolicy,
    redactor: Redactor | None = None,
) -> FastAPI:
    """Build the control/read FastAPI app over a shared store and command queue."""
    reads = ReadAPI(store, redactor)
    writes = ControlAPI(store, command_queue)

    async def require_auth(request: Request) -> None:
        try:
            security.check(
                token=request.headers.get(TOKEN_HEADER),
                host=request.headers.get("host"),
                origin=request.headers.get("origin"),
            )
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    app = FastAPI(title="Satay control/read API", dependencies=[Depends(require_auth)])

    # -- control (writes: enqueue and return; the worker applies on its poll tick) ----

    @app.post("/runs", status_code=202)
    async def start_run(body: StartBody) -> dict[str, Any]:
        run_id = writes.start(
            body.workflow,
            body.input,
            idempotency_key=body.idempotency_key,
            run_id=body.run_id,
        )
        return {"run_id": run_id, "status": "running"}

    @app.post("/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str) -> dict[str, Any]:
        writes.cancel(run_id)
        return {"run_id": run_id, "status": "cancelling"}

    @app.post("/runs/{run_id}/events", status_code=202)
    async def send_event(run_id: str, body: SendEventBody) -> dict[str, Any]:
        writes.send_event(body.event_type, key=body.key, payload=body.payload, run_id=run_id)
        return {"run_id": run_id, "status": "accepted"}

    @app.post("/runs/{run_id}/fork", status_code=202)
    async def fork_run(run_id: str, body: ForkBody) -> dict[str, Any]:
        try:
            await writes.validate_fork(run_id, body.fork_point_seq)
        except ForkValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Route exists and validates; creating the forked run is deferred to V7.
        return {"source_run_id": run_id, "status": "accepted", "deferred": "v7"}

    # -- reads (direct to the store; redaction enforced; never block on the worker) ---

    @app.get("/runs")
    async def list_runs() -> dict[str, Any]:
        return await reads.run_list()

    @app.get("/runs/{run_id}/timeline")
    async def run_timeline(run_id: str) -> dict[str, Any]:
        return await _read(reads.timeline(run_id))

    @app.get("/runs/{run_id}/tree")
    async def run_tree(run_id: str) -> dict[str, Any]:
        return await _read(reads.tree(run_id))

    @app.get("/runs/{run_id}/tasks/{identity}")
    async def run_task_detail(run_id: str, identity: str) -> dict[str, Any]:
        return await _read(reads.task_detail(run_id, identity))

    @app.get("/runs/{run_id}/compare")
    async def run_compare(run_id: str, to: str = Query(...)) -> dict[str, Any]:
        return await _read(reads.compare(run_id, to))

    return app


async def _read(awaitable: Any) -> dict[str, Any]:
    """Await a read view, mapping an unknown run to HTTP 404."""
    try:
        result: dict[str, Any] = await awaitable
        return result
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def serve(
    *,
    store: Store,
    command_queue: CommandQueue,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    allowed_origins: frozenset[str] = frozenset(),
    redactor: Redactor | None = None,
    log_level: str = "info",
) -> None:
    """Run the embedded server on a loopback bind (refuses non-loopback, ADR-0014)."""
    ensure_loopback_bind(host)
    security = SecurityPolicy(token=token or generate_token(), allowed_origins=allowed_origins)
    app = create_app(store=store, command_queue=command_queue, security=security, redactor=redactor)
    uvicorn.run(app, host=host, port=port, log_level=log_level)


__all__ = ["ForkBody", "SendEventBody", "StartBody", "create_app", "serve"]
