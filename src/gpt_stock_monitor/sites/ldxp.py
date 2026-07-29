"""Playwright adapter for public LDXP shop pages."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page, Route

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
_ProductFingerprint = tuple[tuple[str, str, str, str], ...]


def _visible_text(value: str) -> str:
    return " ".join(value.split())


class LdxpAdapter:
    """Collect configured categories through an ordinary Playwright page."""

    def __init__(self, page: Page, *, navigation_timeout_ms: int = 15_000) -> None:
        self._page = page
        self._navigation_timeout_ms = navigation_timeout_ms
        self._unsafe_request_detected = False

    async def collect(self, config: MonitorConfig) -> Mapping[str, CategoryObservation]:
        self._unsafe_request_detected = False

        async def guard_request(route: Route) -> None:
            if self._request_is_approved(config, route):
                await route.fallback()
                return
            self._unsafe_request_detected = True
            await route.abort()

        context = self._page.context
        await context.route("**/*", guard_request)
        try:
            observations: dict[str, CategoryObservation] = {}
            for category in config.categories:
                observations[category] = await self._collect_with_retries(config, category)
            return observations
        finally:
            try:
                await context.unroute("**/*", guard_request)
            except PlaywrightError:
                if not self._page.is_closed():
                    raise

    def _request_is_approved(self, config: MonitorConfig, route: Route) -> bool:
        request = route.request
        if request.is_navigation_request():
            try:
                validate_final_url(config.shop_id, request.url)
            except ValueError:
                return False
            return True

        try:
            parsed = urlsplit(request.url)
            port = parsed.port
        except (TypeError, ValueError):
            return False
        return (
            parsed.scheme == "https"
            and parsed.hostname == "pay.ldxp.cn"
            and parsed.username is None
            and parsed.password is None
            and port in (None, 443)
        )

    def _raise_if_unsafe_request(self) -> None:
        if self._unsafe_request_detected:
            raise SiteNavigationError("shop request was blocked by navigation policy") from None

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
                if self._unsafe_request_detected:
                    raise
                last_error = exc
            except PlaywrightError as exc:
                last_error = SuspiciousExtractionError("site extraction lifecycle failed")
                last_error.__cause__ = exc
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

        self._raise_if_unsafe_request()
        self._validate_page_url(config)
        if navigation_error is not None:
            raise SiteNavigationError("shop navigation failed") from navigation_error

        try:
            body_text = await self._page.locator("body").inner_text()
        except PlaywrightError as exc:
            raise SiteNavigationError("shop body could not be read after navigation") from exc
        if _CHALLENGE_TEXT.search(_visible_text(body_text)):
            raise InteractiveChallengeError("shop requires interactive verification")

    def _validate_page_url(self, config: MonitorConfig) -> None:
        self._raise_if_unsafe_request()
        try:
            validate_final_url(config.shop_id, self._page.url)
        except ValueError as exc:
            raise SiteNavigationError("shop navigation reached an unapproved URL") from exc

    async def _extract_category(
        self, config: MonitorConfig, requested_category: str
    ) -> CategoryObservation:
        category = await self._find_category(requested_category)
        previous_fingerprint: _ProductFingerprint | None = None
        if not await self._is_selected(category):
            previous_fingerprint = await self._product_area_fingerprint()
            if previous_fingerprint is None:
                raise SuspiciousExtractionError("product area cannot be fingerprinted safely")
            await category.click(timeout=self._navigation_timeout_ms)
            self._validate_page_url(config)
            await self._wait_for_selected(config, category)
            category = await self._find_category(requested_category)
            self._validate_page_url(config)

        explicit_count = await self._category_count(category)
        await self._wait_for_product_area_stability(config, explicit_count, previous_fingerprint)
        self._validate_page_url(config)

        cards = self._page.locator(".goods_item.has_image")
        card_count = await cards.count()
        if card_count != explicit_count:
            raise SuspiciousExtractionError("visible product count does not match category count")
        if card_count == 0:
            return CategoryObservation(products=(), explicit_count=0)

        card_names: set[str] = set()
        for index in range(card_count):
            name_field = cards.nth(index).locator(".name")
            try:
                if await name_field.count() != 1 or not await name_field.is_visible():
                    raise SuspiciousExtractionError("product card lacks one visible name")
                card_name = _visible_text(await name_field.inner_text())
            except PlaywrightError as exc:
                raise SuspiciousExtractionError("product card name could not be read") from exc
            if not card_name:
                raise SuspiciousExtractionError("product card contains an empty name")
            if card_name in card_names:
                raise SuspiciousExtractionError("product names are ambiguous")
            card_names.add(card_name)

        products: list[Product] = []
        product_keys: set[str] = set()
        for index in range(card_count):
            product = await self._extract_product(config, cards.nth(index))
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
            if not await candidate.is_visible():
                continue
            child_texts = await self._visible_direct_child_texts(candidate)
            if expected in child_texts:
                matches.append(candidate)
        if not matches:
            raise CategoryNotFoundError(f"configured category not found: {requested_category}")
        if len(matches) != 1:
            raise SuspiciousExtractionError("category identity is ambiguous")
        return matches[0]

    async def _wait_for_selected(self, config: MonitorConfig, category: Locator) -> None:
        max_samples = max(1, self._navigation_timeout_ms // 50)
        for _sample in range(max_samples):
            self._validate_page_url(config)
            if await self._is_selected(category):
                return
            await self._page.wait_for_timeout(50)
        raise SuspiciousExtractionError("requested category did not become selected")

    @staticmethod
    async def _is_selected(category: Locator) -> bool:
        return await category.is_visible() and await category.evaluate(
            "node => node.classList.contains('fl_box_leng_xz')"
        )

    async def _wait_for_product_area_stability(
        self,
        config: MonitorConfig,
        expected_count: int,
        previous_fingerprint: _ProductFingerprint | None,
    ) -> None:
        stable_fingerprint: _ProductFingerprint | None = None
        stable_samples = 0
        max_samples = max(3, self._navigation_timeout_ms // 50)
        for _sample in range(max_samples):
            self._validate_page_url(config)
            fingerprint = await self._product_area_fingerprint()
            if fingerprint is None or len(fingerprint) != expected_count:
                stable_samples = 0
                stable_fingerprint = None
            elif previous_fingerprint is not None and fingerprint == previous_fingerprint:
                stable_samples = 0
                stable_fingerprint = None
            else:
                if fingerprint == stable_fingerprint:
                    stable_samples += 1
                else:
                    stable_fingerprint = fingerprint
                    stable_samples = 1
                if stable_samples == 2:
                    return
            await self._page.wait_for_timeout(50)
        raise SuspiciousExtractionError("product area did not become stable")

    async def _product_area_fingerprint(self) -> _ProductFingerprint | None:
        visible_cards: list[tuple[str, str, str, str]] = []
        cards = self._page.locator(".goods_item.has_image")
        try:
            for index in range(await cards.count()):
                card = cards.nth(index)
                if not await card.is_visible():
                    continue
                fields = []
                for selector in (
                    ".name",
                    ".goods-price .currency",
                    ".goods-price .nowPrice",
                    ".stock",
                ):
                    field = card.locator(selector)
                    if await field.count() != 1 or not await field.is_visible():
                        return None
                    fields.append(_visible_text(await field.inner_text()))
                visible_cards.append((fields[0], fields[1], fields[2], fields[3]))
        except PlaywrightError:
            return None
        return tuple(sorted(visible_cards))

    async def _category_count(self, category: Locator) -> int:
        count_values = []
        for text in await self._visible_direct_child_texts(category):
            match = _COUNT.fullmatch(text)
            if match is not None:
                count_values.append(int(match.group(1)))
        if len(count_values) != 1:
            raise SuspiciousExtractionError("category lacks one trusted visible count")
        return count_values[0]

    @staticmethod
    async def _visible_direct_child_texts(parent: Locator) -> list[str]:
        texts: list[str] = []
        children = parent.locator(":scope > *")
        for index in range(await children.count()):
            child = children.nth(index)
            if await child.is_visible():
                texts.append(_visible_text(await child.inner_text()))
        return texts

    async def _extract_product(self, config: MonitorConfig, card: Locator) -> Product:
        await self._close_live_modal(config)
        try:
            name = _visible_text(await card.locator(".name").inner_text())
            currency = _visible_text(await card.locator(".goods-price .currency").inner_text())
            price = _visible_text(await card.locator(".goods-price .nowPrice").inner_text())
            stock_text = _visible_text(await card.locator(".stock").inner_text())
        except PlaywrightError as exc:
            raise SuspiciousExtractionError("product card is incomplete") from exc
        if not name or not currency or not _PRICE.fullmatch(price):
            raise SuspiciousExtractionError("product card contains invalid fields")

        extraction_error: SiteNavigationError | SuspiciousExtractionError | None = None
        product: Product | None = None
        try:
            await card.click(timeout=self._navigation_timeout_ms)
            self._validate_page_url(config)
            item_id, modal_out_of_stock = await self._wait_for_matching_modal(config, name)
            availability = self._availability(stock_text)
            if modal_out_of_stock:
                availability = Availability.OUT_OF_STOCK
            product = Product(
                key=item_id,
                name=name,
                price=price,
                price_text=f"{currency}{price}",
                availability=availability,
                stock_text=stock_text,
                url=f"https://pay.ldxp.cn/item/{item_id}",
            )
        except PlaywrightError as exc:
            extraction_error = SuspiciousExtractionError(
                "product click did not expose stable identity"
            )
            extraction_error.__cause__ = exc
        except (SiteNavigationError, SuspiciousExtractionError) as exc:
            extraction_error = exc

        try:
            await self._close_live_modal(config)
        except (SiteNavigationError, SuspiciousExtractionError):
            if extraction_error is None:
                raise
        if extraction_error is not None:
            raise extraction_error
        assert product is not None
        return product

    async def _wait_for_matching_modal(
        self, config: MonitorConfig, expected_name: str
    ) -> tuple[str, bool]:
        max_samples = max(1, self._navigation_timeout_ms // 50)
        for _sample in range(max_samples):
            self._validate_page_url(config)
            modals = self._page.locator(".arco-modal.confirm_order")
            modal_count = await modals.count()
            if modal_count > 1:
                raise SuspiciousExtractionError("multiple product modals are ambiguous")
            if modal_count == 0:
                await self._page.wait_for_timeout(50)
                continue

            modal = modals.first
            modal_name = modal.locator(".pr_name")
            if await modal_name.count() != 1 or not await modal_name.is_visible():
                raise SuspiciousExtractionError("product modal lacks one visible product name")
            actual_name = _visible_text(await modal_name.inner_text())
            self._validate_page_url(config)
            if actual_name != expected_name:
                await self._close_modal(config, modal)
                await self._page.wait_for_timeout(50)
                continue

            link = modal.locator('a[href^="/item/"]')
            if await link.count() != 1:
                raise SuspiciousExtractionError("product modal lacks one canonical item link")
            href = await link.get_attribute("href")
            match = _ITEM_PATH.fullmatch(href or "")
            if match is None:
                raise SuspiciousExtractionError("product item link is noncanonical")
            quantity = modal.locator('input[role="spinbutton"]')
            out_of_stock = (
                await quantity.count() == 1 and await quantity.get_attribute("aria-valuemax") == "0"
            )
            self._validate_page_url(config)
            return match.group(1), out_of_stock
        raise SuspiciousExtractionError("no matching fresh product modal appeared")

    async def _close_live_modal(self, config: MonitorConfig) -> None:
        self._validate_page_url(config)
        modals = self._page.locator(".arco-modal.confirm_order")
        count = await modals.count()
        if count > 1:
            raise SuspiciousExtractionError("multiple product modals are ambiguous")
        if count == 1:
            await self._close_modal(config, modals.first)

    async def _close_modal(self, config: MonitorConfig, modal: Locator) -> None:
        modal_handle = await modal.element_handle()
        if modal_handle is None:
            raise SuspiciousExtractionError("product modal disappeared before safe close")
        close = modal.locator('[role="button"][aria-label="Close"]')
        if await close.count() != 1 or not await close.is_visible():
            raise SuspiciousExtractionError("product modal lacks one visible close control")
        await close.click(timeout=self._navigation_timeout_ms)
        self._validate_page_url(config)
        try:
            await self._page.wait_for_function(
                "node => !node.isConnected",
                arg=modal_handle,
                timeout=self._navigation_timeout_ms,
            )
        except PlaywrightError as exc:
            raise SuspiciousExtractionError("product modal did not close safely") from exc
        self._validate_page_url(config)

    @staticmethod
    def _availability(stock_text: str) -> Availability:
        if stock_text in {"有货", "库存充足"}:
            return Availability.IN_STOCK
        if stock_text == "库存少量":
            return Availability.LOW_STOCK
        if stock_text == "缺货":
            return Availability.OUT_OF_STOCK
        return Availability.UNKNOWN
