"""aiogram 3 bot: catalog, live stock, purchase flow, auto-delivery.

Store-agnostic: works with the in-memory Store (dicts via __dict__ shim) and
the Postgres-backed PGStore (plain dicts). All store calls are async-safe
(in-memory Store methods are sync but wrapped via maybe_await helper).
"""

from __future__ import annotations

import asyncio
import inspect
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from cryptopay import CryptoPayClient, CryptoPayError
from store import Order, Product, Store

log = logging.getLogger("shopbot")

PAGE_SIZE = 8  # buttons per catalog page

BUTTON_LABELS = {"🛍 Shop", "👤 Profile", "📦 My Orders"}


def parse_add_args(text: str):
    """Parse '/add Title | 2.50' -> (title, price) or None."""
    body = text.partition(" ")[2].strip()
    if "|" not in body:
        return None
    title, _, price_s = body.partition("|")
    title = title.strip()
    try:
        price = float(price_s.strip())
    except ValueError:
        return None
    if not title or price <= 0:
        return None
    return title, round(price, 2)


def parse_restock_args(text: str):
    """Parse '/restock 3' -> product_id or None."""
    body = text.partition(" ")[2].strip()
    try:
        pid = int(body)
    except ValueError:
        return None
    return pid if pid > 0 else None


async def s(store_method, *args):
    """Call a store method that may be sync (Store) or async (PGStore)."""
    result = store_method(*args)
    if inspect.isawaitable(result):
        result = await result
    return result


def _fmt_member_since(profile: dict) -> str:
    from datetime import datetime, timezone
    ms = profile.get("member_since")
    if ms is None:
        return "—"
    if isinstance(ms, (int, float)):
        dt = datetime.fromtimestamp(ms, tz=timezone.utc)
    else:
        dt = ms if ms.tzinfo else ms.replace(tzinfo=timezone.utc)
    return dt.strftime("%d %b %Y, %H:%M UTC")


# Persistent reply keyboard shown at the bottom of the chat
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Shop"), KeyboardButton(text="👤 Profile")],
            [KeyboardButton(text="📦 My Orders")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Browse or use the buttons below…",
    )


