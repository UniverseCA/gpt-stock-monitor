"""Domain models for snapshots, changes, and persisted monitor state."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Price = Annotated[str, Field(strict=True, pattern=r"^(?:0|[1-9]\d*)(?:\.\d+)?$")]


def state_key(monitor_id: str, category: str) -> str:
    """Return the internal key for a monitored category."""
    return f"{monitor_id}\x1f{category}"


class Availability(StrEnum):
    """Normalized product availability."""

    IN_STOCK = "in_stock"
    LOW_STOCK = "low_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


class Product(BaseModel, frozen=True):
    """A normalized product shown within a monitored category."""

    key: NonEmptyStr
    name: NonEmptyStr
    price: Price
    price_text: str
    availability: Availability
    stock_text: str
    url: str | None = None


class Snapshot(BaseModel, frozen=True):
    """The semantic products observed for one monitored category."""

    monitor_id: NonEmptyStr
    category: NonEmptyStr
    products: tuple[Product, ...]

    @field_validator("products")
    @classmethod
    def reject_duplicate_product_keys(cls, products: tuple[Product, ...]) -> tuple[Product, ...]:
        keys = [product.key for product in products]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate product key")
        return products


class ChangeKind(StrEnum):
    """Kinds of semantic product change reported by the diff layer."""

    ADDED = "added"
    REMOVED = "removed"
    AVAILABILITY = "availability"
    STOCK_TEXT = "stock_text"
    PRICE = "price"
    NAME = "name"


class Change(BaseModel, frozen=True):
    """One field-level semantic change for a product."""

    kind: ChangeKind
    product_key: NonEmptyStr
    product_name: NonEmptyStr
    before: str | None = None
    after: str | None = None
    url: str | None = None


class PendingEvent(BaseModel, frozen=True):
    """An ordered event whose immutable message parts await delivery."""

    sequence: int = Field(ge=0)
    event_id: NonEmptyStr
    message_parts: tuple[str, ...] = Field(min_length=1)


class HealthRecord(BaseModel, frozen=True):
    """Failure and notification state for one monitored category."""

    consecutive_failures: int = Field(default=0, ge=0)
    last_error: str | None = None
    failure_notified: bool = False


class EmptyCandidate(BaseModel, frozen=True):
    """Consecutive explicit-empty observations awaiting confirmation."""

    consecutive_observations: int = Field(default=1, ge=1)


class StateDocument(BaseModel, frozen=True):
    """Versioned, deterministic monitor state persisted on the state branch."""

    schema_version: Literal[1] = 1
    snapshots: Mapping[str, Snapshot] = Field(default_factory=lambda: MappingProxyType({}))
    health: Mapping[str, HealthRecord] = Field(default_factory=lambda: MappingProxyType({}))
    empty_candidates: Mapping[str, EmptyCandidate] = Field(
        default_factory=lambda: MappingProxyType({})
    )
    pending_events: tuple[PendingEvent, ...] = ()
    delivered_event_ids: tuple[str, ...] = ()
    next_event_sequence: int = Field(default=1, ge=1)

    @field_validator("snapshots", "health", "empty_candidates")
    @classmethod
    def make_mapping_read_only(cls, values: Mapping[str, object]) -> Mapping[str, object]:
        return MappingProxyType(dict(values))

    @field_serializer("snapshots", "health", "empty_candidates")
    def serialize_read_only_mapping(self, values: Mapping[str, object]) -> dict[str, object]:
        return dict(values)

    @field_validator("pending_events")
    @classmethod
    def sort_pending_events(cls, events: tuple[PendingEvent, ...]) -> tuple[PendingEvent, ...]:
        return tuple(sorted(events, key=lambda event: (event.sequence, event.event_id)))

    @model_validator(mode="after")
    def validate_state_invariants(self) -> Self:
        for key, snapshot in self.snapshots.items():
            if key != state_key(snapshot.monitor_id, snapshot.category):
                raise ValueError("snapshot state key does not match monitor and category")

        event_ids = [event.event_id for event in self.pending_events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("duplicate pending event id")
        if (
            self.pending_events
            and max(event.sequence for event in self.pending_events) >= self.next_event_sequence
        ):
            raise ValueError("next_event_sequence must be greater than all pending event sequences")
        return self
