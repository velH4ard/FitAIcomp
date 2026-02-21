import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db import get_db
from app.deps import get_current_user
from app.main import app


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePaywallConn:
    def __init__(self, *, photos_used: int = 0, fail_events: bool = False):
        self.photos_used = photos_used
        self.fail_events = fail_events
        self.flags: set[tuple[str, str, object]] = set()
        self.events: list[dict] = []

    def transaction(self):
        return _Tx()

    async def fetchrow(self, query, *args):
        if "SELECT photos_used FROM usage_daily" in query:
            return {"photos_used": self.photos_used}

        if "INSERT INTO user_daily_flags" in query:
            if len(args) >= 3:
                flag_date = args[2]
            else:
                flag_date = datetime.now(timezone.utc).date()
            key = (str(args[0]), str(args[1]), flag_date)
            if key in self.flags:
                return None
            self.flags.add(key)
            return {"user_id": args[0]}

        return None

    async def execute(self, query, *args):
        if "INSERT INTO events" in query:
            if self.fail_events:
                raise RuntimeError("events unavailable")
            payload = args[2]
            if isinstance(payload, str):
                payload = json.loads(payload)
            self.events.append(
                {
                    "user_id": str(args[0]),
                    "event_type": args[1],
                    "payload": payload,
                }
            )
        return "OK"


def _override_user(user: dict):
    app.dependency_overrides[get_current_user] = lambda: user


def _override_db(conn):
    app.dependency_overrides[get_db] = lambda: conn


def _clear_overrides():
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_paywall_context_unauthorized(client):
    response = await client.get("/v1/paywall/context")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_paywall_context_precedence_blocked(client):
    user = {
        "id": "00000000-0000-0000-0000-000000000701",
        "telegram_id": 7701,
        "subscription_status": "blocked",
        "subscription_active_until": datetime.now(timezone.utc) + timedelta(days=30),
        "is_onboarded": True,
        "profile": {},
    }
    conn = FakePaywallConn(photos_used=0)
    _override_user(user)
    _override_db(conn)

    try:
        response = await client.get("/v1/paywall/context")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["subscriptionStatus"] == "blocked"
    assert body["reason"] == "quota_reached"


@pytest.mark.asyncio
async def test_paywall_context_precedence_quota_reached_over_expired(client):
    user = {
        "id": "00000000-0000-0000-0000-000000000702",
        "telegram_id": 7702,
        "subscription_status": "free",
        "subscription_active_until": None,
        "is_onboarded": True,
        "profile": {},
    }
    conn = FakePaywallConn(photos_used=2)
    _override_user(user)
    _override_db(conn)

    try:
        response = await client.get("/v1/paywall/context")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["subscriptionStatus"] == "free"
    assert body["reason"] == "quota_reached"


@pytest.mark.asyncio
async def test_paywall_context_precedence_expiring_soon(client):
    user = {
        "id": "00000000-0000-0000-0000-000000000703",
        "telegram_id": 7703,
        "subscription_status": "active",
        "subscription_active_until": datetime.now(timezone.utc) + timedelta(days=1, hours=1),
        "is_onboarded": True,
        "profile": {},
    }
    conn = FakePaywallConn(photos_used=0)
    _override_user(user)
    _override_db(conn)

    try:
        response = await client.get("/v1/paywall/context")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["subscriptionStatus"] == "active"
    assert body["daysLeft"] in {1, 2}
    assert body["reason"] == "expiring_soon"


@pytest.mark.asyncio
async def test_subscription_status_emits_expiring_soon_event_once_per_day(client):
    user = {
        "id": "00000000-0000-0000-0000-000000000704",
        "telegram_id": 7704,
        "subscription_status": "active",
        "subscription_active_until": datetime.now(timezone.utc) + timedelta(days=2),
        "is_onboarded": True,
        "profile": {},
    }
    conn = FakePaywallConn(photos_used=0)
    _override_user(user)
    _override_db(conn)

    try:
        first = await client.get("/v1/subscription/status")
        second = await client.get("/v1/subscription/status")
    finally:
        _clear_overrides()

    assert first.status_code == 200
    assert second.status_code == 200

    events = [e for e in conn.events if e["event_type"] == "subscription_expiring_soon"]
    assert len(events) == 1
    assert len(conn.flags) == 1


@pytest.mark.asyncio
async def test_subscription_status_event_failure_does_not_break_response(client):
    user = {
        "id": "00000000-0000-0000-0000-000000000705",
        "telegram_id": 7705,
        "subscription_status": "active",
        "subscription_active_until": datetime.now(timezone.utc) + timedelta(days=1),
        "is_onboarded": True,
        "profile": {},
    }
    conn = FakePaywallConn(photos_used=0, fail_events=True)
    _override_user(user)
    _override_db(conn)

    try:
        response = await client.get("/v1/subscription/status")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "active"
    assert body["willExpireSoon"] is True


@pytest.mark.asyncio
async def test_paywall_context_blocked_with_remaining_returns_none_never_soft_hint(client):
    user = {
        "id": "00000000-0000-0000-0000-000000000706",
        "telegram_id": 7706,
        "subscription_status": "blocked",
        "subscription_active_until": None,
        "referral_credits": 2,
        "is_onboarded": True,
        "profile": {},
    }
    conn = FakePaywallConn(photos_used=1)
    _override_user(user)
    _override_db(conn)

    try:
        response = await client.get("/v1/paywall/context")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["subscriptionStatus"] == "blocked"
    assert body["remaining"] > 0
    assert body["reason"] == "none"
    assert body["reason"] != "soft_hint"


@pytest.mark.asyncio
async def test_paywall_context_blocked_with_zero_remaining_returns_quota_reached(client):
    user = {
        "id": "00000000-0000-0000-0000-000000000707",
        "telegram_id": 7707,
        "subscription_status": "blocked",
        "subscription_active_until": None,
        "referral_credits": 2,
        "is_onboarded": True,
        "profile": {},
    }
    conn = FakePaywallConn(photos_used=2)
    _override_user(user)
    _override_db(conn)

    try:
        response = await client.get("/v1/paywall/context")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["subscriptionStatus"] == "blocked"
    assert body["remaining"] == 0
    assert body["reason"] == "quota_reached"
