import pytest
from app.main import app
from app.deps import get_current_user

@pytest.mark.asyncio
async def test_get_me_unauthorized(client):
    """Test GET /v1/me without token."""
    response = await client.get("/v1/me")
    # Should return 401 because of get_current_user dependency
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"

@pytest.mark.asyncio
async def test_get_me_success(client):
    """Test GET /v1/me with valid token (mocked user)."""
    mock_user = {
        "id": "00000000-0000-0000-0000-000000000000",
        "telegram_id": 123456789,
        "username": "testuser",
        "is_onboarded": True,
        "subscription_status": "active",
        "subscription_active_until": None,
        "profile": '{"gender": "male", "age": 25, "heightCm": 180, "weightKg": 75, "goal": "maintain"}'
    }
    
    # Override dependency
    app.dependency_overrides[get_current_user] = lambda: mock_user
    
    try:
        response = await client.get("/v1/me", headers={"Authorization": "Bearer fake-token"})
        
        assert response.status_code == 200
        data = response.json()
        assert data["telegramId"] == 123456789
        assert data["isOnboarded"] is True
        assert data["profile"]["age"] == 25
        assert data["subscription"]["status"] == "active"
    finally:
        # Clean up override
        del app.dependency_overrides[get_current_user]
