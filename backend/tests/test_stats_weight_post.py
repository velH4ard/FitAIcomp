import pytest
from datetime import date
from app.main import app
from app.deps import get_current_user
from app.db import get_db

def _active_user() -> dict:
    return {
        "id": "00000000-0000-0000-0000-00000000bb01",
        "subscription_status": "active",
        "subscription_active_until": None,
        "daily_goal_auto": 2000,
        "daily_goal_override": None,
        "profile": {},
    }

class MockConn:
    async def fetchrow(self, query, *args):
        if "INSERT INTO weight_logs" in query:
            return {
                "id": "11111111-1111-1111-1111-111111111111",
                "user_id": args[0],
                "date": args[1],
                "weight_kg": args[2],
                "created_at": date.today()
            }
        return None

@pytest.mark.asyncio
async def test_post_weight_log(client):
    app.dependency_overrides[get_current_user] = _active_user

    async def override_get_db():
        yield MockConn()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = await client.post("/v1/stats/weight", json={"date": "2026-03-04", "weightKg": 75.5})
        assert response.status_code == 200
        body = response.json()
        assert body["weightKg"] == 75.5
        assert body["date"] == "2026-03-04"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
