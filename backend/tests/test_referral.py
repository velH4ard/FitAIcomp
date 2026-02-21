from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app.db import get_db
from app.deps import get_current_user
from app.main import app


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeReferralConn:
    def __init__(self):
        self.referral_codes_by_user = {}
        self.referral_codes_by_code = {}
        self.referral_redemptions = {}
        self.users = {}
        self.events = []

    def transaction(self):
        return _Tx()

    def _ensure_user(self, user_id: str):
        self.users.setdefault(str(user_id), {"referral_credits": 0})

    async def fetchval(self, query, *args):
        if "SELECT code FROM referral_codes WHERE user_id = $1" in query:
            return self.referral_codes_by_user.get(str(args[0]))

        if "INSERT INTO referral_codes" in query and "RETURNING code" in query:
            user_id = str(args[0])
            code = str(args[1])
            if user_id in self.referral_codes_by_user:
                raise asyncpg.UniqueViolationError("duplicate user referral code")
            if code in self.referral_codes_by_code:
                raise asyncpg.UniqueViolationError("duplicate referral code")

            self.referral_codes_by_user[user_id] = code
            self.referral_codes_by_code[code] = user_id
            return code

        return None

    async def fetchrow(self, query, *args):
        if "FROM events" in query and "event_type = 'referral_redeem_attempt'" in query:
            user_id = str(args[0])
            border = datetime.now(timezone.utc) - timedelta(minutes=1)
            attempts = len(
                [
                    event
                    for event in self.events
                    if event["user_id"] == user_id
                    and event["event_type"] == "referral_redeem_attempt"
                    and event["created_at"] >= border
                ]
            )
            return {"attempts": attempts}

        if "SELECT user_id FROM referral_codes WHERE code = $1" in query:
            code = str(args[0])
            user_id = self.referral_codes_by_code.get(code)
            if not user_id:
                return None
            return {"user_id": user_id}

        if "SELECT photos_used FROM usage_daily" in query:
            return None

        return None

    async def execute(self, query, *args):
        if "INSERT INTO referral_redemptions" in query:
            redeemer_user_id = str(args[0])
            referrer_user_id = str(args[1])
            code = str(args[2])
            credits_granted = int(args[3])
            if redeemer_user_id in self.referral_redemptions:
                raise asyncpg.UniqueViolationError("duplicate redemption")
            self.referral_redemptions[redeemer_user_id] = {
                "referrer_user_id": referrer_user_id,
                "code": code,
                "credits_granted": credits_granted,
            }
            return "INSERT 0 1"

        if "UPDATE users" in query and "SET referral_credits = referral_credits + $1" in query:
            bonus = int(args[0])
            user_ids = [str(value) for value in args[1]]
            for user_id in user_ids:
                self._ensure_user(user_id)
                self.users[user_id]["referral_credits"] += bonus
            return "UPDATE 2"

        if "INSERT INTO events" in query:
            user_id, event_type, payload = args
            self.events.append(
                {
                    "user_id": str(user_id),
                    "event_type": str(event_type),
                    "payload": payload,
                    "created_at": datetime.now(timezone.utc),
                }
            )
            return "INSERT 0 1"

        return "OK"


@pytest.fixture
def referral_conn():
    conn = FakeReferralConn()
    conn.users["00000000-0000-0000-0000-000000000901"] = {"referral_credits": 0}
    conn.users["00000000-0000-0000-0000-000000000902"] = {"referral_credits": 0}
    return conn


@pytest.fixture
def referral_overrides(referral_conn):
    current_user = {
        "id": "00000000-0000-0000-0000-000000000901",
        "telegram_id": 901,
        "username": "redeemer",
        "is_onboarded": True,
        "subscription_status": "free",
        "subscription_active_until": None,
        "referral_credits": 0,
        "profile": {},
    }

    app.dependency_overrides[get_current_user] = lambda: current_user
    app.dependency_overrides[get_db] = lambda: referral_conn
    try:
        yield current_user, referral_conn
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_get_referral_code_creates_and_returns_stable_code(client, referral_overrides):
    _, conn = referral_overrides

    first = await client.get("/v1/referral/code")
    second = await client.get("/v1/referral/code")

    assert first.status_code == 200
    assert second.status_code == 200
    first_code = first.json()["code"]
    assert second.json()["code"] == first_code
    assert len(first_code) == 10
    assert first_code.isalnum()
    assert first_code == first_code.upper()
    assert len(conn.referral_codes_by_user) == 1


@pytest.mark.asyncio
async def test_referral_redeem_success_updates_both_credits(client, referral_overrides):
    current_user, conn = referral_overrides
    referrer_user_id = "00000000-0000-0000-0000-000000000902"
    conn.referral_codes_by_user[referrer_user_id] = "REFERRAL1"
    conn.referral_codes_by_code["REFERRAL1"] = referrer_user_id

    response = await client.post("/v1/referral/redeem", json={"code": "REFERRAL1"})

    assert response.status_code == 200
    assert response.json() == {"redeemed": True}
    assert current_user["id"] in conn.referral_redemptions
    assert conn.referral_redemptions[current_user["id"]]["credits_granted"] == 1
    assert conn.users[current_user["id"]]["referral_credits"] == 1
    assert conn.users[referrer_user_id]["referral_credits"] == 1


@pytest.mark.asyncio
async def test_referral_redeem_rejects_self_code(client, referral_overrides):
    current_user, conn = referral_overrides
    conn.referral_codes_by_user[current_user["id"]] = "SELFCODE1"
    conn.referral_codes_by_code["SELFCODE1"] = current_user["id"]

    response = await client.post("/v1/referral/redeem", json={"code": "SELFCODE1"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REFERRAL_SELF_REDEEM"
    assert current_user["id"] not in conn.referral_redemptions


@pytest.mark.asyncio
async def test_referral_redeem_rejects_second_redeem_attempt(client, referral_overrides):
    current_user, conn = referral_overrides
    referrer_user_id = "00000000-0000-0000-0000-000000000902"
    conn.referral_codes_by_user[referrer_user_id] = "REFERRAL2"
    conn.referral_codes_by_code["REFERRAL2"] = referrer_user_id

    first = await client.post("/v1/referral/redeem", json={"code": "REFERRAL2"})
    second = await client.post("/v1/referral/redeem", json={"code": "REFERRAL2"})

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "REFERRAL_ALREADY_REDEEMED"


@pytest.mark.asyncio
async def test_referral_redeem_rejects_unknown_code(client, referral_overrides):
    response = await client.post("/v1/referral/redeem", json={"code": "UNKNOWN1"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REFERRAL_CODE"


@pytest.mark.asyncio
async def test_referral_redeem_rate_limited_has_no_business_side_effects(client, referral_overrides):
    current_user, conn = referral_overrides
    now = datetime.now(timezone.utc)
    for _ in range(5):
        conn.events.append(
            {
                "user_id": current_user["id"],
                "event_type": "referral_redeem_attempt",
                "payload": {},
                "created_at": now,
            }
        )

    referrer_user_id = "00000000-0000-0000-0000-000000000902"
    conn.referral_codes_by_user[referrer_user_id] = "REFERRAL3"
    conn.referral_codes_by_code["REFERRAL3"] = referrer_user_id

    response = await client.post("/v1/referral/redeem", json={"code": "REFERRAL3"})

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert current_user["id"] not in conn.referral_redemptions
    assert conn.users[current_user["id"]]["referral_credits"] == 0
    assert conn.users[referrer_user_id]["referral_credits"] == 0
