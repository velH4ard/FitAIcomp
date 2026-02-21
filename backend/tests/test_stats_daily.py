from datetime import date, datetime, timezone

import pytest

from app.db import get_db
from app.deps import get_current_user
from app.main import app


MOCK_USER = {
    "id": "00000000-0000-0000-0000-000000000010",
    "telegram_id": 101010101,
    "subscription_status": "free",
    "subscription_active_until": None,
    "is_onboarded": True,
    "profile": "{}",
}


class FakeStatsConn:
    def __init__(self):
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        assert "FROM daily_stats" in query
        assert "AND date = $2::date" in query

        selected_date = args[1]
        if selected_date == date(2026, 2, 13):
            return {
                "calories_kcal": 1234.0,
                "protein_g": 77.5,
                "fat_g": 44.0,
                "carbs_g": 130.25,
                "meals_count": 3,
            }

        return {
            "calories_kcal": 0,
            "protein_g": 0,
            "fat_g": 0,
            "carbs_g": 0,
            "meals_count": 0,
        }


@pytest.mark.asyncio
async def test_stats_daily_returns_aggregated_values_for_date(client):
    fake_conn = FakeStatsConn()
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER
    app.dependency_overrides[get_db] = lambda: fake_conn

    try:
        response = await client.get("/v1/stats/daily?date=2026-02-13")
        assert response.status_code == 200
        body = response.json()
        assert body == {
            "date": "2026-02-13",
            "calories_kcal": 1234.0,
            "protein_g": 77.5,
            "fat_g": 44.0,
            "carbs_g": 130.25,
            "mealsCount": 3,
        }
        assert len(fake_conn.calls) == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_stats_daily_without_date_defaults_to_today(client):
    fake_conn = FakeStatsConn()
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER
    app.dependency_overrides[get_db] = lambda: fake_conn

    try:
        response = await client.get("/v1/stats/daily")
        assert response.status_code == 200
        body = response.json()
        assert body["mealsCount"] == 0
        assert body["date"] == datetime.now(timezone.utc).date().isoformat()
        assert len(fake_conn.calls) == 1
        _, args = fake_conn.calls[0]
        assert args[1] == datetime.now(timezone.utc).date()
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_stats_daily_invalid_date_returns_validation_failed(client):
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER

    try:
        response = await client.get("/v1/stats/daily?date=13-02-2026")
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
