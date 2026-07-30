from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from playwright.async_api import Error as PlaywrightError
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
    window.__fixtureCloseClicks = 0;
    window.__fixtureMaxLiveModals = 0;
    window.__fixturePendingStale = null;
    const openModal = (spec, card) => {
      const modal = document.createElement("div");
      modal.className = "arco-modal confirm_order";
      const productName = document.createElement("div");
      productName.className = "pr_name";
      productName.textContent = spec.name ?? card.querySelector(".name").textContent;
      modal.append(productName);
      const close = document.createElement("div");
      close.className = "arco-modal-close-btn";
      close.setAttribute("role", "button");
      close.setAttribute("aria-label", "Close");
      close.setAttribute("tabindex", "-1");
      close.textContent = "x";
      close.addEventListener("click", () => {
        window.__fixtureCloseClicks += 1;
        if (!spec.closeFails) modal.remove();
      });
      modal.append(close);
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
      window.__fixtureMaxLiveModals = Math.max(
        window.__fixtureMaxLiveModals,
        document.querySelectorAll(".arco-modal.confirm_order").length
      );
    };
    document.querySelectorAll(".goods_item.has_image").forEach((card, index) => {
      card.addEventListener("click", () => {
        if (window.__fixturePendingStale) {
          const pending = window.__fixturePendingStale;
          window.__fixturePendingStale = null;
          setTimeout(() => openModal(pending.spec, pending.card), pending.spec.delay);
        }
        const spec = links[index];
        if (spec.url) history.replaceState({}, "", spec.url);
        if (spec.delay) setTimeout(() => openModal(spec, card), spec.delay);
        else openModal(spec, card);
        if (spec.stale) {
          window.__fixturePendingStale = {spec: spec.stale, card};
        }
        window.__fixtureClicks.push(index);
      });
    });
  });
}
"""


async def _collect(
    html: str,
    links: list[dict[str, Any]],
    *,
    monitor: MonitorConfig | None = None,
    inspect_page: bool = False,
    navigation_requests: list[str] | None = None,
    abort_shop_navigation: bool = False,
    extra_init_script: str = "",
    close_page_after_ms: int | None = None,
) -> tuple[Any, dict[str, Any]]:
    background_tasks: set[asyncio.Task[None]] = set()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        harness_argument = json.dumps({"links": links}, ensure_ascii=False)
        await page.add_init_script(script=f"({HARNESS})({harness_argument});\n{extra_init_script}")

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

        await page.context.route("**/*", route_request)
        if close_page_after_ms is not None:

            async def close_page_later() -> None:
                await page.wait_for_timeout(close_page_after_ms)
                await page.close()

            close_task = asyncio.create_task(close_page_later())
            background_tasks.add(close_task)
            close_task.add_done_callback(background_tasks.discard)
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
              closeClicks: window.__fixtureCloseClicks,
              maxLiveModals: window.__fixtureMaxLiveModals,
              liveModals: document.querySelectorAll('.arco-modal.confirm_order').length
            })"""
        )
        await browser.close()
        return observation, harness_state


