"""Web-layer tests: health route + webhook endpoint + duplicate-webhook safety.
Runs in web-only mode (no TELEGRAM_BOT_TOKEN) — same mode Render uses if token missing."""

import pytest
from fastapi.testclient import TestClient

import main as app_module
from store import Store, seed_demo_data


@pytest.fixture
def client():
    # no tokens -> web-only mode, no polling task, demo bot token
    with TestClient(app_module.app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def _make_order(app_state, invoice_id: str):
    store = app_state["store"]
    seed = store.all_products()[0]
    return store.create_order(1, "tester", seed.id, invoice_id,
                              seed.price_usd), seed


def test_webhook_paid_invoice_delivers(client):
    order, product = _make_order(app_module.state, "42")
    stock_before = product.stock

    r = client.post("/crypto-pay", json={
        "update_type": "invoice_paid",
        "payload": {"invoice_id": 42},
    })
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert order.status == "delivered"
    assert product.stock == stock_before - 1
    assert order.delivered_item is not None


def test_webhook_duplicate_is_idempotent(client):
    order, product = _make_order(app_module.state, "43")
    body = {"update_type": "invoice_paid",
            "payload": {"invoice_id": 43}}
    client.post("/crypto-pay", json=body)
    stock_after_first = product.stock
    r = client.post("/crypto-pay", json=body)  # duplicate delivery attempt
    assert r.status_code == 200
    assert product.stock == stock_after_first  # no double stock hit
    assert order.status == "delivered"


def test_webhook_unknown_invoice_404(client):
    r = client.post("/crypto-pay", json={
        "update_type": "invoice_paid",
        "payload": {"invoice_id": 999999},
    })
    assert r.status_code == 404


def test_webhook_ignores_other_updates(client):
    r = client.post("/crypto-pay", json={"update_type": "something_else"})
    assert r.status_code == 200
