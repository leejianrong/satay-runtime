"""Drive an awaitable to completion under a :class:`~satay.testing.clock.ManualClock`.

Under a ``ManualClock`` nothing that waits on time moves until someone advances it, which
makes awaiting a run directly a deadlock: the drive suspends on a clock sleeper (a retry
backoff — ``base * 2**(failure-1)``, full-jitter, capped at 60s, ADR-0006 — or the loser
of a timeout race), and the only caller who could advance the clock is the one now blocked
awaiting the drive.

:func:`settle` is that caller. It runs the drive as a task and, whenever the drive is
suspended on a sleeper, advances virtual time far enough to fire it. A whole retry
schedule therefore replays in zero wall-clock time while the *recorded* delays stay the
real ones — nobody sat and waited for them.

This is a plain importable coroutine function rather than a pytest fixture on purpose: the
``examples/`` scripts need exactly this loop and cannot use a fixture. The pytest ``drain``
fixture in :mod:`satay.testing.fixtures` returns this same function, so there is one
implementation and not two that can drift.

It never hangs. After ``max_steps`` passes that make no progress it cancels the drive and
raises :class:`NeverSettledError`, which is the useful answer: something in the run is
waiting on real time, or on an event nobody is going to send.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from satay.testing.clock import ManualClock

#: Virtual seconds per advance. Coarse on purpose — one step clears the 60s backoff cap
#: (ADR-0006), so a full retry schedule resolves in a handful of passes. Which is why
#: journal timestamps jump a minute at a time while the recorded delays are sub-second:
#: virtual time is free, so there is no reason to be precise with it.
DEFAULT_STEP_SECONDS = 61.0

#: Passes tolerated before giving up. A run that can settle does so in a few passes, so
#: this ceiling only bounds the *failure* path and can afford to be generous.
DEFAULT_MAX_STEPS = 2000

#: Event-loop yields per pass, so the drive reaches its next suspension point before we
#: look at the clock. The runtime's await chain (handle → engine → executor → clock) is
#: deeper than one yield.
_YIELDS_PER_PASS = 4


class NeverSettledError(AssertionError):
    """:func:`settle` gave up: the drive was not waiting on the manual clock.

    Subclasses ``AssertionError`` so that inside a test this reads (and is reported by
    pytest) as a failed expectation, while still being a distinct type a script can catch.
    That also keeps the historical behaviour of the ``drain`` fixture, which raised a bare
    ``AssertionError`` before this function existed.
    """


async def settle[T](
    target: Awaitable[T] | Callable[[], Awaitable[T]],
    clock: ManualClock,
    *,
    step: float = DEFAULT_STEP_SECONDS,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> T:
    """Await ``target``, advancing ``clock`` through every wait it suspends on.

    ``target`` is either an awaitable (``settle(handle.result(), clock)``) or a
    zero-argument callable returning one (``settle(handle.result, clock)``). The callable
    form is what the examples and the ``drain`` fixture use — it reads better at the call
    site and keeps a coroutine from being created until it is about to be driven.

    Returns whatever the drive returns, and propagates whatever it raises: a
    ``WorkflowFailedError`` from a failed run, a ``SimulatedCrash`` from an injected
    fault. A drive that parks (on a durable timer or an event wait) returns normally —
    parking is a result, not a stall, so ``settle`` hands back ``satay.PARKED`` rather
    than waiting for a worker tick that only the caller can make (ADR-0030).

    Raises:
        NeverSettledError: after ``max_steps`` passes without the drive finishing. The
            drive is cancelled first, so nothing is left running.
    """
    awaitable = target() if callable(target) else target
    task = asyncio.ensure_future(awaitable)
    try:
        for _ in range(max_steps):
            for _ in range(_YIELDS_PER_PASS):
                await asyncio.sleep(0)  # let the drive reach its next suspension point
            if task.done():
                return await task
            if clock.pending_sleepers:
                clock.advance(step)
    finally:
        if not task.done():
            task.cancel()
    raise NeverSettledError(
        "the run never settled — is something waiting on real time? "
        f"(gave up after {max_steps} passes of {step}s of virtual time; "
        f"{clock.pending_sleepers} sleeper(s) pending on the manual clock)"
    )
