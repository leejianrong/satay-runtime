"""Decoration-time validation of ``@satay.workflow`` (KAN-579).

Pure, no store: the whole point of these checks is that they fire at import, before any
run exists. Uncallable workflows used to reach the replay engine, where the ``TypeError``
was caught by the generic failure handler and written to an append-only journal as a
``WorkflowFailed`` event — an authoring typo leaving a permanent junk run behind, dressed
up as a runtime failure.

The registry is process-global and rejects a duplicate name, so every workflow here carries
a ``dec_`` prefix and each accepted shape gets its own name.
"""

from __future__ import annotations

from typing import Any

import pytest

from satay.api.decorators import workflow


def test_zero_parameter_workflow_is_rejected_at_decoration() -> None:
    """The headline case: a workflow the runtime has nowhere to put the input."""
    with pytest.raises(TypeError, match="cannot be called with one argument"):

        @workflow
        async def dec_zero() -> int:
            return 1


def test_two_required_parameters_is_rejected_at_decoration() -> None:
    """Satay passes one input, so a second required parameter can never be filled."""
    with pytest.raises(TypeError, match="cannot be called with one argument"):

        @workflow
        async def dec_two(a: int, b: int) -> int:
            return a + b


def test_keyword_only_parameter_is_rejected_at_decoration() -> None:
    """The input is passed positionally; a keyword-only parameter cannot receive it."""
    with pytest.raises(TypeError, match="cannot be called with one argument"):

        @workflow
        async def dec_kwonly(*, value: int = 1) -> int:
            return value


def test_kwargs_only_is_rejected_at_decoration() -> None:
    """``**kwargs`` absorbs keywords, not the positional input."""
    with pytest.raises(TypeError, match="cannot be called with one argument"):

        @workflow
        async def dec_kwargs(**kwargs: Any) -> int:
            return 1


def test_a_sync_def_workflow_is_rejected_at_decoration() -> None:
    """Async-only is a runtime-wide rule, and a plain ``def`` failed just as badly."""
    with pytest.raises(TypeError, match="must be an `async def`"):

        @workflow
        def dec_sync(value: int) -> int:
            return value


def test_the_error_names_the_convention_and_the_fix() -> None:
    """A decoration-time error is only better than a run failure if it says what to do."""
    with pytest.raises(TypeError) as excinfo:

        @workflow
        async def dec_message() -> int:
            return 1

    message = str(excinfo.value)
    assert "dec_message()" in message  # the offending signature, rendered
    assert "satay.start()" in message  # where the input comes from
    assert "_: Any = None" in message  # the fix for a workflow that wants no input


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(lambda: _accept_plain, id="one-required"),
        pytest.param(lambda: _accept_default, id="one-with-default"),
        pytest.param(lambda: _accept_ignored, id="ignored-optional"),
        pytest.param(lambda: _accept_posonly, id="positional-only"),
        pytest.param(lambda: _accept_second_default, id="second-param-defaulted"),
        pytest.param(lambda: _accept_varargs, id="varargs"),
        pytest.param(lambda: _accept_unannotated, id="unannotated"),
    ],
)
def test_every_shape_the_runtime_can_call_is_accepted(factory: Any) -> None:
    """The check must not be stricter than the runtime it is protecting.

    Each of these binds cleanly to one positional argument, so each is genuinely drivable —
    and shapes like ``(_: Any = None)`` and the unannotated single parameter are in real use
    elsewhere in this suite. A parameter *count* rule would have failed several of them,
    which is why the predicate is an actual bind against the signature.
    """
    assert factory() is not None


@workflow
async def _accept_plain(value: int) -> int:
    return value


@workflow
async def _accept_default(value: int = 5) -> int:
    return value


@workflow
async def _accept_ignored(_: Any = None) -> int:
    return 1


@workflow
async def _accept_posonly(value: int, /) -> int:
    return value


@workflow
async def _accept_second_default(value: int, other: int = 2) -> int:
    return value + other


@workflow
async def _accept_varargs(*args: Any) -> int:
    return len(args)


@workflow
async def _accept_unannotated(value):  # type: ignore[no-untyped-def]
    return value
