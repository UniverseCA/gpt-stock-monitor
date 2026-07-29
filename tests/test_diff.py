from __future__ import annotations

import pytest

from gpt_stock_monitor.diff import compare_snapshots
from gpt_stock_monitor.models import Availability, Change, ChangeKind, Product, Snapshot


def make_product(
    key: str = "product-1",
    *,
    name: str = "GPT Plus",
    price: str = "13.50",
    availability: Availability = Availability.IN_STOCK,
    stock_text: str = "有货",
    url: str | None = "https://pay.ldxp.cn/goods/1",
) -> Product:
    return Product(
        key=key,
        name=name,
        price=price,
        price_text=f"¥{price}",
        availability=availability,
        stock_text=stock_text,
        url=url,
    )


def make_snapshot(
    *products: Product,
    monitor_id: str = "demo",
    category: str = "GPT 成品号",
) -> Snapshot:
    return Snapshot(monitor_id=monitor_id, category=category, products=products)


def expected_change(
    kind: ChangeKind,
    *,
    product_name: str = "GPT Plus",
    before: str | None = None,
    after: str | None = None,
    url: str | None = "https://pay.ldxp.cn/goods/1",
) -> Change:
    return Change(
        kind=kind,
        product_key="product-1",
        product_name=product_name,
        before=before,
        after=after,
        url=url,
    )


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        (
            make_snapshot(),
            make_snapshot(make_product()),
            (expected_change(ChangeKind.ADDED),),
        ),
        (
            make_snapshot(make_product(name="Old name", url="https://old.example/item")),
            make_snapshot(),
            (
                expected_change(
                    ChangeKind.REMOVED,
                    product_name="Old name",
                    url="https://old.example/item",
                ),
            ),
        ),
        (
            make_snapshot(
                make_product(
                    availability=Availability.OUT_OF_STOCK,
                    stock_text="售罄",
                )
            ),
            make_snapshot(make_product(availability=Availability.IN_STOCK, stock_text="现货")),
            (
                expected_change(
                    ChangeKind.AVAILABILITY,
                    before="out_of_stock",
                    after="in_stock",
                ),
            ),
        ),
        (
            make_snapshot(make_product(availability=Availability.IN_STOCK, stock_text="现货")),
            make_snapshot(
                make_product(
                    availability=Availability.OUT_OF_STOCK,
                    stock_text="售罄",
                )
            ),
            (
                expected_change(
                    ChangeKind.AVAILABILITY,
                    before="in_stock",
                    after="out_of_stock",
                ),
            ),
        ),
        (
            make_snapshot(make_product(availability=Availability.UNKNOWN, stock_text="待确认")),
            make_snapshot(
                make_product(availability=Availability.LOW_STOCK, stock_text="仅剩 2 件")
            ),
            (
                expected_change(
                    ChangeKind.AVAILABILITY,
                    before="unknown",
                    after="low_stock",
                ),
            ),
        ),
        (
            make_snapshot(make_product(stock_text="库存 充足")),
            make_snapshot(make_product(stock_text="仅剩 2 件")),
            (
                expected_change(
                    ChangeKind.STOCK_TEXT,
                    before="库存 充足",
                    after="仅剩 2 件",
                ),
            ),
        ),
        (
            make_snapshot(make_product(price="13.50")),
            make_snapshot(make_product(price="15.000")),
            (expected_change(ChangeKind.PRICE, before="13.5", after="15"),),
        ),
        (
            make_snapshot(make_product(name="GPT Plus")),
            make_snapshot(make_product(name="GPT Pro")),
            (
                expected_change(
                    ChangeKind.NAME,
                    product_name="GPT Pro",
                    before="GPT Plus",
                    after="GPT Pro",
                ),
            ),
        ),
    ],
    ids=[
        "added",
        "removed",
        "restocked",
        "sold-out",
        "other-availability",
        "stock-text",
        "price",
        "name",
    ],
)
def test_compare_snapshots_reports_semantic_changes(
    previous: Snapshot,
    current: Snapshot,
    expected: tuple[Change, ...],
) -> None:
    assert compare_snapshots(previous, current) == expected


@pytest.mark.parametrize(
    ("previous_product", "current_product"),
    [
        (make_product(name="\uff27\uff30\uff34\u3000Plus"), make_product(name="GPT   Plus")),
        (make_product(stock_text="库存\n 充足"), make_product(stock_text="库存\t充足")),
        (make_product(price="13.50"), make_product(price="13.5000")),
    ],
    ids=["name-nfkc-and-whitespace", "stock-whitespace", "equivalent-price"],
)
def test_compare_snapshots_ignores_format_only_differences(
    previous_product: Product,
    current_product: Product,
) -> None:
    assert (
        compare_snapshots(
            make_snapshot(previous_product),
            make_snapshot(current_product),
        )
        == ()
    )


def test_compare_snapshots_ignores_page_product_order() -> None:
    alpha = make_product("alpha", name="Alpha")
    beta = make_product("beta", name="Beta")

    assert (
        compare_snapshots(
            make_snapshot(beta, alpha),
            make_snapshot(alpha, beta),
        )
        == ()
    )


def test_one_product_name_and_price_changes_have_fixed_order() -> None:
    previous = make_product(name="Old name", price="13.50", url="https://old.example/item")
    current = make_product(name="New name", price="15.00", url="https://new.example/item")

    assert compare_snapshots(make_snapshot(previous), make_snapshot(current)) == (
        expected_change(
            ChangeKind.PRICE,
            product_name="New name",
            before="13.5",
            after="15",
            url="https://new.example/item",
        ),
        expected_change(
            ChangeKind.NAME,
            product_name="New name",
            before="Old name",
            after="New name",
            url="https://new.example/item",
        ),
    )


def test_multiple_products_and_fields_have_deterministic_order() -> None:
    previous = make_snapshot(
        make_product("zeta", name="Zeta", price="9", url=None),
        make_product("alpha", name="Alpha", stock_text="有货"),
    )
    current = make_snapshot(
        make_product("alpha", name="Alpha 2", price="14", stock_text="少量"),
        make_product("zeta", name="Zeta 2", price="10", url="https://new.example/zeta"),
    )

    changes = compare_snapshots(previous, current)

    assert [(change.product_key, change.kind) for change in changes] == [
        ("alpha", ChangeKind.STOCK_TEXT),
        ("alpha", ChangeKind.PRICE),
        ("alpha", ChangeKind.NAME),
        ("zeta", ChangeKind.PRICE),
        ("zeta", ChangeKind.NAME),
    ]
    assert all(change.product_name.endswith("2") for change in changes)
    assert changes[-1].url == "https://new.example/zeta"


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (
            make_snapshot(make_product(), monitor_id="monitor-a"),
            make_snapshot(make_product(), monitor_id="monitor-b"),
        ),
        (
            make_snapshot(make_product(), category="category-a"),
            make_snapshot(make_product(), category="category-b"),
        ),
    ],
    ids=["monitor-id", "category"],
)
def test_compare_snapshots_rejects_different_monitor_scope(
    previous: Snapshot,
    current: Snapshot,
) -> None:
    with pytest.raises(ValueError, match="snapshot scopes do not match"):
        compare_snapshots(previous, current)
