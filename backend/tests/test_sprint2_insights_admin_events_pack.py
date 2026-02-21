from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from app.config import settings
from app.db import get_db
from app.deps import get_current_user
from app.main import app


TEST_USER_ID = "00000000-0000-0000-0000-00000000e001"
OTHER_USER_ID = "00000000-0000-0000-0000-00000000e099"
ADMIN_USER_ID = "00000000-0000-0000-0000-00000000ea11"


def _auth_user(user_id: str) -> dict[str, Any]:
    return {
        "id": user_id,
        "telegram_id": 987654321,
        "username": "insights-user",
        "is_onboarded": True,
        "subscription_status": "free",
        "subscription_active_until": None,
        "profile": {},
    }


def _parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pick(payload: dict[str, Any], *keys: str):
    for key in keys:
        if key in payload:
            return payload[key]
    raise AssertionError(f"Expected one of keys={keys} in payload={payload}")


def _event_id(row: dict[str, Any]) -> str:
    return str(_pick(row, "id", "eventId"))


def _event_created_at(row: dict[str, Any]) -> datetime:
    raw = _pick(row, "createdAt", "created_at")
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(timezone.utc)
    return _parse_iso_datetime(str(raw))


def _event_type(row: dict[str, Any]) -> str:
    return str(_pick(row, "eventType", "event_type"))


def _day_date(row: dict[str, Any]) -> date:
    raw = _pick(row, "date", "day")
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    return date.fromisoformat(str(raw))


def _day_value(row: dict[str, Any], canonical: str) -> float:
    aliases = {
        "calories_kcal": ("calories_kcal", "calories", "calories_today"),
        "protein_g": ("protein_g", "protein", "protein_today"),
        "fat_g": ("fat_g", "fat", "fat_today"),
        "carbs_g": ("carbs_g", "carbs", "carbs_today"),
        "meals_count": ("mealsCount", "meals_count", "count"),
    }
    return float(_pick(row, *aliases[canonical]))


