import pytest
from datetime import date, timedelta
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

class WeightChartConn:
    async def fetch(self, query, *args):
        if "FROM weight_logs" in query:
            return [
                {"date": date(2026, 2, 10), "weight_kg": 86.2},
                {"date": date(2026, 2, 12), "weight_kg": 85.9},
            ]
        return []

@pytest.mark.asyncio
async def test_weight_chart_returns_points(client):
    app.dependency_overrides[get_current_user] = _active_user

    async def override_get_db():
        yield WeightChartConn()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = await client.get("/v1/stats/charts/weight")
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == [
            {"date": "2026-02-10", "weight": 86.2},
            {"date": "2026-02-12", "weight": 85.9},
        ]
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
