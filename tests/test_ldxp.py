from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Route, async_playwright

from gpt_stock_monitor.config import MonitorConfig
from gpt_stock_monitor.models import Availability
from gpt_stock_monitor.sites.base import (
    CategoryNotFoundError,
    InteractiveChallengeError,
    SiteNavigationError,
    SuspiciousExtractionError,
)
from gpt_stock_monitor.sites.ldxp import LdxpAdapter

SHOP_URL = "https://pay.ldxp.cn/shop/NIFGEAC5"
CATEGORY = "GPT-plus成品号"
FIXTURES = Path(__file__).parent / "fixtures" / "ldxp"


def config(*categories: str) -> MonitorConfig:
    return MonitorConfig.model_validate(
        {
            "id": "ldxp-test",
            "name": "LDXP test shop",
            "url": SHOP_URL,
            "categories": list(categories or (CATEGORY,)),
        }
    )


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def shop_html(
    cards: list[tuple[str, str, str]],
    *,
    category: str = CATEGORY,
    displayed_count: int | None = None,
) -> str:
    count = len(cards) if displayed_count is None else displayed_count
    card_html = "".join(
        f"""
        <div class="goods_item has_image">
          <div class="name">{name}</div>
          <div class="goods-price"><div class="currency">￥</div>
            <div class="nowPrice">{price}</div></div>
          <span class="stock">{stock}</span>
        </div>
        """
        for name, price, stock in cards
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head>
    <body><div class="fl_box_leng fl_box_leng_xz"><div>{category}</div>
    <div>共{count}种商品</div></div>{card_html}</body></html>"""


HARNESS = """
({ links }) => {
  document.addEventListener("DOMContentLoaded", () => {
    window.__fixtureClicks = [];
    window.__fixtureMaxLiveModals = 0;
    document.querySelectorAll(".goods_item.has_image").forEach((card, index) => {
      card.addEventListener("click", () => {
        document.querySelectorAll(".arco-modal.confirm_order").forEach(node => node.remove());
        const modal = document.createElement("div");
        modal.className = "arco-modal confirm_order";
        const spec = links[index];
        if (spec.href !== null) {
          const anchor = document.createElement("a");
          anchor.href = spec.href;
          anchor.textContent = "商品详情";
          modal.append(anchor);
        }
        if (spec.max !== null) {
          const input = document.createElement("input");
          input.setAttribute("role", "spinbutton");
          input.setAttribute("aria-valuemax", spec.max);
          modal.append(input);
        }
        document.body.append(modal);
        window.__fixtureClicks.push(index);
        window.__fixtureMaxLiveModals = Math.max(
          window.__fixtureMaxLiveModals,
          document.querySelectorAll(".arco-modal.confirm_order").length
        );
      });
    });
  });
}
"""


async def _collect(
    html: str,
    links: list[dict[str, str | None]],
    *,
    monitor: MonitorConfig | None = None,
    inspect_page: bool = False,
    navigation_requests: list[str] | None = None,
    abort_shop_navigation: bool = False,
) -> tuple[Any, dict[str, Any]]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        harness_argument = json.dumps({"links": links}, ensure_ascii=False)
        await page.add_init_script(script=f"({HARNESS})({harness_argument})")

        async def route_request(route: Route) -> None:
            if route.request.url == SHOP_URL:
                if navigation_requests is not None:
                    navigation_requests.append(route.request.url)
                if abort_shop_navigation:
                    await route.abort()
                else:
                    await route.fulfill(
                        status=200, content_type="text/html; charset=utf-8", body=html
                    )
            else:
                await route.abort()

        await page.route("**/*", route_request)
        if inspect_page:
            await page.goto(SHOP_URL, wait_until="domcontentloaded")
            assert await page.locator("a[href^='/item/']").count() == 0
            assert await page.locator(".arco-modal.confirm_order").count() == 0

        observation = await LdxpAdapter(page, navigation_timeout_ms=2_000).collect(
            monitor or config()
        )
        harness_state = await page.evaluate(
            """() => ({
              clicks: window.__fixtureClicks,
              maxLiveModals: window.__fixtureMaxLiveModals,
              liveModals: document.querySelectorAll('.arco-modal.confirm_order').length
            })"""
        )
        await browser.close()
        return observation, harness_state


def collect(
    html: str,
    links: list[dict[str, str | None]],
    *,
    monitor: MonitorConfig | None = None,
    inspect_page: bool = False,
    navigation_requests: list[str] | None = None,
    abort_shop_navigation: bool = False,
) -> tuple[Any, dict[str, Any]]:
    return asyncio.run(
        _collect(
            html,
            links,
            monitor=monitor,
            inspect_page=inspect_page,
            navigation_requests=navigation_requests,
            abort_shop_navigation=abort_shop_navigation,
        )
    )


def test_collects_sampled_card_details_from_post_click_canonical_link() -> None:
    observations, state = collect(
        fixture("in_stock.html"),
        [{"href": "/item/3xxf1n", "max": None}],
        inspect_page=True,
    )

    product = observations[CATEGORY].products[0]
    assert product.key == "3xxf1n"
    assert product.url == "https://pay.ldxp.cn/item/3xxf1n"
    assert product.name == "gpt 直卡 plus"
    assert product.price == "13.5"
    assert product.price_text == "￥13.5"
    assert product.availability is Availability.LOW_STOCK
    assert product.stock_text == "库存少量"
    assert observations[CATEGORY].explicit_count is None
    assert state == {"clicks": [0], "maxLiveModals": 1, "liveModals": 0}


def test_two_cards_require_clicks_and_yield_distinct_stable_ids() -> None:
    html = shop_html([("Product A", "10", "有货"), ("Product B", "20", "缺货")])

    observations, state = collect(
        html,
        [
            {"href": "/item/item-a", "max": "9"},
            {"href": "/item/item-b", "max": "0"},
        ],
        inspect_page=True,
    )

    assert [product.key for product in observations[CATEGORY].products] == ["item-a", "item-b"]
    assert state == {"clicks": [0, 1], "maxLiveModals": 1, "liveModals": 0}


@pytest.mark.parametrize(
    ("stock_text", "expected"),
    [
        ("有货", Availability.IN_STOCK),
        ("库存少量", Availability.LOW_STOCK),
        ("缺货", Availability.OUT_OF_STOCK),
        ("等待补充", Availability.UNKNOWN),
    ],
)
def test_maps_four_inventory_states(stock_text: str, expected: Availability) -> None:
    observations, _ = collect(
        shop_html([("Product", "10", stock_text)]),
        [{"href": "/item/item-1", "max": None}],
    )

    product = observations[CATEGORY].products[0]
    assert product.availability is expected
    assert product.stock_text == stock_text


def test_modal_explicit_zero_quantity_is_out_of_stock_evidence() -> None:
    observations, _ = collect(
        fixture("changed.html"),
        [{"href": "/item/3xxf1n", "max": "0"}],
    )

    product = observations[CATEGORY].products[0]
    assert product.availability is Availability.OUT_OF_STOCK
    assert product.price == "14"


def test_accepts_only_visible_exact_zero_as_empty_observation() -> None:
    observations, _ = collect(fixture("explicit_zero.html"), [])

    assert observations[CATEGORY].products == ()
    assert observations[CATEGORY].explicit_count == 0


def test_rejects_count_that_disagrees_with_visible_cards() -> None:
    with pytest.raises(SuspiciousExtractionError):
        collect(
            shop_html([("Product", "10", "有货")], displayed_count=2),
            [{"href": "/item/item-1", "max": None}],
        )


def test_rejects_duplicate_ids_from_two_clicked_cards() -> None:
    with pytest.raises(SuspiciousExtractionError):
        collect(
            shop_html([("A", "10", "有货"), ("B", "20", "有货")]),
            [
                {"href": "/item/same-id", "max": None},
                {"href": "/item/same-id", "max": None},
            ],
        )


@pytest.mark.parametrize(
    "href",
    [
        None,
        "/item/",
        "/item/unsafe/id",
        "/item/item-1?next=evil",
        "/item/item-1#frag",
        "https://evil.example/item/item-1",
    ],
)
def test_rejects_missing_or_noncanonical_item_link(href: str | None) -> None:
    with pytest.raises(SuspiciousExtractionError):
        collect(
            shop_html([("Product", "10", "有货")]),
            [{"href": href, "max": None}],
        )


def test_challenge_page_raises_interactive_challenge_error() -> None:
    with pytest.raises(InteractiveChallengeError):
        collect(fixture("challenge.html"), [])


def test_missing_exact_category_raises_after_at_most_three_attempts() -> None:
    navigation_requests: list[str] = []

    with pytest.raises(CategoryNotFoundError):
        collect(
            fixture("missing_category.html"), [], navigation_requests=navigation_requests
        )

    assert navigation_requests == [SHOP_URL, SHOP_URL, SHOP_URL]


def test_normalizes_category_whitespace_but_requires_exact_text() -> None:
    observations, _ = collect(
        shop_html([("Product", "10", "有货")], category=" \n GPT-plus成品号 \t"),
        [{"href": "/item/item-1", "max": None}],
    )

    assert tuple(observations) == (CATEGORY,)


def test_rejects_unapproved_final_url_after_navigation() -> None:
    html = """<!doctype html><script>
    history.replaceState({}, '', '/shop/OTHER');
    </script><div class="fl_box_leng"><div>GPT-plus成品号</div><div>共0种商品</div></div>"""

    with pytest.raises(SiteNavigationError):
        collect(html, [])


def test_navigation_failure_raises_site_navigation_error() -> None:
    with pytest.raises(SiteNavigationError):
        collect("", [], abort_shop_navigation=True)
