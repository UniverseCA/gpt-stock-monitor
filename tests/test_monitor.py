from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from gpt_stock_monitor.config import AppConfig
from gpt_stock_monitor.models import PendingEvent, Product, Snapshot, StateDocument, state_key
from gpt_stock_monitor.monitor import run_once
from gpt_stock_monitor.sites.base import CategoryObservation
from gpt_stock_monitor.state import PublishResult, PublishStatus, VersionedState

CHECKED_AT = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)


def app_config(*categories: str) -> AppConfig:
    return AppConfig.model_validate(
        {
            "monitors": [
                {
                    "id": "shop-a",
                    "name": "Shop A",
                    "url": "https://pay.ldxp.cn/shop/a",
                    "categories": list(categories or ("General",)),
                }
            ]
        }
    )


class MemoryRepository:
    def __init__(
        self,
        state: StateDocument,
        *,
        version: str | None = "v1",
        statuses: list[PublishStatus] | None = None,
    ) -> None:
        self.current = VersionedState(version, state)
        self.loads = 0
        self.publishes: list[tuple[str | None, StateDocument, str]] = []
        self.statuses = list(statuses or [])

    def load(self) -> VersionedState:
        self.loads += 1
        return self.current

    def publish(
        self, expected_parent: str | None, state: StateDocument, message: str
    ) -> PublishResult:
        self.publishes.append((expected_parent, state, message))
        assert expected_parent == self.current.version
        status = self.statuses.pop(0) if self.statuses else PublishStatus.PUBLISHED
        if status is PublishStatus.CONFLICT:
            return PublishResult(status, self.current.version)
        version = f"v{len(self.publishes) + 1}"
        self.current = VersionedState(version, state)
        return PublishResult(status, version)


class Adapter:
    def __init__(self, result: dict[str, CategoryObservation] | None = None) -> None:
        self.result = result or {"General": CategoryObservation(products=(), explicit_count=0)}
        self.calls: list[str] = []

    async def collect(self, config: Any) -> dict[str, CategoryObservation]:
        self.calls.append(config.id)
        return self.result


class Notifier:
    def __init__(self, *, fail_on: int | None = None) -> None:
        self.calls: list[tuple[dict[str, Any], ...]] = []
        self.fail_on = fail_on

    async def send_parts(self, parts: Any) -> None:
        self.calls.append(tuple(parts))
        if self.fail_on == len(self.calls):
            raise RuntimeError("webhook secret=do-not-leak")


def pending(sequence: int, event_id: str) -> PendingEvent:
    payload = {"msg_type": "text", "content": {"text": event_id}}
    return PendingEvent(
        sequence=sequence,
        event_id=event_id,
        message_parts=(json.dumps(payload, sort_keys=True, separators=(",", ":")),),
    )


def test_old_pending_events_are_delivered_and_confirmed_one_at_a_time() -> None:
    repository = MemoryRepository(
        StateDocument(pending_events=(pending(2, "b"), pending(1, "a")), next_event_sequence=3)
    )
    notifier = Notifier(fail_on=2)
    adapter = Adapter()

    result = asyncio.run(
        run_once(app_config(), repository, adapter, notifier, dry_run=False, checked_at=CHECKED_AT)
    )

    assert result.exit_code == 3
    assert [[part["content"]["text"] for part in call] for call in notifier.calls] == [["a"], ["b"]]
    assert repository.current.document.delivered_event_ids == ("a",)
    assert [event.event_id for event in repository.current.document.pending_events] == ["b"]
    assert adapter.calls == []


def test_dry_run_is_read_only_and_reports_pending_without_message_bodies() -> None:
    original = StateDocument(pending_events=(pending(1, "a"),), next_event_sequence=2)
    repository = MemoryRepository(original)
    notifier = Notifier()
    adapter = Adapter()

    result = asyncio.run(
        run_once(app_config(), repository, adapter, notifier, dry_run=True, checked_at=CHECKED_AT)
    )

    assert result.exit_code == 0
    assert result.output["pending_events"] == [{"event_id": "a", "sequence": 1, "part_count": 1}]
    assert "content" not in json.dumps(result.output)
    assert repository.current.document == original
    assert repository.publishes == []
    assert notifier.calls == []
    assert adapter.calls == ["shop-a"]


def test_naive_checked_at_is_rejected_before_external_operations() -> None:
    repository = MemoryRepository(StateDocument())
    adapter = Adapter()
    notifier = Notifier()

    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(
            run_once(
                app_config(),
                repository,
                adapter,
                notifier,
                dry_run=False,
                checked_at=datetime(2026, 7, 29),
            )
        )

    assert repository.loads == 0
    assert adapter.calls == []
    assert notifier.calls == []


def product(*, price: str = "10") -> Product:
    return Product(
        key="p1",
        name="Product",
        price=price,
        price_text=f"¥{price}",
        availability="in_stock",
        stock_text="available",
    )


