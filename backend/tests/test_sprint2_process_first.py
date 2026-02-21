import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from app import payments
from app.db import get_db
from app.deps import get_current_user
from app.main import app


PROCESS_USER = {
    "id": "00000000-0000-0000-0000-000000000777",
    "telegram_id": 777001,
    "username": "process-first-user",
    "is_onboarded": True,
    "subscription_status": "free",
    "subscription_active_until": None,
    "profile": "{}",
}

VALID_AI_JSON = {
    "recognized": True,
    "overall_confidence": 0.73,
    "totals": {
        "calories_kcal": 540,
        "protein_g": 28,
        "fat_g": 19,
        "carbs_g": 60,
    },
    "items": [
        {
            "name": "plov",
            "grams": 300,
            "calories_kcal": 540,
            "protein_g": 28,
            "fat_g": 19,
            "carbs_g": 60,
            "confidence": 0.62,
        }
    ],
    "warnings": ["portion estimate is approximate"],
    "assumptions": ["plate size about 24 cm"],
}


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeProcessFirstConn:
    def __init__(self):
        self.usage_daily = {}
        self.analyze_requests = {}
        self.rate_limits = []
        self.events = []

    def transaction(self):
        return _Tx()

    async def execute(self, query, *args):
        if "INSERT INTO usage_daily" in query:
            user_id, day = str(args[0]), args[1]
            self.usage_daily.setdefault((user_id, day), 0)
            return "INSERT 0 1"

        if "UPDATE usage_daily SET photos_used = photos_used + 1" in query:
            user_id, day = str(args[0]), args[1]
            self.usage_daily[(user_id, day)] = self.usage_daily.get((user_id, day), 0) + 1
            return "UPDATE 1"

        if "UPDATE usage_daily SET photos_used = GREATEST(0, photos_used - 1)" in query:
            user_id, day = str(args[0]), args[1]
            current = self.usage_daily.get((user_id, day), 0)
            self.usage_daily[(user_id, day)] = max(0, current - 1)
            return "UPDATE 1"

        if "UPDATE analyze_requests" in query and "SET status = 'failed'" in query:
            req_id = str(args[0])
            for req in self.analyze_requests.values():
                if req.get("id") == req_id and req["status"] == "processing":
                    req["status"] = "failed"
                    break
            return "UPDATE 1"

        if "INSERT INTO rate_limits" in query:
            user_id = str(args[0])
            marker = args[1] if len(args) > 1 else "analyze"
            created_at = args[2] if len(args) > 2 else datetime.now(timezone.utc)
            self.rate_limits.append(
                {
                    "user_id": user_id,
                    "marker": str(marker),
                    "created_at": created_at,
                }
            )
            return "INSERT 0 1"

        if "INSERT INTO events" in query:
            user_id, event_type, payload = args
            parsed_payload = payload
            if isinstance(parsed_payload, str):
                parsed_payload = json.loads(parsed_payload)
            self.events.append(
                {
                    "user_id": str(user_id) if user_id is not None else None,
                    "event_type": event_type,
                    "payload": parsed_payload if isinstance(parsed_payload, dict) else {},
                    "created_at": datetime.now(timezone.utc),
                }
            )
            return "INSERT 0 1"

        return "OK"

    async def fetchrow(self, query, *args):
        if "INSERT INTO analyze_requests" in query and "RETURNING id" in query:
            user_id, idem_key = str(args[0]), args[1]
            key = (user_id, idem_key)
            if key in self.analyze_requests:
                raise asyncpg.UniqueViolationError("duplicate idempotency key")
            req_id = str(uuid4())
            self.analyze_requests[key] = {
                "id": req_id,
                "status": "processing",
                "response_json": None,
            }
            return {"id": req_id}

        if "SELECT id, status, response_json FROM analyze_requests" in query:
            user_id, idem_key = str(args[0]), args[1]
            return self.analyze_requests.get((user_id, idem_key))

        if "SELECT photos_used FROM usage_daily" in query:
            user_id, day = str(args[0]), args[1]
            return {"photos_used": self.usage_daily.get((user_id, day), 0)}

        if "INSERT INTO meals" in query and "RETURNING id" in query:
            created_at = args[2] if len(args) > 2 else datetime.now(timezone.utc)
            return {"id": str(uuid4()), "created_at": created_at}

        if "UPDATE analyze_requests" in query and "SET status = 'completed'" in query and "RETURNING id" in query:
            response_json, req_id = args
            for req in self.analyze_requests.values():
                if req["id"] == str(req_id) and req["status"] == "processing":
                    req["status"] = "completed"
                    req["response_json"] = response_json
                    return {"id": req_id}
            return None

        if "FROM rate_limits" in query and "COUNT" in query:
            user_id = str(args[0]) if args else PROCESS_USER["id"]
            hits = len([row for row in self.rate_limits if row["user_id"] == user_id])
            return {"count": hits, "hits": hits, "requests_count": hits}

        if "FROM events" in query and "COUNT" in query:
            user_id = str(args[0]) if args else PROCESS_USER["id"]
            hits = len(
                [
                    row
                    for row in self.events
                    if row["user_id"] == user_id and row["event_type"] == "analyze_started"
                ]
            )
            return {"events_count": hits}

        return None

    def photos_used_today(self, user_id):
        today = datetime.now(timezone.utc).date()
        return self.usage_daily.get((str(user_id), today), 0)


