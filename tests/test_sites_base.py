from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError

import pytest

from gpt_stock_monitor.config import MonitorConfig
from gpt_stock_monitor.models import Availability, Product
from gpt_stock_monitor.sites.base import (
    CategoryNotFoundError,
    CategoryObservation,
    InteractiveChallengeError,
    SiteAdapter,
    SiteNavigationError,
    SuspiciousExtractionError,
)


def make_product() -> Product:
    return Product(
        key="product-1",
        name="GPT Plus",
        price="13.50",
        price_text="13.50 yuan",
        availability=Availability.IN_STOCK,
        stock_text="in stock",
        url="https://pay.ldxp.cn/goods/1",
    )


def test_category_observation_accepts_non_empty_products_without_explicit_count() -> None:
    product = make_product()

    observation = CategoryObservation(products=(product,), explicit_count=None)

    assert observation.products == (product,)
    assert observation.explicit_count is None


def test_category_observation_accepts_only_explicit_zero_for_empty_products() -> None:
    observation = CategoryObservation(products=(), explicit_count=0)

    assert observation.products == ()
    assert observation.explicit_count == 0


def test_category_observation_copies_input_list_to_immutable_tuple() -> None:
    product = make_product()
    source = [product]

    observation = CategoryObservation(products=source, explicit_count=None)  # type: ignore[arg-type]
    source.clear()

    assert observation.products == (product,)
    assert isinstance(observation.products, tuple)
    with pytest.raises(FrozenInstanceError):
        observation.explicit_count = 0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("products", "explicit_count"),
    [
        ((), None),
        ((make_product(),), 0),
        ((), 1),
        ((), -1),
    ],
)
def test_category_observation_rejects_ambiguous_or_inconsistent_states(
    products: tuple[Product, ...], explicit_count: int | None
) -> None:
    with pytest.raises(ValueError):
        CategoryObservation(products=products, explicit_count=explicit_count)


@pytest.mark.parametrize(
    "error_type",
    [
        SiteNavigationError,
        InteractiveChallengeError,
        CategoryNotFoundError,
        SuspiciousExtractionError,
    ],
)
def test_site_errors_are_runtime_errors(error_type: type[RuntimeError]) -> None:
    assert issubclass(error_type, RuntimeError)


def test_site_adapter_protocol_accepts_async_structural_implementation() -> None:
    class FakeSiteAdapter:
        async def collect(self, config: MonitorConfig) -> Mapping[str, CategoryObservation]:
            return {
                category: CategoryObservation(products=(), explicit_count=0)
                for category in config.categories
            }

    adapter: SiteAdapter = FakeSiteAdapter()

    assert isinstance(adapter, SiteAdapter)