def collect(
    html: str,
    links: list[dict[str, Any]],
    *,
    monitor: MonitorConfig | None = None,
    inspect_page: bool = False,
    navigation_requests: list[str] | None = None,
    abort_shop_navigation: bool = False,
    extra_init_script: str = "",
    close_page_after_ms: int | None = None,
) -> tuple[Any, dict[str, Any]]:
    return asyncio.run(
        _collect(
            html,
            links,
            monitor=monitor,
            inspect_page=inspect_page,
            navigation_requests=navigation_requests,
            abort_shop_navigation=abort_shop_navigation,
            extra_init_script=extra_init_script,
            close_page_after_ms=close_page_after_ms,
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
    assert state["clicks"] == [0]
    assert state["maxLiveModals"] == 1
    assert state["liveModals"] == 0


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
    assert state["clicks"] == [0, 1]
    assert state["maxLiveModals"] == 1
    assert state["liveModals"] == 0


def test_product_modals_are_closed_through_confirmed_control_before_next_card() -> None:
    observations, state = collect(
        shop_html([("Product A", "10", "有货"), ("Product B", "20", "有货")]),
        [
            {"href": "/item/item-a", "max": None},
            {"href": "/item/item-b", "max": None},
        ],
    )

    assert [product.key for product in observations[CATEGORY].products] == ["item-a", "item-b"]
    assert state["closeClicks"] == 2
    assert state["maxLiveModals"] == 1
    assert state["liveModals"] == 0


def test_ignores_delayed_mismatched_stale_modal_and_waits_for_clicked_card() -> None:
    observations, state = collect(
        shop_html([("Product A", "10", "有货"), ("Product B", "20", "有货")]),
        [
            {
                "href": "/item/item-a",
                "max": None,
                "stale": {
                    "href": "/item/stale-x",
                    "max": None,
                    "name": "Product A",
                    "delay": 100,
                },
            },
            {"href": "/item/item-b", "max": None, "delay": 1000},
        ],
    )

    assert [product.key for product in observations[CATEGORY].products] == ["item-a", "item-b"]
    assert state["closeClicks"] == 3
    assert state["maxLiveModals"] == 1


def test_rejects_duplicate_product_names_before_clicking_cards() -> None:
    with pytest.raises(SuspiciousExtractionError, match="product names are ambiguous"):
        collect(
            shop_html([("Same", "10", "有货"), ("Same", "20", "有货")]),
            [
                {
                    "href": "/item/item-a",
                    "max": None,
                    "stale": {
                        "href": "/item/stale-x",
                        "max": None,
                        "name": "Same",
                        "delay": 100,
                    },
                },
                {"href": "/item/item-b", "max": None, "delay": 1000},
            ],
        )


def test_rejects_url_change_caused_by_product_click() -> None:
    with pytest.raises(SiteNavigationError):
        collect(
            shop_html([("Product", "10", "有货")]),
            [{"href": "/item/item-1", "max": None, "url": "/shop/OTHER"}],
        )


def test_successful_extraction_fails_if_modal_cannot_be_closed() -> None:
    with pytest.raises(SuspiciousExtractionError):
        collect(
            shop_html([("Product", "10", "有货")]),
            [{"href": "/item/item-1", "max": None, "closeFails": True}],
        )


def test_cleanup_failure_does_not_mask_existing_extraction_error() -> None:
    with pytest.raises(SuspiciousExtractionError, match="lacks one canonical item link"):
        collect(
            shop_html([("Product", "10", "有货")]),
            [{"href": None, "max": None, "closeFails": True}],
        )


def test_playwright_page_lifecycle_error_never_escapes_collect() -> None:
    with pytest.raises((SiteNavigationError, SuspiciousExtractionError)):
        collect(
            shop_html([("Product", "10", "有货")]),
            [{"href": "/item/item-1", "max": None}],
            close_page_after_ms=20,
        )


def test_body_playwright_error_is_mapped_to_site_navigation_error() -> None:
    async def exercise() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def route_request(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=shop_html([]),
                )

            await page.context.route("**/*", route_request)
            with patch.object(page, "locator", side_effect=PlaywrightError("body unavailable")):
                await LdxpAdapter(page, navigation_timeout_ms=2_000).collect(config())

    with pytest.raises(SiteNavigationError):
        asyncio.run(exercise())


@pytest.mark.parametrize(
    "intermediate_url",
    [
        "https://evil.example/redirect",
        "http://127.0.0.1/redirect",
        "https://pay.ldxp.cn/shop/OTHER",
        "https://pay.ldxp.cn/shop/NIFGEAC5?next=1",
        "https://pay.ldxp.cn:444/shop/NIFGEAC5",
    ],
)
def test_aborts_unapproved_redirect_hop_before_existing_route_observes_it(
    intermediate_url: str,
) -> None:
    observed_requests: list[str] = []

    async def exercise() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            returning_to_shop = False

            async def route_request(route: Route) -> None:
                nonlocal returning_to_shop
                observed_requests.append(route.request.url)
                if route.request.url == SHOP_URL:
                    if returning_to_shop:
                        await route.fulfill(
                            status=200,
                            content_type="text/html; charset=utf-8",
                            body=shop_html([]),
                        )
                    else:
                        await route.fulfill(
                            status=302,
                            headers={"location": intermediate_url},
                        )
                elif route.request.url == intermediate_url:
                    returning_to_shop = True
                    await route.fulfill(status=302, headers={"location": SHOP_URL})
                else:
                    await route.abort()

            await page.context.route("**/*", route_request)
            try:
                await LdxpAdapter(page, navigation_timeout_ms=2_000).collect(config())
            finally:
                await browser.close()

    with pytest.raises(SiteNavigationError) as exc_info:
        asyncio.run(exercise())

    assert intermediate_url not in observed_requests
    assert intermediate_url not in "".join(
        traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb)
    )


def test_unroutes_failed_monitor_guard_before_collecting_another_monitor() -> None:
    other_shop_url = "https://pay.ldxp.cn/shop/OTHER"
    intermediate_url = "https://evil.example/redirect"

    async def exercise() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def route_request(route: Route) -> None:
                if route.request.url == SHOP_URL:
                    await route.fulfill(status=302, headers={"location": intermediate_url})
                elif route.request.url in {intermediate_url, other_shop_url}:
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=shop_html([]),
                    )
                else:
                    await route.abort()

            await page.context.route("**/*", route_request)
            try:
                with pytest.raises(SiteNavigationError):
                    await LdxpAdapter(page, navigation_timeout_ms=2_000).collect(config())

                other_monitor = MonitorConfig.model_validate(
                    {
                        "id": "other",
                        "name": "Other",
                        "url": other_shop_url,
                        "categories": [CATEGORY],
                    }
                )
                observations = await LdxpAdapter(page, navigation_timeout_ms=2_000).collect(
                    other_monitor
                )
                assert tuple(observations) == (CATEGORY,)
            finally:
                await browser.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "popup_url",
    [
        "https://evil.example/popup",
        "http://127.0.0.1/popup",
    ],
)
def test_context_guard_aborts_popup_first_navigation_before_existing_route(
    popup_url: str,
) -> None:
    observed_requests: list[str] = []

    async def exercise() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            page_html = shop_html([]).replace(
                "</body>",
                f"""<script>window.open({json.dumps(popup_url)}, "_blank")</script></body>""",
            )

            async def route_request(route: Route) -> None:
                observed_requests.append(route.request.url)
                if route.request.url == SHOP_URL:
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=page_html,
                    )
                else:
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body="<body>unapproved popup</body>",
                    )

            await page.context.route("**/*", route_request)
            try:
                await LdxpAdapter(page, navigation_timeout_ms=2_000).collect(config())
            finally:
                await browser.close()

    with pytest.raises(SiteNavigationError) as exc_info:
        asyncio.run(exercise())

    assert popup_url not in observed_requests
    assert popup_url not in "".join(
        traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb)
    )