class FakeInsightsConn:
    def __init__(self):
        tie_ts = datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc)

        self.events = [
            {
                "id": "00000000-0000-0000-0000-00000000e0ab",
                "user_id": TEST_USER_ID,
                "event_type": "analyze_completed",
                "payload": {"seed": 1},
                "created_at": tie_ts,
            },
            {
                "id": "00000000-0000-0000-0000-00000000e0aa",
                "user_id": TEST_USER_ID,
                "event_type": "rate_limited",
                "payload": {"seed": 2},
                "created_at": tie_ts,
            },
            {
                "id": "00000000-0000-0000-0000-00000000e0ac",
                "user_id": TEST_USER_ID,
                "event_type": "payment_created",
                "payload": {"seed": 3},
                "created_at": datetime(2026, 2, 14, 8, 0, 0, tzinfo=timezone.utc),
            },
            {
                "id": "00000000-0000-0000-0000-00000000e0ad",
                "user_id": TEST_USER_ID,
                "event_type": "analyze_failed",
                "payload": {"seed": 4},
                "created_at": datetime(2026, 2, 13, 9, 30, 0, tzinfo=timezone.utc),
            },
            {
                "id": "00000000-0000-0000-0000-00000000e0ae",
                "user_id": TEST_USER_ID,
                "event_type": "payment_succeeded",
                "payload": {"seed": 5},
                "created_at": datetime(2026, 2, 12, 7, 0, 0, tzinfo=timezone.utc),
            },
            {
                "id": "00000000-0000-0000-0000-00000000e0ff",
                "user_id": OTHER_USER_ID,
                "event_type": "analyze_completed",
                "payload": {"seed": 99},
                "created_at": datetime(2026, 2, 16, 8, 0, 0, tzinfo=timezone.utc),
            },
        ]

        self.weekly_days = [
            {
                "date": date(2026, 2, 9),
                "calories_kcal": 200.0,
                "protein_g": 12.0,
                "fat_g": 7.0,
                "carbs_g": 30.0,
                "meals_count": 1,
            },
            {
                "date": date(2026, 2, 10),
                "calories_kcal": 320.0,
                "protein_g": 18.0,
                "fat_g": 9.0,
                "carbs_g": 40.0,
                "meals_count": 2,
            },
            {
                "date": date(2026, 2, 11),
                "calories_kcal": 0.0,
                "protein_g": 0.0,
                "fat_g": 0.0,
                "carbs_g": 0.0,
                "meals_count": 0,
            },
            {
                "date": date(2026, 2, 12),
                "calories_kcal": 480.0,
                "protein_g": 25.0,
                "fat_g": 15.0,
                "carbs_g": 55.0,
                "meals_count": 2,
            },
            {
                "date": date(2026, 2, 13),
                "calories_kcal": 510.0,
                "protein_g": 27.0,
                "fat_g": 18.0,
                "carbs_g": 60.0,
                "meals_count": 2,
            },
            {
                "date": date(2026, 2, 14),
                "calories_kcal": 150.0,
                "protein_g": 9.0,
                "fat_g": 5.0,
                "carbs_g": 20.0,
                "meals_count": 1,
            },
            {
                "date": date(2026, 2, 15),
                "calories_kcal": 610.0,
                "protein_g": 31.0,
                "fat_g": 20.0,
                "carbs_g": 75.0,
                "meals_count": 3,
            },
        ]

    async def fetch(self, query, *args):
        if "FROM events" in query:
            return self._fetch_events(query, args)
        if "FROM daily_stats" in query or "FROM meals" in query:
            return self._fetch_weekly_rows(query, args)
        return []

    async def fetchrow(self, query, *args):
        if "FROM events" in query:
            rows = self._fetch_events(query, args)
            return rows[0] if rows else None

        if "FROM daily_stats" in query or "FROM meals" in query:
            rows = self._fetch_weekly_rows(query, args)
            return {
                "calories_kcal": sum(float(r["calories_kcal"]) for r in rows),
                "protein_g": sum(float(r["protein_g"]) for r in rows),
                "fat_g": sum(float(r["fat_g"]) for r in rows),
                "carbs_g": sum(float(r["carbs_g"]) for r in rows),
                "meals_count": sum(int(r["meals_count"]) for r in rows),
            }

        return None

    async def execute(self, query, *args):
        return "OK"

    async def fetchval(self, query, *args):
        return 0

    def _fetch_events(self, query: str, args: tuple[Any, ...]):
        if not args:
            return []

        user_id = str(args[0])
        limit = 50
        event_type_filter = None
        cursor_id = None
        cursor_created_at = None
        date_args: list[datetime] = []
        day_args: list[date] = []

        for arg in args[1:]:
            if isinstance(arg, int):
                limit = arg
            elif isinstance(arg, datetime):
                if arg.tzinfo is None:
                    date_args.append(arg.replace(tzinfo=timezone.utc))
                else:
                    date_args.append(arg.astimezone(timezone.utc))
            elif isinstance(arg, date):
                day_args.append(arg)
            elif isinstance(arg, str):
                if len(arg) == 36 and arg.count("-") == 4:
                    cursor_id = arg
                else:
                    event_type_filter = arg

        since = None
        until = None

        if "(created_at, id) <" in query and cursor_id and date_args:
            cursor_created_at = date_args[-1]
            date_args = date_args[:-1]

        if "created_at >=" in query and date_args:
            since = date_args[0]
        elif "created_at >=" in query and day_args:
            since = datetime.combine(day_args[0], datetime.min.time(), tzinfo=timezone.utc)

        if "created_at <" in query and day_args:
            until = datetime.combine(day_args[-1], datetime.min.time(), tzinfo=timezone.utc)
        elif ("created_at <=" in query or "created_at <" in query) and len(date_args) >= 2:
            until = date_args[1]
        elif "created_at <=" in query and len(date_args) == 1 and since is None:
            until = date_args[0]
        elif "created_at <" in query and len(date_args) == 1 and since is None:
            until = date_args[0]

        rows = [event for event in self.events if event["user_id"] == user_id]

        if event_type_filter:
            rows = [event for event in rows if event["event_type"] == event_type_filter]

        if since is not None:
            rows = [event for event in rows if event["created_at"] >= since]

        if until is not None:
            if "created_at <" in query:
                rows = [event for event in rows if event["created_at"] < until]
            else:
                rows = [event for event in rows if event["created_at"] <= until]

        rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)

        if cursor_created_at is not None and cursor_id is not None:
            rows = [
                row
                for row in rows
                if (row["created_at"], row["id"]) < (cursor_created_at, cursor_id)
            ]

        return [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "event_type": row["event_type"],
                "payload": row["payload"],
                "created_at": row["created_at"],
            }
            for row in rows[:limit]
        ]

    def _fetch_weekly_rows(self, query: str, args: tuple[Any, ...]):
        start_date = date(2026, 2, 9)
        end_date = date(2026, 2, 15)

        date_args = [
            arg
            for arg in args
            if isinstance(arg, date) and not isinstance(arg, datetime)
        ]
        if len(date_args) >= 2:
            start_date = date_args[0]
            end_date = date_args[1]

        rows = [
            row
            for row in self.weekly_days
            if start_date <= row["date"] <= end_date
        ]
        return [
            {
                "date": row["date"],
                "calories": row["calories_kcal"],
                "protein": row["protein_g"],
                "fat": row["fat_g"],
                "carbs": row["carbs_g"],
                "meals_count": row["meals_count"],
            }
            for row in rows
        ]


