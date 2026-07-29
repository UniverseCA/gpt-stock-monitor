"""Playwright adapter for public LDXP shop pages."""

from __future__ import annotations

import re
from collections.abc import Mapping

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from gpt_stock_monitor.config import MonitorConfig, validate_final_url
from gpt_stock_monitor.models import Availability, Product
from gpt_stock_monitor.sites.base import (
    CategoryNotFoundError,
    CategoryObservation,
    InteractiveChallengeError,
    SiteNavigationError,
    SuspiciousExtractionError,
)

_COUNT = re.compile(r"^共\s*(\d+)\s*种商品$")
_ITEM_PATH = re.compile(r"^/item/([A-Za-z0-9_-]+)$")
_PRICE = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
_CHALLENGE_TEXT = re.compile(r"安全验证|人机验证")


def _visible_text(value: str) -> str:
    return " ".join(value.split())


class LdxpAdapter:
    """Collect configured categories through an ordinary Playwright page."""

    def __init__(self, page: Page, *, navigation_timeout_ms: int = 15_000) -> None:
        self._page = page
        self._navigation_timeout_ms = navigation_timeout_ms

    async def collect(self, config: MonitorConfig) -> Mapping[str, CategoryObservation]:
        observations: dict[str, CategoryObservation] = {}
        for category in config.categories:
            observations[category] = await self._collect_with_retries(config, category)
        return observations

    async def _collect_with_retries(
        self, config: MonitorConfig, category: str
    ) -> CategoryObservation:
        last_error: RuntimeError | None = None
        for _attempt in range(3):
            try:
                await self._navigate(config)
                return await self._extract_category(config, category)
            except InteractiveChallengeError:
                raise
            except (CategoryNotFoundError, SiteNavigationError, SuspiciousExtractionError) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def _navigate(self, config: MonitorConfig) -> None:
        navigation_error: PlaywrightError | None = None
        try:
            await self._page.goto(
                str(config.url),
                timeout=self._navigation_timeout_ms,
                wait_until="domcontentloaded",
            )
        except PlaywrightError as exc:
            navigation_error = exc

        self._validate_page_url(config)
        if navigation_error is not None:
            raise SiteNavigationError("shop navigation failed") from navigation_error

        body_text = await self._page.locator("body").inner_text()
        if _CHALLENGE_TEXT.search(_visible_text(body_text)):
            raise InteractiveChallengeError("shop requires interactive verification")

    def _validate_page_url(self, config: MonitorConfig) -> None:
        try:
            validate_final_url(config.shop_id, self._page.url)
        except ValueError as exc:
            raise SiteNavigationError("shop navigation reached an unapproved URL") from exc

    async def _extract_category(
        self, config: MonitorConfig, requested_category: str
    ) -> CategoryObservation:
        category = await self._find_category(requested_category)
        explicit_count = await self._category_count(category)
        if not await category.evaluate("node => node.classList.contains('fl_box_leng_xz')"):
            await category.click(timeout=self._navigation_timeout_ms)
            self._validate_page_url(config)

        await self._wait_for_product_area_stability(explicit_count)
        self._validate_page_url(config)

        cards = self._page.locator(".goods_item.has_image")
        card_count = await cards.count()
        if card_count != explicit_count:
            raise SuspiciousExtractionError("visible product count does not match category count")
        if card_count == 0:
            return CategoryObservation(products=(), explicit_count=0)

        products: list[Product] = []
        product_keys: set[str] = set()
        for index in range(card_count):
            product = await self._extract_product(cards.nth(index))
            if product.key in product_keys:
                raise SuspiciousExtractionError("duplicate canonical product id")
            product_keys.add(product.key)
            products.append(product)
        return CategoryObservation(products=tuple(products), explicit_count=None)

    async def _find_category(self, requested_category: str) -> Locator:
        expected = _visible_text(requested_category)
        matches: list[Locator] = []
        categories = self._page.locator(".fl_box_leng")
        for index in range(await categories.count()):
            candidate = categories.nth(index)
            child_texts = await candidate.locator(":scope > *").all_inner_texts()
            if expected in {_visible_text(text) for text in child_texts}:
                matches.append(candidate)
        if not matches:
            raise CategoryNotFoundError(f"configured category not found: {requested_category}")
        if len(matches) != 1:
            raise SuspiciousExtractionError("category identity is ambiguous")
        return matches[0]

    async def _wait_for_product_area_stability(self, expected_count: int) -> None:
        cards = self._page.locator(".goods_item.has_image")
        stable_samples = 0
        max_samples = max(3, self._navigation_timeout_ms // 50)
        for _sample in range(max_samples):
            count = await cards.count()
            if count == expected_count:
                stable_samples += 1
                if stable_samples == 2:
                    return
            else:
                stable_samples = 0
            await self._page.wait_for_timeout(50)
        raise SuspiciousExtractionError("product area did not become stable")

    async def _category_count(self, category: Locator) -> int:
        count_values = []
        for text in await category.locator(":scope > *").all_inner_texts():
            match = _COUNT.fullmatch(_visible_text(text))
            if match is not None:
                count_values.append(int(match.group(1)))
        if len(count_values) != 1:
            raise SuspiciousExtractionError("category lacks one trusted visible count")
        return count_values[0]

    async def _extract_product(self, card: Locator) -> Product:
        await self._remove_live_modals()
        try:
            name = _visible_text(await card.locator(".name").inner_text())
            currency = _visible_text(await card.locator(".goods-price .currency").inner_text())
            price = _visible_text(await card.locator(".goods-price .nowPrice").inner_text())
            stock_text = _visible_text(await card.locator(".stock").inner_text())
        except PlaywrightError as exc:
            raise SuspiciousExtractionError("product card is incomplete") from exc
        if not name or not currency or not _PRICE.fullmatch(price):
            raise SuspiciousExtractionError("product card contains invalid fields")

        try:
            await card.click(timeout=self._navigation_timeout_ms)
            modal = self._page.locator(".arco-modal.confirm_order")
            await modal.wait_for(state="attached", timeout=self._navigation_timeout_ms)
            if await modal.count() != 1:
                raise SuspiciousExtractionError("product click did not open exactly one modal")

            link = modal.locator('a[href^="/item/"]')
            if await link.count() != 1:
                raise SuspiciousExtractionError("product modal lacks one canonical item link")
            href = await link.get_attribute("href")
            match = _ITEM_PATH.fullmatch(href or "")
            if match is None:
                raise SuspiciousExtractionError("product item link is noncanonical")
            item_id = match.group(1)

            availability = self._availability(stock_text)
            quantity = modal.locator('input[role="spinbutton"]')
            if await quantity.count() == 1 and await quantity.get_attribute("aria-valuemax") == "0":
                availability = Availability.OUT_OF_STOCK
            return Product(
                key=item_id,
                name=name,
                price=price,
                price_text=f"{currency}{price}",
                availability=availability,
                stock_text=stock_text,
                url=f"https://pay.ldxp.cn/item/{item_id}",
            )
        except PlaywrightError as exc:
            raise SuspiciousExtractionError("product click did not expose stable identity") from exc
        finally:
            await self._remove_live_modals()

    async def _remove_live_modals(self) -> None:
        modals = self._page.locator(".arco-modal.confirm_order")
        for index in reversed(range(await modals.count())):
            await modals.nth(index).evaluate("node => node.remove()")

    @staticmethod
    def _availability(stock_text: str) -> Availability:
        if stock_text in {"有货", "库存充足"}:
            return Availability.IN_STOCK
        if stock_text == "库存少量":
            return Availability.LOW_STOCK
        if stock_text == "缺货":
            return Availability.OUT_OF_STOCK
        return Availability.UNKNOWN
