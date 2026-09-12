import asyncio

import pytest

from store import Store, seed_demo_data


@pytest.fixture
def store():
    s = Store()
    seed_demo_data(s)
    return s


def test_catalog_seeded(store):
    products = store.all_products()
    assert len(products) == 3
    assert products[0].stock == 3
    assert products[1].stock == 5


def test_order_state_machine(store):
    """pending -> paid -> delivered with atomic item handout."""
    p = store.all_products()[0]
    order = store.create_order(1, "buyer", p.id, "INV-1", p.price_usd)
    assert order.status == "pending"

    # delivering before payment must fail (no double-delivery path)
    assert store.deliver(order.id) is None

    store.mark_paid(order.id)
    item = store.deliver(order.id)
    assert item is not None
    assert order.status == "delivered"
    assert order.delivered_item == item
    assert p.stock == 2  # live stock decremented

    # re-delivery attempt (duplicate webhook) is a no-op
    assert store.deliver(order.id) is None
    assert p.stock == 2


def test_stock_exhaustion(store):
    p = store.all_products()[2]  # stock 2
    for i in range(2):
        order = store.create_order(2, "x", p.id, f"INV-{i}", p.price_usd)
        store.mark_paid(order.id)
        assert store.deliver(order.id) is not None
    assert p.stock == 0
    order = store.create_order(2, "x", p.id, "INV-3", p.price_usd)
    store.mark_paid(order.id)
    assert store.deliver(order.id) is None  # sold out — paid but undeliverable


def test_pending_orders_recovery(store):
    """Simulates the restart reconciler path: find pending, mark paid, deliver."""
    p = store.all_products()[1]
    order = store.create_order(3, "y", p.id, "INV-9", p.price_usd)
    assert store.pending_orders() == [order]
    # webhook arrives (was paid while down)
    store.mark_paid(order.id)
    assert store.pending_orders() == []  # reconciler would now deliver
    assert store.deliver(order.id) is not None


def test_user_profile_stats(store):
    """Profile tracks membership, orders, purchases and spend."""
    store.touch_user(777, "Alice", "alice")
    profile = store.get_user(777)
    assert profile is not None
    assert profile.first_name == "Alice"
    assert profile.total_orders == 0 and profile.total_spent == 0.0

    p = store.all_products()[0]  # $2.50, stock 3
    for i in range(2):
        order = store.create_order(777, "alice", p.id, f"INV-{i}", p.price_usd)
        store.mark_paid(order.id)
        assert store.deliver(order.id) is not None

    # one pending order that never got paid (shouldn't count as purchase)
    store.create_order(777, "alice", p.id, "INV-x", p.price_usd)

    profile = store.get_user(777)
    assert profile.total_orders == 3
    assert profile.total_purchases == 2
    assert profile.total_spent == 5.00  # 2 x $2.50
    assert profile.member_since <= profile.last_seen
