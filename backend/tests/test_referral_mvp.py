import pytest
import asyncpg

from app.db import get_db
from app.deps import get_current_user
from app.main import app


OWNER_USER_ID = "00000000-0000-0000-0000-0000000000a1"
REDEEMER_USER_ID = "00000000-0000-0000-0000-0000000000b2"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class InMemoryReferralConn:
    def __init__(self):
        self.codes_by_user = {}
        self.codes_by_code = {}
        self.redemptions = {}
        self.users = {
            OWNER_USER_ID: {"referral_credits": 0},
            REDEEMER_USER_ID: {"referral_credits": 0},
        }
        self.events = []

    def transaction(self):
        return _Tx()

    async def fetchrow(self, query, *args):
        if "SELECT code FROM referral_codes WHERE user_id = $1" in query:
            user_id = str(args[0])
            code = self.codes_by_user.get(user_id)
            return {"code": code} if code else None

        if "INSERT INTO referral_codes" in query and "RETURNING code" in query:
            user_id = str(args[0])
            code = str(args[1])
            if user_id in self.codes_by_user or code in self.codes_by_code:
                raise asyncpg.UniqueViolationError("unique")
            self.codes_by_user[user_id] = code
            self.codes_by_code[code] = user_id
            return {"code": code}

        if "SELECT COUNT(*)::int AS attempts" in query:
            user_id = str(args[0])
            attempts = len(
                [
                    event
                    for event in self.events
                    if event["user_id"] == user_id
                    and event["event_type"] == "referral_redeem_attempt"
                ]
            )
            return {"attempts": attempts}

        if "SELECT user_id FROM referral_codes WHERE code = $1" in query:
            code = str(args[0])
            user_id = self.codes_by_code.get(code)
            return {"user_id": user_id} if user_id else None

        if "SELECT photos_used FROM usage_daily" in query:
            return None

        return None

    async def execute(self, query, *args):
        if "INSERT INTO referral_redemptions" in query:
            redeemer_user_id = str(args[0])
            referrer_user_id = str(args[1])
            code = str(args[2])
            credits_granted = int(args[3])
            if redeemer_user_id in self.redemptions:
                raise asyncpg.UniqueViolationError("unique")
            self.redemptions[redeemer_user_id] = {
                "referrer_user_id": referrer_user_id,
                "code": code,
                "credits_granted": credits_granted,
            }
            return "INSERT 0 1"

        if "UPDATE users" in query and "referral_credits = referral_credits + $1" in query:
            credits_delta = int(args[0])
            user_ids = [str(value) for value in args[1]]
            for user_id in user_ids:
                self.users.setdefault(user_id, {"referral_credits": 0})
                self.users[user_id]["referral_credits"] += credits_delta
            return "UPDATE 2"

        if "INSERT INTO events" in query:
            user_id, event_type, payload = args
            self.events.append({"user_id": str(user_id), "event_type": str(event_type), "payload": payload})
            return "INSERT 0 1"

        return "OK"


def assert_fitai_error(response, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == code


@pytest.mark.asyncio
async def test_referral_routes_are_mounted_under_v1(client):
    code_response = await client.get("/v1/referral/code")
    redeem_response = await client.post("/v1/referral/redeem", json={"code": "ABC123"})

    assert_fitai_error(code_response, 401, "UNAUTHORIZED")
    assert_fitai_error(redeem_response, 401, "UNAUTHORIZED")


@pytest.fixture
def auth_context():
    state = {
        "user": {
            "id": OWNER_USER_ID,
            "telegram_id": 101001,
            "username": "owner",
            "is_onboarded": True,
            "subscription_status": "free",
            "subscription_active_until": None,
            "referral_credits": 0,
            "profile": {},
        }
    }
    conn = InMemoryReferralConn()

    def _override_current_user():
        return state["user"]

    app.dependency_overrides[get_current_user] = _override_current_user
    app.dependency_overrides[get_db] = lambda: conn
    try:
        yield state, conn
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_referral_code_is_stable_across_repeated_get_calls(client, auth_context):
    first = await client.get("/v1/referral/code")
    second = await client.get("/v1/referral/code")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["code"] == second.json()["code"]


@pytest.mark.asyncio
async def test_referral_redeem_success_for_valid_code(client, auth_context):
    state, conn = auth_context

    state["user"] = {
        "id": OWNER_USER_ID,
        "telegram_id": 101001,
        "username": "owner",
        "is_onboarded": True,
        "subscription_status": "free",
        "subscription_active_until": None,
        "referral_credits": 0,
        "profile": {},
    }
    valid_code = (await client.get("/v1/referral/code")).json()["code"]

    state["user"] = {
        "id": REDEEMER_USER_ID,
        "telegram_id": 202002,
        "username": "redeemer",
        "is_onboarded": True,
        "subscription_status": "free",
        "subscription_active_until": None,
        "referral_credits": conn.users[REDEEMER_USER_ID]["referral_credits"],
        "profile": {},
    }

    redeem_response = await client.post("/v1/referral/redeem", json={"code": valid_code})
    assert redeem_response.status_code == 200
    assert redeem_response.json() == {"redeemed": True}
    assert conn.redemptions[REDEEMER_USER_ID]["credits_granted"] == 1


@pytest.mark.asyncio
async def test_referral_redeem_twice_returns_already_redeemed(client, auth_context):
    state, _ = auth_context

    state["user"]["id"] = OWNER_USER_ID
    valid_code = (await client.get("/v1/referral/code")).json()["code"]

    state["user"]["id"] = REDEEMER_USER_ID
    first_redeem = await client.post("/v1/referral/redeem", json={"code": valid_code})
    assert first_redeem.status_code == 200

    second_redeem = await client.post("/v1/referral/redeem", json={"code": valid_code})
    assert_fitai_error(second_redeem, 409, "REFERRAL_ALREADY_REDEEMED")


@pytest.mark.asyncio
async def test_referral_self_redeem_returns_conflict(client, auth_context):
    state, _ = auth_context

    state["user"]["id"] = OWNER_USER_ID
    own_code = (await client.get("/v1/referral/code")).json()["code"]

    self_redeem = await client.post("/v1/referral/redeem", json={"code": own_code})
    assert_fitai_error(self_redeem, 409, "REFERRAL_SELF_REDEEM")


@pytest.mark.asyncio
async def test_usage_daily_limit_includes_referral_credits_after_redeem(client, auth_context):
    state, conn = auth_context

    state["user"]["id"] = OWNER_USER_ID
    owner_code = (await client.get("/v1/referral/code")).json()["code"]

    state["user"] = {
        "id": REDEEMER_USER_ID,
        "telegram_id": 202002,
        "username": "redeemer",
        "is_onboarded": True,
        "subscription_status": "free",
        "subscription_active_until": None,
        "referral_credits": conn.users[REDEEMER_USER_ID]["referral_credits"],
        "profile": {},
    }
    redeem_response = await client.post("/v1/referral/redeem", json={"code": owner_code})
    assert redeem_response.status_code == 200

    state["user"]["referral_credits"] = conn.users[REDEEMER_USER_ID]["referral_credits"]
    usage_response = await client.get("/v1/usage/today")
    usage = usage_response.json()

    assert usage_response.status_code == 200
    assert usage["subscriptionStatus"] == "free"
    assert usage["dailyLimit"] > 2
