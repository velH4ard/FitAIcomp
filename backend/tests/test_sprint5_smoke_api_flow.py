import base64
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.config import settings
from app.db import get_db
from app.errors import FitAIError
from app.main import app
from app.payments import _webhook_dedupe_memory


VALID_AI_RESULT = {
    "recognized": True,
    "overall_confidence": 0.76,
    "totals": {
        "calories_kcal": 520,
        "protein_g": 26,
        "fat_g": 18,
        "carbs_g": 58,
    },
    "items": [
        {
            "name": "rice bowl",
            "grams": 300,
            "calories_kcal": 520,
            "protein_g": 26,
            "fat_g": 18,
            "carbs_g": 58,
            "confidence": 0.7,
        }
    ],
    "warnings": ["portion may be approximate"],
    "assumptions": ["standard medium bowl"],
}


def _auth_header(shop_id: str, secret: str) -> dict[str, str]:
    token = base64.b64encode(f"{shop_id}:{secret}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _assert_error(response, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    body = response.json()
    assert set(body.keys()) == {"error"}
    assert body["error"]["code"] == code
    assert "message" in body["error"]
    assert "details" in body["error"]


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


class SmokeConn:
    def __init__(self):
        self.users_by_id: dict[str, dict] = {}
        self.user_id_by_telegram: dict[int, str] = {}
        self.usage_daily: dict[tuple[str, object], int] = {}
        self.analyze_requests: dict[tuple[str, str], dict] = {}
        self.meals: list[dict] = []
        self.daily_stats: dict[tuple[str, date], dict] = {}
        self.events: list[dict] = []
        self.payment_webhook_events: set[str] = set()

    def transaction(self):
        return _Tx()

    async def execute(self, query, *args):
        if "INSERT INTO events" in query:
            self.events.append(
                {
                    "user_id": str(args[0]),
                    "event_type": str(args[1]),
                    "payload": json.loads(args[2]) if isinstance(args[2], str) else args[2],
                }
            )
            return "INSERT 0 1"

        if "INSERT INTO usage_daily" in query:
            user_id = str(args[0])
            day = args[1]
            self.usage_daily.setdefault((user_id, day), 0)
            return "INSERT 0 1"

        if "UPDATE usage_daily SET photos_used = photos_used + 1" in query:
            user_id = str(args[0])
            day = args[1]
            self.usage_daily[(user_id, day)] = self.usage_daily.get((user_id, day), 0) + 1
            return "UPDATE 1"

        if "UPDATE usage_daily SET photos_used = GREATEST(0, photos_used - 1)" in query:
            user_id = str(args[0])
            day = args[1]
            self.usage_daily[(user_id, day)] = max(0, self.usage_daily.get((user_id, day), 0) - 1)
            return "UPDATE 1"

        if "INSERT INTO daily_stats" in query:
            return "INSERT 0 1"

        if "UPDATE analyze_requests" in query and "SET status = 'failed'" in query:
            req_id = str(args[0])
            for request in self.analyze_requests.values():
                if request["id"] == req_id and request["status"] == "processing":
                    request["status"] = "failed"
            return "UPDATE 1"

        if "INSERT INTO payment_webhook_events" in query:
            dedupe_key = str(args[0])
            if dedupe_key in self.payment_webhook_events:
                raise asyncpg.UniqueViolationError("duplicate dedupe key")
            self.payment_webhook_events.add(dedupe_key)
            return "INSERT 0 1"

        if "UPDATE payment_webhook_events" in query and "status = 'completed'" in query:
            return "UPDATE 1"

        if "DELETE FROM payment_webhook_events" in query:
            self.payment_webhook_events.discard(str(args[0]))
            return "DELETE 1"

        return "OK"

    async def fetchrow(self, query, *args):
        if "INSERT INTO users" in query and "telegram_id" in query:
            telegram_id = int(args[0])
            username = args[1]
            existing_user_id = self.user_id_by_telegram.get(telegram_id)
            if existing_user_id:
                user = self.users_by_id[existing_user_id]
                user["username"] = username
                return user

            user_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"fitai-smoke-{telegram_id}"))
            row = {
                "id": user_id,
                "telegram_id": telegram_id,
                "username": username,
                "is_onboarded": False,
                "subscription_status": "free",
                "subscription_active_until": None,
                "referral_credits": 0,
                "profile": {},
                "daily_goal_auto": 2000,
                "daily_goal_override": None,
            }
            self.user_id_by_telegram[telegram_id] = user_id
            self.users_by_id[user_id] = row
            return row

        if "FROM users" in query and "WHERE id = $1" in query:
            user_id = str(args[0])
            return self.users_by_id.get(user_id)

        if "UPDATE users" in query and "is_onboarded = TRUE" in query:
            profile_json = args[0]
            user_id = str(args[2])
            user = self.users_by_id.get(user_id)
            if user is None:
                return None
            user["is_onboarded"] = True
            user["profile"] = json.loads(profile_json) if isinstance(profile_json, str) else profile_json
            user["daily_goal_auto"] = int(args[1])
            return {
                "id": user_id,
                "is_onboarded": True,
                "profile": user["profile"],
            }

        if "UPDATE users" in query and "daily_goal_override = $1" in query:
            override_goal = int(args[0])
            user_id = str(args[1])
            user = self.users_by_id.get(user_id)
            if user is None:
                return None
            user["daily_goal_override"] = override_goal
            auto_goal = int(user.get("daily_goal_auto") or 2000)
            return {
                "daily_goal_auto": auto_goal,
                "daily_goal_override": override_goal,
            }

        if "SELECT photos_used FROM usage_daily" in query:
            user_id = str(args[0])
            day = args[1]
            return {"photos_used": self.usage_daily.get((user_id, day), 0)}

        if "SELECT COUNT(*)::int AS events_count" in query and "FROM events" in query:
            user_id = str(args[0])
            count = sum(
                1
                for event in self.events
                if event["user_id"] == user_id and event["event_type"] == "analyze_started"
            )
            return {"events_count": count}

        if "INSERT INTO analyze_requests" in query and "RETURNING id" in query:
            user_id = str(args[0])
            idem_key = str(args[1])
            key = (user_id, idem_key)
            if key in self.analyze_requests:
                raise asyncpg.UniqueViolationError("duplicate idempotency key")
            self.analyze_requests[key] = {
                "id": str(uuid.uuid4()),
                "status": "processing",
                "response_json": None,
            }
            return {"id": self.analyze_requests[key]["id"]}

        if "SELECT id, status, response_json FROM analyze_requests" in query:
            user_id = str(args[0])
            idem_key = str(args[1])
            return self.analyze_requests.get((user_id, idem_key))

        if "UPDATE analyze_requests" in query and "SET status = 'completed'" in query and "RETURNING id" in query:
            response_json = args[0]
            req_id = str(args[1])
            for request in self.analyze_requests.values():
                if request["id"] == req_id and request["status"] == "processing":
                    request["status"] = "completed"
                    request["response_json"] = response_json
                    return {"id": req_id}
            return None

        if "INSERT INTO meals" in query and "RETURNING id" in query:
            meal_id = str(args[0])
            result_json = json.loads(args[7]) if isinstance(args[7], str) else args[7]
            self.meals.append(
                {
                    "id": meal_id,
                    "user_id": str(args[1]),
                    "created_at": args[2],
                    "meal_time": "unknown",
                    "description": args[3],
                    "image_url": None,
                    "image_path": args[4],
                    "ai_provider": "openrouter",
                    "ai_model": str(args[5]),
                    "ai_confidence": float(args[6]),
                    "result_json": result_json,
                    "idempotency_key": args[8],
                    "analyze_request_id": str(args[9]),
                }
            )
            return {"id": meal_id, "created_at": args[2]}

        if "FROM meals" in query and "WHERE id = $1 AND user_id = $2" in query:
            meal_id = str(args[0])
            user_id = str(args[1])
            for meal in self.meals:
                if meal["id"] == meal_id and meal["user_id"] == user_id:
                    return {
                        "id": meal["id"],
                        "created_at": meal["created_at"],
                        "meal_time": meal["meal_time"],
                        "image_url": meal["image_url"] or meal["image_path"],
                        "ai_provider": meal["ai_provider"],
                        "ai_model": meal["ai_model"],
                        "ai_confidence": meal["ai_confidence"],
                        "result_json": meal["result_json"],
                    }
            return None

        if "UPDATE users" in query and "subscription_status = 'active'" in query and "RETURNING subscription_active_until" in query:
            user_id = str(args[0])
            duration_days = int(args[1])
            user = self.users_by_id.get(user_id)
            if user is None:
                return None

            current_until = user.get("subscription_active_until")
            now_utc = datetime.now(timezone.utc)
            if isinstance(current_until, datetime) and current_until > now_utc:
                base = current_until
            else:
                base = now_utc

            new_until = base + timedelta(days=duration_days)
            user["subscription_status"] = "active"
            user["subscription_active_until"] = new_until
            return {"subscription_active_until": new_until}

        return None

    async def fetch(self, query, *args):
        if "FROM daily_stats" in query and "ORDER BY date ASC" in query:
            user_id = str(args[0])
            rows = []
            for (uid, day), stats in sorted(self.daily_stats.items(), key=lambda item: item[0][1]):
                if uid != user_id:
                    continue
                rows.append(
                    {
                        "date": day,
                        "calories_kcal": stats["calories_kcal"],
                    }
                )
            return rows

        if "FROM meals" not in query or "ORDER BY created_at DESC, id DESC" not in query:
            return []

        user_id = str(args[0])
        limit = int(args[-1])
        meals = [meal for meal in self.meals if meal["user_id"] == user_id]
        meals.sort(key=lambda m: (m["created_at"], m["id"]), reverse=True)

        result = []
        for meal in meals[:limit]:
            totals = meal["result_json"]["totals"]
            result.append(
                {
                    "id": meal["id"],
                    "created_at": meal["created_at"],
                    "meal_time": meal["meal_time"],
                    "image_url": meal["image_url"] or meal["image_path"],
                    "calories_kcal": totals["calories_kcal"],
                    "protein_g": totals["protein_g"],
                    "fat_g": totals["fat_g"],
                    "carbs_g": totals["carbs_g"],
                }
            )
        return result