@pytest.mark.parametrize(
    "subresource_url",
    [
        "https://evil.example/app.js",
        "http://evil.example/app.js",
        "https://127.0.0.1/app.js",
        "https://10.0.0.1/app.js",
    ],
)
def test_aborts_unapproved_subresource_before_existing_route_observes_it(
    subresource_url: str,
) -> None:
    observed_requests: list[str] = []

    async def exercise() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            if subresource_url.startswith("http://"):
                page_html = shop_html([]).replace(
                    "</body>", f'<img src="{subresource_url}"></body>'
                )
            else:
                page_html = shop_html([]).replace(
                    "</head>", f'<script src="{subresource_url}"></script></head>'
                )

            async def route_request(route: Route) -> None:
                observed_requests.append(route.request.url)
                if route.request.url == SHOP_URL:
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=page_html,
                    )
                else:
                    await route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body="",
                    )

            await page.context.route("**/*", route_request)
            try:
                await LdxpAdapter(page, navigation_timeout_ms=2_000).collect(config())
            finally:
                await browser.close()

    with pytest.raises(SiteNavigationError):
        asyncio.run(exercise())

    assert subresource_url not in observed_requests


def test_allows_approved_default_port_https_subresource_via_existing_route() -> None:
    subresource_url = "https://pay.ldxp.cn/app.js"
    observed_requests: list[str] = []

    async def exercise() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def route_request(route: Route) -> None:
                observed_requests.append(route.request.url)
                if route.request.url == SHOP_URL:
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=shop_html([]).replace(
                            "</head>", f'<script src="{subresource_url}"></script></head>'
                        ),
                    )
                else:
                    await route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body="",
                    )

            await page.context.route("**/*", route_request)
            try:
                await LdxpAdapter(page, navigation_timeout_ms=2_000).collect(config())
            finally:
                await browser.close()

    asyncio.run(exercise())

    assert subresource_url in observed_requests


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
        collect(fixture("missing_category.html"), [], navigation_requests=navigation_requests)

    assert navigation_requests == [SHOP_URL, SHOP_URL, SHOP_URL]


def test_hidden_exact_category_is_not_a_trusted_match() -> None:
    html = f"""<!doctype html><html><body>
    <div class="fl_box_leng fl_box_leng_xz" style="display: none">
      <div>{CATEGORY}</div><div>共0种商品</div>
    </div>
    <div class="fl_box_leng"><div>其他分类</div><div>共0种商品</div></div>
    </body></html>"""

    with pytest.raises(CategoryNotFoundError):
        collect(html, [])


def test_hidden_category_count_is_not_trusted_zero_evidence() -> None:
    html = f"""<!doctype html><html><body>
    <div class="fl_box_leng fl_box_leng_xz">
      <div>{CATEGORY}</div><div style="display: none">共0种商品</div>
    </div>
    </body></html>"""

    with pytest.raises(SuspiciousExtractionError):
        collect(html, [])


