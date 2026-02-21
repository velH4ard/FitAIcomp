from datetime import date, datetime, timedelta, timezone

import pytest

from app.db import get_db
from app.deps import get_current_user
from app.main import app


def _active_user() -> dict:
    return {
        "id": "00000000-0000-0000-0000-00000000bb01",
        "subscription_status": "active",
        "subscription_active_until": datetime.now(timezone.utc) + timedelta(days=30),
        "daily_goal_auto": 2000,
        "daily_goal_override": None,
        "profile": {},
    }


def _free_user() -> dict:
    return {
        "id": "00000000-0000-0000-0000-00000000bb02",
        "subscription_status": "free",
        "subscription_active_until": None,
        "daily_goal_auto": 2000,
        "daily_goal_override": None,
        "profile": {},
    }


class FailOnDbUseConn:
    async def fetch(self, query, *args):
        raise AssertionError("DB must not be used when paywall blocks request")

    async def fetchrow(self, query, *args):
        raise AssertionError("DB must not be used when paywall blocks request")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "feature"),
    [
        ("GET", "/v1/reports/weekly", "reports.weekly"),
        ("GET", "/v1/reports/monthly", "reports.monthly"),
        ("GET", "/v1/analysis/why-not-losing", "analysis.why_not_losing"),
        ("GET", "/v1/charts/weight", "charts.weight"),
        ("PATCH", "/v1/notifications/settings", "notifications.settings"),
    ],
)
async def test_premium_endpoints_block_non_premium_before_payload_compute(client, method, path, feature):
    app.dependency_overrides[get_current_user] = _free_user

    async def override_get_db():
        yield FailOnDbUseConn()

    app.dependency_overrides[get_db] = override_get_db

    try:
        if method == "PATCH":
            response = await client.patch(path, json={"enabled": True})
        else:
            response = await client.get(path)

        assert response.status_code == 402
        body = response.json()
        assert body["error"]["code"] == "PAYWALL_BLOCKED"
        assert body["error"]["message"] == "Доступно только в Premium"
        assert body["error"]["details"] == {
            "feature": feature,
            "prices": {"original": 1499, "current": 499},
        }
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


class WeeklyReportConn:
    async def fetch(self, query, *args):
        assert "FROM daily_stats" in query
        start_date = args[1]
        return [
            {"date": start_date + timedelta(days=0), "calories_kcal": 2200.0},
            {"date": start_date + timedelta(days=1), "calories_kcal": 1800.0},
            {"date": start_date + timedelta(days=2), "calories_kcal": 2000.0},
            {"date": start_date + timedelta(days=3), "calories_kcal": 2100.0},
            {"date": start_date + timedelta(days=4), "calories_kcal": 1900.0},
            {"date": start_date + timedelta(days=5), "calories_kcal": 2300.0},
            {"date": start_date + timedelta(days=6), "calories_kcal": 1700.0},
        ]


@pytest.mark.asyncio
async def test_weekly_report_metrics_are_calculated_correctly(client):
    app.dependency_overrides[get_current_user] = _active_user

    async def override_get_db():
        yield WeeklyReportConn()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = await client.get("/v1/reports/weekly")
        assert response.status_code == 200
        body = response.json()
        assert body["startDate"]
        assert body["endDate"]
        assert len(body["days"]) == 7
        assert body["totals"]["calories_kcal"] == 14000.0
        assert body["totals"]["goalCalories_kcal"] == 14000.0
        assert body["totals"]["deltaCalories_kcal"] == 0.0
        assert body["weightForecast"]["method"] == "7700kcal_per_kg"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


class WhyNotLosingConn:
    def __init__(self, mode: str):
        self.mode = mode

    async def fetch(self, query, *args):
        start_date = args[1]
        if self.mode == "surplus_low_logging":
            return [
                {"date": start_date + timedelta(days=0), "calories_kcal": 2500.0, "meals_count": 2},
                {"date": start_date + timedelta(days=1), "calories_kcal": 2400.0, "meals_count": 2},
                {"date": start_date + timedelta(days=2), "calories_kcal": 2300.0, "meals_count": 2},
                {"date": start_date + timedelta(days=3), "calories_kcal": 2100.0, "meals_count": 1},
            ]
        return [
            {"date": start_date + timedelta(days=0), "calories_kcal": 1900.0, "meals_count": 2},
            {"date": start_date + timedelta(days=1), "calories_kcal": 1920.0, "meals_count": 2},
            {"date": start_date + timedelta(days=2), "calories_kcal": 1950.0, "meals_count": 2},
            {"date": start_date + timedelta(days=3), "calories_kcal": 1880.0, "meals_count": 2},
            {"date": start_date + timedelta(days=4), "calories_kcal": 1910.0, "meals_count": 2},
            {"date": start_date + timedelta(days=5), "calories_kcal": 1930.0, "meals_count": 2},
            {"date": start_date + timedelta(days=6), "calories_kcal": 1890.0, "meals_count": 2},
        ]


@pytest.mark.asyncio
async def test_why_not_losing_detects_surpluses_and_low_logging(client):
    app.dependency_overrides[get_current_user] = _active_user

    async def override_get_db():
        yield WhyNotLosingConn("surplus_low_logging")

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = await client.get("/v1/analysis/why-not-losing")
        assert response.status_code == 200
        body = response.json()
        insights = {item["rule"]: item for item in body["insights"]}
        assert "FREQUENT_SURPLUSES" in insights
        assert "LOW_LOGGING_FREQUENCY" in insights
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_why_not_losing_detects_small_deficit(client):
    app.dependency_overrides[get_current_user] = _active_user

    async def override_get_db():
        yield WhyNotLosingConn("small_deficit")

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = await client.get("/v1/analysis/why-not-losing")
        assert response.status_code == 200
        body = response.json()
        insights = {item["rule"]: item for item in body["insights"]}
        assert "LOW_DEFICIT" in insights
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


class WeightChartConn:
    async def fetch(self, query, *args):
        assert "FROM weight_logs" in query
        return [
            {"date": date(2026, 2, 10), "weight_kg": 86.2},
            {"date": date(2026, 2, 12), "weight_kg": 85.9},
        ]


@pytest.mark.asyncio
async def test_weight_chart_returns_points(client):
    app.dependency_overrides[get_current_user] = _active_user

    async def override_get_db():
        yield WeightChartConn()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = await client.get("/v1/charts/weight")
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == [
            {"date": "2026-02-10", "weight": 86.2},
            {"date": "2026-02-12", "weight": 85.9},
        ]
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_profile_goal_validates_override_bounds(client):
    app.dependency_overrides[get_current_user] = _active_user
    try:
        low_response = await client.patch("/v1/profile/goal", json={"dailyGoal": 999})
        assert low_response.status_code == 400
        assert low_response.json()["error"]["code"] == "VALIDATION_FAILED"

        high_response = await client.patch("/v1/profile/goal", json={"dailyGoal": 5001})
        assert high_response.status_code == 400
        assert high_response.json()["error"]["code"] == "VALIDATION_FAILED"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
