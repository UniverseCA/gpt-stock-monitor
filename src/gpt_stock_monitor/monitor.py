"""One-shot monitoring orchestration with durable notification delivery."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from gpt_stock_monitor.config import AppConfig
from gpt_stock_monitor.diff import compare_snapshots
from gpt_stock_monitor.health import (
    HealthEvent,
    observe_explicit_zero,
    record_failure,
    record_success,
)
from gpt_stock_monitor.models import Change, PendingEvent, Snapshot, StateDocument, state_key
from gpt_stock_monitor.notifiers.feishu import (
    FeishuError,
    MessageTooLargeError,
    build_message_parts,
)
from gpt_stock_monitor.sites.base import (
    CategoryNotFoundError,
    CategoryObservation,
    InteractiveChallengeError,
    SiteAdapter,
    SiteNavigationError,
    SuspiciousExtractionError,
)
from gpt_stock_monitor.state import (
    PublishStatus,
    StateRepository,
    StateRepositoryError,
    VersionedState,
    prune_unconfigured,
)

_SITE_ERRORS = (
    SiteNavigationError,
    InteractiveChallengeError,
    CategoryNotFoundError,
    SuspiciousExtractionError,
)


class Notifier(Protocol):
    """Delivery boundary for already-built notification payloads."""

    async def send_parts(self, parts: Sequence[dict[str, object]]) -> None: ...


@dataclass(frozen=True)
class RunResult:
    """Stable process result and JSON-safe summary."""

    exit_code: int
    output: dict[str, object]


def _output(state: StateDocument) -> dict[str, object]:
    return {
        "pending_events": [
            {
                "event_id": event.event_id,
                "sequence": event.sequence,
                "part_count": len(event.message_parts),
            }
            for event in state.pending_events
        ],
        "changes": [],
        "health_events": [],
    }


@dataclass(frozen=True)
class _Collected:
    observation: CategoryObservation | None
    failure_reason: str | None


@dataclass(frozen=True)
class _Applied:
    state: StateDocument
    changes: tuple[Change, ...]
    health_events: tuple[HealthEvent, ...]


def _parse_parts(event: PendingEvent) -> tuple[dict[str, object], ...] | None:
    parts: list[dict[str, object]] = []
    try:
        for serialized in event.message_parts:
            payload = json.loads(serialized)
            if not isinstance(payload, dict) or set(payload) != {"msg_type", "content"}:
                return None
            content = payload.get("content")
            if (
                payload.get("msg_type") != "text"
                or not isinstance(content, dict)
                or set(content) != {"text"}
                or not isinstance(content.get("text"), str)
                or not content["text"].strip()
            ):
                return None
            parts.append(payload)
    except json.JSONDecodeError:
        return None
    return tuple(parts)


def _delivered(state: StateDocument, event_id: str) -> StateDocument:
    payload = state.model_dump(mode="json")
    payload["pending_events"] = [
        event for event in payload["pending_events"] if event["event_id"] != event_id
    ]
    delivered = list(payload["delivered_event_ids"])
    if event_id not in delivered:
        delivered.append(event_id)
    payload["delivered_event_ids"] = delivered
    return StateDocument.model_validate(payload)


def _with_snapshot(state: StateDocument, key: str, snapshot: Snapshot) -> StateDocument:
    payload = state.model_dump(mode="json")
    snapshots = payload["snapshots"]
    if not isinstance(snapshots, dict):
        raise TypeError("snapshot mapping must serialize as a dictionary")
    snapshots[key] = snapshot.model_dump(mode="json")
    return StateDocument.model_validate(payload)


async def _collect(config: AppConfig, adapter: SiteAdapter) -> dict[str, _Collected]:
    collected: dict[str, _Collected] = {}
    for monitor in config.monitors:
        try:
            observations = await adapter.collect(monitor)
            if not isinstance(observations, Mapping):
                raise TypeError("adapter result must be a mapping")
        except _SITE_ERRORS as exc:
            reason = type(exc).__name__
            for category in monitor.categories:
                collected[state_key(monitor.id, category)] = _Collected(None, reason)
            continue

        for category in monitor.categories:
            key = state_key(monitor.id, category)
            observation = observations.get(category)
            if not isinstance(observation, CategoryObservation):
                collected[key] = _Collected(None, "CategoryNotFoundError")
            else:
                collected[key] = _Collected(observation, None)
    return collected


def _apply_observations(
    base: StateDocument,
    config: AppConfig,
    collected: Mapping[str, _Collected],
) -> _Applied:
    active_keys = {
        state_key(monitor.id, category)
        for monitor in config.monitors
        for category in monitor.categories
    }
    state = prune_unconfigured(base, active_keys)
    changes: list[Change] = []
    health_events: list[HealthEvent] = []

    for monitor in config.monitors:
        for category in monitor.categories:
            key = state_key(monitor.id, category)
            result = collected[key]
            if result.failure_reason is not None:
                transition = record_failure(state, key, result.failure_reason)
                state = transition.state
            else:
                assert result.observation is not None
                if result.observation.products:
                    previous = state.snapshots.get(key)
                    transition = record_success(state, key)
                    state = transition.state
                    current = Snapshot(
                        monitor_id=monitor.id,
                        category=category,
                        products=result.observation.products,
                    )
                    if previous is not None:
                        changes.extend(compare_snapshots(previous, current))
                    state = _with_snapshot(state, key, current)
                else:
                    transition = observe_explicit_zero(state, key)
                    state = transition.state
            for event in transition.events:
                if isinstance(event, Change):
                    changes.append(event)
                else:
                    health_events.append(event)
    return _Applied(state, tuple(changes), tuple(health_events))


def _event_document(applied: _Applied, checked_at: datetime) -> dict[str, object]:
    return {
        "checked_at": checked_at.isoformat(),
        "changes": [change.model_dump(mode="json") for change in applied.changes],
        "health_events": [
            {
                "kind": event.kind.value,
                "key": event.key,
                "count": event.count,
                "reason": event.reason,
            }
            for event in applied.health_events
        ],
    }


def _event_id(parent: str | None, event_document: Mapping[str, object]) -> str:
    canonical = json.dumps(
        event_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    material = f"{parent if parent is not None else '<none>'}{canonical}".encode()
    return hashlib.sha256(material).hexdigest()


def _health_part(event_id: str, events: Sequence[HealthEvent]) -> dict[str, object]:
    lines = [f"Monitor health\nEvent: {event_id}"]
    for event in events:
        line = f"- {event.kind.value} | {event.key} | count={event.count}"
        if event.reason is not None:
            line = f"{line} | {event.reason}"
        lines.append(line)
    return {"msg_type": "text", "content": {"text": "\n".join(lines)}}


def _with_pending(
    state: StateDocument,
    parent: str | None,
    applied: _Applied,
    checked_at: datetime,
) -> tuple[StateDocument, PendingEvent]:
    event_id = _event_id(parent, _event_document(applied, checked_at))
    parts: list[dict[str, object]] = list(
        build_message_parts(event_id, applied.changes, checked_at)
    )
    if applied.health_events:
        parts.append(_health_part(event_id, applied.health_events))
    serialized = tuple(
        json.dumps(part, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for part in parts
    )
    event = PendingEvent(
        sequence=state.next_event_sequence,
        event_id=event_id,
        message_parts=serialized,
    )
    payload = state.model_dump(mode="json")
    payload["pending_events"] = [*payload["pending_events"], event.model_dump(mode="json")]
    payload["next_event_sequence"] = state.next_event_sequence + 1
    return StateDocument.model_validate(payload), event


def _summary(applied: _Applied, state: StateDocument) -> dict[str, object]:
    output = _output(state)
    output["changes"] = [change.model_dump(mode="json") for change in applied.changes]
    output["health_events"] = [
        {"kind": event.kind.value, "key": event.key, "count": event.count}
        for event in applied.health_events
    ]
    return output


async def _confirm_delivery(
    repository: StateRepository,
    current: VersionedState,
    event_id: str,
) -> VersionedState | None:
    for _ in range(3):
        if event_id in current.document.delivered_event_ids:
            return current
        if not any(event.event_id == event_id for event in current.document.pending_events):
            return None
        updated = _delivered(current.document, event_id)
        result = repository.publish(current.version, updated, "confirm delivered monitor event")
        if result.status is not PublishStatus.CONFLICT:
            return VersionedState(result.remote_version, updated)
        current = repository.load()
    return None


async def _drain_pending(
    repository: StateRepository,
    current: VersionedState,
    notifier: Notifier,
) -> VersionedState | RunResult:
    while True:
        if current.document.pending_events:
            for event in current.document.pending_events:
                if event.event_id in current.document.delivered_event_ids:
                    continue
                active_event = next(
                    (
                        pending
                        for pending in current.document.pending_events
                        if pending.event_id == event.event_id
                    ),
                    None,
                )
                if active_event is None:
                    return RunResult(3, _output(current.document))
                parts = _parse_parts(active_event)
                if parts is None:
                    return RunResult(3, {"error": "invalid pending event"})
                try:
                    await notifier.send_parts(parts)
                except FeishuError:
                    return RunResult(3, _output(current.document))
                try:
                    confirmed = await _confirm_delivery(repository, current, active_event.event_id)
                except StateRepositoryError:
                    return RunResult(3, _output(current.document))
                if confirmed is None:
                    return RunResult(3, _output(current.document))
                current = confirmed
        try:
            current = repository.load()
        except StateRepositoryError:
            return RunResult(3, {"error": "state load failed"})
        if not current.document.pending_events:
            return current


async def run_once(
    config: AppConfig,
    state_repo: StateRepository,
    adapter: SiteAdapter,
    notifier: Notifier,
    *,
    dry_run: bool,
    checked_at: datetime,
) -> RunResult:
    """Run one collection cycle and durably deliver its notifications."""
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("checked_at must be timezone-aware")

    try:
        current = state_repo.load()
    except StateRepositoryError:
        return RunResult(3, {"error": "state load failed"})

    if not dry_run:
        drained = await _drain_pending(state_repo, current, notifier)
        if isinstance(drained, RunResult):
            return drained
        current = drained

    collected = await _collect(config, adapter)
    applied = _apply_observations(current.document, config, collected)
    if dry_run:
        return RunResult(0, _summary(applied, current.document))

    staged_event: PendingEvent | None = None
    staged = applied.state
    if applied.changes or applied.health_events:
        try:
            staged, staged_event = _with_pending(staged, current.version, applied, checked_at)
        except MessageTooLargeError:
            return RunResult(3, _summary(applied, current.document))

    if staged != current.document:
        for _ in range(3):
            try:
                published = state_repo.publish(
                    current.version, staged, "update one-shot monitor state"
                )
            except StateRepositoryError:
                return RunResult(3, _summary(applied, current.document))
            if published.status is not PublishStatus.CONFLICT:
                current = VersionedState(published.remote_version, staged)
                break
            try:
                current = state_repo.load()
            except StateRepositoryError:
                return RunResult(3, _summary(applied, current.document))
            if current.document.pending_events:
                return RunResult(3, _summary(applied, current.document))
            applied = _apply_observations(current.document, config, collected)
            staged = applied.state
            staged_event = None
            if applied.changes or applied.health_events:
                try:
                    staged, staged_event = _with_pending(
                        staged, current.version, applied, checked_at
                    )
                except MessageTooLargeError:
                    return RunResult(3, _summary(applied, current.document))
            if staged == current.document:
                break
        else:
            return RunResult(3, _summary(applied, current.document))

    if staged_event is not None:
        parts = _parse_parts(staged_event)
        assert parts is not None
        try:
            await notifier.send_parts(parts)
        except FeishuError:
            return RunResult(3, _summary(applied, current.document))
        try:
            confirmed = await _confirm_delivery(state_repo, current, staged_event.event_id)
        except StateRepositoryError:
            return RunResult(3, _summary(applied, current.document))
        if confirmed is None:
            return RunResult(3, _summary(applied, current.document))
        current = confirmed

    return RunResult(0, _summary(applied, current.document))
