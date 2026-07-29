"""Pure, deterministic comparison of semantic product snapshots."""

from __future__ import annotations

import unicodedata
from decimal import Decimal

from gpt_stock_monitor.models import Change, ChangeKind, Product, Snapshot

__all__ = ["compare_snapshots"]


_CHANGE_KIND_PRIORITY = {
    ChangeKind.ADDED: 0,
    ChangeKind.REMOVED: 1,
    ChangeKind.AVAILABILITY: 2,
    ChangeKind.STOCK_TEXT: 3,
    ChangeKind.PRICE: 4,
    ChangeKind.NAME: 5,
}


def _normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _normalize_stock_text(value: str) -> str:
    return " ".join(value.split())


def _normalize_price(value: str) -> str:
    formatted = format(Decimal(value), "f")
    if "." not in formatted:
        return formatted
    return formatted.rstrip("0").rstrip(".")


def _field_change(
    kind: ChangeKind,
    previous: Product,
    current: Product,
    before: str,
    after: str,
) -> Change:
    return Change(
        kind=kind,
        product_key=current.key,
        product_name=_normalize_name(current.name),
        before=before,
        after=after,
        url=current.url if current.url is not None else previous.url,
    )


def compare_snapshots(previous: Snapshot, current: Snapshot) -> tuple[Change, ...]:
    """Return stable field-level changes between snapshots of the same monitor scope."""
    if (previous.monitor_id, previous.category) != (current.monitor_id, current.category):
        raise ValueError("snapshot scopes do not match")

    previous_products = {product.key: product for product in previous.products}
    current_products = {product.key: product for product in current.products}
    changes: list[Change] = []

    for product_key in previous_products.keys() - current_products.keys():
        product = previous_products[product_key]
        changes.append(
            Change(
                kind=ChangeKind.REMOVED,
                product_key=product.key,
                product_name=_normalize_name(product.name),
                url=product.url,
            )
        )

    for product_key in current_products.keys() - previous_products.keys():
        product = current_products[product_key]
        changes.append(
            Change(
                kind=ChangeKind.ADDED,
                product_key=product.key,
                product_name=_normalize_name(product.name),
                url=product.url,
            )
        )

    for product_key in previous_products.keys() & current_products.keys():
        old = previous_products[product_key]
        new = current_products[product_key]

        if old.availability != new.availability:
            changes.append(
                _field_change(
                    ChangeKind.AVAILABILITY,
                    old,
                    new,
                    old.availability.value,
                    new.availability.value,
                )
            )
        else:
            old_stock_text = _normalize_stock_text(old.stock_text)
            new_stock_text = _normalize_stock_text(new.stock_text)
            if old_stock_text != new_stock_text:
                changes.append(
                    _field_change(
                        ChangeKind.STOCK_TEXT,
                        old,
                        new,
                        old_stock_text,
                        new_stock_text,
                    )
                )

        if Decimal(old.price) != Decimal(new.price):
            changes.append(
                _field_change(
                    ChangeKind.PRICE,
                    old,
                    new,
                    _normalize_price(old.price),
                    _normalize_price(new.price),
                )
            )

        old_name = _normalize_name(old.name)
        new_name = _normalize_name(new.name)
        if old_name != new_name:
            changes.append(_field_change(ChangeKind.NAME, old, new, old_name, new_name))

    return tuple(
        sorted(
            changes,
            key=lambda change: (change.product_key, _CHANGE_KIND_PRIORITY[change.kind]),
        )
    )
