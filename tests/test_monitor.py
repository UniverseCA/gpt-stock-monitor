from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

import gpt_stock_monitor.monitor as monitor_module
from gpt_stock_monitor.config import AppConfig
from gpt_stock_monitor.models import (
    Availability,
    PendingEvent,
    Product,
    Snapshot,
    StateDocument,
    state_key,
)
from gpt_stock_monitor.monitor import run_once
from gpt_stock_monitor.notifiers.feishu import FeishuError
from gpt_stock_monitor.sites.base import CategoryObservation
from gpt_stock_monitor.state import (
    PublishResult,
    PublishStatus,
    StateError,
    StateRepositoryError,
    VersionedState,
)

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
            raise FeishuError("webhook secret=do-not-leak")


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
        availability=Availability.IN_STOCK,
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


def test_health_events_are_split_on_item_boundaries_below_message_limit() -> None:
    categories = tuple(f"category-{index:03d}-{'x' * 100}" for index in range(220))
    repository = MemoryRepository(StateDocument())
    notifier = Notifier()

    result = asyncio.run(
        run_once(
            app_config(*categories),
            repository,
            Adapter(),
            notifier,
            dry_run=False,
            checked_at=CHECKED_AT,
        )
    )

    assert result.exit_code == 0
    assert len(notifier.calls) == 1
    sent_parts = notifier.calls[0]
    assert len(sent_parts) > 1
    pending_event = repository.publishes[0][1].pending_events[0]
    assert pending_event.message_parts == tuple(
        json.dumps(part, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for part in sent_parts
    )
    total = len(sent_parts)
    for index, part in enumerate(sent_parts, start=1):
        encoded = json.dumps(part, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert len(encoded) <= 18_000
        text = part["content"]["text"]
        assert f"Event: {pending_event.event_id}" in text
        assert f"Part: {index}/{total}" in text


def test_single_oversized_health_item_fails_before_publish_without_echoing_content() -> None:
    category = "oversized-" + "x" * 19_000
    repository = MemoryRepository(StateDocument())
    notifier = Notifier()

    result = asyncio.run(
        run_once(
            app_config(category),
            repository,
            Adapter(),
            notifier,
            dry_run=False,
            checked_at=CHECKED_AT,
        )
    )

    assert result.exit_code == 3
    assert repository.publishes == []
    assert notifier.calls == []
    assert category not in json.dumps(result.output)


def test_mixed_change_and_health_parts_all_respect_message_limit() -> None:
    key = state_key("shop-a", "General")
    health_categories = tuple(f"missing-{index:03d}-{'y' * 100}" for index in range(220))
    repository = MemoryRepository(StateDocument(snapshots={key: snapshot()}))
    notifier = Notifier()
    adapter = Adapter(
        {"General": CategoryObservation(products=(product(price="12"),), explicit_count=None)}
    )

    result = asyncio.run(
        run_once(
            app_config("General", *health_categories),
            repository,
            adapter,
            notifier,
            dry_run=False,
            checked_at=CHECKED_AT,
        )
    )

    assert result.exit_code == 0
    sent_parts = notifier.calls[0]
    assert "Monitor health" not in sent_parts[0]["content"]["text"]
    assert all(
        len(json.dumps(part, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 18_000
        for part in sent_parts
    )
    first_health = next(
        index
        for index, part in enumerate(sent_parts)
        if "Monitor health" in part["content"]["text"]
    )
    assert all("Monitor health" in part["content"]["text"] for part in sent_parts[first_health:])


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
        "checked_at": CHECKED_AT.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "changes": result.output["changes"],
        "health_events": [],
    }
    canonical = json.dumps(
        event_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    expected = hashlib.sha256(f"v1{canonical}".encode()).hexdigest()
    assert repository.current.document.pending_events[0].event_id == expected


def test_event_id_uses_canonical_utc_instant_not_input_timezone() -> None:
    key = state_key("shop-a", "General")
    initial = StateDocument(snapshots={key: snapshot()})
    observation = {
        "General": CategoryObservation(products=(product(price="12"),), explicit_count=None)
    }
    utc_repository = MemoryRepository(initial)
    shanghai_repository = MemoryRepository(initial)

    utc_result = asyncio.run(
        run_once(
            app_config(),
            utc_repository,
            Adapter(observation),
            Notifier(fail_on=1),
            dry_run=False,
            checked_at=CHECKED_AT,
        )
    )
    shanghai_result = asyncio.run(
        run_once(
            app_config(),
            shanghai_repository,
            Adapter(observation),
            Notifier(fail_on=1),
            dry_run=False,
            checked_at=CHECKED_AT.astimezone(timezone(timedelta(hours=8))),
        )
    )

    assert utc_result.exit_code == shanghai_result.exit_code == 3
    assert (
        utc_repository.current.document.pending_events[0].event_id
        == shanghai_repository.current.document.pending_events[0].event_id
    )


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


def test_pending_injected_by_post_drain_reload_is_handled_before_collection() -> None:
    injected = StateDocument(pending_events=(pending(1, "old"),), next_event_sequence=2)

    class InjectingRepository(MemoryRepository):
        def load(self) -> VersionedState:
            self.loads += 1
            if self.loads == 2:
                self.current = VersionedState("v2", injected)
            return self.current

    repository = InjectingRepository(StateDocument())
    adapter = Adapter()
    notifier = Notifier(fail_on=1)

    result = asyncio.run(
        run_once(app_config(), repository, adapter, notifier, dry_run=False, checked_at=CHECKED_AT)
    )

    assert result.exit_code == 3
    assert [[part["content"]["text"] for part in call] for call in notifier.calls] == [["old"]]
    assert adapter.calls == []


def test_four_injected_pending_batches_are_all_drained_before_collection() -> None:
    class FourBatchRepository(MemoryRepository):
        def load(self) -> VersionedState:
            self.loads += 1
            sequence = self.loads
            if sequence <= 4:
                self.current = VersionedState(
                    f"load-{sequence}",
                    StateDocument(
                        pending_events=(pending(sequence, f"old-{sequence}"),),
                        delivered_event_ids=tuple(
                            f"old-{delivered}" for delivered in range(1, sequence)
                        ),
                        next_event_sequence=sequence + 1,
                    ),
                )
            return self.current

    repository = FourBatchRepository(StateDocument())
    adapter = Adapter()
    notifier = Notifier()

    result = asyncio.run(
        run_once(app_config(), repository, adapter, notifier, dry_run=False, checked_at=CHECKED_AT)
    )

    assert result.exit_code == 0
    assert [call[0]["content"]["text"] for call in notifier.calls] == [
        "old-1",
        "old-2",
        "old-3",
        "old-4",
    ]
    assert adapter.calls == ["shop-a"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"msg_type": "text", "content": {"text": 3}},
        {"msg_type": "text", "content": {"text": ""}},
        {"msg_type": "text", "content": {"text": "ok"}, "secret": "leak"},
        {"msg_type": "text", "content": {"text": "ok", "extra": "leak"}},
    ],
)
def test_persisted_parts_require_exact_text_payload_schema(payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, separators=(",", ":"))
    event = PendingEvent(sequence=1, event_id="bad", message_parts=(serialized,))
    repository = MemoryRepository(StateDocument(pending_events=(event,), next_event_sequence=2))
    adapter = Adapter()
    notifier = Notifier()

    result = asyncio.run(
        run_once(app_config(), repository, adapter, notifier, dry_run=False, checked_at=CHECKED_AT)
    )

    assert result.exit_code == 3
    assert notifier.calls == []
    assert repository.current.document.delivered_event_ids == ()
    assert "leak" not in json.dumps(result.output)
    assert adapter.calls == []


def test_adapter_assertion_error_propagates() -> None:
    class BrokenAdapter(Adapter):
        async def collect(self, config: Any) -> dict[str, CategoryObservation]:
            raise AssertionError("programming bug")

    with pytest.raises(AssertionError, match="programming bug"):
        asyncio.run(
            run_once(
                app_config(),
                MemoryRepository(StateDocument()),
                BrokenAdapter(),
                Notifier(),
                dry_run=True,
                checked_at=CHECKED_AT,
            )
        )


def test_notifier_assertion_error_propagates() -> None:
    class BrokenNotifier(Notifier):
        async def send_parts(self, parts: Any) -> None:
            raise AssertionError("programming bug")

    repository = MemoryRepository(
        StateDocument(pending_events=(pending(1, "old"),), next_event_sequence=2)
    )
    with pytest.raises(AssertionError, match="programming bug"):
        asyncio.run(
            run_once(
                app_config(),
                repository,
                Adapter(),
                BrokenNotifier(),
                dry_run=False,
                checked_at=CHECKED_AT,
            )
        )


def test_repository_assertion_error_propagates() -> None:
    class BrokenRepository(MemoryRepository):
        def load(self) -> VersionedState:
            raise AssertionError("programming bug")

    with pytest.raises(AssertionError, match="programming bug"):
        asyncio.run(
            run_once(
                app_config(),
                BrokenRepository(StateDocument()),
                Adapter(),
                Notifier(),
                dry_run=False,
                checked_at=CHECKED_AT,
            )
        )


def test_repository_boundary_error_is_a_safe_exit_three() -> None:
    class FailingRepository(MemoryRepository):
        def load(self) -> VersionedState:
            raise StateRepositoryError("safe repository failure")

    result = asyncio.run(
        run_once(
            app_config(),
            FailingRepository(StateDocument()),
            Adapter(),
            Notifier(),
            dry_run=False,
            checked_at=CHECKED_AT,
        )
    )

    assert result.exit_code == 3
    assert result.output == {"error": "state load failed"}


def test_invalid_repository_state_is_a_safe_exit_three_without_side_effects() -> None:
    class InvalidStateRepository(MemoryRepository):
        def load(self) -> VersionedState:
            self.loads += 1
            raise StateError("secret invalid state contents")

    repository = InvalidStateRepository(StateDocument())
    adapter = Adapter()
    notifier = Notifier()

    result = asyncio.run(
        run_once(
            app_config(),
            repository,
            adapter,
            notifier,
            dry_run=False,
            checked_at=CHECKED_AT,
        )
    )

    assert result.exit_code == 3
    assert result.output == {"error": "state load failed"}
    assert "secret" not in json.dumps(result.output)
    assert repository.loads == 1
    assert repository.publishes == []
    assert adapter.calls == []
    assert notifier.calls == []


def test_monitor_does_not_import_concrete_state_git_backend() -> None:
    source = inspect.getsource(monitor_module)

    assert "gpt_stock_monitor.state_git" not in source
    assert "StateGitError" not in source
