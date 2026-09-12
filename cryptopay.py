"""Crypto Pay (CryptoBot) integration.

Docs: https://pay.crypt.bot/api/rest (or @CryptoBot -> Crypto Pay -> Create App)
Sandbox: in @CryptoBot create a *test* app -> get test token -> set CRYPTO_PAY_TEST=True

Phase 2: validate webhook signatures + query invoice by API before delivering
(never trust webhook payload alone — already in the plan checklist).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx

MAIN_API = "https://pay.crypt.bot/api"
TEST_API = "https://testnet-pay.crypt.bot/api"


@dataclass
class Invoice:
    invoice_id: int
    status: str          # active | paid | expired
    amount_usd: float
    pay_url: str          # https://t.me/CryptoBot?start=XXXX
    bot_pay_url: str      # mini-app url (button in bot)


class CryptoPayError(RuntimeError):
    pass


class CryptoPayClient:
    """Thin async client; keeps aiogram polling loop unblocked."""

    def __init__(self, token: str, test_mode: bool = False, timeout: float = 15.0):
        self._base = TEST_API if test_mode else MAIN_API
        self._headers = {"Crypto-Pay-API-Token": token}
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self._base, headers=self._headers, timeout=self._timeout
            )
        return self._http

    async def aclose(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    async def _call(self, method: str, params: Optional[dict] = None) -> dict:
        client = await self._client()
        resp = await client.post(f"/{method}", json=params or {})
        data = resp.json()
        if not data.get("ok"):
            raise CryptoPayError(f"crypto pay error: {data}")
        return data["result"]

    async def create_invoice(self, amount_usd: float, description: str,
                             payload: str = "") -> Invoice:
        """payload carries our internal order id -> matched on webhook."""
        result = await self._call("createInvoice", {
            "currency_type": "fiat",
            "fiat": "USD",
            "amount": f"{amount_usd:.2f}",
            "description": description[:64],
            "payload": payload,
            "expires_in": 3600,
        })
        return Invoice(
            invoice_id=result["invoice_id"],
            status=result["status"],
            amount_usd=float(result["amount"]),
            pay_url=result["pay_url"],  # payUrl in API docs
            bot_pay_url=result.get("bot_pay_url") or result.get("botPayUrl", ""),
        )

    async def get_invoice(self, invoice_id: int) -> Invoice:
        """Used to re-verify pending orders after restart (idempotency)."""
        result = await self._call("getInvoices", {"invoice_ids": str(invoice_id)})
        items = result.get("items") or []
        if not items:
            raise CryptoPayError(f"invoice {invoice_id} not found")
        inv = items[0]
        return Invoice(
            invoice_id=inv["invoice_id"],
            status=inv["status"],
            amount_usd=float(inv["amount"]),
            pay_url=inv.get("pay_url") or inv.get("payUrl", ""),
            bot_pay_url=inv.get("bot_pay_url") or inv.get("botPayUrl", ""),
        )
