"""Postgres/Supabase-backed store — same interface as the in-memory Store.

Uses the atomic claim_item()/record_delivery() functions created in the
Supabase migration: double-delivery is impossible at the DB level
(FOR UPDATE SKIP LOCKED), and user stats update in the same transaction.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg

log = logging.getLogger("shopbot.pgstore")

POOLER_RECONNECTIONS = 3


class PGStore:
    """Async Postgres implementation. Method names match store.Store."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        for attempt in range(1, POOLER_RECONNECTIONS + 1):
            try:
                self.pool = await asyncpg.create_pool(
                    self.dsn, min_size=1, max_size=5,
                    command_timeout=15, statement_cache_size=100,
                )
                log.info("pg store connected (attempt %d)", attempt)
                return
            except Exception as e:
                log.warning("pg connect attempt %d failed: %s", attempt, e)
                if attempt == POOLER_RECONNECTIONS:
                    raise
                await asyncio.sleep(2 * attempt)

    async def aclose(self) -> None:
        if self.pool:
            await self.pool.close()

    async def _fetch(self, sql: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def _fetchrows(self, sql: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, *args)

    async def _execute(self, sql: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.execute(sql, *args)

    # ---- products ----
    async def all_products(self):
        rows = await self._fetchrows(
            "select id, title, price_usd, stock from products order by id")
        return [dict(r) for r in rows]

    async def get_product(self, product_id: int) -> Optional[dict]:
        row = await self._fetch(
            "select id, title, price_usd, stock from products where id = $1",
            product_id)
        return dict(row) if row else None

    async def add_product(self, title: str, price_usd: float, items: list[str]):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "insert into products (title, price_usd, stock) "
                    "values ($1, $2, $3) returning id, title, price_usd, stock",
                    title, price_usd, len(items))
                await conn.executemany(
                    "insert into product_items (product_id, code) values ($1, $2)",
                    [(row["id"], code) for code in items])
                return dict(row)

    async def restock(self, product_id: int, items: list[str]) -> Optional[dict]:
        if not items:
            return None
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                exists = await conn.fetchval(
                    "select 1 from products where id = $1", product_id)
                if not exists:
                    return None
                await conn.executemany(
                    "insert into product_items (product_id, code) values ($1, $2)",
                    [(product_id, code) for code in items])
                row = await conn.fetchrow(
                    "update products set stock = stock + $2 where id = $1 "
                    "returning id, title, price_usd, stock",
                    product_id, len(items))
                return dict(row)

    async def stats(self) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "select coalesce(sum(amount_usd) filter "
                "(where status = 'delivered'), 0)::float8 as revenue, "
                "count(*) filter (where status = 'pending') as pending, "
                "count(*) filter (where status = 'paid') as paid, "
                "count(*) filter (where status = 'delivered') as delivered "
                "from orders")
            users = await conn.fetchval("select count(*) from shop_users")
            products = await conn.fetch(
                "select id, title, stock from products order by id")
        return {
            "revenue": round(row["revenue"], 2),
            "pending": row["pending"],
            "paid": row["paid"],
            "delivered": row["delivered"],
            "users": users,
            "products": [dict(p) for p in products],
        }

    # ---- users ----
    async def touch_user(self, user_id: int, first_name: str = "",
                         username: Optional[str] = None) -> dict:
        row = await self._fetch(
            "insert into shop_users (user_id, first_name, username) "
            "values ($1, $2, $3) "
            "on conflict (user_id) do update set "
            "  first_name = case when excluded.first_name != '' "
            "                    then excluded.first_name else shop_users.first_name end, "
            "  username = coalesce(excluded.username, shop_users.username), "
            "  last_seen = now() "
            "returning *",
            user_id, first_name, username)
        return dict(row)

    async def get_user(self, user_id: int) -> Optional[dict]:
        row = await self._fetch(
            "select * from shop_users where user_id = $1", user_id)
        return dict(row) if row else None

    # ---- orders ----
    async def create_order(self, user_id: int, username: Optional[str],
                           product_id: int, invoice_id: str,
                           amount_usd: float) -> dict:
        row = await self._fetch(
            "insert into orders (user_id, username, product_id, invoice_id, amount_usd) "
            "values ($1, $2, $3, $4, $5) "
            "returning id::text as id, user_id, username, product_id::text as product_id, "
            "invoice_id, amount_usd::float8 as amount_usd, status",
            user_id, username, product_id, invoice_id, amount_usd)
        await self._execute(
            "update shop_users set total_orders = total_orders + 1 where user_id = $1",
            user_id)
        d = dict(row)
        return d

    async def get_order_by_invoice(self, invoice_id: str) -> Optional[dict]:
        row = await self._fetch(
            "select id::text as id, user_id, username, product_id::text as product_id, "
            "invoice_id, amount_usd::float8 as amount_usd, status, delivered_item "
            "from orders where invoice_id = $1", invoice_id)
        return dict(row) if row else None

    async def mark_paid(self, order_id: str) -> Optional[dict]:
        row = await self._fetch(
            "update orders set status = 'paid' "
            "where id = $1::uuid and status = 'pending' "
            "returning id::text as id, user_id, username, product_id::text as product_id, "
            "invoice_id, amount_usd::float8 as amount_usd, status",
            order_id)
        return dict(row) if row else None

    async def pending_orders(self) -> list[dict]:
        rows = await self._fetchrows(
            "select id::text as id, user_id, username, product_id::text as product_id, "
            "invoice_id, amount_usd::float8 as amount_usd, status "
            "from orders where status = 'pending'")
        return [dict(r) for r in rows]

    # ---- delivery (atomic, DB-level guarantee) ----
    async def deliver(self, order_id: str) -> Optional[str]:
        """record_delivery() marks item sold + updates stats in ONE transaction."""
        async with self.pool.acquire() as conn:
            code = await conn.fetchval(
                "select record_delivery($1::uuid)", order_id)
        if code is None:
            log.error("delivery failed for order %s (sold out or not paid)",
                      order_id)
        return code

    # ---- user-facing helpers ----
    async def user_orders(self, user_id: int, limit: int = 10) -> list[dict]:
        rows = await self._fetchrows(
            "select o.id::text as id, o.amount_usd::float8 as amount_usd, "
            "o.delivered_item, p.title "
            "from orders o join products p on p.id = o.product_id "
            "where o.user_id = $1 and o.status = 'delivered' "
            "order by o.created_at desc limit $2", user_id, limit)
        return [dict(r) for r in rows]
