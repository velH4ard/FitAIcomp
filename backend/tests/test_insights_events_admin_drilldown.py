from datetime import date, datetime, timedelta, timezone
from typing import Union
from uuid import UUID

import pytest

from app.config import settings
from app.db import get_db
from app.deps import get_current_user
from app.main import app


USER_ID = "00000000-0000-0000-0000-000000000201"
ADMIN_USER_ID = "00000000-0000-0000-0000-000000000202"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000203"

USER = {
    "id": USER_ID,
    "telegram_id": 200001,
    "username": "insights-user",
    "is_onboarded": True,
    "subscription_status": "free",
    "subscription_active_until": None,
    "profile": {},
}

ADMIN = {
    **USER,
    "id": ADMIN_USER_ID,
}


class FakeInsightsConn:
    def __init__(self):
        self.fetch_calls: list[tuple[str, tuple]] = []
        now = datetime(2026, 2, 13, 12, 0, tzinfo=timezone.utc)

        self.events = [
            {
                "id": "00000000-0000-0000-0000-00000000e011",
                "user_id": USER_ID,
                "event_type": "MEAL_ANALYZE_OK",
                "payload": {"mealId": "00000000-0000-0000-0000-00000000m001"},
                "created_at": now,
            },
            {
                "id": "00000000-0000-0000-0000-00000000e010",
                "user_id": USER_ID,
                "event_type": "PAYMENT_CREATE_OK",
                "payload": {"paymentId": "p-1"},
                "created_at": now - timedelta(minutes=1),
            },
            {
                "id": "00000000-0000-0000-0000-00000000e009",
                "user_id": OTHER_USER_ID,
                "event_type": "PAYMENT_WEBHOOK_OK",
                "payload": {"paymentId": "p-2"},
                "created_at": now - timedelta(minutes=2),
            },
        ]

        self.meals = [
            {
                "user_id": USER_ID,
                "created_at": datetime(2026, 2, 13, 10, 0, tzinfo=timezone.utc),
                "totals": {"calories": 500.0, "protein": 20.0, "fat": 15.0, "carbs": 60.0},
            },
            {
                "user_id": USER_ID,
                "created_at": datetime(2026, 2, 11, 9, 0, tzinfo=timezone.utc),
                "totals": {"calories": 700.0, "protein": 35.0, "fat": 25.0, "carbs": 70.0},
            },
            {
                "user_id": USER_ID,
                "created_at": datetime(2026, 2, 11, 13, 0, tzinfo=timezone.utc),
                "totals": {"calories": 300.0, "protein": 10.0, "fat": 10.0, "carbs": 30.0},
            },
            {
                "user_id": OTHER_USER_ID,
                "created_at": datetime(2026, 2, 13, 11, 0, tzinfo=timezone.utc),
                "totals": {"calories": 999.0, "protein": 99.0, "fat": 99.0, "carbs": 99.0},
            },
        ]

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        if "FROM events" in query:
            return self._fetch_events(query, *args)
        if "FROM meals" in query and "GROUP BY created_at::date" in query:
            return self._fetch_weekly_stats(*args)
        return []

    def _fetch_events(self, query, *args):
        limit = int(args[-1])
        user_scoped = "WHERE user_id = $1" in query

        filter_user_id = str(args[0]) if user_scoped else None
        event_type = None
        since_date = None
        until_exclusive = None
        cursor_created_at = None
        cursor_id = None

        arg_idx = 1 if user_scoped else 0
        while arg_idx < len(args) - 1:
            arg = args[arg_idx]
            if isinstance(arg, datetime):
                cursor_created_at = arg
            elif isinstance(arg, date):
                if "created_at >=" in query and since_date is None:
                    since_date = arg
                else:
                    until_exclusive = arg
            elif isinstance(arg, str):
                parsed_uuid = None
                try:
                    parsed_uuid = UUID(arg)
                except Exception:
                    parsed_uuid = None

                if parsed_uuid is not None:
                    if cursor_created_at is not None and cursor_id is None:
                        cursor_id = arg
                    elif not user_scoped and "user_id =" in query and filter_user_id is None:
                        filter_user_id = arg
                elif event_type is None:
                    event_type = arg
            arg_idx += 1

        rows = list(self.events)
        if filter_user_id is not None:
            rows = [row for row in rows if row["user_id"] == filter_user_id]
        if event_type is not None:
            rows = [row for row in rows if row["event_type"] == event_type]
        if since_date is not None:
            rows = [row for row in rows if row["created_at"] >= datetime.combine(since_date, datetime.min.time(), tzinfo=timezone.utc)]
        if until_exclusive is not None:
            rows = [row for row in rows if row["created_at"] < datetime.combine(until_exclusive, datetime.min.time(), tzinfo=timezone.utc)]
        if cursor_created_at is not None and cursor_id is not None:
            rows = [
                row
                for row in rows
                if (row["created_at"], row["id"]) < (cursor_created_at, cursor_id)
            ]

        rows.sort(key=lambda x: (x["created_at"], x["id"]), reverse=True)
        return rows[:limit]

    def _fetch_weekly_stats(self, *args):
        user_id = str(args[0])
        start_date = args[1]
        end_date = args[2]
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        if isinstance(end_date, datetime):
            end_date = end_date.date()
        per_day: dict[date, dict[str, Union[float, int]]] = {}

        for meal in self.meals:
            if meal["user_id"] != user_id:
                continue

            created_date = meal["created_at"].date()
            if created_date < start_date or created_date > end_date:
                continue

            bucket = per_day.setdefault(
                created_date,
                {"calories": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "meals_count": 0},
            )
            bucket["calories"] += meal["totals"]["calories"]
            bucket["protein"] += meal["totals"]["protein"]
            bucket["fat"] += meal["totals"]["fat"]
            bucket["carbs"] += meal["totals"]["carbs"]
            bucket["meals_count"] += 1

        rows = []
        for stat_date in sorted(per_day.keys()):
            bucket = per_day[stat_date]
            rows.append(
                {
                    "date": stat_date,
                    "calories": bucket["calories"],
                    "protein": bucket["protein"],
                    "fat": bucket["fat"],
                    "carbs": bucket["carbs"],
                    "meals_count": bucket["meals_count"],
                }
            )
        return rows


