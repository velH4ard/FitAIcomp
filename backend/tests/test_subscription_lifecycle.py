from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app import payments


USER_ID = "00000000-0000-0000-0000-000000009001"
TELEGRAM_ID = 9001001


class StubConn:
    def __init__(self):
        self.payment_events = set()

    async def fetchrow(self, query, *args):
        if "SELECT photos_used FROM usage_daily" in query:
            return None
        if "RETURNING subscription_active_until" in query:
            return None
        return None

    async def execute(self, query, *args):
        if "INSERT INTO payment_webhook_events" in query:
            dedupe_key = args[0]
            if dedupe_key in self.payment_events:
                return "OK"
            self.payment_events.add(dedupe_key)
            return "OK"
        return "OK"


@pytest.fixture(autouse=True)
def clear_webhook_memory():
    payments._webhook_dedupe_memory.clear()
    yield
    payments._webhook_dedupe_memory.clear()


@pytest.fixture
def override_db():
    conn = StubConn()

    async def _override_get_db():
        yield conn

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield conn
    finally:
        app.dependency_overrides.pop(get_db, None)


def make_user(subscription_status: str, active_until: Optional[datetime]):
    return {
        "id": USER_ID,
        "telegram_id": TELEGRAM_ID,
        "username": "subscription-user",
        "is_onboarded": True,
        "subscription_status": subscription_status,
        "subscription_active_until": active_until,
        "profile": {},
    }


def paid_webhook_payload(event_id: str, user_id: str = USER_ID):
    return {
        "id": event_id,
        "event": "payment.succeeded",
        "object": {
            "id": f"payment-{event_id}",
            "status": "succeeded",
            "paid": True,
            "captured": True,
            "metadata": {
                "user_id": user_id,
                "telegram_id": str(TELEGRAM_ID),
                "plan": "monthly_499",
            },
        },
    }


@pytest.mark.asyncio
async def test_subscription_active_status_returns_active_and_daily_limit_20(client, override_db):
    user = make_user("active", datetime(2099, 1, 1, tzinfo=timezone.utc))
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = await client.get("/v1/subscription")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "active"
        assert body["dailyLimit"] == 20
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_subscription_expired_status_returns_expired_and_daily_limit_2(client, override_db):
    user = make_user("active", datetime(2000, 1, 1, tzinfo=timezone.utc))
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = await client.get("/v1/subscription")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "expired"
        assert body["dailyLimit"] == 2
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_webhook_extends_from_existing_future_active_until(client, override_db, monkeypatch):
    future_until = datetime(2099, 6, 1, tzinfo=timezone.utc)
    user = make_user("active", future_until)

    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(payments, "verify_yookassa_webhook", lambda *_args, **_kwargs: True)

    try:
        response = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=paid_webhook_payload("evt-future-extend"),
            headers={"X-YooKassa-Signature": "valid"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert user["subscription_status"] == "active"
        assert user["subscription_active_until"] == future_until + timedelta(days=30)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_webhook_extends_from_now_when_active_until_in_past(client, override_db, monkeypatch):
    user = make_user("expired", datetime(2000, 1, 1, tzinfo=timezone.utc))

    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(payments, "verify_yookassa_webhook", lambda *_args, **_kwargs: True)

    before = datetime.now(timezone.utc)
    try:
        response = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=paid_webhook_payload("evt-past-extend"),
            headers={"X-YooKassa-Signature": "valid"},
        )
        after = datetime.now(timezone.utc)

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert user["subscription_status"] == "active"

        expected_min = before + timedelta(days=30)
        expected_max = after + timedelta(days=30)
        assert expected_min <= user["subscription_active_until"] <= expected_max
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_price_override_is_used_in_create_payment_and_subscription_response(
    client,
    override_db,
    monkeypatch,
):
    user = make_user("free", None)
    app.dependency_overrides[get_current_user] = lambda: user

    captured_payload = {}

    async def fake_create_payment(payload, idempotence_key):
        captured_payload["payload"] = payload
        captured_payload["idempotence_key"] = idempotence_key
        return {
            "id": "pay-override-10",
            "confirmation": {"confirmation_url": "https://yookassa.test/confirm/pay-override-10"},
        }

    monkeypatch.setattr(payments.settings, "SUBSCRIPTION_PRICE_RUB", 10)
    monkeypatch.setattr(payments, "_create_yookassa_payment", fake_create_payment)

    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-price-override",
            },
        )
        assert create_response.status_code == 200
        assert captured_payload["payload"]["amount"]["value"] == "10.00"

        subscription_response = await client.get("/v1/subscription")
        assert subscription_response.status_code == 200
        assert subscription_response.json()["priceRubPerMonth"] == 10
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_default_price_is_499_when_no_override(client, override_db, monkeypatch):
    user = make_user("free", None)
    app.dependency_overrides[get_current_user] = lambda: user

    monkeypatch.setattr(payments.settings, "SUBSCRIPTION_PRICE_RUB", 499)
    try:
        response = await client.get("/v1/subscription")
        assert response.status_code == 200
        assert response.json()["priceRubPerMonth"] == 499
    finally:
        app.dependency_overrides.pop(get_current_user, None)
