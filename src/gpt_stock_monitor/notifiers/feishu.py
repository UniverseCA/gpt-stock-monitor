"""Build and send size-bounded Feishu webhook messages."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from unicodedata import category
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from gpt_stock_monitor.models import Change, ChangeKind

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class FeishuError(RuntimeError):
    """A safely redacted Feishu delivery failure."""


class MessageTooLargeError(FeishuError):
    """A single change cannot fit in a Feishu message part."""


def _sanitize_text(value: str) -> str:
    characters = (" " if category(character).startswith("C") else character for character in value)
    collapsed = " ".join("".join(characters).split())
    return collapsed.replace("<", "\uff1c").replace(">", "\uff1e")


def _display_text(value: str | None, fallback: str) -> str:
    if value is None:
        return fallback
    return _sanitize_text(value) or fallback


def _safe_url(value: str | None) -> str | None:
    if value is None or any(
        category(character).startswith("C") or character.isspace() or character in '<>"'
        for character in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value


def _change_text(change: Change) -> str:
    labels = {
        ChangeKind.ADDED: "新增",
        ChangeKind.REMOVED: "移除",
        ChangeKind.AVAILABILITY: "可用状态",
        ChangeKind.STOCK_TEXT: "库存",
        ChangeKind.PRICE: "价格",
        ChangeKind.NAME: "名称",
    }
    label = labels[change.kind]
    if change.kind is ChangeKind.ADDED:
        detail = _display_text(change.after, "已新增")
    elif change.kind is ChangeKind.REMOVED:
        detail = _display_text(change.before, "已移除")
    else:
        before = _display_text(change.before, "无")
        after = _display_text(change.after, "无")
        detail = f"{before} → {after}"

    product_name = _display_text(change.product_name, "(空白名称)")
    lines = [f"- {label} | {product_name} | {detail}"]
    url = _safe_url(change.url)
    if url is not None:
        lines.append(f"  链接: {url}")
    return "\n".join(lines)


def _payload(
    event_id: str,
    checked_at_text: str,
    items: Sequence[str],
    part_number: int,
    total_parts: int,
) -> dict[str, Any]:
    header = (
        f"库存变更通知\n事件: {event_id}\n时间: {checked_at_text}\n"
        f"分片: {part_number}/{total_parts}"
    )
    return {"msg_type": "text", "content": {"text": f"{header}\n\n" + "\n".join(items)}}


def _json_size(payload: dict[str, Any]) -> int:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(encoded)


def _partition(
    event_id: str,
    checked_at_text: str,
    item_texts: Sequence[str],
    total_parts: int,
    max_bytes: int,
) -> list[tuple[str, ...]]:
    groups: list[tuple[str, ...]] = []
    current: list[str] = []

    for item in item_texts:
        candidate = (*current, item)
        part_number = len(groups) + 1
        if (
            _json_size(_payload(event_id, checked_at_text, candidate, part_number, total_parts))
            <= max_bytes
        ):
            current.append(item)
            continue

        if not current:
            raise MessageTooLargeError("Feishu message item exceeds maximum size")
        groups.append(tuple(current))
        current = [item]
        part_number = len(groups) + 1
        if (
            _json_size(_payload(event_id, checked_at_text, current, part_number, total_parts))
            > max_bytes
        ):
            raise MessageTooLargeError("Feishu message item exceeds maximum size")

    if current:
        groups.append(tuple(current))
    return groups


def build_message_parts(
    event_id: str,
    changes: Sequence[Change],
    checked_at: datetime,
    max_bytes: int = 18_000,
) -> tuple[dict[str, Any], ...]:
    """Render deterministic Feishu payloads, split only between changes."""
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("checked_at must be timezone-aware")
    if not changes:
        return ()

    checked_at_text = checked_at.astimezone(_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")
    safe_event_id = _display_text(event_id, "(空白事件)")
    item_texts = tuple(_change_text(change) for change in changes)

    total_parts = 1
    while True:
        groups = _partition(safe_event_id, checked_at_text, item_texts, total_parts, max_bytes)
        if len(groups) == total_parts:
            break
        total_parts = len(groups)

    return tuple(
        _payload(safe_event_id, checked_at_text, group, index, total_parts)
        for index, group in enumerate(groups, start=1)
    )


def _is_numeric_zero(value: object) -> bool:
    return type(value) is int and value == 0


def _safe_json_type(value: object, *, present: bool) -> str:
    if not present:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "other"


class _FailureKind(Enum):
    TIMEOUT = "timeout"
    REQUEST = "request error"
    HTTP = "HTTP error"
    INVALID_JSON = "invalid JSON"
    INVALID_JSON_OBJECT = "invalid JSON object"
    BUSINESS = "business status"


@dataclass(frozen=True)
class _DeliveryFailure:
    kind: _FailureKind
    http_status: int | None = None
    code_type: str = "missing"
    legacy_type: str = "missing"


class _SecretWebhook:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted Feishu webhook>"


async def _post_and_validate(
    client: httpx.AsyncClient,
    webhook: _SecretWebhook,
    part: dict[str, Any],
    timeout: float | httpx.Timeout,
) -> _DeliveryFailure | None:
    try:
        response = await client.post(webhook.reveal(), json=part, timeout=timeout)
    except httpx.TimeoutException:
        return _DeliveryFailure(_FailureKind.TIMEOUT)
    except Exception:
        return _DeliveryFailure(_FailureKind.REQUEST)

    try:
        return _validate_response(response)
    except Exception:
        return _DeliveryFailure(_FailureKind.REQUEST)


def _validate_response(response: httpx.Response) -> _DeliveryFailure | None:
    status = response.status_code
    if not 200 <= status < 300:
        safe_status = status if type(status) is int and 100 <= status <= 599 else None
        return _DeliveryFailure(_FailureKind.HTTP, http_status=safe_status)

    try:
        body = response.json()
    except Exception:
        return _DeliveryFailure(_FailureKind.INVALID_JSON)
    if not isinstance(body, dict):
        return _DeliveryFailure(_FailureKind.INVALID_JSON_OBJECT)

    code_present = "code" in body
    legacy_present = "StatusCode" in body
    code = body.get("code")
    legacy_code = body.get("StatusCode")
    if code_present:
        if _is_numeric_zero(code):
            return None
    elif legacy_present and _is_numeric_zero(legacy_code):
        return None

    return _DeliveryFailure(
        _FailureKind.BUSINESS,
        code_type=_safe_json_type(code, present=code_present),
        legacy_type=_safe_json_type(legacy_code, present=legacy_present),
    )


def _raise_delivery_error(failure: _DeliveryFailure) -> None:
    if failure.kind is _FailureKind.HTTP:
        status = f" status {failure.http_status}" if failure.http_status is not None else ""
        message = f"Feishu delivery failed: HTTP{status}"
    elif failure.kind is _FailureKind.BUSINESS:
        message = (
            "Feishu delivery failed: business status "
            f"(code={failure.code_type}, StatusCode={failure.legacy_type})"
        )
    else:
        message = f"Feishu delivery failed: {failure.kind.value}"
    raise FeishuError(message) from None


class FeishuNotifier:
    """Send pre-built parts through an explicitly owned HTTP client."""

    def __init__(
        self,
        webhook_url: str,
        *,
        client: httpx.AsyncClient,
        timeout: float | httpx.Timeout = 10.0,
    ) -> None:
        self._webhook = _SecretWebhook(webhook_url)
        self._client = client
        self._timeout = timeout

    async def send_parts(self, parts: Sequence[dict[str, Any]]) -> None:
        """POST each part in order and stop at the first failure."""
        for part in parts:
            failure = await _post_and_validate(
                self._client,
                self._webhook,
                part,
                self._timeout,
            )
            if failure is None:
                continue
            del part
            del parts
            _raise_delivery_error(failure)
