from __future__ import annotations

import asyncio
import json
import traceback
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from gpt_stock_monitor.models import Change, ChangeKind
from gpt_stock_monitor.notifiers.feishu import (
    FeishuError,
    FeishuNotifier,
    MessageTooLargeError,
    build_message_parts,
)

CHECKED_AT = datetime(2026, 7, 29, 1, 2, 3, tzinfo=UTC)


def make_change(
    kind: ChangeKind = ChangeKind.PRICE,
    *,
    name: str = "GPT Plus",
    before: str | None = "10.00",
    after: str | None = "12.00",
    url: str | None = "https://shop.example/items/plus",
) -> Change:
    return Change(
        kind=kind,
        product_key=f"key-{kind.value}",
        product_name=name,
        before=before,
        after=after,
        url=url,
    )


def encoded_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def payload_text(payload: dict[str, Any]) -> str:
    assert payload["msg_type"] == "text"
    content = payload["content"]
    assert isinstance(content, dict)
    text = content["text"]
    assert isinstance(text, str)
    return text


def test_build_message_parts_returns_nothing_for_no_changes() -> None:
    assert build_message_parts("event-1", (), CHECKED_AT) == ()


def test_build_message_parts_renders_all_change_kinds_and_url_under_one_event() -> None:
    changes = (
        make_change(ChangeKind.NAME, before="Old name", after="New name"),
        make_change(ChangeKind.PRICE, before="10.00", after="12.00"),
        make_change(ChangeKind.AVAILABILITY, before="out_of_stock", after="in_stock"),
        make_change(ChangeKind.STOCK_TEXT, before="无货", after="剩余 2 件"),
        make_change(ChangeKind.ADDED, before=None, after="已上架"),
        make_change(ChangeKind.REMOVED, before="已上架", after=None),
    )

    parts = build_message_parts("event-stable", changes, CHECKED_AT)

    assert len(parts) == 1
    text = payload_text(parts[0])
    assert "事件: event-stable" in text
    assert "时间: 2026-07-29 09:02:03 Asia/Shanghai" in text
    assert "分片: 1/1" in text
    for label in ("名称", "价格", "可用状态", "库存", "新增", "移除"):
        assert label in text
    for value in ("Old name", "New name", "10.00", "12.00", "剩余 2 件"):
        assert value in text
    assert "https://shop.example/items/plus" in text


def test_build_message_parts_splits_only_between_changes_and_preserves_order() -> None:
    changes = tuple(make_change(name=f"产品-{index}-" + "汉" * 30, url=None) for index in range(5))
    one_item_size = encoded_size(build_message_parts("event-2", changes[:1], CHECKED_AT)[0])
    max_bytes = one_item_size + 20

    parts = build_message_parts("event-2", changes, CHECKED_AT, max_bytes=max_bytes)

    assert len(parts) > 1
    combined = "\n".join(payload_text(part) for part in parts)
    positions = [combined.index(f"产品-{index}-") for index in range(5)]
    assert positions == sorted(positions)
    for index, part in enumerate(parts, start=1):
        text = payload_text(part)
        assert "事件: event-2" in text
        assert "时间: 2026-07-29 09:02:03 Asia/Shanghai" in text
        assert f"分片: {index}/{len(parts)}" in text
        assert encoded_size(part) <= max_bytes


def test_build_message_parts_uses_actual_utf8_json_boundary() -> None:
    changes = tuple(make_change(name="中" * 12, url=None) for _ in range(3))
    generous = build_message_parts("event-3", changes, CHECKED_AT)
    exact_limit = encoded_size(generous[0])

    assert len(build_message_parts("event-3", changes, CHECKED_AT, exact_limit)) == 1
    split = build_message_parts("event-3", changes, CHECKED_AT, exact_limit - 1)
    assert len(split) > 1
    assert all(encoded_size(part) <= exact_limit - 1 for part in split)


def test_build_message_parts_rejects_naive_checked_at() -> None:
    with pytest.raises(ValueError, match="checked_at must be timezone-aware"):
        build_message_parts("event-4", (make_change(),), datetime(2026, 7, 29))


