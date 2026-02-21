from datetime import datetime, timedelta, timezone

import pytest

from app.db import get_db
from app.deps import get_current_user
from app.main import app


def _override_user(user: dict) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def _override_db(conn) -> None:
    app.dependency_overrides[get_db] = lambda: conn


def _clear_overrides() -> None:
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


class _NoopConn:
    async def fetch(self, query, *args):
        return []

    async def fetchrow(self, query, *args):
        return None

    async def execute(self, query, *args):
        return "OK"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint,feature",
    [
        ("/v1/reports/weekly", "reports.weekly"),
        ("/v1/reports/monthly", "reports.monthly"),
        ("/v1/analysis/why-not-losing", "analysis.why_not_losing"),
        ("/v1/charts/weight", "charts.weight"),
    ],
)
async def test_non_premium_endpoints_return_paywall_blocked_with_semantic_details(client, endpoint, feature):
    user = {
        "id": "00000000-0000-0000-0000-00000000aa01",
        "telegram_id": 911001,
        "subscription_status": "free",
        "subscription_active_until": None,
        "is_onboarded": True,
        "profile": {},
    }

    _override_user(user)
    _override_db(_NoopConn())
    try:
        response = await client.get(endpoint)
    finally:
        _clear_overrides()

    assert response.status_code == 402
    body = response.json()
    assert body["error"]["code"] == "PAYWALL_BLOCKED"
    assert body["error"]["details"]["feature"] == feature
    assert body["error"]["details"]["prices"] == {"original": 1499, "current": 499}


@pytest.mark.asyncio
async def test_weekly_report_active_user_returns_7_days_and_consistent_totals(client):
    user = {
        "id": "00000000-0000-0000-0000-00000000aa02",
        "telegram_id": 911002,
        "subscription_status": "active",
        "subscription_active_until": datetime.now(timezone.utc) + timedelta(days=7),
        "is_onboarded": True,
        "profile": {},
    }

    _override_user(user)
    _override_db(_NoopConn())
    try:
        response = await client.get("/v1/reports/weekly?endDate=2026-02-13")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    body = response.json()

    assert body["startDate"] == "2026-02-07"
    assert body["endDate"] == "2026-02-13"
    assert len(body["days"]) == 7

    per_day_sum = sum(day["deltaCalories_kcal"] for day in body["days"])
    assert body["totals"]["deltaCalories_kcal"] == per_day_sum


@pytest.mark.asyncio
async def test_why_not_losing_returns_rule_based_patterns(client):
    user = {
        "id": "00000000-0000-0000-0000-00000000aa03",
        "telegram_id": 911003,
        "subscription_status": "active",
        "subscription_active_until": datetime.now(timezone.utc) + timedelta(days=7),
        "is_onboarded": True,
        "profile": {},
    }

    _override_user(user)
    _override_db(_NoopConn())
    try:
        response = await client.get("/v1/analysis/why-not-losing?windowDays=14")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    body = response.json()

    assert body["analysisType"] == "rule_based_v1"
    assert body["windowDays"] == 14
    assert isinstance(body["summary"], str)
    assert body["summary"].strip()
    assert isinstance(body["insights"], list)
    assert len(body["insights"]) >= 1
    for insight in body["insights"]:
        assert insight["rule"] == insight["rule"].upper()
        assert " " not in insight["rule"]
        assert isinstance(insight["text"], str) and insight["text"].strip()
        assert isinstance(insight["recommendation"], str) and insight["recommendation"].strip()


@pytest.mark.asyncio
async def test_weight_chart_response_shape_and_order(client):
    user = {
        "id": "00000000-0000-0000-0000-00000000aa04",
        "telegram_id": 911004,
        "subscription_status": "active",
        "subscription_active_until": datetime.now(timezone.utc) + timedelta(days=7),
        "is_onboarded": True,
        "profile": {},
    }

    _override_user(user)
    _override_db(_NoopConn())
    try:
        response = await client.get("/v1/charts/weight?dateFrom=2026-02-01&dateTo=2026-02-07")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["items"], list)

    dates = [item["date"] for item in body["items"]]
    assert dates == sorted(dates)

    for item in body["items"]:
        assert set(item.keys()) == {"date", "weight"}
        assert isinstance(item["date"], str)
        assert 20 <= float(item["weight"]) <= 400


@pytest.mark.asyncio
async def test_profile_goal_override_behavior_returns_effective_and_override(client):
    user = {
        "id": "00000000-0000-0000-0000-00000000aa05",
        "telegram_id": 911005,
        "subscription_status": "free",
        "subscription_active_until": None,
        "is_onboarded": True,
        "profile": {},
    }

    class _GoalConn(_NoopConn):
        async def fetchrow(self, query, *args):
            if "daily_goal_override = $1" in query:
                return {
                    "daily_goal_auto": 2100,
                    "daily_goal_override": int(args[0]),
                }
            return None

    _override_user(user)
    _override_db(_GoalConn())
    try:
        response = await client.patch("/v1/profile/goal", json={"dailyGoal": 2400})
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json() == {
        "dailyGoal": 2400,
        "autoGoal": 2100,
        "override": 2400,
    }
