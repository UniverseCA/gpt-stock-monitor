from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path

import httpx
from playwright.async_api import Route, async_playwright

from gpt_stock_monitor.cli import RuntimeServices, main
from gpt_stock_monitor.models import PendingEvent, StateDocument
from gpt_stock_monitor.notifiers.feishu import FeishuNotifier
from gpt_stock_monitor.sites.ldxp import LdxpAdapter
from gpt_stock_monitor.state import (
    PublishResult,
    PublishStatus,
    VersionedState,
    load_state,
    serialize_state,
    write_state_atomic,
)

SHOP_URL = "https://pay.ldxp.cn/shop/NIFGEAC5"
CATEGORY = "GPT 商品"
WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/test-only"

MODAL_SCRIPT = """
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".goods_item.has_image").forEach((card) => {
    card.addEventListener("click", () => {
      const modal = document.createElement("div");
      modal.className = "arco-modal confirm_order";
      modal.innerHTML = `
        <div class="pr_name">${card.dataset.modalName}</div>
        <a href="/item/${card.dataset.itemId}">item</a>
        <input role="spinbutton" aria-valuemax="${card.dataset.max}">
        <button role="button" aria-label="Close">close</button>`;
      modal.querySelector("button").addEventListener("click", () => modal.remove());
      document.body.append(modal);
    });
  });
});
"""


def shop_html(products: list[tuple[str, str, str, str, str]]) -> str:
    cards = "".join(
        (
            '<div class="goods_item has_image" '
            f'data-item-id="{item_id}" data-modal-name="{name}" data-max="{maximum}">'
            f'<div class="name">{name}</div>'
            '<div class="goods-price"><div class="currency">¥</div>'
            f'<div class="nowPrice">{price}</div></div>'
            f'<span class="stock">{stock}</span></div>'
        )
        for item_id, name, price, stock, maximum in products
    )
    return (
        "<!doctype html><html><body>"
        f'<div class="fl_box_leng fl_box_leng_xz"><div>{CATEGORY}</div>'
        f"<div>共 {len(products)} 种商品</div></div>{cards}</body></html>"
    )


class FileStateRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> VersionedState:
        document = load_state(self.path)
        version = sha256(serialize_state(document)).hexdigest() if self.path.exists() else None
        return VersionedState(version, document)

    def publish(
        self,
        expected_parent: str | None,
        state: StateDocument,
        message: str,
    ) -> PublishResult:
        del message
        current = self.load()
        if current.version != expected_parent:
            return PublishResult(PublishStatus.CONFLICT, current.version)
        if current.document == state and self.path.exists():
            return PublishResult(PublishStatus.UNCHANGED, current.version)
        write_state_atomic(self.path, state)
        version = sha256(serialize_state(state)).hexdigest()
        return PublishResult(PublishStatus.PUBLISHED, version)


def test_real_cli_pipeline_and_dry_run_preserve_pending_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "monitors.yaml"
    config_path.write_text(
        (
            "monitors:\n"
            "  - id: shop\n"
            "    name: 集成商店\n"
            f"    url: {SHOP_URL}\n"
            "    categories:\n"
            f"      - {CATEGORY}\n"
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "state.json"
    repository = FileStateRepository(state_path)
    baseline = shop_html(
        [
            ("A", "Alpha", "10", "缺货", "0"),
            ("B", "Beta", "20", "有货", "5"),
            ("C", "Old Charlie", "30", "有货", "5"),
            ("D", "Delta", "40", "有货", "5"),
            ("E", "Echo", "50", "有货", "5"),
            ("F", "Foxtrot", "60", "有货", "5"),
        ]
    )
    changed = shop_html(
        [
            ("A", "Alpha", "10", "有货", "5"),
            ("B", "Beta", "20", "缺货", "0"),
            ("C", "New Charlie", "30", "有货", "5"),
            ("D", "Delta", "41", "有货", "5"),
            ("F", "Foxtrot", "60", "有货", "5"),
            ("G", "Golf", "70", "有货", "5"),
        ]
    )
    pages = iter((baseline, changed, changed))
    requests: list[dict[str, object]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0})

    @asynccontextmanager
    async def factory(webhook_url: str | None) -> AsyncIterator[RuntimeServices]:
        html = next(pages)
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.add_init_script(script=MODAL_SCRIPT)

        async def route_request(route: Route) -> None:
            if route.request.url == SHOP_URL:
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=html,
                )
            else:
                await route.abort()

        await page.route("**/*", route_request)
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        notifier = FeishuNotifier(webhook_url or WEBHOOK, client=client)
        try:
            yield RuntimeServices(repository, LdxpAdapter(page), notifier)
        finally:
            await client.aclose()
            await browser.close()
            await playwright.stop()

    monkeypatch.setenv("FEISHU_WEBHOOK_URL", WEBHOOK)

    assert main(["--config", str(config_path)], services_factory=factory) == 0
    first_output = json.loads(capsys.readouterr().out)
    assert first_output["changes"] == []
    assert state_path.exists()
    assert requests == []

    assert main(["--config", str(config_path)], services_factory=factory) == 0
    second_stdout = capsys.readouterr().out
    second_output = json.loads(second_stdout)
    change_kinds = {change["kind"] for change in second_output["changes"]}
    assert {"added", "removed", "availability", "name", "price"} <= change_kinds
    combined_notification = "\n".join(
        request["content"]["text"] for request in requests  # type: ignore[index]
    )
    for expected in (
        "Alpha",
        "Beta",
        "New Charlie",
        "Delta",
        "Echo",
        "Golf",
    ):
        assert expected in second_stdout
        assert expected in combined_notification

    state = load_state(state_path)
    payload = state.model_dump(mode="json")
    payload["pending_events"] = [
        PendingEvent(
            sequence=state.next_event_sequence,
            event_id="pending-dry-run",
            message_parts=('{"content":{"text":"pending"},"msg_type":"text"}',),
        ).model_dump(mode="json")
    ]
    payload["next_event_sequence"] = state.next_event_sequence + 1
    write_state_atomic(state_path, StateDocument.model_validate(payload))
    before = state_path.read_bytes()
    request_count = len(requests)

    assert (
        main(
            ["--config", str(config_path), "--dry-run"],
            services_factory=factory,
        )
        == 0
    )
    capsys.readouterr()
    assert state_path.read_bytes() == before
    assert len(requests) == request_count
