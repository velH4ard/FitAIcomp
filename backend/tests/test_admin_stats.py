from datetime import datetime, timezone

import pytest

from app.config import settings
from app.db import get_db
from app.deps import get_current_user
from app.main import app


EXPECTED_ADMIN_STATS_KEYS = (
    "activeSubscriptions",
    "mrrRubEstimate",
    "todayAnalyzes",
    "todayRateLimited",
    "todayAiFailures",
    "todayPaymentsCreated",
    "todayPaymentsSucceeded",
    "todaySubscriptionsActivated",
)

ADMIN_USER_ID = "00000000-0000-0000-0000-00000000a101"
NON_ADMIN_USER_ID = "00000000-0000-0000-0000-00000000a102"


def _auth_user(user_id: str) -> dict:
    return {
        "id": user_id,
        "telegram_id": 123456,
        "username": "admin-metrics",
        "is_onboarded": True,
        "subscription_status": "active",
        "subscription_active_until": datetime(2099, 1, 1, tzinfo=timezone.utc),
        "profile": {},
    }


def _assert_numeric(value):
    assert isinstance(value, (int, float)) and not isinstance(value, bool)


class AdminStatsConn:
    async def fetchval(self, query, *args):
        if "FROM users" in query:
            return 3
        if "FROM usage_daily" in query:
            return 9
        return 0

    async def fetchrow(self, query, *args):
        return {
            "today_rate_limited": 2,
            "today_ai_failures": 1,
            "today_payments_created": 4,
            "today_payments_succeeded": 3,
            "today_subscriptions_activated": 3,
        }


@pytest.mark.asyncio
async def test_admin_stats_non_admin_access_returns_non_200_fitai_error(client, monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: _auth_user(NON_ADMIN_USER_ID)
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_USER_ID)

    try:
        response = await client.get("/v1/admin/stats")
        assert response.status_code != 200
        body = response.json()
        assert "error" in body
        assert body["error"]["code"] in {"UNAUTHORIZED", "FORBIDDEN"}
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_admin_stats_admin_access_returns_200_with_numeric_fields(client, monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: _auth_user(ADMIN_USER_ID)
    app.dependency_overrides[get_db] = lambda: AdminStatsConn()
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_USER_ID)
    monkeypatch.setattr(settings, "SUBSCRIPTION_PRICE_RUB", 500)

    try:
        response = await client.get("/v1/admin/stats")
        assert response.status_code == 200
        body = response.json()

        for key in EXPECTED_ADMIN_STATS_KEYS:
            assert key in body
            _assert_numeric(body[key])
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_admin_stats_shape_validation_for_all_required_fields(client, monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: _auth_user(ADMIN_USER_ID)
    app.dependency_overrides[get_db] = lambda: AdminStatsConn()
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_USER_ID)
    monkeypatch.setattr(settings, "SUBSCRIPTION_PRICE_RUB", 500)

    try:
        response = await client.get("/v1/admin/stats")
        assert response.status_code == 200
        body = response.json()

        assert set(EXPECTED_ADMIN_STATS_KEYS).issubset(set(body.keys()))
        for key in EXPECTED_ADMIN_STATS_KEYS:
            _assert_numeric(body[key])
            assert body[key] >= 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_admin_stats_smoke_shape_after_perf_hardening(client, monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: _auth_user(ADMIN_USER_ID)
    app.dependency_overrides[get_db] = lambda: AdminStatsConn()
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_USER_ID)
    monkeypatch.setattr(settings, "SUBSCRIPTION_PRICE_RUB", 500)

    try:
        response = await client.get("/v1/admin/stats")
        assert response.status_code == 200
        body = response.json()

        assert set(body.keys()) == set(EXPECTED_ADMIN_STATS_KEYS)
        for key in EXPECTED_ADMIN_STATS_KEYS:
            _assert_numeric(body[key])
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
