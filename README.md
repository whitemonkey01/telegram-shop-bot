# Telegram Shop Bot — Demo

Free-tier shop bot: catalog → live stock → crypto invoice (Crypto Pay) → auto-delivery.
Full hosting plan: see [SHOP_BOT_PLAN.md](../SHOP_BOT_PLAN.md)

## Run locally
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env  # fill tokens, or leave empty for web-only mode
.venv/bin/uvicorn main:app --port 8000
```

## Test
```bash
.venv/bin/python -m pytest -q
```

## Deploy (Render free)
1. Push this folder to a GitHub repo
2. Render → New → Blueprint (uses `render.yaml`) or Web Service (start command in `Procfile`)
3. Set env vars: `TELEGRAM_BOT_TOKEN`, `CRYPTO_PAY_TOKEN` (test app token), `ADMIN_IDS`
4. In @CryptoBot app settings, set webhook URL: `https://<your-app>.onrender.com/crypto-pay`
5. UptimeRobot: HTTP monitor → `https://<your-app>.onrender.com/health` every 5 min

## Files
- `main.py` — FastAPI app (health + webhook) + bot task, one process
- `bot.py` — aiogram 3 bot: catalog, stock, purchase flow, delivery, reconciler
- `store.py` — order/product storage; in-memory now, Supabase in Phase 2 (same interface)
- `cryptopay.py` — CryptoBot Crypto Pay API client (create/verify invoices)
- `render.yaml` / `Procfile` — Render config

## Demo catalog
Seeded in `store.seed_demo_data` — 3 fake products with random codes. Replace via admin commands in Phase 2.
