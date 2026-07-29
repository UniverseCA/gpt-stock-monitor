from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from gpt_stock_monitor.models import (
    Availability,
    PendingEvent,
    Product,
    Snapshot,
    StateDocument,
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


def test_state_document_schema_version_is_fixed_at_one() -> None:
    assert StateDocument().schema_version == 1
    assert get_args(StateDocument.model_fields["schema_version"].annotation) == (1,)

    with pytest.raises(ValidationError):
        StateDocument(schema_version=2)  # type: ignore[arg-type]


def test_state_document_sorts_pending_events_deterministically() -> None:
    later = PendingEvent(sequence=2, event_id="event-c", message_parts=("later",))
    tie_breaker = PendingEvent(sequence=1, event_id="event-b", message_parts=("b",))
    first = PendingEvent(sequence=1, event_id="event-a", message_parts=("a",))

    state = StateDocument(pending_events=(later, tie_breaker, first))

    assert [(event.sequence, event.event_id) for event in state.pending_events] == [
        (1, "event-a"),
        (1, "event-b"),
        (2, "event-c"),
    ]