def test_duplicate_visible_zero_counts_are_ambiguous() -> None:
    html = f"""<!doctype html><html><body>
    <div class="fl_box_leng fl_box_leng_xz">
      <div>{CATEGORY}</div><div>共0种商品</div><div>共0种商品</div>
    </div>
    </body></html>"""

    with pytest.raises(SuspiciousExtractionError):
        collect(html, [])


DELAYED_CATEGORY_SWITCH = """
document.addEventListener("DOMContentLoaded", () => {
  document.querySelector("#target-category").addEventListener("click", () => {
    setTimeout(() => {
      document.querySelector("#old-category").classList.remove("fl_box_leng_xz");
      document.querySelector("#target-category").classList.add("fl_box_leng_xz");
      document.querySelector(".goods_item.has_image .name").textContent = "Target product";
    }, 100);
  });
});
"""


def delayed_category_html() -> str:
    return f"""<!doctype html><html><body>
    <div id="old-category" class="fl_box_leng fl_box_leng_xz">
      <div>其他分类</div><div>共1种商品</div>
    </div>
    <div id="target-category" class="fl_box_leng">
      <div>{CATEGORY}</div><div>共1种商品</div>
    </div>
    <div class="goods_item has_image">
      <div class="name">Stale product</div>
      <div class="goods-price"><div class="currency">￥</div>
        <div class="nowPrice">10</div></div>
      <span class="stock">有货</span>
    </div>
    </body></html>"""


def test_waits_for_requested_category_to_be_visibly_selected() -> None:
    observations, _ = collect(
        delayed_category_html(),
        [{"href": "/item/target-id", "max": None}],
        extra_init_script=DELAYED_CATEGORY_SWITCH,
    )

    assert observations[CATEGORY].products[0].name == "Target product"


def test_waits_for_same_count_product_semantics_after_category_is_selected() -> None:
    split_switch = """
    document.addEventListener("DOMContentLoaded", () => {
      document.querySelector("#target-category").addEventListener("click", () => {
        setTimeout(() => {
          document.querySelector("#old-category").classList.remove("fl_box_leng_xz");
          document.querySelector("#target-category").classList.add("fl_box_leng_xz");
        }, 50);
        setTimeout(() => {
          document.querySelector(".goods_item.has_image .name").textContent = "Target product";
          document.querySelector(".goods_item.has_image .nowPrice").textContent = "20";
          document.querySelector(".goods_item.has_image .stock").textContent = "缺货";
        }, 500);
      });
    });
    """

    observations, _ = collect(
        delayed_category_html(),
        [{"href": "/item/target-id", "max": None}],
        extra_init_script=split_switch,
    )

    product = observations[CATEGORY].products[0]
    assert product.name == "Target product"
    assert product.price == "20"
    assert product.stock_text == "缺货"
    assert product.availability is Availability.OUT_OF_STOCK


def test_never_accepts_pre_click_fingerprint_after_a_brief_transition() -> None:
    reverting_switch = """
    document.addEventListener("DOMContentLoaded", () => {
      document.querySelector("#target-category").addEventListener("click", () => {
        setTimeout(() => {
          document.querySelector("#old-category").classList.remove("fl_box_leng_xz");
          document.querySelector("#target-category").classList.add("fl_box_leng_xz");
        }, 50);
        setTimeout(() => {
          let tick = 0;
          const changing = setInterval(() => {
            tick += 1;
            document.querySelector(".goods_item.has_image .name").textContent = `Transient ${tick}`;
          }, 10);
          setTimeout(() => {
            clearInterval(changing);
            document.querySelector(".goods_item.has_image .name").textContent = "Stale product";
          }, 500);
        }, 100);
      });
    });
    """

    with pytest.raises(SuspiciousExtractionError):
        collect(
            delayed_category_html(),
            [{"href": "/item/target-id", "max": None}],
            extra_init_script=reverting_switch,
        )


def test_rejects_unsafe_url_reached_during_delayed_category_switch() -> None:
    unsafe_switch = """
    document.addEventListener("DOMContentLoaded", () => {
      document.querySelector("#target-category").addEventListener("click", () => {
        setTimeout(() => {
          document.querySelector("#old-category").classList.remove("fl_box_leng_xz");
          document.querySelector("#target-category").classList.add("fl_box_leng_xz");
          history.replaceState({}, "", "/shop/OTHER");
        }, 100);
      });
    });
    """

    with pytest.raises(SiteNavigationError):
        collect(
            delayed_category_html(),
            [{"href": "/item/target-id", "max": None}],
            extra_init_script=unsafe_switch,
        )


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
    navigation_requests: list[str] = []

    with pytest.raises(SiteNavigationError):
        collect(
            "",
            [],
            abort_shop_navigation=True,
            navigation_requests=navigation_requests,
        )

    assert navigation_requests == [SHOP_URL, SHOP_URL, SHOP_URL]