@pytest.fixture(autouse=True)
def clear_webhook_dedupe_memory():
    _webhook_dedupe_memory.clear()
    yield
    _webhook_dedupe_memory.clear()


@pytest.fixture
def smoke_conn():
    return SmokeConn()


@pytest.fixture(autouse=True)
def override_db(smoke_conn):
    async def _override_get_db():
        yield smoke_conn

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def mock_ai(monkeypatch):
    async def _fake_analyze_image(*args, **kwargs):
        return json.dumps(VALID_AI_RESULT)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", _fake_analyze_image)


async def _auth_user(client, monkeypatch, telegram_id: int, username: str = "smoke-user"):
    fake_verify = lambda _: {"id": telegram_id, "username": username}
    monkeypatch.setattr("app.main.verify_telegram_init_data", fake_verify)

    # Keep this smoke helper stable even if another test reloaded app.main
    # and the ASGI app object still points to an older endpoint globals dict.
    for route in app.router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint and getattr(route, "path", "") == "/v1/auth/telegram":
            endpoint.__globals__["verify_telegram_init_data"] = fake_verify
            break

    response = await client.post("/v1/auth/telegram", json={"initData": "smoke-init"})
    assert response.status_code == 200
    body = response.json()
    return body["accessToken"], body["user"]["id"]