class ShopBot:
    def __init__(self, bot: Bot, store, pay: CryptoPayClient,
                 admin_ids: set[int]):
        self.bot = bot
        self.store = store
        self.pay = pay
        self.admin_ids = admin_ids
        self._pending_restock: dict[int, int] = {}  # admin_id -> product_id
        self.dp = Dispatcher()

        self.dp.message(CommandStart())(self.cmd_start)
        self.dp.message(Command("shop"))(self.cmd_shop)
        self.dp.message(Command("myorders"))(self.cmd_my_orders)
        self.dp.message(Command("profile"))(self.cmd_profile)
        self.dp.message(F.text == "🛍 Shop")(self.cb_shop_button)
        self.dp.message(F.text == "📦 My Orders")(self.cb_my_orders_button)
        self.dp.message(F.text == "👤 Profile")(self.cb_profile_button)
        self.dp.message(Command("add"))(self.cmd_add)
        self.dp.message(Command("restock"))(self.cmd_restock)
        self.dp.message(Command("stats"))(self.cmd_stats)
        self.dp.message(Command("cancel"))(self.cmd_cancel_admin)
        self.dp.callback_query(F.data == "catalog:0")(self.cb_catalog_first)
        self.dp.callback_query(F.data.startswith("page:"))(self.cb_page)
        self.dp.callback_query(F.data.startswith("buy:"))(self.cb_buy)
        self.dp.callback_query(F.data == "cancel")(self.cb_cancel)
        self.dp.message(F.text)(self.on_plain_text)  # admin pending codes, last

    # ---------- keyboards ----------

    async def catalog_kb(self, page: int = 0) -> InlineKeyboardMarkup:
        products = await s(self.store.all_products)
        # normalize rows to dicts (in-memory returns Product dataclasses)
        norm = []
        for p in products:
            if isinstance(p, dict):
                norm.append(p)
            else:
                norm.append({"id": p.id, "title": p.title,
                             "price_usd": float(p.price_usd), "stock": p.stock})
        total_pages = max(1, (len(norm) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        chunk = norm[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

        rows = []
        for p in chunk:
            price = float(p["price_usd"])
            label = (f"{p['title']} — ${price:.2f} ({p['stock']} left)"
                     if p["stock"] else f"❌ {p['title']} — out of stock")
            rows.append([InlineKeyboardButton(text=label,
                                              callback_data=f"buy:{p['id']}")])
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton(text="⬅️",
                                                callback_data=f"page:{page - 1}"))
            nav.append(InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton(text="➡️",
                                                callback_data=f"page:{page + 1}"))
            rows.append(nav)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def order_kb(self, product: dict) -> InlineKeyboardMarkup:
        price = float(product["price_usd"])
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"💳 Pay ${price:.2f}",
                callback_data=f"buy:{product['id']}:confirm",
            )],
            [InlineKeyboardButton(text="✖️ Cancel", callback_data="cancel")],
        ])

    # ---------- handlers ----------

    async def cmd_start(self, msg: Message) -> None:
        await msg.answer(
            "🛒 Welcome to <b>Matrix Shop</b>!\n"
            "Crypto payments via @CryptoBot.\n"
            "Digital goods delivered instantly after payment.\n\n"
            "/shop — browse catalog\n"
            "/profile — your stats & balance\n"
            "/myorders — your purchase history",
            reply_markup=await self.catalog_kb(0),
        )
        await msg.answer("⌨️ Use the buttons below 👇",
                         reply_markup=main_keyboard())

    async def cb_shop_button(self, msg: Message) -> None:
        await msg.answer("🛍 Catalog:", reply_markup=await self.catalog_kb(0))

    async def cb_my_orders_button(self, msg: Message) -> None:
        await self.cmd_my_orders(msg)

    # ---------- profile ----------

    def _render_profile(self, profile: dict) -> str:
        handle = f"@{profile.get('username')}" if profile.get("username") else "—"
        return (
            f"👤 <b>{profile.get('first_name') or 'Customer'}</b>\n"
            f"🆔 ID: <code>{profile.get('user_id')}</code>\n"
            f"🔗 Username: {handle}\n\n"
            f"💰 Balance: <b>${float(profile.get('balance', 0)):.2f}</b>\n\n"
            f"🧾 Total orders: <b>{profile.get('total_orders', 0)}</b>\n"
            f"📦 Purchases: <b>{profile.get('total_purchases', 0)}</b>\n"
            f"💸 Total spent: <b>${float(profile.get('total_spent', 0)):.2f}</b>\n\n"
            f"📅 Member since: <b>{_fmt_member_since(profile)}</b>"
        )

    async def cmd_profile(self, msg: Message) -> None:
        profile = await s(self.store.get_user, msg.from_user.id)
        if not profile:
            await msg.answer("Profile not found — press /start first.")
            return
        if not isinstance(profile, dict):
            profile = profile.__dict__
        await msg.answer(self._render_profile(profile),
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="🛍 Continue shopping",
                                                   callback_data="catalog:0")],
                         ]))

    async def cb_profile_button(self, msg: Message) -> None:
        await self.cmd_profile(msg)

    async def cmd_shop(self, msg: Message) -> None:
        await msg.answer("🛍 Catalog:", reply_markup=await self.catalog_kb(0))

    async def cmd_my_orders(self, msg: Message) -> None:
        orders = await s(self.store.user_orders, msg.from_user.id, 10) \
            if hasattr(self.store, "user_orders") else \
            [o.__dict__ for o in self.store.orders.values()
             if o.user_id == msg.from_user.id and o.status == "delivered"]
        if not orders:
            await msg.answer("No purchases yet. /shop")
            return
        lines = []
        for o in orders[-10:]:
            title = o.get("title", o.get("product_id", "?"))
            lines.append(
                f"🛍 {title} — ${float(o['amount_usd']):.2f} — "
                f"<code>{o.get('delivered_item', '—')}</code>")
        await msg.answer("📦 Your purchases:\n" + "\n".join(lines))

    async def cb_catalog_first(self, cb: CallbackQuery) -> None:
        await cb.message.edit_reply_markup(reply_markup=await self.catalog_kb(0))
        await cb.answer()

    async def cb_page(self, cb: CallbackQuery) -> None:
        page = int(cb.data.split(":")[1])
        await cb.message.edit_reply_markup(reply_markup=await self.catalog_kb(page))
        await cb.answer()

    async def cb_buy(self, cb: CallbackQuery) -> None:
        _, raw_pid, *rest = cb.data.split(":")
        product = await s(self.store.get_product, int(raw_pid))
        if product and not isinstance(product, dict):
            product = product.__dict__
        if not product:
            await cb.answer("Product gone.", show_alert=True)
            return
        if product["stock"] <= 0:
            await cb.answer("Out of stock!", show_alert=True)
            return
        if rest and rest[0] == "confirm":
            await self._create_invoice(cb, product)
        else:
            await cb.message.answer(
                f"<b>{product['title']}</b>\n"
                f"Price: ${float(product['price_usd']):.2f}\n"
                f"In stock: {product['stock']}\n\n"
                "Continue to crypto payment?",
                reply_markup=self.order_kb(product),
            )
            await cb.answer()

    async def _create_invoice(self, cb: CallbackQuery, product: dict) -> None:
        user = cb.from_user
        try:
            invoice = await self.pay.create_invoice(
                amount_usd=float(product["price_usd"]),
                description=product["title"],
                payload=f"uid:{user.id}",
            )
        except CryptoPayError as e:
            log.error("invoice failed: %s", e)
            await cb.answer("Payment provider error. Try later.", show_alert=True)
            return

        order = await s(self.store.create_order,
                        user.id, user.username,
                        int(product["id"]), str(invoice.invoice_id),
                        float(product["price_usd"]))
        if order and not isinstance(order, dict):
            order = order.__dict__
        await cb.message.edit_text(
            f"🧾 Order <code>{str(order['id'])[:8]}</code>\n"
            f"{product['title']} — ${float(product['price_usd']):.2f}\n\n"
            "Pay via CryptoBot (USDT/BTC/TON). Button opens the invoice.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 Open invoice", url=invoice.pay_url)],
                [InlineKeyboardButton(text="✖️ Cancel", callback_data="cancel")],
            ]),
        )
        await cb.answer()

    async def cb_cancel(self, cb: CallbackQuery) -> None:
        await cb.message.delete()
        await cb.answer("Cancelled")

    # ---------- delivery (called from webhook or reconciler) ----------

    async def deliver_order(self, order: dict) -> bool:
        item = await s(self.store.deliver, str(order["id"]))
        if item is None:
            log.error("delivery failed for order %s (stock/state)", order["id"])
            return False
        product = await s(self.store.get_product, int(order["product_id"]))
        if product and not isinstance(product, dict):
            product = product.__dict__
        title = product["title"] if product else "your item"
        try:
            await self.bot.send_message(
                order["user_id"],
                "✅ Payment confirmed!\n\n"
                f"🛍 <b>{title}</b>\n"
                f"🔑 Your item:\n<code>{item}</code>\n\n"
                "Thanks for the purchase!",
            )
        except Exception as e:  # delivered in DB even if user blocked the bot
            log.error("notify user %s failed: %s", order["user_id"], e)
        if self.admin_ids:
            for admin_id in self.admin_ids:
                try:
                    await self.bot.send_message(
                        admin_id,
                        f"💰 New sale: order <code>{str(order['id'])[:8]}</code> "
                        f"${float(order['amount_usd']):.2f} → user {order['user_id']}",
                    )
                except Exception:
                    pass
        return True

    # ---------- admin (gated by ADMIN_IDS) ----------

    def _is_admin(self, msg: Message) -> bool:
        return msg.from_user.id in self.admin_ids

    async def cmd_add(self, msg: Message) -> None:
        if not self._is_admin(msg):
            return
        parsed = parse_add_args(msg.text or "")
        if not parsed:
            await msg.answer(
                "Usage: <code>/add Title | 2.50</code>\n"
                "Example: <code>/add Netflix 1 Month | 2.50</code>\n\n"
                "Product is created with 0 stock — then send the codes, "
                "one per line.")
            return
        title, price = parsed
        product = await s(self.store.add_product, title, price, [])
        if product and not isinstance(product, dict):
            product = product.__dict__
        self._pending_restock[msg.from_user.id] = int(product["id"])
        await msg.answer(
            f"✅ Product <b>{title}</b> created — "
            f"$<b>{price:.2f}</b>, id <code>{product['id']}</code>.\n\n"
            "Now send the deliverable codes, one per line.\n"
            "/cancel to abort.")

    async def cmd_restock(self, msg: Message) -> None:
        if not self._is_admin(msg):
            return
        pid = parse_restock_args(msg.text or "")
        if not pid:
            await msg.answer(
                "Usage: <code>/restock 3</code>\n"
                "Find the id in /stats or the catalog.")
            return
        product = await s(self.store.get_product, pid)
        if not product:
            await msg.answer(f"No product with id <code>{pid}</code>.")
            return
        if product and not isinstance(product, dict):
            product = product.__dict__
        self._pending_restock[msg.from_user.id] = pid
        await msg.answer(
            f"📦 Restocking <b>{product['title']}</b> "
            f"(stock: {product['stock']}).\n\n"
            "Send the new codes, one per line.\n"
            "/cancel to abort.")

    async def cmd_cancel_admin(self, msg: Message) -> None:
        if not self._is_admin(msg):
            return
        if self._pending_restock.pop(msg.from_user.id, None) is None:
            await msg.answer("Nothing pending.")
        else:
            await msg.answer("Cancelled — no codes were added.")

    async def cmd_stats(self, msg: Message) -> None:
        if not self._is_admin(msg):
            return
        st = await s(self.store.stats)
        lines = [
            "📊 <b>Shop stats</b>\n",
            f"💸 Revenue: <b>${st['revenue']:.2f}</b>",
            f"🧾 Pending: <b>{st['pending']}</b> · "
            f"Paid: <b>{st['paid']}</b> · "
            f"Delivered: <b>{st['delivered']}</b>",
            f"👥 Users: <b>{st['users']}</b>\n",
            "<b>Stock:</b>",
        ]
        for p in st["products"]:
            lines.append(f"  #{p['id']} {p['title']} — <b>{p['stock']}</b> left")
        await msg.answer("\n".join(lines))

    async def on_plain_text(self, msg: Message) -> None:
        uid = msg.from_user.id
        if uid not in self.admin_ids or uid not in self._pending_restock:
            return
        text = (msg.text or "").strip()
        if not text or text.startswith("/") or text in BUTTON_LABELS:
            return
        pid = self._pending_restock[uid]
        codes = [line.strip() for line in text.splitlines() if line.strip()]
        if not codes:
            await msg.answer("Send at least one code, or /cancel.")
            return
        product = await s(self.store.restock, pid, codes)
        self._pending_restock.pop(uid, None)
        if not product:
            await msg.answer(f"Product <code>{pid}</code> no longer exists.")
            return
        if product and not isinstance(product, dict):
            product = product.__dict__
        await msg.answer(
            f"✅ Added <b>{len(codes)}</b> codes to "
            f"<b>{product['title']}</b>.\n"
            f"Stock now: <b>{product['stock']}</b>.")

    # ---------- middleware factory (user tracking) ----------

    @staticmethod
    def _make_tracker(store):
        from aiogram import BaseMiddleware
        from aiogram.types import User as TgUser

        class TrackUserMiddleware(BaseMiddleware):
            async def __call__(self, handler, event, data):
                user: TgUser | None = data.get("event_from_user")
                if user and not user.is_bot:
                    await s(store.touch_user, user.id, user.first_name,
                            user.username)
                return await handler(event, data)

        return TrackUserMiddleware()

    # ---------- startup reconciler (idempotency, see plan) ----------

    async def reconcile_pending(self) -> int:
        """After restart: mark + deliver orders paid while we were down."""
        delivered = 0
        pending = await s(self.store.pending_orders)
        for order in pending:
            if order and not isinstance(order, dict):
                order = order.__dict__
            try:
                inv = await self.pay.get_invoice(int(order["invoice_id"]))
            except CryptoPayError:
                continue
            if inv.status == "paid":
                paid = await s(self.store.mark_paid, str(order["id"]))
                if paid is not None and await self.deliver_order(
                        paid if isinstance(paid, dict) else paid.__dict__):
                    delivered += 1
        if delivered:
            log.info("reconciler delivered %d pending orders", delivered)
        return delivered

    async def run_polling(self) -> None:
        self.dp.message.middleware(self._make_tracker(self.store))
        self.dp.callback_query.middleware(self._make_tracker(self.store))
        await self.reconcile_pending()
        await self.dp.start_polling(self.bot)
