"""FastAPI app: health route (Render port binding + UptimeRobot target)
and Crypto Pay webhook endpoint. Bot runs as asyncio task in same process."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from bot import ShopBot
from cryptopay import CryptoPayClient
from store import build_store, seed_demo_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("shopbot.app")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "")
CRYPTO_PAY_TEST = os.getenv("CRYPTO_PAY_TEST", "true").lower() in ("1", "true", "yes")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DATABASE_URL = os.getenv("DATABASE_URL", "")

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties

    pay = CryptoPayClient(CRYPTO_PAY_TOKEN or "missing", test_mode=CRYPTO_PAY_TEST)
    bot = Bot(TELEGRAM_TOKEN or "123:demo",
              default=DefaultBotProperties(parse_mode="HTML"))

    if DATABASE_URL:
        from pgstore import PGStore
        store = PGStore(DATABASE_URL)
        await store.connect()
        log.info("using Supabase Postgres store")
    else:
        from store import Store, seed_demo_data
        store = Store()
        seed_demo_data(store)
        log.warning("DATABASE_URL not set — in-memory demo store (resets on restart)")

    shop = ShopBot(bot=bot, store=store, pay=pay, admin_ids=ADMIN_IDS)

    state.update(store=store, pay=pay, bot=bot, shop=shop)

    polling = None
    if TELEGRAM_TOKEN:
        import asyncio
        polling = asyncio.create_task(shop.run_polling())
        log.info("bot polling started")
    else:
        log.warning("TELEGRAM_BOT_TOKEN not set — running web-only mode (local tests)")

    yield

    if polling:
        polling.cancel()
    if hasattr(store, "aclose"):
        try:
            await store.aclose()
        except Exception:
            pass
    await pay.aclose()
    await bot.session.close()


app = FastAPI(title="Telegram Shop Bot", lifespan=lifespan)


@app.get("/health")
async def health():
    """UptimeRobot pings this every 5 min to prevent Render spin-down."""
    return {"status": "ok"}


@app.post("/crypto-pay")
async def crypto_pay_webhook(request: Request):
    """Crypto Pay sends updates here (set webhook URL in @CryptoBot app settings).

    Phase 2: verify webhook signature header + re-query getInvoices before
    delivering (never trust payload alone — checklist in SHOP_BOT_PLAN.md).
    """
    update = await request.json()
    update_type = update.get("update_type")

    if update_type in ("invoice_paid",):
        payload = update.get("payload") or {}
        invoice_id = str(payload.get("invoice_id", ""))
        store, shop = state["store"], state["shop"]

        from bot import s
        order = await s(store.get_order_by_invoice, invoice_id)
        if not order:
            log.warning("webhook for unknown invoice %s", invoice_id)
            raise HTTPException(404, "unknown invoice")
        if not isinstance(order, dict):
            order = order.__dict__

        if order["status"] == "pending":
            paid = await s(store.mark_paid, str(order["id"]))
            if paid is not None:
                await shop.deliver_order(paid if isinstance(paid, dict)
                                         else paid.__dict__)
            return {"ok": True}
        return {"ok": True, "note": "already processed"}

    return {"ok": True}  # other update types ignored
