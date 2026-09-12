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

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from aiogram import Bot

    store = build_store()
    seed_demo_data(store)  # demo catalog; Phase 2: real DB rows instead
    pay = CryptoPayClient(CRYPTO_PAY_TOKEN or "missing", test_mode=CRYPTO_PAY_TEST)
    bot = Bot(TELEGRAM_TOKEN or "123:demo")
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

        order = store.get_order_by_invoice(invoice_id)
        if not order:
            log.warning("webhook for unknown invoice %s", invoice_id)
            raise HTTPException(404, "unknown invoice")

        if order.status == "pending":
            store.mark_paid(order.id)
            await shop.deliver_order(order)  # updates live stock too
            return {"ok": True}
        return {"ok": True, "note": "already processed"}

    return {"ok": True}  # other update types ignored