def snapshot(*, price: str = "10") -> Snapshot:
    return Snapshot(monitor_id="shop-a", category="General", products=(product(price=price),))


def test_first_nonempty_observation_is_a_silent_persisted_baseline() -> None:
    repository = MemoryRepository(StateDocument())
    adapter = Adapter({"General": CategoryObservation(products=(product(),), explicit_count=None)})
    notifier = Notifier()

    result = asyncio.run(
        run_once(app_config(), repository, adapter, notifier, dry_run=False, checked_at=CHECKED_AT)
    )

    assert result.exit_code == 0
    assert repository.current.document.snapshots[state_key("shop-a", "General")] == snapshot()
    assert len(repository.publishes) == 1
    assert notifier.calls == []
    assert result.output["changes"] == []


def test_changed_observation_is_published_before_its_notification_is_sent() -> None:
    key = state_key("shop-a", "General")
    repository = MemoryRepository(StateDocument(snapshots={key: snapshot()}))
    adapter = Adapter(
        {"General": CategoryObservation(products=(product(price="12"),), explicit_count=None)}
    )
    notifier = Notifier()

    result = asyncio.run(
        run_once(app_config(), repository, adapter, notifier, dry_run=False, checked_at=CHECKED_AT)
    )

    assert result.exit_code == 0
    assert repository.current.document.snapshots[key] == snapshot(price="12")
    assert len(notifier.calls) == 1
    assert repository.current.document.pending_events == ()
    assert len(repository.current.document.delivered_event_ids) == 1
    assert result.output["changes"] == [
        {
            "kind": "price",
            "product_key": "p1",
            "product_name": "Product",
            "before": "10",
            "after": "12",
            "url": None,
        }
    ]


def test_missing_category_records_failure_while_other_category_succeeds() -> None:
    repository = MemoryRepository(StateDocument())
    adapter = Adapter({"General": CategoryObservation(products=(product(),), explicit_count=None)})

    result = asyncio.run(
        run_once(
            app_config("General", "Missing"),
            repository,
            adapter,
            Notifier(),
            dry_run=False,
            checked_at=CHECKED_AT,
        )
    )

    assert result.exit_code == 0
    assert state_key("shop-a", "General") in repository.current.document.snapshots
    assert (
        repository.current.document.health[state_key("shop-a", "Missing")].consecutive_failures == 1
    )


def test_explicit_zero_requires_two_observations_before_replacing_snapshot() -> None:
    key = state_key("shop-a", "General")
    repository = MemoryRepository(StateDocument(snapshots={key: snapshot()}))
    adapter = Adapter({"General": CategoryObservation(products=(), explicit_count=0)})

    first = asyncio.run(
        run_once(
            app_config(), repository, adapter, Notifier(), dry_run=False, checked_at=CHECKED_AT
        )
    )
    second = asyncio.run(
        run_once(
            app_config(), repository, adapter, Notifier(), dry_run=False, checked_at=CHECKED_AT
        )
    )

    assert first.exit_code == second.exit_code == 0
    assert repository.current.document.snapshots[key].products == ()
    assert key not in repository.current.document.empty_candidates


def test_malformed_pending_part_fails_safely_without_sending_or_collecting() -> None:
    bad = PendingEvent(sequence=1, event_id="bad", message_parts=("[]",))
    repository = MemoryRepository(StateDocument(pending_events=(bad,), next_event_sequence=2))
    adapter = Adapter()
    notifier = Notifier()

    result = asyncio.run(
        run_once(app_config(), repository, adapter, notifier, dry_run=False, checked_at=CHECKED_AT)
    )

    assert result.exit_code == 3
    assert notifier.calls == []
    assert adapter.calls == []
    assert "[]" not in json.dumps(result.output)


def test_delivery_confirmation_retries_conflicts_without_resending() -> None:
    key = state_key("shop-a", "General")
    repository = MemoryRepository(
        StateDocument(
            snapshots={key: Snapshot(monitor_id="shop-a", category="General", products=())},
            pending_events=(pending(1, "a"),),
            next_event_sequence=2,
        ),
        statuses=[PublishStatus.CONFLICT, PublishStatus.CONFLICT, PublishStatus.PUBLISHED],
    )
    notifier = Notifier()

    result = asyncio.run(
        run_once(
            app_config(), repository, Adapter(), notifier, dry_run=False, checked_at=CHECKED_AT
        )
    )

    assert result.exit_code == 0
    assert len(notifier.calls) == 1
    assert len(repository.publishes) == 3
    assert repository.current.document.delivered_event_ids == ("a",)


