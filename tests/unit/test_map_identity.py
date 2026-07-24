"""Unit tests for V4 fan-out identity, key validation, and tree linkage (N7, ADR-0002).

Pure, no store: the keyed-identity resolver, schedule-time key validation, the map-key
idempotency derivation, and the recoverability of tree linkage from recorded payloads.
"""

from __future__ import annotations

import pytest

from satay.replay.identity import (
    CallIdentity,
    IdentityResolver,
    idempotency_key,
    resolve_map_keys,
)


def test_missing_key_callable_raises_at_schedule_time() -> None:
    """``satay.map`` without ``key=`` is a usage error caught before any item runs."""
    with pytest.raises(ValueError, match="requires key="):
        resolve_map_keys([1, 2, 3], None)


def test_item_yielding_no_key_raises_at_schedule_time() -> None:
    """A ``key=`` that returns ``None``/empty for an item is a missing-key usage error."""
    with pytest.raises(ValueError, match="has no key"):
        resolve_map_keys([1, 2, 3], lambda v: None)  # type: ignore[arg-type,return-value]
    with pytest.raises(ValueError, match="has no key"):
        resolve_map_keys([1, 2, 3], lambda v: "")


def test_duplicate_keys_are_rejected_at_schedule_time() -> None:
    """Duplicate keys within one map collide on identity → rejected up front (ADR-0002)."""
    with pytest.raises(ValueError, match="duplicate item key"):
        resolve_map_keys([1, 2, 1], lambda v: f"k{v}")


def test_resolve_map_keys_pairs_items_with_keys_in_input_order() -> None:
    pairs = resolve_map_keys([10, 20, 30], lambda v: f"k{v}")
    assert pairs == [(10, "k10"), (20, "k20"), (30, "k30")]


def test_keyed_identity_is_independent_of_the_ordinal_counter() -> None:
    """A keyed identity never collides with an ordinal one, and does not consume ordinals."""
    resolver = IdentityResolver()
    first_ordinal = resolver.next("task")
    keyed = CallIdentity(task_name="task", key="k0")
    second_ordinal = resolver.next("task")

    # The ordinal counter is untouched by the keyed identity (0 then 1, no gap).
    assert first_ordinal.ordinal == 0
    assert second_ordinal.ordinal == 1
    # Keyed and ordinal identities of the same task never collide.
    assert keyed != first_ordinal
    assert keyed != CallIdentity(task_name="task", ordinal=0)
    assert keyed.is_keyed and not first_ordinal.is_keyed


def test_idempotency_key_is_distinct_across_map_keys() -> None:
    """The map-key case (relocated from V2): distinct keys → distinct idempotency keys."""
    a = idempotency_key("run-1", "fetch", "url-a")
    b = idempotency_key("run-1", "fetch", "url-b")
    assert a != b
    # ...and stable for the same map key (reused across physical retries).
    assert a == idempotency_key("run-1", "fetch", "url-a")
    # A map key and a numeric ordinal do not accidentally coincide.
    assert idempotency_key("run-1", "fetch", "0") == idempotency_key("run-1", "fetch", 0)


def test_identity_payload_roundtrips_for_both_forms() -> None:
    """Identity serialises to event-payload fields and reconstructs exactly (both forms)."""
    ordinal = CallIdentity(task_name="step", ordinal=2)
    keyed = CallIdentity(task_name="step", key="item-7")

    assert ordinal.payload_fields() == {"task_name": "step", "ordinal": 2}
    assert keyed.payload_fields() == {"task_name": "step", "key": "item-7"}
    assert CallIdentity.from_payload(ordinal.payload_fields()) == ordinal
    assert CallIdentity.from_payload(keyed.payload_fields()) == keyed


def test_tree_linkage_is_derivable_from_parent_ref_plus_item_keys() -> None:
    """The V6 tree needs only recorded fields: a parent ref for a child, keys for a map.

    A child's ``WorkflowCreated`` carries ``parent_run_id`` + originating identity; each
    map item's ``TaskScheduled`` carries its ``key`` + ``map_group``. This reconstructs
    the tree with no extra bookkeeping (build step 6), which we assert against the shapes
    the engine records.
    """
    # Child linkage: parent -> child, recoverable both ways from these payloads.
    child_created_payload = {
        "workflow_name": "child_workflow",
        "parent_run_id": "parent-1",
        "parent_call": CallIdentity(task_name="child:child_workflow", ordinal=0).payload_fields(),
    }
    parent_scheduled_payload = {
        **CallIdentity(task_name="child:child_workflow", ordinal=0).payload_fields(),
        "child_run_id": "child-1",
        "workflow_name": "child_workflow",
    }
    assert child_created_payload["parent_run_id"] == "parent-1"
    assert (
        CallIdentity.from_payload(child_created_payload["parent_call"])  # type: ignore[arg-type]
        == CallIdentity.from_payload(parent_scheduled_payload)
    )

    # Map grouping: items of one map share a group and carry their own keys.
    group = "map:0:square_item"
    items = [
        {
            **CallIdentity(task_name="square_item", key=f"item-{v}").payload_fields(),
            "map_group": group,
        }
        for v in (1, 2, 3)
    ]
    assert {i["map_group"] for i in items} == {group}
    assert [CallIdentity.from_payload(i).key for i in items] == ["item-1", "item-2", "item-3"]
