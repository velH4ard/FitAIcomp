from datetime import datetime, timedelta, timezone

import pytest

from app import payments
from app.db import get_db
from app.deps import get_current_user
from app.main import app


BASE_USER = {
    "id": "00000000-0000-0000-0000-000000000922",
    "telegram_id": 922001,
    "username": "paywall-user",
    "is_onboarded": True,
    "subscription_status": "free",
    "subscription_active_until": None,
    "referral_credits": 0,
    "profile": "{}",
}


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePaywallConn:
    def __init__(self):
        self.usage_daily = {}
        self.user_daily_flags = set()
        self.events = []
        self.event_insert_attempts = 0
        self.fail_event_insert = False

    def transaction(self):
        return _Tx()

    async def execute(self, query, *args):
        if "INSERT INTO user_daily_flags" in query:
            user_id = str(args[0])
            flag_name = str(args[1])
            if len(args) >= 3:
                flag_date = args[2]
            else:
                flag_date = payments.get_now_utc().date()
            self.user_daily_flags.add((user_id, flag_name, flag_date))
            return "INSERT 0 1"

        if "INSERT INTO events" in query:
            self.event_insert_attempts += 1
            user_id, event_type, payload = args
            if self.fail_event_insert and event_type in {"subscription_expiring_soon", "referral_bonus_available_shown"}:
                raise RuntimeError("events store unavailable")

            self.events.append(
                {
                    "user_id": str(user_id) if user_id is not None else None,
                    "event_type": event_type,
                    "payload": payload,
                }
            )
            return "INSERT 0 1"

        return "OK"

    async def fetchrow(self, query, *args):
        if "SELECT photos_used FROM usage_daily" in query:
            user_id = str(args[0])
            day = args[1]
            return {"photos_used": self.usage_daily.get((user_id, day), 0)}

        if "FROM user_daily_flags" in query and "SELECT" in query:
            user_id = str(args[0])
            flag_name = str(args[1])
            flag_date = args[2]
            if (user_id, flag_name, flag_date) in self.user_daily_flags:
                return {"user_id": user_id, "flag_name": flag_name, "flag_date": flag_date}
            return None

        if "INSERT INTO user_daily_flags" in query and "RETURNING" in query:
            user_id = str(args[0])
            flag_name = str(args[1])
            if len(args) >= 3:
                flag_date = args[2]
            else:
                flag_date = payments.get_now_utc().date()
            key = (user_id, flag_name, flag_date)
            if key in self.user_daily_flags:
                return None
            self.user_daily_flags.add(key)
            return {"user_id": user_id}

        return None

    async def fetchval(self, query, *args):
        row = await self.fetchrow(query, *args)
        if not row:
            return None
        return next(iter(row.values()))


def _set_overrides(user, conn):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: conn


def _clear_overrides():
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


def _make_user(*, status, active_until):
    return {
        **BASE_USER,
        "subscription_status": status,
        "subscription_active_until": active_until,
    }


@pytest.fixture
def fake_paywall_conn():
    return FakePaywallConn()


@pytest.fixture
def freeze_paywall_now(monkeypatch):
    fixed_now = datetime(2026, 2, 20, 12, 0, 0, tzinfo=timezone.utc)
    real_datetime = datetime

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(payments, "datetime", FrozenDateTime, raising=False)
    return fixed_now


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_name,user,photos_used,expected_reason",
    [
        (
            "blocked_to_quota_reached",
            _make_user(status="blocked", active_until=datetime(2026, 3, 1, tzinfo=timezone.utc)),
            0,
            "quota_reached",
        ),
        (
            "remaining_zero_to_quota_reached",
            _make_user(status="free", active_until=None),
            2,
            "quota_reached",
        ),
        (
            "non_active_to_soft_hint",
            _make_user(status="expired", active_until=datetime(2026, 2, 1, tzinfo=timezone.utc)),
            0,
            "soft_hint",
        ),
        (
            "active_will_expire_soon_to_expiring_soon",
            _make_user(status="active", active_until=datetime(2026, 2, 22, tzinfo=timezone.utc)),
            0,
            "expiring_soon",
        ),
        (
            "free_with_remaining_to_soft_hint",
            _make_user(status="free", active_until=None),
            1,
            "soft_hint",
        ),
        (
            "default_to_none",
            _make_user(status="active", active_until=datetime(2026, 3, 5, tzinfo=timezone.utc)),
            0,
            "none",
        ),
    ],
)
async def test_paywall_context_reason_precedence_table_driven(
    client,
    fake_paywall_conn,
    freeze_paywall_now,
    case_name,
    user,
    photos_used,
    expected_reason,
):
    day = freeze_paywall_now.date()
    fake_paywall_conn.usage_daily[(str(user["id"]), day)] = photos_used
    _set_overrides(user, fake_paywall_conn)
    try:
        response = await client.get("/v1/paywall/context")
    finally:
        _clear_overrides()

    assert response.status_code == 200, case_name
    assert response.json()["reason"] == expected_reason, case_name


