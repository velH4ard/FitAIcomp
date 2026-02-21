from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from app.deps import get_current_user
from app.main import app


TEST_USER_ID = "6489db75-92ed-42bc-8b2b-87b40e6aa855"


def _make_user(subscription_status: str, active_until: Optional[datetime]):
    return {
        "id": TEST_USER_ID,
        "telegram_id": 987654321,
        "username": "status-user",
        "is_onboarded": True,
        "subscription_status": subscription_status,
        "subscription_active_until": active_until,
        "profile": {},
    }


@pytest.mark.asyncio
async def test_subscription_status_active_not_expiring_soon(client):
    user = _make_user("active", datetime.now(timezone.utc) + timedelta(days=5))
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = await client.get("/v1/subscription/status")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "active"
    assert body["activeUntil"] is not None
    assert body["daysLeft"] >= 3
    assert body["willExpireSoon"] is False


@pytest.mark.asyncio
async def test_subscription_status_active_expiring_soon(client):
    user = _make_user("active", datetime.now(timezone.utc) + timedelta(days=2, hours=1))
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = await client.get("/v1/subscription/status")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "active"
    assert body["daysLeft"] in {2, 3}
    assert body["willExpireSoon"] == (body["daysLeft"] < 3)


@pytest.mark.asyncio
async def test_subscription_status_expired_is_free_with_zero_days(client):
    user = _make_user("active", datetime.now(timezone.utc) - timedelta(seconds=10))
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = await client.get("/v1/subscription/status")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.json() == {
        "status": "free",
        "activeUntil": None,
        "daysLeft": 0,
        "willExpireSoon": False,
    }


@pytest.mark.asyncio
async def test_subscription_status_blocked_overrides_active_until(client):
    user = _make_user("blocked", datetime.now(timezone.utc) + timedelta(days=60))
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = await client.get("/v1/subscription/status")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.json() == {
        "status": "blocked",
        "activeUntil": None,
        "daysLeft": 0,
        "willExpireSoon": False,
    }