@pytest.fixture
def insights_conn():
    return FakeInsightsConn()


@pytest.mark.asyncio
async def test_events_user_scoped_cursor_pagination(client, insights_conn):
    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = lambda: insights_conn

    try:
        page1 = await client.get("/v1/events?limit=1")
        assert page1.status_code == 200
        body1 = page1.json()
        assert len(body1["items"]) == 1
        assert body1["items"][0]["eventType"] == "MEAL_ANALYZE_OK"
        assert isinstance(body1["nextCursor"], str)

        page2 = await client.get(f"/v1/events?limit=1&cursor={body1['nextCursor']}")
        assert page2.status_code == 200
        body2 = page2.json()
        assert len(body2["items"]) == 1
        assert body2["items"][0]["eventType"] == "PAYMENT_CREATE_OK"
        assert body2["nextCursor"] is None
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_events_user_filter_and_validation_errors(client, insights_conn):
    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = lambda: insights_conn

    try:
        filtered = await client.get("/v1/events?eventType=PAYMENT_CREATE_OK&since=2026-02-13&until=2026-02-13")
        assert filtered.status_code == 200
        items = filtered.json()["items"]
        assert len(items) == 1
        assert items[0]["eventType"] == "PAYMENT_CREATE_OK"

        bad_cursor = await client.get("/v1/events?cursor=bad")
        assert bad_cursor.status_code == 400
        assert bad_cursor.json()["error"]["code"] == "VALIDATION_FAILED"
        assert bad_cursor.json()["error"]["details"]["fieldErrors"][0]["field"] == "cursor"

        bad_since = await client.get("/v1/events?since=13-02-2026")
        assert bad_since.status_code == 400
        assert bad_since.json()["error"]["code"] == "VALIDATION_FAILED"
        assert bad_since.json()["error"]["details"]["fieldErrors"][0]["field"] == "since"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_stats_weekly_returns_7_days_and_totals_single_query(client, insights_conn):
    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = lambda: insights_conn

    try:
        response = await client.get("/v1/stats/weekly?endDate=2026-02-13")
        assert response.status_code == 200
        body = response.json()

        assert len(body["days"]) == 7
        assert body["days"][0]["date"] == "2026-02-07"
        assert body["days"][6]["date"] == "2026-02-13"

        day_11 = [day for day in body["days"] if day["date"] == "2026-02-11"][0]
        assert day_11["calories_kcal"] == 1000.0
        assert day_11["mealsCount"] == 2

        assert body["totals"] == {
            "calories_kcal": 1500.0,
            "protein_g": 65.0,
            "fat_g": 50.0,
            "carbs_g": 160.0,
            "mealsCount": 3,
        }

        meals_queries = [q for q, _ in insights_conn.fetch_calls if "FROM meals" in q]
        assert len(meals_queries) == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_stats_weekly_invalid_date_returns_validation_failed(client):
    app.dependency_overrides[get_current_user] = lambda: USER
    try:
        response = await client.get("/v1/stats/weekly?endDate=13-02-2026")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"
        assert response.json()["error"]["details"]["fieldErrors"][0]["field"] == "endDate"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_admin_events_requires_admin_and_supports_filters(client, insights_conn, monkeypatch):
    app.dependency_overrides[get_db] = lambda: insights_conn
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_USER_ID)

    app.dependency_overrides[get_current_user] = lambda: USER
    forbidden = await client.get("/v1/admin/events")
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"

    app.dependency_overrides[get_current_user] = lambda: ADMIN
    filtered = await client.get(
        f"/v1/admin/events?userId={USER_ID}&eventType=PAYMENT_CREATE_OK&since=2026-02-13&until=2026-02-13"
    )
    assert filtered.status_code == 200
    body = filtered.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["eventType"] == "PAYMENT_CREATE_OK"
    assert body["items"][0]["userId"] == USER_ID
    assert body["nextCursor"] is None

    malformed_cursor = await client.get("/v1/admin/events?cursor=bad")
    assert malformed_cursor.status_code == 400
    assert malformed_cursor.json()["error"]["details"]["fieldErrors"][0]["field"] == "cursor"

    bad_until = await client.get("/v1/admin/events?until=2026/02/13")
    assert bad_until.status_code == 400
    assert bad_until.json()["error"]["details"]["fieldErrors"][0]["field"] == "until"

    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_admin_events_cursor_keyset_stable(client, insights_conn, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_USER_ID)
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    app.dependency_overrides[get_db] = lambda: insights_conn

    try:
        response = await client.get("/v1/admin/events?limit=1")
        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 1
        assert isinstance(body["nextCursor"], str)

        next_page = await client.get(f"/v1/admin/events?limit=1&cursor={body['nextCursor']}")
        assert next_page.status_code == 200
        assert next_page.json()["items"][0]["id"] != body["items"][0]["id"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
