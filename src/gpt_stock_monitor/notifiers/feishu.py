"""Build and send size-bounded Feishu webhook messages."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from gpt_stock_monitor.models import Change, ChangeKind

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class FeishuError(RuntimeError):
    """A safely redacted Feishu delivery failure."""


class MessageTooLargeError(FeishuError):
    """A single change cannot fit in a Feishu message part."""


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
        detail = change.after or "已新增"
    elif change.kind is ChangeKind.REMOVED:
        detail = change.before or "已移除"
    else:
        detail = f"{change.before or '无'} → {change.after or '无'}"

    lines = [f"- {label} | {change.product_name} | {detail}"]
    if change.url is not None:
        lines.append(f"  链接: {change.url}")
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
    item_texts = tuple(_change_text(change) for change in changes)

    total_parts = 1
    while True:
        groups = _partition(event_id, checked_at_text, item_texts, total_parts, max_bytes)
        if len(groups) == total_parts:
            break
        total_parts = len(groups)

    return tuple(
        _payload(event_id, checked_at_text, group, index, total_parts)
        for index, group in enumerate(groups, start=1)
    )


def _is_numeric_zero(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and value == 0


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


class FeishuNotifier:
    """Send pre-built parts through an explicitly owned HTTP client."""

    def __init__(
        self,
        webhook_url: str,
        *,
        client: httpx.AsyncClient,
        timeout: float | httpx.Timeout = 10.0,
    ) -> None:
        self._webhook_url = webhook_url
        self._client = client
        self._timeout = timeout

    async def send_parts(self, parts: Sequence[dict[str, Any]]) -> None:
        """POST each part in order and stop at the first failure."""
        for part in parts:
            failure: str | None = None
            response: httpx.Response | None = None
            try:
                response = await self._client.post(
                    self._webhook_url,
                    json=part,
                    timeout=self._timeout,
                )
            except httpx.TimeoutException:
                failure = "Feishu delivery failed: timeout"
            except httpx.RequestError:
                failure = "Feishu delivery failed: request error"

            if failure is not None:
                raise FeishuError(failure) from None
            assert response is not None

            if not 200 <= response.status_code < 300:
                raise FeishuError(
                    f"Feishu delivery failed: HTTP status {response.status_code}"
                ) from None

            invalid_json = False
            body: object = None
            try:
                body = response.json()
            except ValueError:
                invalid_json = True
            if invalid_json:
                raise FeishuError("Feishu delivery failed: invalid JSON") from None
            if not isinstance(body, dict):
                raise FeishuError("Feishu delivery failed: invalid JSON object") from None

            code_present = "code" in body
            legacy_present = "StatusCode" in body
            code = body.get("code")
            legacy_code = body.get("StatusCode")
            if _is_numeric_zero(code) or _is_numeric_zero(legacy_code):
                continue

            code_type = _safe_json_type(code, present=code_present)
            legacy_type = _safe_json_type(legacy_code, present=legacy_present)
            raise FeishuError(
                "Feishu delivery failed: business status "
                f"(code={code_type}, StatusCode={legacy_type})"
            ) from None