async def _onboard_user(client, token: str):
    response = await client.put(
        "/v1/me/profile",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "gender": "male",
            "age": 30,
            "heightCm": 178,
            "weightKg": 82,
            "goal": "maintain",
        },
    )
    assert response.status_code == 200
    assert response.json()["isOnboarded"] is True


@pytest.mark.asyncio
async def test_smoke_auth_and_me_flow(client, monkeypatch):
    token, _ = await _auth_user(client, monkeypatch, telegram_id=5001)

    me_response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me_response.status_code == 200
    me = me_response.json()
    assert {"id", "telegramId", "isOnboarded", "subscription"}.issubset(me.keys())

    unauthorized = await client.get("/v1/me")
    _assert_error(unauthorized, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_smoke_onboarding_gate_then_analyze_allowed(client, monkeypatch, mock_ai):
    token, _ = await _auth_user(client, monkeypatch, telegram_id=5002)

    blocked = await client.post(
        "/v1/meals/analyze",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("meal.jpg", b"img", "image/jpeg")},
    )
    _assert_error(blocked, 409, "ONBOARDING_REQUIRED")

    await _onboard_user(client, token)

    allowed = await client.post(
        "/v1/meals/analyze",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("meal.jpg", b"img", "image/jpeg")},
    )
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_smoke_analyze_happy_path_shape_and_usage_invariants(client, monkeypatch, mock_ai):
    token, _ = await _auth_user(client, monkeypatch, telegram_id=5003)
    await _onboard_user(client, token)

    response = await client.post(
        "/v1/meals/analyze",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("meal.jpg", b"img", "image/jpeg")},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"meal", "usage"}

    result = body["meal"]["result"]
    assert set(result.keys()) == {"recognized", "overall_confidence", "totals", "items", "warnings", "assumptions"}

    usage = body["usage"]
    assert usage["dailyLimit"] >= 0
    assert usage["photosUsed"] >= 0
    assert usage["remaining"] >= 0
    assert usage["photosUsed"] <= usage["dailyLimit"]
    assert usage["remaining"] == max(0, usage["dailyLimit"] - usage["photosUsed"])


