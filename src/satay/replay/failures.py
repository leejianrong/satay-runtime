"""The error a *collected* durable-call failure is surfaced as (ADR-0027).

Collect-mode fan-out (``satay.map(..., return_exceptions=True)`` and
``satay.gather(..., return_exceptions=True)``) puts failures into the result list
instead of raising them. What lands in that slot is **always** a
:class:`TaskFailedError`, never the task's own exception class — on the first pass as
much as on replay.

That is deliberate. A replayed run reads its failures back out of the journal, and the
journal stores an error as ``{type, message, traceback}`` strings: a *name*, not an
import path (ADR-0005 rejects embedding Python class paths in data, and rehydrating an
arbitrary class from a journal would be code loading by another name). So the original
class cannot be reconstructed on replay. If the first pass handed back the user's
``ValueError`` and replay handed back something else, a workflow branching on
``isinstance`` would take a different path on replay — nondeterminism manufactured by
the runtime itself. Surfacing one stable type in both passes removes that whole class of
bug; the original exception is still reachable as ``__cause__`` on the pass that raised
it, and its class *name* rides along in ``error_type`` either way.

Attribute names mirror :class:`~satay.api.run_handle.WorkflowFailedError` (``error_type``
/ ``error_message`` / ``traceback_str``), because a collected ``gather`` member that is a
child workflow surfaces as *that* type; both are ``RuntimeError`` subclasses.
"""

from __future__ import annotations


class TaskFailedError(RuntimeError):
    """A durable task that exhausted its retries, collected rather than raised.

    Carries the failing call's identity (``task_name`` plus ``key`` for a fan-out item,
    or ``ordinal`` for an ordinary call) and the recorded error type name, message, and
    native traceback string.
    """

    def __init__(
        self,
        task_name: str,
        error_type: str,
        message: str,
        tb: str = "",
        *,
        key: str | None = None,
        ordinal: int | None = None,
    ) -> None:
        label = f"{task_name}[{key}]" if key is not None else task_name
        super().__init__(f"{label}: {error_type}: {message}")
        self.task_name = task_name
        self.key = key
        self.ordinal = ordinal
        self.error_type = error_type
        self.error_message = message
        self.traceback_str = tb
