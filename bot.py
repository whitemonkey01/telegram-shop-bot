"""aiogram 3 bot: catalog, live stock, purchase flow, auto-delivery."""

from __future__ import annotations

import asyncio
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
import time as _time

log = logging.getLogger("shopbot")

PAGE_SIZE = 8  # buttons per catalog page

# Persistent reply keyboard shown at the bottom of the chat
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Shop"), KeyboardButton(text="👤 Profile")],
            [KeyboardButton(text="📦 My Orders")],
        ],
        resize_keyboard=True,  # fits phone screen
        input_field_placeholder="Browse or use the buttons below…",
    )


class ShopBot:
    def __init__(self, bot: Bot, store: Store, pay: CryptoPayClient,
                 admin_ids: set[int]):
        self.bot = bot
        self.store = store
        self.pay = pay
        self.admin_ids = admin_ids
        self.dp = Dispatcher()

        self.dp.message(CommandStart())(self.cmd_start)
        self.dp.message(Command("shop"))(self.cmd_shop)
        self.dp.message(Command("myorders"))(self.cmd_my_orders)
        self.dp.message(Command("profile"))(self.cmd_profile)
        self.dp.message(F.text == "🛍 Shop")(self.cb_shop_button)
        self.dp.message(F.text == "📦 My Orders")(self.cb_my_orders_button)
        self.dp.message(F.text == "👤 Profile")(self.cb_profile_button)
        self.dp.callback_query(F.data == "catalog:0")(self.cb_catalog_first)
        self.dp.callback_query(F.data.startswith("page:"))(self.cb_page)
        self.dp.callback_query(F.data.startswith("buy:"))(self.cb_buy)
        self.dp.callback_query(F.data == "cancel")(self.cb_cancel)

    # ---------- keyboards ----------

    def catalog_kb(self, page: int = 0) -> InlineKeyboardMarkup:
        products = self.store.all_products()
        total_pages = max(1, (len(products) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        chunk = products[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

        rows = [
            [InlineKeyboardButton(
                text=f"{p.title} — ${p.price_usd:.2f} ({p.stock} left)"
                if p.stock else f"❌ {p.title} — out of stock",
                callback_data=f"buy:{p.id}",
            )]
            for p in chunk
        ]
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"page:{page - 1}"))
            nav.append(InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton(text="➡️", callback_data=f"page:{page + 1}"))
            rows.append(nav)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def order_kb(self, product: Product) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"💳 Pay ${product.price_usd:.2f}",
                callback_data=f"buy:{product.id}:confirm",
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
            "/myorders — your purchase history",
            reply_markup=self.catalog_kb(0),
        )
        # then pin the bottom keyboard for this chat
        await msg.answer("⌨️ Use the buttons below 👇",
                         reply_markup=main_keyboard())

    async def cb_shop_button(self, msg: Message) -> None:
        await msg.answer("🛍 Catalog:", reply_markup=self.catalog_kb(0))

    async def cb_my_orders_button(self, msg: Message) -> None:
        await self.cmd_my_orders(msg)

    # ---------- profile ----------

    def _render_profile(self, profile) -> str:
        from datetime import datetime, timezone
        member_since = datetime.fromtimestamp(profile.member_since, tz=timezone.utc)
        handle = f"@{profile.username}" if profile.username else "—"
        return (
            f"👤 <b>{profile.first_name or 'Customer'}</b>\n"
            f"🆔 ID: <code>{profile.user_id}</code>\n"
            f"🔗 Username: {handle}\n\n"
            f"💰 Balance: <b>${profile.balance:.2f}</b>\n\n"
            f"🧾 Total orders: <b>{profile.total_orders}</b>\n"
            f"📦 Purchases: <b>{profile.total_purchases}</b>\n"
            f"💸 Total spent: <b>${profile.total_spent:.2f}</b>\n\n"
            f"📅 Member since: <b>{member_since.strftime('%d %b %Y, %H:%M UTC')}</b>"
        )

    async def cmd_profile(self, msg: Message) -> None:
        profile = self.store.get_user(msg.from_user.id)
        if not profile:
            await msg.answer("Profile not found — press /start first.")
            return
        await msg.answer(self._render_profile(profile),
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="🛍 Continue shopping",
                                                   callback_data="catalog:0")],
                         ]))

    async def cb_profile_button(self, msg: Message) -> None:
        await self.cmd_profile(msg)

    async def cmd_shop(self, msg: Message) -> None:
        await msg.answer("🛍 Catalog:", reply_markup=self.catalog_kb(0))

    async def cmd_my_orders(self, msg: Message) -> None:
        orders = [o for o in self.store.orders.values()
                  if o.user_id == msg.from_user.id and o.status == "delivered"]
        if not orders:
            await msg.answer("No purchases yet. /shop")
            return
        lines = [f"#{o.id[:8]} — {o.product_id} — ${o.amount_usd:.2f} — "
                 f"<code>{o.delivered_item}</code>" for o in orders[-10:]]
        await msg.answer("Your purchases:\n" + "\n".join(lines))

    async def cb_catalog_first(self, cb: CallbackQuery) -> None:
        await cb.message.edit_reply_markup(reply_markup=self.catalog_kb(0))
        await cb.answer()

    async def cb_page(self, cb: CallbackQuery) -> None:
        page = int(cb.data.split(":")[1])
        await cb.message.edit_reply_markup(reply_markup=self.catalog_kb(page))
        await cb.answer()

    async def cb_buy(self, cb: CallbackQuery) -> None:
        _, raw_pid, *rest = cb.data.split(":")
        product = self.store.get_product(int(raw_pid))
        if not product:
            await cb.answer("Product gone.", show_alert=True)
            return
        if product.stock <= 0:
            await cb.answer("Out of stock!", show_alert=True)
            return
        if rest and rest[0] == "confirm":
            await self._create_invoice(cb, product)
        else:
            await cb.message.answer(
                f"<b>{product.title}</b>\n"
                f"Price: ${product.price_usd:.2f}\n"
                f"In stock: {product.stock}\n\n"
                "Continue to crypto payment?",
                reply_markup=self.order_kb(product),
            )
            await cb.answer()

    async def _create_invoice(self, cb: CallbackQuery, product: Product) -> None:
        user = cb.from_user
        try:
            invoice = await self.pay.create_invoice(
                amount_usd=product.price_usd,
                description=product.title,
                payload=f"uid:{user.id}",
            )
        except CryptoPayError as e:
            log.error("invoice failed: %s", e)
            await cb.answer("Payment provider error. Try later.", show_alert=True)
            return

        order = self.store.create_order(
            user_id=user.id, username=user.username,
            product_id=product.id, invoice_id=str(invoice.invoice_id),
            amount_usd=product.price_usd,
        )
        await cb.message.edit_text(
            f"🧾 Order <code>{order.id[:8]}</code>\n"
            f"{product.title} — ${product.price_usd:.2f}\n\n"
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

    async def deliver_order(self, order: Order) -> bool:
        item = self.store.deliver(order.id)
        if item is None:
            log.error("delivery failed for order %s (stock/state)", order.id)
            return False
        try:
            await self.bot.send_message(
                order.user_id,
                "✅ Payment confirmed!\n\n"
                f"🛍 <b>{self.store.get_product(order.product_id).title}</b>\n"
                f"🔑 Your item:\n<code>{item}</code>\n\n"
                "Thanks for the purchase!",
            )
        except Exception as e:  # delivered in DB even if user blocked the bot
            log.error("notify user %s failed: %s", order.user_id, e)
        if self.admin_ids:
            for admin_id in self.admin_ids:
                try:
                    await self.bot.send_message(
                        admin_id,
                        f"💰 New sale: order <code>{order.id[:8]}</code> "
                        f"${order.amount_usd:.2f} → user {order.user_id}",
                    )
                except Exception:
                    pass
        return True

    # ---------- middleware factory (user tracking) ----------

    @staticmethod
    def _make_tracker(store: "Store"):
        from aiogram import BaseMiddleware
        from aiogram.types import User as TgUser

        class TrackUserMiddleware(BaseMiddleware):
            async def __call__(self, handler, event, data):
                user: TgUser | None = data.get("event_from_user")
                if user and not user.is_bot:
                    store.touch_user(user.id, user.first_name, user.username)
                return await handler(event, data)

        return TrackUserMiddleware()

    # ---------- startup reconciler (idempotency, see plan) ----------

    async def reconcile_pending(self) -> int:
        """After restart: mark + deliver orders paid while we were down."""
        delivered = 0
        for order in self.store.pending_orders():
            try:
                inv = await self.pay.get_invoice(int(order.invoice_id))
            except CryptoPayError:
                continue
            if inv.status == "paid":
                self.store.mark_paid(order.id)
                if await self.deliver_order(order):
                    delivered += 1
        if delivered:
            log.info("reconciler delivered %d pending orders", delivered)
        return delivered

    async def run_polling(self) -> None:
        self.dp.message.middleware(self._make_tracker(self.store))
        self.dp.callback_query.middleware(self._make_tracker(self.store))
        await self.reconcile_pending()
        await self.dp.start_polling(self.bot)
