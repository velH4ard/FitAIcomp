import pytest
from unittest.mock import AsyncMock, patch
from app.main import app
from app.deps import get_current_user
from app.db import get_db

@pytest.mark.asyncio
async def test_update_profile_success(client):
    """Test successful profile update."""
    mock_user = {"id": "00000000-0000-0000-0000-000000000000"}
    app.dependency_overrides[get_current_user] = lambda: mock_user
    
    valid_profile = {
        "gender": "male",
        "age": 25,
        "heightCm": 180,
        "weightKg": 75.5,
        "goal": "lose_weight"
    }
    
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "id": mock_user["id"],
        "is_onboarded": True,
        "profile": valid_profile
    }
    
    async def override_get_db():
        yield mock_conn
        
    app.dependency_overrides[get_db] = override_get_db
    
    try:
        response = await client.put("/v1/me/profile", json=valid_profile)
        
        assert response.status_code == 200
        data = response.json()
        assert data["isOnboarded"] is True
        assert data["profile"]["age"] == 25
        assert data["profile"]["goal"] == "lose_weight"
    finally:
        del app.dependency_overrides[get_current_user]
        del app.dependency_overrides[get_db]

@pytest.mark.asyncio
async def test_update_profile_invalid_range(client):
    """Test profile update with invalid values (age too low)."""
    mock_user = {"id": "00000000-0000-0000-0000-000000000000"}
    app.dependency_overrides[get_current_user] = lambda: mock_user
    
    invalid_profile = {
        "gender": "male",
        "age": 5, # Minimum is 10
        "heightCm": 180,
        "weightKg": 75.5,
        "goal": "lose_weight"
    }
    
    response = await client.put("/v1/me/profile", json=invalid_profile)
    
    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == "VALIDATION_FAILED"
    assert "fieldErrors" in data["error"]["details"]
    
    del app.dependency_overrides[get_current_user]

@pytest.mark.asyncio
async def test_update_profile_invalid_enum(client):
    """Test profile update with invalid enum value for goal."""
    mock_user = {"id": "00000000-0000-0000-0000-000000000000"}
    app.dependency_overrides[get_current_user] = lambda: mock_user
    
    invalid_profile = {
        "gender": "attack_helicopter", # Invalid enum
        "age": 25,
        "heightCm": 180,
        "weightKg": 75.5,
        "goal": "become_god" # Invalid enum
    }
    
    response = await client.put("/v1/me/profile", json=invalid_profile)
    
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    
    del app.dependency_overrides[get_current_user]