class NoQueryConn:
    def __init__(self):
        self.query_attempted = False

    async def fetch(self, query, *args):
        self.query_attempted = True
        raise AssertionError("DB query must not run for non-admin /v1/admin/events")

    async def fetchrow(self, query, *args):
        self.query_attempted = True
        raise AssertionError("DB query must not run for non-admin /v1/admin/events")

    async def fetchval(self, query, *args):
        self.query_attempted = True
        raise AssertionError("DB query must not run for non-admin /v1/admin/events")

    async def execute(self, query, *args):
        self.query_attempted = True
        raise AssertionError("DB query must not run for non-admin /v1/admin/events")


@pytest.fixture
def insights_conn():
    return FakeInsightsConn()


@pytest.fixture
def auth_user_overrides(insights_conn):
    app.dependency_overrides[get_current_user] = lambda: _auth_user(TEST_USER_ID)
    app.dependency_overrides[get_db] = lambda: insights_conn
    try:
        yield insights_conn
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_events_pagination_keyset_and_since_until_filters(client, auth_user_overrides):
    page1 = await client.get("/v1/events?limit=2")
    assert page1.status_code == 200
    body1 = page1.json()
    assert "items" in body1
    assert isinstance(body1["items"], list)
    assert len(body1["items"]) == 2
    assert isinstance(body1.get("nextCursor"), str)

    page1_ids = [_event_id(row) for row in body1["items"]]
    assert page1_ids == [
        "00000000-0000-0000-0000-00000000e0ab",
        "00000000-0000-0000-0000-00000000e0aa",
    ]
    assert _event_created_at(body1["items"][0]) == _event_created_at(body1["items"][1])

    cursor = body1["nextCursor"]
    page2 = await client.get(f"/v1/events?limit=2&cursor={cursor}")
    assert page2.status_code == 200
    body2 = page2.json()
    page2_ids = [_event_id(row) for row in body2["items"]]
    assert set(page1_ids).isdisjoint(set(page2_ids))

    since_response = await client.get("/v1/events?since=2026-02-14")
    assert since_response.status_code == 200
    for row in since_response.json()["items"]:
        assert _event_created_at(row) >= datetime(2026, 2, 14, 0, 0, 0, tzinfo=timezone.utc)

    until_response = await client.get("/v1/events?until=2026-02-13")
    assert until_response.status_code == 200
    for row in until_response.json()["items"]:
        assert _event_created_at(row) < datetime(2026, 2, 14, 0, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_events_filter_by_event_type_uses_eventType_query_param(client, auth_user_overrides):
    response = await client.get("/v1/events?eventType=payment_created")
    assert response.status_code == 200
    items = response.json()["items"]
    assert [_event_type(row) for row in items] == ["payment_created"]


@pytest.mark.asyncio
async def test_stats_weekly_returns_7_days_and_totals_consistent(client, auth_user_overrides):
    response = await client.get("/v1/stats/weekly?endDate=2026-02-15")
    assert response.status_code == 200

    body = response.json()
    days = _pick(body, "days", "items", "rows")
    assert isinstance(days, list)
    assert len(days) == 7

    returned_dates = [_day_date(day_row) for day_row in days]
    assert returned_dates == [date(2026, 2, 9) + timedelta(days=idx) for idx in range(7)]

    expected_by_date = {row["date"]: row for row in auth_user_overrides.weekly_days}
    for day_row in days:
        day_date = _day_date(day_row)
        expected = expected_by_date[day_date]
        assert _day_value(day_row, "calories_kcal") == expected["calories_kcal"]
        assert _day_value(day_row, "protein_g") == expected["protein_g"]
        assert _day_value(day_row, "fat_g") == expected["fat_g"]
        assert _day_value(day_row, "carbs_g") == expected["carbs_g"]
        assert int(_day_value(day_row, "meals_count")) == expected["meals_count"]

    totals = _pick(body, "totals", "summary")
    assert _day_value(totals, "calories_kcal") == sum(r["calories_kcal"] for r in auth_user_overrides.weekly_days)
    assert _day_value(totals, "protein_g") == sum(r["protein_g"] for r in auth_user_overrides.weekly_days)
    assert _day_value(totals, "fat_g") == sum(r["fat_g"] for r in auth_user_overrides.weekly_days)
    assert _day_value(totals, "carbs_g") == sum(r["carbs_g"] for r in auth_user_overrides.weekly_days)
    assert int(_day_value(totals, "meals_count")) == sum(r["meals_count"] for r in auth_user_overrides.weekly_days)


@pytest.mark.asyncio
async def test_admin_events_access_control_and_shape(client, insights_conn, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_USER_ID)
    app.dependency_overrides[get_db] = lambda: insights_conn
    app.dependency_overrides[get_current_user] = lambda: _auth_user(TEST_USER_ID)

    try:
        denied = await client.get("/v1/admin/events")
        assert denied.status_code == 403
        denied_body = denied.json()
        assert "error" in denied_body
        assert denied_body["error"]["code"] == "FORBIDDEN"
        assert set(denied_body["error"].keys()) >= {"code", "message", "details"}
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    app.dependency_overrides[get_current_user] = lambda: _auth_user(ADMIN_USER_ID)
    try:
        allowed = await client.get("/v1/admin/events?limit=2")
        assert allowed.status_code == 200
        allowed_body = allowed.json()
        assert "items" in allowed_body
        assert isinstance(allowed_body["items"], list)
        assert "nextCursor" in allowed_body
        assert allowed_body["nextCursor"] is None or isinstance(allowed_body["nextCursor"], str)
        if allowed_body["items"]:
            first = allowed_body["items"][0]
            _pick(first, "id", "eventId")
            _pick(first, "eventType", "event_type")
            _pick(first, "createdAt", "created_at")
            _pick(first, "details")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_admin_events_non_admin_is_forbidden_before_any_db_query(client, monkeypatch):
    conn = NoQueryConn()
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_USER_ID)
    app.dependency_overrides[get_db] = lambda: conn
    app.dependency_overrides[get_current_user] = lambda: _auth_user(TEST_USER_ID)

    try:
        response = await client.get("/v1/admin/events")
        assert response.status_code == 403
        body = response.json()
        assert "error" in body
        assert body["error"]["code"] == "FORBIDDEN"
        assert conn.query_attempted is False
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
