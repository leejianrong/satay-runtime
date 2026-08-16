"""Control and read API (A7/A8, N15/N16/N18).

The write surface (start / cancel / send_event / fork) and the read surface (run list,
timeline, tree, task/attempt detail, compare), plus the read-time redactor. Reads hit
SQLite directly through the journal store; writes are enqueued on an in-process command
queue and applied by the worker, which stays the single writer (ADR-0012). Security is
the ADR-0014 guard: a per-session token and an ``Origin``/``Host`` allow-list, loopback
bind only.

**Core-dependency boundary (ADR-0013).** Everything re-exported here is **pure Python**
— the read-view builders, the redactor, the command queue and applier, and the security
policy — so ``import satay.control`` pulls none of FastAPI/uvicorn/Pydantic. The HTTP
server assembly lives in the sibling :mod:`satay.control.server` module and imports
FastAPI at *its* module load; the core never imports it. Build (or serve) the app via
:func:`create_app` / :func:`serve`, which import the server lazily so this guarantee
holds even for callers that go through the package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from satay.control.api import ControlAPI, ReadAPI
from satay.control.commands import (
    INHERIT,
    CancelRun,
    Command,
    CommandQueue,
    ForkRun,
    ForkValidationError,
    SendEvent,
    StartRun,
    UnknownWorkflowError,
    append_cancellation,
    apply_command,
    apply_fork,
    create_fork,
    drive_forked_run,
    resolve_fork_point,
    validate_fork_request,
)
from satay.control.redaction import DEFAULT_REDACTION_PATTERNS, REDACTED, Redactor
from satay.control.security import (
    TOKEN_HEADER,
    AuthError,
    NonLoopbackBindError,
    SecurityPolicy,
    ensure_loopback_bind,
    generate_token,
    is_loopback_host,
)
from satay.control.views import (
    RunNotFoundError,
    call_identity,
    compare,
    run_list,
    task_detail,
    timeline,
    tree,
)

if TYPE_CHECKING:
    # Imported for typing only — never at runtime module load, to keep FastAPI out of
    # the core import (the studio stack lives behind the lazy factory below).
    from fastapi import FastAPI


def create_app(*args: Any, **kwargs: Any) -> FastAPI:
    """Build the FastAPI control/read app (studio-only; imports FastAPI lazily)."""
    from satay.control.server import create_app as _create_app

    return _create_app(*args, **kwargs)


def serve(*args: Any, **kwargs: Any) -> None:
    """Run the embedded HTTP server (studio-only; imports uvicorn lazily)."""
    from satay.control.server import serve as _serve

    _serve(*args, **kwargs)


__all__ = [
    "DEFAULT_REDACTION_PATTERNS",
    "INHERIT",
    "REDACTED",
    "TOKEN_HEADER",
    "AuthError",
    "CancelRun",
    "Command",
    "CommandQueue",
    "ControlAPI",
    "ForkRun",
    "ForkValidationError",
    "NonLoopbackBindError",
    "ReadAPI",
    "Redactor",
    "RunNotFoundError",
    "SecurityPolicy",
    "SendEvent",
    "StartRun",
    "UnknownWorkflowError",
    "append_cancellation",
    "apply_command",
    "apply_fork",
    "call_identity",
    "compare",
    "create_app",
    "create_fork",
    "drive_forked_run",
    "ensure_loopback_bind",
    "generate_token",
    "is_loopback_host",
    "resolve_fork_point",
    "run_list",
    "serve",
    "task_detail",
    "timeline",
    "tree",
    "validate_fork_request",
]
