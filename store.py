"""Storage layer.

Demo mode: in-memory store (lost on restart — fine for demo).
Production mode: set SUPABASE_URL + SUPABASE_SERVICE_KEY and the same
interface is served by Supabase Postgres (Phase 2 swap, no bot-code changes).

Order state machine: pending -> paid -> delivered (see SHOP_BOT_PLAN.md).
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Product:
    id: int
    title: str
    price_usd: float
    stock: int
    items: list[str] = field(default_factory=list)  # deliverable codes/keys/links

    def take_item(self) -> Optional[str]:
        """Atomically pop one deliverable. None if out of stock."""
        if self.stock <= 0 or not self.items:
            return None
        self.stock -= 1
        return self.items.pop(0)


@dataclass
class Order:
    id: str
    user_id: int
    username: Optional[str]
    product_id: int
    invoice_id: str
    amount_usd: float
    status: str = "pending"  # pending | paid | delivered
    delivered_item: Optional[str] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class UserProfile:
    user_id: int
    first_name: str = ""
    username: Optional[str] = None
    balance: float = 0.0           # store credit (top-ups come later)
    total_orders: int = 0            # all orders created
    total_purchases: int = 0        # delivered orders only
    total_spent: float = 0.0        # sum of delivered order amounts
    member_since: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


class Store:
    """In-memory implementation (demo). Interface mirrors the future Supabase one."""

    def __init__(self) -> None:
        self.products: dict[int, Product] = {}
        self.orders: dict[str, Order] = {}
        self.users: dict[int, UserProfile] = {}
        self._next_product_id = 1

    # ---- users ----
    def touch_user(self, user_id: int, first_name: str = "",
                   username: Optional[str] = None) -> UserProfile:
        """Create profile on first contact; refresh name/username + last_seen."""
        profile = self.users.get(user_id)
        if profile is None:
            profile = UserProfile(user_id=user_id, first_name=first_name,
                                   username=username)
            self.users[user_id] = profile
        else:
            if first_name:
                profile.first_name = first_name
            if username:
                profile.username = username
            profile.last_seen = time.time()
        return profile

    def get_user(self, user_id: int) -> Optional[UserProfile]:
        return self.users.get(user_id)

    # ---- products ----
    def add_product(self, title: str, price_usd: float, items: list[str]) -> Product:
        pid = self._next_product_id
        self._next_product_id += 1
        product = Product(id=pid, title=title, price_usd=price_usd,
                          stock=len(items), items=list(items))
        self.products[pid] = product
        return product

    def get_product(self, product_id: int) -> Optional[Product]:
        return self.products.get(product_id)

    def all_products(self) -> list[Product]:
        return list(self.products.values())

    def restock(self, product_id: int, items: list[str]) -> Optional[Product]:
        product = self.products.get(product_id)
        if not product or not items:
            return None
        product.items.extend(items)
        product.stock += len(items)
        return product

    def stats(self) -> dict:
        delivered = [o for o in self.orders.values() if o.status == "delivered"]
        pending = [o for o in self.orders.values() if o.status == "pending"]
        paid = [o for o in self.orders.values() if o.status == "paid"]
        return {
            "revenue": round(sum(o.amount_usd for o in delivered), 2),
            "pending": len(pending),
            "paid": len(paid),
            "delivered": len(delivered),
            "users": len(self.users),
            "products": [{"id": p.id, "title": p.title, "stock": p.stock}
                         for p in self.products.values()],
        }

    # ---- orders ----
    def user_orders(self, user_id: int, limit: int = 10) -> list[Order]:
        return [o for o in self.orders.values()
                if o.user_id == user_id and o.status == "delivered"][-limit:]

    def create_order(self, user_id: int, username: Optional[str],
                     product_id: int, invoice_id: str, amount_usd: float) -> Order:
        order = Order(id=uuid.uuid4().hex, user_id=user_id, username=username,
                      product_id=product_id, invoice_id=invoice_id,
                      amount_usd=amount_usd)
        self.orders[order.id] = order
        profile = self.touch_user(user_id, username=username or "")
        profile.total_orders += 1
        return order

    def get_order_by_invoice(self, invoice_id: str) -> Optional[Order]:
        for order in self.orders.values():
            if order.invoice_id == invoice_id:
                return order
        return None

    def mark_paid(self, order_id: str) -> Optional[Order]:
        order = self.orders.get(order_id)
        if order and order.status == "pending":
            order.status = "paid"
        return order

    def mark_delivered(self, order_id: str, item: str) -> Optional[Order]:
        order = self.orders.get(order_id)
        if order and order.status == "paid":
            order.status = "delivered"
            order.delivered_item = item
        return order

    def pending_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.status == "pending"]

    # ---- delivery (atomic: take item only for a paid order) ----
    def deliver(self, order_id: str) -> Optional[str]:
        """Return the deliverable for a paid order, or None if impossible."""
        order = self.orders.get(order_id)
        if not order or order.status != "paid":
            return None
        product = self.products.get(order.product_id)
        if not product:
            return None
        item = product.take_item()
        if item is None:
            return None
        order.status = "delivered"
        order.delivered_item = item
        profile = self.users.get(order.user_id)
        if profile:
            profile.total_purchases += 1
            profile.total_spent += order.amount_usd
        return item


def seed_demo_data(store: Store) -> None:
    """Demo catalog. Phase 2: /add + /restock admin commands instead."""
    store.add_product(
        "Netflix Premium 1 Month", 2.50,
        [f"NF-{uuid.uuid4().hex[:10].upper()}" for _ in range(3)],
    )
    store.add_product(
        "Spotify Premium 1 Month", 1.80,
        [f"SP-{uuid.uuid4().hex[:10].upper()}" for _ in range(5)],
    )
    store.add_product(
        "Steam Gift Card $5", 5.00,
        [f"ST-{uuid.uuid4().hex[:10].upper()}" for _ in range(2)],
    )


def build_store() -> Store:
    """Factory. Phase 2: return SupabaseStore if env vars are set."""
    if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY"):
        # Phase 2: SupabaseStore(...) implementing the same interface
        pass
    return Store()
