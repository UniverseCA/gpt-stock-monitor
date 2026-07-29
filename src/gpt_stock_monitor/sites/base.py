"""Shared site-adapter protocol, observations, and extraction errors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from gpt_stock_monitor.config import MonitorConfig
from gpt_stock_monitor.models import Product


@dataclass(frozen=True)
class CategoryObservation:
    """A non-empty product result or a site-confirmed zero count."""

    products: tuple[Product, ...]
    explicit_count: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "products", tuple(self.products))
        if self.products:
            if self.explicit_count is not None:
                raise ValueError("non-empty products cannot have an explicit count")
        elif self.explicit_count != 0:
            raise ValueError("empty products require an explicit zero count")


@runtime_checkable
class SiteAdapter(Protocol):
    """Collect normalized category observations for one monitor."""

    async def collect(self, config: MonitorConfig) -> Mapping[str, CategoryObservation]: ...


class SiteNavigationError(RuntimeError):
    """The configured site could not be navigated safely."""


class InteractiveChallengeError(RuntimeError):
    """The site presented an interactive challenge."""


class CategoryNotFoundError(RuntimeError):
    """A configured category was not present in the page."""


class SuspiciousExtractionError(RuntimeError):
    """Extracted content did not meet the adapter's safety contract."""