def test_new_notification_is_persisted_before_send_and_failure_leaves_it_pending() -> None:
    key = state_key("shop-a", "General")
    repository = MemoryRepository(StateDocument(snapshots={key: snapshot()}))
    adapter = Adapter(
        {"General": CategoryObservation(products=(product(price="12"),), explicit_count=None)}
    )

    class InspectingNotifier(Notifier):
        async def send_parts(self, parts: Any) -> None:
            assert repository.current.document.snapshots[key] == snapshot(price="12")
            assert len(repository.current.document.pending_events) == 1
            await super().send_parts(parts)

    result = asyncio.run(
        run_once(
            app_config(),
            repository,
            adapter,
            InspectingNotifier(fail_on=1),
            dry_run=False,
            checked_at=CHECKED_AT,
        )
    )

    assert result.exit_code == 3
    assert repository.current.document.snapshots[key] == snapshot(price="12")
    assert len(repository.current.document.pending_events) == 1
    assert repository.current.document.delivered_event_ids == ()


def test_state_publish_conflict_reuses_cached_observation() -> None:
    repository = MemoryRepository(
        StateDocument(), statuses=[PublishStatus.CONFLICT, PublishStatus.PUBLISHED]
    )
    adapter = Adapter({"General": CategoryObservation(products=(product(),), explicit_count=None)})

    result = asyncio.run(
        run_once(
            app_config(), repository, adapter, Notifier(), dry_run=False, checked_at=CHECKED_AT
        )
    )

    assert result.exit_code == 0
    assert adapter.calls == ["shop-a"]
    assert len(repository.publishes) == 2


def test_conflict_remote_already_contains_recomputed_state_does_not_republish() -> None:
    updated_key = state_key("shop-a", "General")

    class ConcurrentRepository(MemoryRepository):
        def publish(
            self, expected_parent: str | None, state: StateDocument, message: str
        ) -> PublishResult:
            self.publishes.append((expected_parent, state, message))
            self.current = VersionedState("v2", StateDocument(snapshots={updated_key: snapshot()}))
            return PublishResult(PublishStatus.CONFLICT, "v2")

    repository = ConcurrentRepository(StateDocument())
    adapter = Adapter({"General": CategoryObservation(products=(product(),), explicit_count=None)})

    result = asyncio.run(
        run_once(
            app_config(), repository, adapter, Notifier(), dry_run=False, checked_at=CHECKED_AT
        )
    )

    assert result.exit_code == 0
    assert len(repository.publishes) == 1


def test_oversized_notification_is_a_safe_exit_three() -> None:
    key = state_key("shop-a", "General")
    huge = product().model_copy(update={"name": "x" * 20_000})
    repository = MemoryRepository(StateDocument(snapshots={key: snapshot()}))
    adapter = Adapter({"General": CategoryObservation(products=(huge,), explicit_count=None)})

    result = asyncio.run(
        run_once(
            app_config(), repository, adapter, Notifier(), dry_run=False, checked_at=CHECKED_AT
        )
    )

    assert result.exit_code == 3
    assert repository.publishes == []


def test_new_event_id_hashes_parent_plus_canonical_event_json() -> None:
    key = state_key("shop-a", "General")
    repository = MemoryRepository(StateDocument(snapshots={key: snapshot()}))
    adapter = Adapter(
        {"General": CategoryObservation(products=(product(price="12"),), explicit_count=None)}
    )

    result = asyncio.run(
        run_once(
            app_config(),
            repository,
            adapter,
            Notifier(fail_on=1),
            dry_run=False,
            checked_at=CHECKED_AT,
        )
    )

    assert result.exit_code == 3
    event_document = {
        "checked_at": CHECKED_AT.isoformat(),
        "changes": result.output["changes"],
        "health_events": [],
    }
    canonical = json.dumps(
        event_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    expected = hashlib.sha256(f"v1{canonical}".encode()).hexdigest()
    assert repository.current.document.pending_events[0].event_id == expected


def test_confirmation_reload_does_not_send_a_later_event_already_delivered_remotely() -> None:
    key = state_key("shop-a", "General")
    first = pending(1, "a")
    second = pending(2, "b")

    class ConcurrentDeliveryRepository(MemoryRepository):
        def publish(
            self, expected_parent: str | None, state: StateDocument, message: str
        ) -> PublishResult:
            self.publishes.append((expected_parent, state, message))
            self.current = VersionedState(
                "v2",
                StateDocument(
                    snapshots={key: Snapshot(monitor_id="shop-a", category="General", products=())},
                    delivered_event_ids=("a", "b"),
                    next_event_sequence=3,
                ),
            )
            return PublishResult(PublishStatus.CONFLICT, "v2")

    repository = ConcurrentDeliveryRepository(
        StateDocument(pending_events=(first, second), next_event_sequence=3)
    )
    notifier = Notifier()

    result = asyncio.run(
        run_once(
            app_config(), repository, Adapter(), notifier, dry_run=False, checked_at=CHECKED_AT
        )
    )

    assert result.exit_code == 0
    assert [[part["content"]["text"] for part in call] for call in notifier.calls] == [["a"]]
