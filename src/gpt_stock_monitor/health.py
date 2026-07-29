"""Pure transitions for monitor health and explicit-empty confirmation."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from gpt_stock_monitor.diff import compare_snapshots
from gpt_stock_monitor.models import (
    Change,
    EmptyCandidate,
    HealthRecord,
    Snapshot,
    StateDocument,
)

__all__ = [
    "HealthEvent",
    "HealthEventKind",
    "Transition",
    "observe_explicit_zero",
    "record_failure",
    "record_success",
]

_MAX_REASON_LENGTH = 240
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_SENSITIVE_KEY_PATTERN = (
    r"[a-z0-9_-]*(?:authorization|api[-_]?key|cookie|password|passwd|token|secret)"
    r"[a-z0-9_-]*"
)
_SENSITIVE_PAIR_PREFIX = (
    rf"(?<![a-z0-9_-])[\"']?{_SENSITIVE_KEY_PATTERN}[\"']?\s*[:=]\s*"
)
_QUOTED_SECRET_PATTERN = re.compile(
    _SENSITIVE_PAIR_PREFIX +
    r'(?:"[^"]*"|\'[^\']*\')',
    re.IGNORECASE,
)
_UNQUOTED_SECRET_PATTERN = re.compile(
    _SENSITIVE_PAIR_PREFIX + r"[^,;}\])\r\n]*",
    re.IGNORECASE,
)


class HealthEventKind(StrEnum):
    """Kinds of monitor-health event emitted by a transition."""

    FAILURE = "failure"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class HealthEvent:
    """A pure-data monitor-health event awaiting later presentation."""

    kind: HealthEventKind
    key: str
    count: int
    reason: str | None = None


@dataclass(frozen=True)
class Transition:
    """A rebuilt state document and the events caused by one observation."""

    state: StateDocument
    events: tuple[HealthEvent | Change, ...]


def _scope_from_key(key: str) -> tuple[str, str]:
    parts = key.split("\x1f")
    if (
        len(parts) != 2
        or not parts[0]
        or not parts[1]
        or parts[0].strip() != parts[0]
        or parts[1].strip() != parts[1]
    ):
        raise ValueError("invalid state key")
    return parts[0], parts[1]


def _payload(state: StateDocument) -> dict[str, Any]:
    payload = state.model_dump(mode="json")
    if not isinstance(payload, dict):
        raise TypeError("state serialization must produce a dictionary")
    return payload


def _rebuilt(payload: dict[str, Any]) -> StateDocument:
    return StateDocument.model_validate(payload)


def _should_notify_failure(count: int) -> bool:
    return count in {1, 3} or (count >= 12 and count % 12 == 0)


def _sanitize_reason(reason: str) -> str:
    text = str(reason)
    text = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in text
    )
    text = " ".join(text.split())
    text = _URL_PATTERN.sub("<url>", text)
    text = _QUOTED_SECRET_PATTERN.sub("<redacted>", text)
    text = _UNQUOTED_SECRET_PATTERN.sub("<redacted>", text)
    if len(text) > _MAX_REASON_LENGTH:
        text = f"{text[: _MAX_REASON_LENGTH - 3].rstrip()}..."
    return text


def record_failure(state: StateDocument, key: str, reason: str) -> Transition:
    """Record one category failure and emit only scheduled reminders."""
    _scope_from_key(key)
    payload = _payload(state)
    health = payload["health"]
    empty_candidates = payload["empty_candidates"]
    if not isinstance(health, dict) or not isinstance(empty_candidates, dict):
        raise TypeError("state mappings must serialize as dictionaries")

    previous = state.health.get(key)
    count = 1 if previous is None else previous.consecutive_failures + 1
    notify = _should_notify_failure(count)
    safe_reason = _sanitize_reason(reason)
    health[key] = HealthRecord(
        consecutive_failures=count,
        last_error=safe_reason,
        failure_notified=(previous.failure_notified if previous is not None else False) or notify,
    ).model_dump(mode="json")
    empty_candidates.pop(key, None)

    events: tuple[HealthEvent | Change, ...] = ()
    if notify:
        events = (
            HealthEvent(
                kind=HealthEventKind.FAILURE,
                key=key,
                count=count,
                reason=safe_reason,
            ),
        )
    return Transition(state=_rebuilt(payload), events=events)


def record_success(state: StateDocument, key: str) -> Transition:
    """Record a non-empty success, recovering health and clearing empty confirmation."""
    _scope_from_key(key)
    payload = _payload(state)
    health = payload["health"]
    empty_candidates = payload["empty_candidates"]
    if not isinstance(health, dict) or not isinstance(empty_candidates, dict):
        raise TypeError("state mappings must serialize as dictionaries")

    previous = state.health.get(key)
    health.pop(key, None)
    empty_candidates.pop(key, None)

    events: tuple[HealthEvent | Change, ...] = ()
    if previous is not None and previous.consecutive_failures > 0:
        events = (
            HealthEvent(
                kind=HealthEventKind.RECOVERY,
                key=key,
                count=previous.consecutive_failures,
            ),
        )
    return Transition(state=_rebuilt(payload), events=events)


def observe_explicit_zero(state: StateDocument, key: str) -> Transition:
    """Confirm an explicit empty category twice before replacing its snapshot."""
    monitor_id, category = _scope_from_key(key)
    payload = _payload(state)
    snapshots = payload["snapshots"]
    health = payload["health"]
    empty_candidates = payload["empty_candidates"]
    if (
        not isinstance(snapshots, dict)
        or not isinstance(health, dict)
        or not isinstance(empty_candidates, dict)
    ):
        raise TypeError("state mappings must serialize as dictionaries")

    previous_health = state.health.get(key)
    health.pop(key, None)
    events: list[HealthEvent | Change] = []
    if previous_health is not None and previous_health.consecutive_failures > 0:
        events.append(
            HealthEvent(
                kind=HealthEventKind.RECOVERY,
                key=key,
                count=previous_health.consecutive_failures,
            )
        )

    previous_snapshot = state.snapshots.get(key)
    candidate = state.empty_candidates.get(key)
    if previous_snapshot is not None and not previous_snapshot.products and candidate is None:
        return Transition(state=_rebuilt(payload), events=tuple(events))

    if candidate is None:
        empty_candidates[key] = EmptyCandidate().model_dump(mode="json")
        return Transition(state=_rebuilt(payload), events=tuple(events))

    empty_candidates.pop(key, None)
    empty_snapshot = Snapshot(monitor_id=monitor_id, category=category, products=())
    snapshots[key] = empty_snapshot.model_dump(mode="json")
    if previous_snapshot is not None:
        events.extend(compare_snapshots(previous_snapshot, empty_snapshot))
    return Transition(state=_rebuilt(payload), events=tuple(events))
