import pytest
from unittest.mock import AsyncMock, patch
from app.main import app
from app.db import get_db

@pytest.mark.asyncio
async def test_auth_telegram_success(client):
    """Test successful Telegram auth."""
    # Mock verify_telegram_init_data to return a valid user
    with patch("app.main.verify_telegram_init_data") as mock_verify:
        mock_verify.return_value = {"id": 123456789, "first_name": "Test", "username": "testuser"}
        
        # Use dependency override for get_db
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = {
            "id": "00000000-0000-0000-0000-000000000000",
            "telegram_id": 123456789,
            "username": "testuser",
            "is_onboarded": False,
            "subscription_status": "free",
            "subscription_active_until": None,
            "profile": None
        }
        
        async def override_get_db():
            yield mock_conn
            
        app.dependency_overrides[get_db] = override_get_db
        
        try:
            response = await client.post("/v1/auth/telegram", json={"initData": "valid_init_data"})
            
            assert response.status_code == 200
            data = response.json()
            assert "accessToken" in data
            assert data["user"]["telegramId"] == 123456789
            assert data["user"]["isOnboarded"] is False
        finally:
            del app.dependency_overrides[get_db]

@pytest.mark.asyncio
async def test_auth_telegram_invalid_hash(client):
    """Test Telegram auth with invalid hash."""
    from app.errors import FitAIError
    with patch("app.main.verify_telegram_init_data") as mock_verify:
        mock_verify.side_effect = FitAIError(code="AUTH_INVALID_INITDATA", message="Invalid hash", status_code=401)
        
        response = await client.post("/v1/auth/telegram", json={"initData": "invalid_data"})
        
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_INVALID_INITDATA"

@pytest.mark.asyncio
async def test_auth_telegram_expired(client):
    """Test Telegram auth with expired data."""
    from app.errors import FitAIError
    with patch("app.main.verify_telegram_init_data") as mock_verify:
        mock_verify.side_effect = FitAIError(code="AUTH_EXPIRED_INITDATA", message="Session expired", status_code=401)
        
        response = await client.post("/v1/auth/telegram", json={"initData": "expired_data"})
        
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_EXPIRED_INITDATA"