@pytest.mark.asyncio
async def test_paywall_context_churn_guard_deduplicates_same_day(client, fake_paywall_conn, freeze_paywall_now):
    user = _make_user(
        status="active",
        active_until=freeze_paywall_now + timedelta(days=2),
    )
    day = freeze_paywall_now.date()
    fake_paywall_conn.usage_daily[(str(user["id"]), day)] = 0

    _set_overrides(user, fake_paywall_conn)
    try:
        first = await client.get("/v1/paywall/context")
        second = await client.get("/v1/paywall/context")
    finally:
        _clear_overrides()

    assert first.status_code == 200
    assert second.status_code == 200

    user_flags = [
        row
        for row in fake_paywall_conn.user_daily_flags
        if row[0] == str(user["id"]) and row[1] == "subscription_expiring_soon" and row[2] == day
    ]
    emitted = [
        event
        for event in fake_paywall_conn.events
        if event["user_id"] == str(user["id"]) and event["event_type"] == "subscription_expiring_soon"
    ]

    assert len(user_flags) == 1
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_paywall_context_event_write_failure_is_best_effort(client, fake_paywall_conn, freeze_paywall_now):
    user = _make_user(
        status="active",
        active_until=freeze_paywall_now + timedelta(days=1),
    )
    day = freeze_paywall_now.date()
    fake_paywall_conn.usage_daily[(str(user["id"]), day)] = 0
    fake_paywall_conn.fail_event_insert = True

    _set_overrides(user, fake_paywall_conn)
    try:
        response = await client.get("/v1/paywall/context")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["reason"] == "expiring_soon"
    assert fake_paywall_conn.event_insert_attempts >= 1


@pytest.mark.asyncio
async def test_paywall_context_referral_bonus_available_reason_precedence(client, fake_paywall_conn, freeze_paywall_now):
    user = _make_user(status="free", active_until=None)
    user["referral_credits"] = 3
    day = freeze_paywall_now.date()
    fake_paywall_conn.usage_daily[(str(user["id"]), day)] = 1

    _set_overrides(user, fake_paywall_conn)
    try:
        response = await client.get("/v1/paywall/context")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["reason"] == "referral_bonus_available"
    assert body["subscriptionStatus"] == "free"


@pytest.mark.asyncio
async def test_paywall_context_referral_bonus_event_dedup_once_per_day(client, fake_paywall_conn, freeze_paywall_now):
    user = _make_user(status="expired", active_until=freeze_paywall_now - timedelta(days=1))
    user["referral_credits"] = 2
    day = freeze_paywall_now.date()
    fake_paywall_conn.usage_daily[(str(user["id"]), day)] = 0

    _set_overrides(user, fake_paywall_conn)
    try:
        first = await client.get("/v1/paywall/context")
        second = await client.get("/v1/paywall/context")
    finally:
        _clear_overrides()

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["reason"] == "referral_bonus_available"
    assert second.json()["reason"] == "referral_bonus_available"

    user_flags = [
        row
        for row in fake_paywall_conn.user_daily_flags
        if row[0] == str(user["id"]) and row[1] == "referral_bonus_available_shown" and row[2] == day
    ]
    emitted = [
        event
        for event in fake_paywall_conn.events
        if event["user_id"] == str(user["id"]) and event["event_type"] == "referral_bonus_available_shown"
    ]

    assert len(user_flags) == 1
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_paywall_context_referral_bonus_event_failure_is_best_effort(client, fake_paywall_conn, freeze_paywall_now):
    user = _make_user(status="free", active_until=None)
    user["referral_credits"] = 1
    day = freeze_paywall_now.date()
    fake_paywall_conn.usage_daily[(str(user["id"]), day)] = 0
    fake_paywall_conn.fail_event_insert = True

    _set_overrides(user, fake_paywall_conn)
    try:
        response = await client.get("/v1/paywall/context")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["reason"] == "referral_bonus_available"