@pytest.fixture
def fake_process_conn():
    return FakeProcessFirstConn()


@pytest.fixture
def valid_image_upload():
    return {"file": ("meal.jpg", b"fake-image-content", "image/jpeg")}


@pytest.fixture
def process_overrides(fake_process_conn):
    app.dependency_overrides[get_current_user] = lambda: PROCESS_USER
    app.dependency_overrides[get_db] = lambda: fake_process_conn
    try:
        yield fake_process_conn
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def _make_user(subscription_status, active_until):
    return {
        **PROCESS_USER,
        "subscription_status": subscription_status,
        "subscription_active_until": active_until,
    }


@pytest.fixture
def freeze_payments_now(monkeypatch):
    fixed_now = datetime(2026, 2, 20, 12, 0, 0, tzinfo=timezone.utc)
    real_datetime = datetime

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(payments, "datetime", FrozenDateTime)
    return fixed_now


@pytest.mark.asyncio
async def test_subscription_status_active_user(client, fake_process_conn, freeze_payments_now):
    user = _make_user("active", freeze_payments_now + timedelta(days=10))
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: fake_process_conn
    try:
        response = await client.get("/v1/subscription/status")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "active"
        assert body["daysLeft"] == 10
        assert body["willExpireSoon"] is False
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_subscription_status_expiring_soon(client, fake_process_conn, freeze_payments_now):
    user = _make_user("active", freeze_payments_now + timedelta(days=2))
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: fake_process_conn
    try:
        response = await client.get("/v1/subscription/status")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "active"
        assert body["daysLeft"] == 2
        assert body["willExpireSoon"] is True
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_subscription_status_expired_returns_free(client, fake_process_conn, freeze_payments_now):
    user = _make_user("active", freeze_payments_now - timedelta(seconds=1))
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: fake_process_conn
    try:
        response = await client.get("/v1/subscription/status")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "free"
        assert body["daysLeft"] == 0
        assert body["willExpireSoon"] is False
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_subscription_status_blocked_override(client, fake_process_conn, freeze_payments_now):
    user = _make_user("blocked", freeze_payments_now + timedelta(days=365))
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: fake_process_conn
    try:
        response = await client.get("/v1/subscription/status")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "blocked"
        assert body["daysLeft"] == 0
        assert body["willExpireSoon"] is False
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_anti_abuse_under_limit_path_unchanged(
    client,
    process_overrides,
    valid_image_upload,
    monkeypatch,
):
    fake_conn = process_overrides
    call_count = {"n": 0}

    async def fake_analyze_image(*args, **kwargs):
        call_count["n"] += 1
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "pr2-under-limit-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body.get("meal"), dict)
    assert isinstance(body.get("usage"), dict)
    assert body["meal"]["result"]["recognized"] == VALID_AI_JSON["recognized"]
    assert body["meal"]["result"]["totals"]["calories_kcal"] == sum(
        int(item["calories_kcal"]) for item in body["meal"]["result"]["items"]
    )
    assert body["usage"]["photosUsed"] == 1
    assert call_count["n"] == 1
    assert fake_conn.photos_used_today(PROCESS_USER["id"]) == 1


@pytest.mark.asyncio
async def test_anti_abuse_over_limit_returns_rate_limited_and_skips_idempotency_insert(
    client,
    process_overrides,
    valid_image_upload,
    monkeypatch,
):
    fake_conn = process_overrides
    monkeypatch.setattr("app.main.settings.MEALS_ANALYZE_RATE_LIMIT_PER_MINUTE", 3)

    for idx in range(3):
        await fake_conn.execute(
            "INSERT INTO events (user_id, event_type, payload) VALUES ($1, $2, $3)",
            PROCESS_USER["id"],
            "analyze_started",
            json.dumps({"seed": idx}),
        )

    async def fake_analyze_image(*args, **kwargs):
        raise AssertionError("AI must not be called on cheap anti-abuse reject")

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "pr2-over-limit-1"},
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert (PROCESS_USER["id"], "pr2-over-limit-1") not in fake_conn.analyze_requests
    assert fake_conn.photos_used_today(PROCESS_USER["id"]) == 0


@pytest.mark.asyncio
async def test_anti_abuse_idempotency_replay_semantics_remain_correct(
    client,
    process_overrides,
    valid_image_upload,
    monkeypatch,
):
    fake_conn = process_overrides
    call_count = {"n": 0}

    async def fake_analyze_image(*args, **kwargs):
        call_count["n"] += 1
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    headers = {"Idempotency-Key": "pr2-replay-1"}
    first = await client.post("/v1/meals/analyze", files=valid_image_upload, headers=headers)
    second = await client.post("/v1/meals/analyze", files=valid_image_upload, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert call_count["n"] == 1
    assert fake_conn.photos_used_today(PROCESS_USER["id"]) == 1