@pytest.mark.asyncio
async def test_smoke_analyze_quota_exceeded_envelope(client, monkeypatch, mock_ai, smoke_conn):
    token, user_id = await _auth_user(client, monkeypatch, telegram_id=5004)
    await _onboard_user(client, token)

    today = datetime.now(timezone.utc).date()
    smoke_conn.usage_daily[(user_id, today)] = 2

    response = await client.post(
        "/v1/meals/analyze",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("meal.jpg", b"img", "image/jpeg")},
    )
    _assert_error(response, 429, "QUOTA_EXCEEDED")


@pytest.mark.asyncio
async def test_smoke_history_list_and_detail_ownership(client, monkeypatch, mock_ai):
    owner_token, _ = await _auth_user(client, monkeypatch, telegram_id=5005, username="owner")
    await _onboard_user(client, owner_token)

    analyze = await client.post(
        "/v1/meals/analyze",
        headers={"Authorization": f"Bearer {owner_token}"},
        files={"file": ("meal.jpg", b"img", "image/jpeg")},
    )
    assert analyze.status_code == 200
    owner_meal_id = analyze.json()["meal"]["id"]

    list_response = await client.get("/v1/meals", headers={"Authorization": f"Bearer {owner_token}"})
    assert list_response.status_code == 200
    list_body = list_response.json()
    assert set(list_body.keys()) == {"items", "nextCursor"}
    assert isinstance(list_body["items"], list)
    assert len(list_body["items"]) >= 1

    owned_detail = await client.get(f"/v1/meals/{owner_meal_id}", headers={"Authorization": f"Bearer {owner_token}"})
    assert owned_detail.status_code == 200
    assert owned_detail.json()["id"] == owner_meal_id

    stranger_token, _ = await _auth_user(client, monkeypatch, telegram_id=5006, username="stranger")
    await _onboard_user(client, stranger_token)

    not_owned = await client.get(f"/v1/meals/{owner_meal_id}", headers={"Authorization": f"Bearer {stranger_token}"})
    _assert_error(not_owned, 404, "NOT_FOUND")

    missing = await client.get(
        "/v1/meals/00000000-0000-0000-0000-00000000ffff",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    _assert_error(missing, 404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_smoke_payment_create_success_and_provider_failure(client, monkeypatch):
    token, _ = await _auth_user(client, monkeypatch, telegram_id=5007)

    async def _provider_ok(*args, **kwargs):
        return {
            "id": "pay_smoke_001",
            "confirmation": {"confirmation_url": "https://yookassa.test/pay_smoke_001"},
        }

    monkeypatch.setattr("app.payments._create_yookassa_payment", _provider_ok)
    success = await client.post(
        "/v1/subscription/yookassa/create",
        headers={"Authorization": f"Bearer {token}"},
        json={"returnUrl": "https://t.me/fitai_bot/app", "idempotencyKey": "smoke-create-1"},
    )
    assert success.status_code == 200
    success_body = success.json()
    assert success_body["paymentId"]
    assert success_body["confirmationUrl"].startswith("https://")

    async def _provider_fail(*args, **kwargs):
        raise FitAIError(
            code="PAYMENT_PROVIDER_ERROR",
            message="Ошибка платежного провайдера",
            status_code=502,
            details={"stage": "create_payment"},
        )

    monkeypatch.setattr("app.payments._create_yookassa_payment", _provider_fail)
    failure = await client.post(
        "/v1/subscription/yookassa/create",
        headers={"Authorization": f"Bearer {token}"},
        json={"returnUrl": "https://t.me/fitai_bot/app", "idempotencyKey": "smoke-create-2"},
    )
    _assert_error(failure, 502, "PAYMENT_PROVIDER_ERROR")


@pytest.mark.asyncio
async def test_smoke_webhook_idempotency_and_invalid_signature(client, monkeypatch):
    token, user_id = await _auth_user(client, monkeypatch, telegram_id=5008)

    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "smoke-shop")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "smoke-secret")

    payload = {
        "id": "evt-smoke-1",
        "event": "payment.succeeded",
        "object": {
            "id": "payment-smoke-1",
            "status": "succeeded",
            "paid": True,
            "metadata": {
                "user_id": user_id,
                "telegram_id": "5008",
                "plan": "monthly_499",
            },
        },
    }

    first = await client.post(
        "/v1/subscription/yookassa/webhook",
        json=payload,
        headers=_auth_header("smoke-shop", "smoke-secret"),
    )
    assert first.status_code == 200
    assert first.json() == {"ok": True}

    sub_first = await client.get("/v1/subscription", headers={"Authorization": f"Bearer {token}"})
    assert sub_first.status_code == 200
    first_until = sub_first.json()["activeUntil"]

    second = await client.post(
        "/v1/subscription/yookassa/webhook",
        json=payload,
        headers=_auth_header("smoke-shop", "smoke-secret"),
    )
    assert second.status_code == 200
    assert second.json() == {"ok": True}

    sub_second = await client.get("/v1/subscription", headers={"Authorization": f"Bearer {token}"})
    assert sub_second.status_code == 200
    assert sub_second.json()["activeUntil"] == first_until

    invalid = await client.post(
        "/v1/subscription/yookassa/webhook",
        json={
            "id": "evt-smoke-invalid",
            "event": "payment.succeeded",
            "object": {"id": "payment-smoke-invalid", "status": "succeeded", "paid": True, "metadata": {"user_id": user_id}},
        },
        headers=_auth_header("smoke-shop", "wrong-secret"),
    )
    _assert_error(invalid, 401, "PAYMENT_WEBHOOK_INVALID")


