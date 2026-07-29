from __future__ import annotations

import pytest
from pydantic import ValidationError

from gpt_stock_monitor.models import (
    Availability,
    EmptyCandidate,
    HealthRecord,
    PendingEvent,
    Product,
    Snapshot,
    StateDocument,
    state_key,
)


def make_product(*, key: str = "product-1") -> Product:
    return Product(
        key=key,
        name="GPT Plus",
        price="13.50",
        price_text="¥13.5",
        availability=Availability.IN_STOCK,
        stock_text="有货",
        url="https://pay.ldxp.cn/goods/1",
    )


@pytest.mark.parametrize("key", ["", "   "])
def test_product_rejects_empty_stable_key(key: str) -> None:
    with pytest.raises(ValidationError):
        make_product(key=key)


def test_snapshot_rejects_duplicate_product_keys() -> None:
    item = make_product()

    with pytest.raises(ValueError, match="duplicate product key"):
        Snapshot(
            monitor_id="demo",
            category="GPT-plus成品号",
            products=(item, item),
        )


def test_pending_event_stores_immutable_message_parts() -> None:
    source_parts = ["part 1", "part 2"]

    event = PendingEvent(
        sequence=1,
        event_id="event-1",
        message_parts=source_parts,
    )
    source_parts.append("part 3")

    assert event.message_parts == ("part 1", "part 2")
    assert isinstance(event.message_parts, tuple)
    with pytest.raises(ValidationError):
        event.message_parts = ("replacement",)


def test_pending_event_allows_sequence_zero() -> None:
    event = PendingEvent(sequence=0, event_id="event-0", message_parts=("first",))

    assert event.sequence == 0


def test_state_document_schema_version_is_fixed_at_one() -> None:
    assert StateDocument().schema_version == 1

    with pytest.raises(ValidationError):
        StateDocument(schema_version=2)  # type: ignore[arg-type]


def test_state_document_sorts_pending_events_deterministically() -> None:
    later = PendingEvent(sequence=2, event_id="event-c", message_parts=("later",))
    tie_breaker = PendingEvent(sequence=1, event_id="event-b", message_parts=("b",))
    first = PendingEvent(sequence=1, event_id="event-a", message_parts=("a",))

    state = StateDocument(pending_events=(later, tie_breaker, first), next_event_sequence=3)

    assert [(event.sequence, event.event_id) for event in state.pending_events] == [
        (1, "event-a"),
        (1, "event-b"),
        (2, "event-c"),
    ]


def test_state_document_rejects_allocated_next_event_sequence() -> None:
    pending = PendingEvent(sequence=2, event_id="event-2", message_parts=("pending",))

    with pytest.raises(ValueError, match="next_event_sequence"):
        StateDocument(pending_events=(pending,), next_event_sequence=1)


def test_state_document_rejects_event_id_that_is_pending_and_delivered() -> None:
    pending = PendingEvent(sequence=1, event_id="event-1", message_parts=("pending",))

    with pytest.raises(ValidationError, match="pending and delivered"):
        StateDocument(
            pending_events=(pending,),
            delivered_event_ids=("event-1",),
            next_event_sequence=2,
        )


def test_state_document_rejects_duplicate_delivered_event_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate delivered event id"):
        StateDocument(delivered_event_ids=("event-1", "event-1"))


@pytest.mark.parametrize("event_id", ["", "   ", "event\x00id", "event\nid"])
def test_state_document_rejects_unsafe_delivered_event_id(event_id: str) -> None:
    with pytest.raises(ValidationError):
        StateDocument(delivered_event_ids=(event_id,))


def test_state_document_copies_delivered_event_id_list_to_tuple() -> None:
    source_ids = ["event-1"]

    state = StateDocument(delivered_event_ids=source_ids)
    source_ids.append("event-2")

    assert state.delivered_event_ids == ("event-1",)
    assert isinstance(state.delivered_event_ids, tuple)


def test_state_document_sorts_pending_events_on_rebuild() -> None:
    later = PendingEvent(sequence=2, event_id="event-c", message_parts=("later",))
    tie_breaker = PendingEvent(sequence=1, event_id="event-b", message_parts=("b",))
    first = PendingEvent(sequence=1, event_id="event-a", message_parts=("a",))

    state = StateDocument(pending_events=(later, tie_breaker, first), next_event_sequence=3)

    assert [(event.sequence, event.event_id) for event in state.pending_events] == [
        (1, "event-a"),
        (1, "event-b"),
        (2, "event-c"),
    ]


def test_failed_state_assignment_does_not_pollute_original() -> None:
    state = StateDocument(next_event_sequence=3)
    invalid = PendingEvent(sequence=3, event_id="event-3", message_parts=("invalid",))

    with pytest.raises(ValidationError):
        state.pending_events = (invalid,)

    assert state.pending_events == ()
    assert state.next_event_sequence == 3


def test_state_document_snapshot_mapping_is_read_only() -> None:
    snapshot = Snapshot(monitor_id="demo", category="GPT-plus成品号", products=(make_product(),))
    key = state_key(snapshot.monitor_id, snapshot.category)
    state = StateDocument(snapshots={key: snapshot})

    with pytest.raises(TypeError):
        state.snapshots["unexpected"] = snapshot

    assert tuple(state.snapshots) == (key,)


def test_nested_health_and_empty_records_are_frozen() -> None:
    key = state_key("demo", "GPT-plus成品号")
    state = StateDocument(health={key: HealthRecord()}, empty_candidates={key: EmptyCandidate()})

    with pytest.raises(ValidationError):
        state.health[key].consecutive_failures = 1
    with pytest.raises(ValidationError):
        state.empty_candidates[key].consecutive_observations = 2


def test_state_document_read_only_mappings_remain_json_serializable() -> None:
    snapshot = Snapshot(monitor_id="demo", category="GPT-plus成品号", products=(make_product(),))
    key = state_key(snapshot.monitor_id, snapshot.category)
    state = StateDocument(
        snapshots={key: snapshot},
        health={key: HealthRecord()},
        empty_candidates={key: EmptyCandidate()},
    )

    dumped = state.model_dump(mode="json")

    assert dumped["snapshots"][key]["monitor_id"] == "demo"
    assert StateDocument.model_validate_json(state.model_dump_json()) == state
