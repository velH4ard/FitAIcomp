from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.config import settings
from app.db import get_db
from app.deps import get_current_user
from app.main import app


ADMIN_USER_ID = "00000000-0000-0000-0000-00000000b301"
NON_ADMIN_USER_ID = "00000000-0000-0000-0000-00000000b302"


def _auth_user(user_id: str) -> dict:
    return {
        "id": user_id,
        "telegram_id": 333001,
        "username": "admin-referral",
        "is_onboarded": True,
        "subscription_status": "active",
        "subscription_active_until": datetime(2099, 1, 1, tzinfo=timezone.utc),
        "profile": {},
    }


class NoQueryConn:
    def __init__(self):
        self.query_attempted = False

    async def fetch(self, query, *args):
        self.query_attempted = True
        raise AssertionError("DB query must not run for non-admin admin/referral endpoints")

    async def fetchrow(self, query, *args):
        self.query_attempted = True
        raise AssertionError("DB query must not run for non-admin admin/referral endpoints")


class AdminReferralConn:
    def __init__(self):
        now = datetime(2026, 2, 16, 12, 0, tzinfo=timezone.utc)
        self.redemptions = [
            {
                "id": "00000000-0000-0000-0000-00000000c103",
                "created_at": now,
                "redeemer_user_id": "00000000-0000-0000-0000-00000000d003",
                "referrer_user_id": "00000000-0000-0000-0000-00000000e001",
                "code": "REFCODE003",
                "credits_granted": 1,
            },
            {
                "id": "00000000-0000-0000-0000-00000000c102",
                "created_at": now - timedelta(minutes=1),
                "redeemer_user_id": "00000000-0000-0000-0000-00000000d002",
                "referrer_user_id": "00000000-0000-0000-0000-00000000e001",
                "code": "REFCODE002",
                "credits_granted": 2,
            },
            {
                "id": "00000000-0000-0000-0000-00000000c101",
                "created_at": now - timedelta(minutes=2),
                "redeemer_user_id": "00000000-0000-0000-0000-00000000d001",
                "referrer_user_id": "00000000-0000-0000-0000-00000000e002",
                "code": "REFCODE001",
                "credits_granted": 3,
            },
        ]

    async def fetchrow(self, query, *args):
        if "today_codes_issued" in query:
            return {
                "today_codes_issued": 5,
                "today_redeems": 3,
                "today_unique_redeemers": 3,
                "today_credits_granted": 6,
            }

        if "codes_issued" in query and "FROM referral_codes" in query:
            return {
                "codes_issued": 27,
                "redeems": 11,
                "credits_granted": 17,
            }

        return None

    async def fetch(self, query, *args):
        if "FROM referral_redemptions" not in query:
            return []

        rows = list(self.redemptions)
        cursor_created_at = None
        cursor_id = None
        limit = int(args[-1])

        if "redeemer_user_id =" in query:
            user_filter = str(args[0])
            rows = [row for row in rows if row["redeemer_user_id"] == user_filter]

        if "referrer_user_id =" in query:
            referrer_filter_idx = 1 if "redeemer_user_id =" in query else 0
            referrer_filter = str(args[referrer_filter_idx])
            rows = [row for row in rows if row["referrer_user_id"] == referrer_filter]

        for arg in args[:-1]:
            if isinstance(arg, datetime):
                cursor_created_at = arg
            elif isinstance(arg, str):
                try:
                    UUID(arg)
                except Exception:
                    continue
                if cursor_created_at is not None:
                    cursor_id = arg

        if cursor_created_at is not None and cursor_id is not None:
            rows = [
                row
                for row in rows
                if (row["created_at"], row["id"]) < (cursor_created_at, cursor_id)
            ]

        rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
        return rows[:limit]


@pytest.mark.asyncio
async def test_admin_referral_stats_non_admin_forbidden_before_db_calls(client, monkeypatch):
    conn = NoQueryConn()
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_USER_ID)
    app.dependency_overrides[get_current_user] = lambda: _auth_user(NON_ADMIN_USER_ID)
    app.dependency_overrides[get_db] = lambda: conn

    try:
        response = await client.get("/v1/admin/referral/stats")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert conn.query_attempted is False


@pytest.mark.asyncio
async def test_admin_referral_stats_shape_and_optional_totals(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_USER_ID)
    app.dependency_overrides[get_current_user] = lambda: _auth_user(ADMIN_USER_ID)
    app.dependency_overrides[get_db] = lambda: AdminReferralConn()

    try:
        without_totals = await client.get("/v1/admin/referral/stats")
        with_totals = await client.get("/v1/admin/referral/stats?includeTotalsAllTime=true")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)

    assert without_totals.status_code == 200
    body_without_totals = without_totals.json()
    assert set(body_without_totals.keys()) == {
        "todayCodesIssued",
        "todayRedeems",
        "todayUniqueRedeemers",
        "todayCreditsGranted",
    }

    assert with_totals.status_code == 200
    body_with_totals = with_totals.json()
    assert set(body_with_totals.keys()) == {
        "todayCodesIssued",
        "todayRedeems",
        "todayUniqueRedeemers",
        "todayCreditsGranted",
        "totalsAllTime",
    }
    assert body_with_totals["todayCreditsGranted"] == 6
    assert body_with_totals["totalsAllTime"] == {
        "codesIssued": 27,
        "redeems": 11,
        "creditsGranted": 17,
    }


@pytest.mark.asyncio
async def test_admin_referral_redemptions_requires_admin(client, monkeypatch):
    conn = NoQueryConn()
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_USER_ID)
    app.dependency_overrides[get_current_user] = lambda: _auth_user(NON_ADMIN_USER_ID)
    app.dependency_overrides[get_db] = lambda: conn

    try:
        response = await client.get("/v1/admin/referral/redemptions")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert conn.query_attempted is False


@pytest.mark.asyncio
async def test_admin_referral_redemptions_keyset_pagination_is_stable(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", ADMIN_USER_ID)
    app.dependency_overrides[get_current_user] = lambda: _auth_user(ADMIN_USER_ID)
    app.dependency_overrides[get_db] = lambda: AdminReferralConn()

    try:
        page1 = await client.get("/v1/admin/referral/redemptions?limit=1")
        assert page1.status_code == 200
        body1 = page1.json()
        assert len(body1["items"]) == 1
        assert isinstance(body1["nextCursor"], str)

        page2 = await client.get(f"/v1/admin/referral/redemptions?limit=1&cursor={body1['nextCursor']}")
        assert page2.status_code == 200
        body2 = page2.json()
        assert len(body2["items"]) == 1
        assert body1["items"][0]["id"] != body2["items"][0]["id"]
        assert body1["items"][0]["creditsGranted"] == 1
        assert body2["items"][0]["creditsGranted"] == 2
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