@pytest.mark.asyncio
async def test_smoke_goal_override_and_streak(client, monkeypatch, smoke_conn):
    token, user_id = await _auth_user(client, monkeypatch, telegram_id=5009)
    await _onboard_user(client, token)

    goal = await client.patch(
        "/v1/profile/goal",
        headers={"Authorization": f"Bearer {token}"},
        json={"dailyGoal": 2300},
    )
    assert goal.status_code == 200
    goal_body = goal.json()
    assert goal_body["override"] == 2300
    assert goal_body["dailyGoal"] == 2300

    today = datetime.now(timezone.utc).date()
    smoke_conn.daily_stats[(user_id, today)] = {
        "calories_kcal": 1800.0,
        "protein_g": 100.0,
        "fat_g": 60.0,
        "carbs_g": 180.0,
        "meals_count": 2,
    }

    streak = await client.get("/v1/streak", headers={"Authorization": f"Bearer {token}"})
    assert streak.status_code == 200
    streak_body = streak.json()
    assert streak_body["currentStreak"] == 1
    assert streak_body["bestStreak"] >= 1
    assert streak_body["lastCompletedDate"] == today.isoformat()


@pytest.mark.asyncio
async def test_smoke_reminder_job_runs_with_mocked_dependencies(monkeypatch):
    from app.notifications import reminders as reminders_module

    fake_conn = object()

    class _AcquireCtx:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakePool:
        def acquire(self):
            return _AcquireCtx()

    create_pool = AsyncMock()
    close_pool = AsyncMock()
    fake_sender = AsyncMock()
    run_daily = AsyncMock(
        return_value=SimpleNamespace(total_scanned=1, eligible=1, sent=1, skipped=0, failed=0)
    )

    monkeypatch.setattr(reminders_module.db, "create_pool", create_pool)
    monkeypatch.setattr(reminders_module.db, "close_pool", close_pool)
    monkeypatch.setattr(reminders_module.db, "pool", _FakePool())
    monkeypatch.setattr(reminders_module.telegram_bot_client, "send_message", fake_sender)
    monkeypatch.setattr(reminders_module, "run_daily_reminders", run_daily)

    exit_code = await reminders_module._run()

    assert exit_code == 0
    create_pool.assert_awaited_once()
    close_pool.assert_awaited_once()
    run_daily.assert_awaited_once()
    assert run_daily.await_args is not None
    args = run_daily.await_args.args
    kwargs = run_daily.await_args.kwargs
    assert args[0] is fake_conn
    assert kwargs["sender"] is fake_sender
