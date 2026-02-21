import pytest
from datetime import datetime, timezone
from app.main import app
from app.db import get_db
from app.deps import get_current_user

@pytest.mark.asyncio
async def test_get_usage_unauthorized(client):
    """Test GET /v1/usage/today without token."""
    response = await client.get("/v1/usage/today")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"

@pytest.mark.asyncio
async def test_get_usage_success_free(client):
    """Test GET /v1/usage/today with free user."""
    mock_user = {
        "id": "00000000-0000-0000-0000-000000000001",
        "telegram_id": 111111111,
        "subscription_status": "free",
        "subscription_active_until": None,
        "referral_credits": 0,
        "is_onboarded": True,
        "profile": "{}"
    }
    
    app.dependency_overrides[get_current_user] = lambda: mock_user
    
    try:
        response = await client.get("/v1/usage/today")
        assert response.status_code == 200
        data = response.json()
        assert data["photosUsed"] == 0
        assert data["dailyLimit"] == 2
        assert data["remaining"] == 2
        assert data["subscriptionStatus"] == "free"
        assert data["upgradeHint"] == "soft"
    finally:
        del app.dependency_overrides[get_current_user]

@pytest.mark.asyncio
async def test_get_usage_success_active(client):
    """Test GET /v1/usage/today with active user."""
    mock_user = {
        "id": "00000000-0000-0000-0000-000000000002",
        "telegram_id": 222222222,
        "subscription_status": "active",
        "subscription_active_until": datetime(2099, 1, 1, tzinfo=timezone.utc),
        "referral_credits": 0,
        "is_onboarded": True,
        "profile": "{}"
    }
    
    app.dependency_overrides[get_current_user] = lambda: mock_user
    
    try:
        response = await client.get("/v1/usage/today")
        assert response.status_code == 200
        data = response.json()
        assert data["dailyLimit"] == 20
        assert data["subscriptionStatus"] == "active"
        assert data["upgradeHint"] is None
    finally:
        del app.dependency_overrides[get_current_user]

@pytest.mark.asyncio
async def test_get_usage_success_blocked(client):
    """Test GET /v1/usage/today with blocked user."""
    mock_user = {
        "id": "00000000-0000-0000-0000-000000000003",
        "telegram_id": 333333333,
        "subscription_status": "blocked",
        "subscription_active_until": None,
        "referral_credits": 0,
        "is_onboarded": True,
        "profile": "{}"
    }
    
    app.dependency_overrides[get_current_user] = lambda: mock_user
    
    try:
        response = await client.get("/v1/usage/today")
        assert response.status_code == 200
        data = response.json()
        assert data["dailyLimit"] == 0
        assert data["remaining"] == 0
        assert data["subscriptionStatus"] == "blocked"
        assert data["upgradeHint"] == "hard"
    finally:
        del app.dependency_overrides[get_current_user]


@pytest.mark.asyncio
async def test_get_usage_upgrade_hint_soft_when_remaining_one(client):
    mock_user = {
        "id": "00000000-0000-0000-0000-000000000004",
        "telegram_id": 444444444,
        "subscription_status": "free",
        "subscription_active_until": None,
        "referral_credits": 0,
        "is_onboarded": True,
        "profile": "{}",
    }

    class Conn:
        async def fetchrow(self, query, *args):
            return {"photos_used": 1}

    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[get_db] = lambda: Conn()

    try:
        response = await client.get("/v1/usage/today")
        assert response.status_code == 200
        data = response.json()
        assert data["remaining"] == 1
        assert data["upgradeHint"] == "soft"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_get_usage_upgrade_hint_hard_when_remaining_zero(client):
    mock_user = {
        "id": "00000000-0000-0000-0000-000000000005",
        "telegram_id": 555555555,
        "subscription_status": "free",
        "subscription_active_until": None,
        "referral_credits": 0,
        "is_onboarded": True,
        "profile": "{}",
    }

    class Conn:
        async def fetchrow(self, query, *args):
            return {"photos_used": 2}

    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[get_db] = lambda: Conn()

    try:
        response = await client.get("/v1/usage/today")
        assert response.status_code == 200
        data = response.json()
        assert data["remaining"] == 0
        assert data["upgradeHint"] == "hard"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_get_usage_includes_referral_credits_in_daily_limit(client):
    mock_user = {
        "id": "00000000-0000-0000-0000-000000000006",
        "telegram_id": 666666666,
        "subscription_status": "free",
        "subscription_active_until": None,
        "referral_credits": 3,
        "is_onboarded": True,
        "profile": "{}",
    }

    class Conn:
        async def fetchrow(self, query, *args):
            return {"photos_used": 1}

    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[get_db] = lambda: Conn()

    try:
        response = await client.get("/v1/usage/today")
        assert response.status_code == 200
        data = response.json()
        assert data["dailyLimit"] == 5
        assert data["remaining"] == 4
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