def test_build_message_parts_oversize_error_does_not_echo_change_data() -> None:
    secret = "SENSITIVE-PRODUCT-AND-URL"
    change = make_change(name=secret * 10, url=f"https://example.invalid/{secret}")

    with pytest.raises(MessageTooLargeError) as caught:
        build_message_parts("event-5", (change,), CHECKED_AT, max_bytes=80)

    rendered = " ".join(
        (
            str(caught.value),
            repr(caught.value),
            repr(caught.value.args),
            "".join(traceback.format_exception(caught.value)),
        )
    )
    assert secret not in rendered
    assert str(caught.value) == "Feishu message item exceeds maximum size"


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: Sequence[httpx.Response | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses[len(self.requests) - 1]
        if isinstance(response, Exception):
            raise response
        return response


def run_send(
    responses: Sequence[httpx.Response | Exception],
    parts: Sequence[dict[str, Any]],
) -> tuple[list[httpx.Request], BaseException | None]:
    async def exercise() -> tuple[list[httpx.Request], BaseException | None]:
        transport = RecordingTransport(responses)
        async with httpx.AsyncClient(transport=transport) as client:
            notifier = FeishuNotifier(
                "https://open.feishu.invalid/hook/SENTINEL-WEBHOOK-SECRET",
                client=client,
                timeout=0.25,
            )
            try:
                await notifier.send_parts(parts)
            except BaseException as error:
                return transport.requests, error
        return transport.requests, None

    return asyncio.run(exercise())


@pytest.mark.parametrize(
    "body",
    [
        {"code": 0},
        {"StatusCode": 0},
        {"code": 1, "StatusCode": 0},
        {"code": 0, "StatusCode": 1},
        {"code": 0.0},
    ],
)
def test_send_parts_posts_in_order_for_documented_success_codes(body: dict[str, Any]) -> None:
    parts = ({"part": 1}, {"part": 2})
    requests, error = run_send(
        [httpx.Response(200, json=body), httpx.Response(204, json={"code": 0})],
        parts,
    )

    assert error is None
    assert [json.loads(request.content) for request in requests] == list(parts)


def test_send_parts_does_not_request_for_empty_parts() -> None:
    requests, error = run_send([], ())

    assert error is None
    assert requests == []


@pytest.mark.parametrize("body", [{"code": False}, {"StatusCode": False}, {"code": "0"}])
def test_send_parts_rejects_non_numeric_zero(body: dict[str, Any]) -> None:
    _, error = run_send([httpx.Response(200, json=body)], ({"part": 1},))

    assert isinstance(error, FeishuError)


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (httpx.Response(503, json={"code": 0}), "HTTP status 503"),
        (httpx.Response(200, json={"code": 923, "msg": "secret response"}), "business"),
        (httpx.Response(200, content=b"not-json SECRET-BODY"), "invalid JSON"),
    ],
)
def test_send_parts_returns_only_safe_response_failure_categories(
    response: httpx.Response, category: str
) -> None:
    _, error = run_send([response], ({"part": 1},))

    assert isinstance(error, FeishuError)
    assert category in str(error)
    rendered = " ".join(
        (str(error), repr(error), repr(error.args), "".join(traceback.format_exception(error)))
    )
    for secret in ("SENTINEL-WEBHOOK-SECRET", "secret response", "SECRET-BODY"):
        assert secret not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize("failure_kind", ["timeout", "request"])
def test_send_parts_redacts_request_exceptions(failure_kind: str) -> None:
    url = "https://open.feishu.invalid/hook/SENTINEL-WEBHOOK-SECRET"
    request = httpx.Request("POST", url)
    if failure_kind == "timeout":
        failure: Exception = httpx.ReadTimeout(f"timeout for {url}", request=request)
    else:
        failure = httpx.ConnectError(f"failed for {url}", request=request)

    _, error = run_send([failure], ({"part": 1},))

    assert isinstance(error, FeishuError)
    rendered = " ".join(
        (str(error), repr(error), repr(error.args), "".join(traceback.format_exception(error)))
    )
    assert "SENTINEL-WEBHOOK-SECRET" not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def test_send_parts_stops_after_first_failure() -> None:
    parts = ({"part": 1}, {"part": 2}, {"part": 3})
    requests, error = run_send(
        [httpx.Response(200, json={"code": 0}), httpx.Response(500), httpx.Response(200)],
        parts,
    )

    assert isinstance(error, FeishuError)
    assert len(requests) == 2
