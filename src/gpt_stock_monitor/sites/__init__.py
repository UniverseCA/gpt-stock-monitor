"""Public site-adapter interfaces."""

from gpt_stock_monitor.sites.base import (
    CategoryNotFoundError,
    CategoryObservation,
    InteractiveChallengeError,
    SiteAdapter,
    SiteNavigationError,
    SuspiciousExtractionError,
)

__all__ = [
    "CategoryNotFoundError",
    "CategoryObservation",
    "InteractiveChallengeError",
    "SiteAdapter",
    "SiteNavigationError",
    "SuspiciousExtractionError",
]
