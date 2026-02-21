import inspect
import json
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import asyncpg
import pytest

from app.db import get_db
from app.deps import get_current_user
from app.errors import FitAIError
from app.config import settings
from app.main import app, analyze_meal


MOCK_USER = {
    "id": "00000000-0000-0000-0000-000000000001",
    "telegram_id": 123456789,
    "subscription_status": "free",
    "subscription_active_until": None,
    "is_onboarded": True,
    "profile": "{}",
}

BLOCKED_USER = {
    **MOCK_USER,
    "subscription_status": "blocked",
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

VALID_AI_JSON_300 = {
    "recognized": True,
    "overall_confidence": 0.7,
    "totals": {
        "calories_kcal": 300,
        "protein_g": 30,
        "fat_g": 10,
        "carbs_g": 20,
    },
    "items": [
        {
            "name": "rice",
            "grams": 220,
            "calories_kcal": 300,
            "protein_g": 30,
            "fat_g": 10,
            "carbs_g": 20,
            "confidence": 0.7,
        }
    ],
    "warnings": [],
    "assumptions": [],
}


def _assert_totals_equal_items(result: dict):
    items = result["items"]
    assert result["totals"]["calories_kcal"] == sum(int(item["calories_kcal"]) for item in items)
    assert result["totals"]["protein_g"] == pytest.approx(round(sum(float(item["protein_g"]) for item in items), 1))
    assert result["totals"]["fat_g"] == pytest.approx(round(sum(float(item["fat_g"]) for item in items), 1))
    assert result["totals"]["carbs_g"] == pytest.approx(round(sum(float(item["carbs_g"]) for item in items), 1))


def _assert_bounded_jitter(result: dict):
    item = result["items"][0]
    source_item = VALID_AI_JSON["items"][0]

    calories_delta = abs(item["calories_kcal"] - source_item["calories_kcal"]) / source_item["calories_kcal"]
    protein_delta = abs(item["protein_g"] - source_item["protein_g"]) / source_item["protein_g"]
    fat_delta = abs(item["fat_g"] - source_item["fat_g"]) / source_item["fat_g"]
    carbs_delta = abs(item["carbs_g"] - source_item["carbs_g"]) / source_item["carbs_g"]

    assert calories_delta <= 0.10
    assert protein_delta <= 0.10
    assert fat_delta <= 0.10
    assert carbs_delta <= 0.10


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeAnalyzeConn:
    def __init__(self):
        self.usage_daily = {}
        self.analyze_requests = {}
        self.meals = []
        self.daily_stats = {}
        self.events = []
        self.fail_meal_insert = False

    def transaction(self):
        return _Tx()

    async def execute(self, query, *args):
        if "INSERT INTO events" in query:
            user_id, event_type, payload = args
            payload_value = payload
            if isinstance(payload_value, str):
                payload_value = json.loads(payload_value)
            self.events.append(
                {
                    "user_id": str(user_id),
                    "event_type": str(event_type),
                    "payload": payload_value,
                    "created_at": datetime.now(timezone.utc),
                }
            )
            return "INSERT 0 1"

        if "INSERT INTO usage_daily" in query:
            user_id, day = args
            self.usage_daily.setdefault((user_id, day), 0)
            return "INSERT 0 1"

        if "UPDATE usage_daily SET photos_used = photos_used + 1" in query:
            user_id, day = args
            self.usage_daily[(user_id, day)] = self.usage_daily.get((user_id, day), 0) + 1
            return "UPDATE 1"

        if "UPDATE usage_daily SET photos_used = GREATEST(0, photos_used - 1)" in query:
            user_id, day = args
            current = self.usage_daily.get((user_id, day), 0)
            self.usage_daily[(user_id, day)] = max(0, current - 1)
            return "UPDATE 1"

        if "INSERT INTO daily_stats" in query:
            user_id = str(args[0])
            meal_date = args[1]
            calories = float(args[2])
            protein = float(args[3])
            fat = float(args[4])
            carbs = float(args[5])

            key = (user_id, meal_date)
            current = self.daily_stats.get(
                key,
                {
                    "calories_kcal": 0.0,
                    "protein_g": 0.0,
                    "fat_g": 0.0,
                    "carbs_g": 0.0,
                    "meals_count": 0,
                },
            )
            self.daily_stats[key] = {
                "calories_kcal": current["calories_kcal"] + calories,
                "protein_g": current["protein_g"] + protein,
                "fat_g": current["fat_g"] + fat,
                "carbs_g": current["carbs_g"] + carbs,
                "meals_count": current["meals_count"] + 1,
            }
            return "INSERT 0 1"

        if "SET status = 'failed'" in query and "UPDATE analyze_requests" in query:
            if len(args) == 1:
                req_id = str(args[0])
                for req in self.analyze_requests.values():
                    if req.get("id") == req_id and req["status"] == "processing":
                        req["status"] = "failed"
                        break
            else:
                user_id, idem_key = args
                req_key = (user_id, idem_key)
                req = self.analyze_requests.get(req_key)
                if req and req["status"] == "processing":
                    req["status"] = "failed"
            return "UPDATE 1"

        return "OK"

    async def fetchrow(self, query, *args):
        if "SELECT COUNT(*)::int AS events_count" in query and "FROM events" in query:
            user_id = str(args[0])
            now_utc = datetime.now(timezone.utc)
            count = 0
            for event in self.events:
                if event["user_id"] != user_id:
                    continue
                if event["event_type"] != "analyze_started":
                    continue
                age_seconds = (now_utc - event["created_at"]).total_seconds()
                if age_seconds <= 60:
                    count += 1
            return {"events_count": count}

        if "INSERT INTO analyze_requests" in query and "RETURNING id" in query:
            user_id, idem_key = args
            req_key = (user_id, idem_key)
            if req_key in self.analyze_requests:
                raise asyncpg.UniqueViolationError("duplicate idempotency key")
            req_id = str(uuid4())
            self.analyze_requests[req_key] = {
                "id": req_id,
                "status": "processing",
                "response_json": None,
            }
            return {"id": req_id}

        if "SELECT id, status, response_json FROM analyze_requests" in query:
            user_id, idem_key = args
            req = self.analyze_requests.get((user_id, idem_key))
            return req

        if "INSERT INTO meals" in query and "RETURNING id" in query:
            (
                meal_id,
                user_id,
                created_at,
                description,
                image_path,
                ai_model,
                ai_confidence,
                response_json,
                idempotency_key,
                analyze_request_id,
            ) = args

            if self.fail_meal_insert:
                raise RuntimeError("forced meal insert failure")

            for meal in self.meals:
                if meal["analyze_request_id"] == analyze_request_id:
                    return None

            result_json = response_json
            if isinstance(result_json, str):
                result_json = json.loads(result_json)

            meal_id = str(meal_id)
            self.meals.append(
                {
                    "id": meal_id,
                    "user_id": user_id,
                    "created_at": created_at,
                    "meal_time": "unknown",
                    "description": description,
                    "image_url": None,
                    "image_path": image_path,
                    "ai_provider": "openrouter",
                    "ai_model": ai_model,
                    "ai_confidence": ai_confidence,
                    "result_json": result_json,
                    "idempotency_key": idempotency_key,
                    "analyze_request_id": analyze_request_id,
                }
            )
            return {"id": meal_id, "created_at": created_at}

        if "UPDATE analyze_requests" in query and "SET status = 'completed'" in query and "RETURNING id" in query:
            response_json, req_id = args
            for req in self.analyze_requests.values():
                if req["id"] == str(req_id) and req["status"] == "processing":
                    req["status"] = "completed"
                    req["response_json"] = response_json
                    return {"id": req_id}
            return None

        if "SELECT photos_used FROM usage_daily" in query:
            user_id, day = args
            if (user_id, day) not in self.usage_daily:
                return {"photos_used": 0}
            return {"photos_used": self.usage_daily[(user_id, day)]}

        if "FROM meals" in query and "WHERE id = $1 AND user_id = $2" in query:
            meal_id, user_id = str(args[0]), str(args[1])
            for meal in self.meals:
                if meal["id"] == meal_id and str(meal["user_id"]) == user_id:
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

        if "FROM daily_stats" in query and "AND date = $2::date" in query:
            user_id = str(args[0])
            selected_date = args[1]
            stats = self.daily_stats.get((user_id, selected_date))
            if stats is None:
                return None
            return {
                "calories_kcal": stats["calories_kcal"],
                "protein_g": stats["protein_g"],
                "fat_g": stats["fat_g"],
                "carbs_g": stats["carbs_g"],
                "meals_count": stats["meals_count"],
            }

        return None

    async def fetch(self, query, *args):
        if "FROM meals" not in query or "ORDER BY created_at DESC, id DESC" not in query:
            return []

        user_id = str(args[0])
        limit = int(args[-1])
        rows = [m for m in self.meals if m["user_id"] == user_id]
        rows.sort(key=lambda x: (x["created_at"], x["id"]), reverse=True)

        result = []
        for meal in rows[:limit]:
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

    def photos_used_today(self, user_id):
        today = datetime.now(timezone.utc).date()
        return self.usage_daily.get((user_id, today), 0)

    def request_state(self, user_id, idem_key):
        return self.analyze_requests.get((user_id, idem_key))

    def meal_count(self, user_id):
        return len([m for m in self.meals if m["user_id"] == user_id])


def _runtime_settings():
    return analyze_meal.__globals__["settings"]


def assert_error_envelope(response, status_code, code):
    assert response.status_code == status_code
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == code
    assert "message" in body["error"]
    assert "details" in body["error"]


def add_analyze_started_event(fake_conn: FakeAnalyzeConn, user_id: str):
    fake_conn.events.append(
        {
            "user_id": str(user_id),
            "event_type": "analyze_started",
            "payload": {"source": "test"},
            "created_at": datetime.now(timezone.utc),
        }
    )


@pytest.fixture
def fake_conn():
    return FakeAnalyzeConn()


@pytest.fixture(autouse=True)
def disable_force_fail_switch():
    runtime_settings = _runtime_settings()
    original = runtime_settings.MEALS_ANALYZE_FORCE_FAIL_AFTER_RESERVE
    runtime_settings.MEALS_ANALYZE_FORCE_FAIL_AFTER_RESERVE = 0
    yield
    runtime_settings.MEALS_ANALYZE_FORCE_FAIL_AFTER_RESERVE = original


@pytest.fixture
def auth_and_db_overrides(fake_conn):
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER
    app.dependency_overrides[get_db] = lambda: fake_conn
    yield fake_conn
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def valid_image_upload():
    return {"image": ("meal.jpg", b"fake-image-content", "image/jpeg")}


@pytest.fixture
def legacy_file_upload():
    return {"file": ("meal.jpg", b"fake-image-content", "image/jpeg")}


@pytest.mark.asyncio
async def test_analyze_meal_accepts_canonical_image_field(client, auth_and_db_overrides, valid_image_upload, monkeypatch):
    async def fake_analyze_image(*args, **kwargs):
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "idem-image-field-1"},
    )

    assert response.status_code == 200
    assert set(response.json().keys()) == {"meal", "usage"}


@pytest.mark.asyncio
async def test_analyze_meal_accepts_legacy_file_field(client, auth_and_db_overrides, legacy_file_upload, monkeypatch):
    async def fake_analyze_image(*args, **kwargs):
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=legacy_file_upload,
        headers={"Idempotency-Key": "idem-file-field-1"},
    )

    assert response.status_code == 200
    assert set(response.json().keys()) == {"meal", "usage"}


@pytest.mark.asyncio
async def test_analyze_meal_missing_both_fields_returns_image_validation_error(client, auth_and_db_overrides):
    response = await client.post(
        "/v1/meals/analyze",
        files={},
        headers={"Idempotency-Key": "idem-missing-image-file-1"},
    )

    assert_error_envelope(response, 400, "VALIDATION_FAILED")
    field_error = response.json()["error"]["details"]["fieldErrors"][0]
    assert field_error["field"] == "image"
    assert field_error["issue"] == "Field required"


@pytest.fixture
def valid_image_upload_image_field():
    return {"image": ("meal.jpg", b"fake-image-content", "image/jpeg")}


@pytest.mark.asyncio
async def test_analyze_meal_with_description_trims_and_passes_context(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    fake_conn = auth_and_db_overrides
    captured_description: Optional[str] = None

    async def fake_analyze_image(*args, **kwargs):
        nonlocal captured_description
        captured_description = kwargs.get("description")
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        data={"description": "   chicken breast with rice   "},
        headers={"Idempotency-Key": "idem-description-1"},
    )

    assert response.status_code == 200
    assert captured_description == "chicken breast with rice"
    assert fake_conn.meals[0]["description"] == "chicken breast with rice"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("description_value", "idempotency_key"),
    [
        ("", "idem-description-empty-1"),
        ("    ", "idem-description-blank-1"),
    ],
)
async def test_analyze_meal_empty_or_whitespace_description_is_normalized_to_none(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch, description_value, idempotency_key
):
    fake_conn = auth_and_db_overrides
    captured_description: Optional[str] = "not-set"

    async def fake_analyze_image(*args, **kwargs):
        nonlocal captured_description
        captured_description = kwargs.get("description")
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        data={"description": description_value},
        headers={"Idempotency-Key": idempotency_key},
    )

    assert response.status_code == 200
    assert captured_description is None
    assert fake_conn.meals[0]["description"] is None


@pytest.mark.asyncio
async def test_analyze_meal_too_long_description_returns_validation_failed(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    ai_called = False

    async def fake_analyze_image(*args, **kwargs):
        nonlocal ai_called
        ai_called = True
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        data={"description": "x" * 501},
        headers={"Idempotency-Key": "idem-description-too-long-1"},
    )

    assert_error_envelope(response, 400, "VALIDATION_FAILED")
    body = response.json()
    assert body["error"]["code"] != "INTERNAL_ERROR"
    field_error = body["error"]["details"]["fieldErrors"][0]
    assert field_error["field"] == "description"
    assert field_error["issue"] == "must be <= 500 chars"
    assert field_error["maxLen"] == 500
    assert body["error"]["details"]["maxLen"] == 500
    assert ai_called is False


@pytest.mark.asyncio
async def test_analyze_meal_description_non_text_multipart_part_returns_validation_failed(
    client, auth_and_db_overrides, monkeypatch
):
    ai_called = False

    async def fake_analyze_image(*args, **kwargs):
        nonlocal ai_called
        ai_called = True
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files={
            "file": ("meal.jpg", b"fake-image-content", "image/jpeg"),
            "description": ("notes.txt", b"unexpected-binary", "text/plain"),
        },
        headers={"Idempotency-Key": "idem-description-non-text-1"},
    )

    assert_error_envelope(response, 400, "VALIDATION_FAILED")
    details = response.json()["error"]["details"]
    assert details["fieldErrors"][0]["field"] == "description"
    assert details["maxLen"] == 500
    assert ai_called is False


@pytest.mark.asyncio
async def test_analyze_meal_happy_path_marks_completed_and_increments_usage(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    fake_conn = auth_and_db_overrides
    call_count = {"n": 0}

    async def fake_analyze_image(*args, **kwargs):
        call_count["n"] += 1
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "idem-happy-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body.get("meal"), dict)
    assert isinstance(body.get("usage"), dict)
    assert set(body["meal"]["result"].keys()) == set(VALID_AI_JSON.keys())
    assert body["meal"]["result"]["recognized"] is True
    _assert_totals_equal_items(body["meal"]["result"])
    _assert_bounded_jitter(body["meal"]["result"])
    assert set(body["meal"]["result"].keys()) == {
        "recognized",
        "overall_confidence",
        "totals",
        "items",
        "warnings",
        "assumptions",
    }
    assert call_count["n"] == 1
    assert fake_conn.photos_used_today(MOCK_USER["id"]) == 1

    req = fake_conn.request_state(MOCK_USER["id"], "idem-happy-1")
    assert req is not None
    assert req["status"] == "completed"
    assert req["response_json"] is not None
    assert fake_conn.meal_count(MOCK_USER["id"]) == 1


@pytest.mark.asyncio
async def test_analyze_meal_post_ai_error_applied_after_validation_and_within_plus_minus_10_percent(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    async def fake_analyze_image(*args, **kwargs):
        return json.dumps(VALID_AI_JSON_300)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "idem-300-bounds-1"},
    )

    assert response.status_code == 200
    result = response.json()["meal"]["result"]
    calories = result["items"][0]["calories_kcal"]

    assert 270 <= calories <= 330
    _assert_totals_equal_items(result)


@pytest.mark.asyncio
async def test_analyze_meal_without_description_is_treated_as_absent(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    fake_conn = auth_and_db_overrides
    captured_description: Optional[str] = "not-set"

    async def fake_analyze_image(*args, **kwargs):
        nonlocal captured_description
        captured_description = kwargs.get("description")
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
    )

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body.get("meal"), dict)
    assert isinstance(body.get("usage"), dict)
    assert captured_description is None
    assert fake_conn.meals[0]["description"] is None


@pytest.mark.asyncio
async def test_analyze_meal_response_contract_has_exact_top_level_keys_meal_and_usage(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    async def fake_analyze_image(*args, **kwargs):
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
    )

    assert response.status_code == 200
    assert set(response.json().keys()) == {"meal", "usage"}


@pytest.mark.asyncio
async def test_analyze_meal_accepts_multipart_image_field_and_returns_meal_usage(
    client, auth_and_db_overrides, valid_image_upload_image_field, monkeypatch
):
    async def fake_analyze_image(*args, **kwargs):
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload_image_field,
        headers={"Idempotency-Key": "idem-image-field-compat-1"},
    )

    assert response.status_code == 200
    assert set(response.json().keys()) == {"meal", "usage"}


@pytest.mark.asyncio
async def test_analyze_meal_accepts_multipart_file_field_and_returns_meal_usage(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    async def fake_analyze_image(*args, **kwargs):
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "idem-file-field-compat-1"},
    )

    assert response.status_code == 200
    assert set(response.json().keys()) == {"meal", "usage"}


@pytest.mark.asyncio
async def test_analyze_meal_missing_image_and_file_returns_validation_failed_with_image_field_error(
    client, auth_and_db_overrides, monkeypatch
):
    ai_called = False

    async def fake_analyze_image(*args, **kwargs):
        nonlocal ai_called
        ai_called = True
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        headers={"Idempotency-Key": "idem-missing-image-field-compat-1"},
    )

    assert_error_envelope(response, 400, "VALIDATION_FAILED")
    field_errors = response.json()["error"]["details"]["fieldErrors"]
    assert any("image" in str(item.get("field", "")) for item in field_errors)
    assert ai_called is False


@pytest.mark.asyncio
async def test_analyze_meal_idempotency_same_key_returns_cached_and_single_usage_increment(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    fake_conn = auth_and_db_overrides
    call_count = {"n": 0}

    async def fake_analyze_image(*args, **kwargs):
        call_count["n"] += 1
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    headers = {"Idempotency-Key": "idem-repeat-1"}
    usage_before = await client.get("/v1/usage/today")
    assert usage_before.status_code == 200
    assert usage_before.json()["photosUsed"] == 0

    response1 = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        data={"description": "protein bowl"},
        headers=headers,
    )
    usage_after_first = await client.get("/v1/usage/today")
    assert usage_after_first.status_code == 200
    assert usage_after_first.json()["photosUsed"] == 1

    meals_after_first = await client.get("/v1/meals?limit=10")
    assert meals_after_first.status_code == 200
    assert len(meals_after_first.json()["items"]) == 1

    response2 = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        data={"description": "protein bowl"},
        headers=headers,
    )
    usage_after_replay = await client.get("/v1/usage/today")
    assert usage_after_replay.status_code == 200
    assert usage_after_replay.json()["photosUsed"] == 1

    meals_after_replay = await client.get("/v1/meals?limit=10")
    assert meals_after_replay.status_code == 200
    assert len(meals_after_replay.json()["items"]) == 1

    assert response1.status_code == 200
    assert response2.status_code == 200
    assert response1.json() == response2.json()
    assert call_count["n"] == 1
    assert fake_conn.photos_used_today(MOCK_USER["id"]) == 1
    assert fake_conn.meal_count(MOCK_USER["id"]) == 1


@pytest.mark.asyncio
async def test_analyze_meal_idempotency_replay_with_empty_description_does_not_recall_ai(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    fake_conn = auth_and_db_overrides
    call_count = {"n": 0}

    async def fake_analyze_image(*args, **kwargs):
        call_count["n"] += 1
        assert kwargs.get("description") is None
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    headers = {"Idempotency-Key": "idem-empty-description-replay-1"}
    response1 = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        data={"description": "   "},
        headers=headers,
    )
    response2 = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        data={"description": "   "},
        headers=headers,
    )

    assert response1.status_code == 200
    assert response2.status_code == 200
    assert response1.json() == response2.json()
    assert call_count["n"] == 1
    assert fake_conn.photos_used_today(MOCK_USER["id"]) == 1


@pytest.mark.asyncio
async def test_analyze_meal_created_row_visible_in_history_list(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    async def fake_analyze_image(*args, **kwargs):
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    analyze_response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "idem-history-visible-1"},
    )
    assert analyze_response.status_code == 200
    analyzed_totals = analyze_response.json()["meal"]["result"]["totals"]

    history_response = await client.get("/v1/meals?limit=3")
    assert history_response.status_code == 200
    body = history_response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["totals"] == analyzed_totals


@pytest.mark.asyncio
async def test_analyze_meal_idempotency_replay_decodes_json_string_to_object(
    client, auth_and_db_overrides, valid_image_upload
):
    fake_conn = auth_and_db_overrides
    fake_conn.analyze_requests[(MOCK_USER["id"], "idem-cached-json-string")] = {
        "status": "completed",
        "response_json": json.dumps({"meal": {"result": VALID_AI_JSON}, "usage": {}}),
    }

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "idem-cached-json-string"},
    )

    assert response.status_code == 200
    assert isinstance(response.json(), dict)
    assert response.json() == {"meal": {"result": VALID_AI_JSON}, "usage": {}}


@pytest.mark.asyncio
async def test_analyze_meal_invalid_ai_json_compensates_and_failed_key_conflicts_on_retry(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    fake_conn = auth_and_db_overrides

    async def fake_analyze_image(*args, **kwargs):
        return "this is not valid json"

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    headers = {"Idempotency-Key": "idem-invalid-ai-1"}
    response1 = await client.post("/v1/meals/analyze", files=valid_image_upload, headers=headers)

    assert_error_envelope(response1, 400, "VALIDATION_FAILED")
    assert fake_conn.photos_used_today(MOCK_USER["id"]) == 0

    req = fake_conn.request_state(MOCK_USER["id"], "idem-invalid-ai-1")
    assert req is not None
    assert req["status"] == "failed"

    response2 = await client.post("/v1/meals/analyze", files=valid_image_upload, headers=headers)
    assert_error_envelope(response2, 409, "IDEMPOTENCY_CONFLICT")
    assert fake_conn.photos_used_today(MOCK_USER["id"]) == 0


@pytest.mark.asyncio
async def test_analyze_meal_ai_provider_error_compensates_and_marks_failed(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    fake_conn = auth_and_db_overrides

    async def fake_analyze_image(*args, **kwargs):
        raise FitAIError(
            code="AI_PROVIDER_ERROR",
            message="Ошибка ИИ провайдера",
            status_code=502,
            details={"provider": "openrouter", "stage": "timeout"},
        )

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "idem-provider-error-1"},
    )

    assert_error_envelope(response, 502, "AI_PROVIDER_ERROR")
    assert fake_conn.photos_used_today(MOCK_USER["id"]) == 0

    req = fake_conn.request_state(MOCK_USER["id"], "idem-provider-error-1")
    assert req is not None
    assert req["status"] == "failed"


@pytest.mark.asyncio
async def test_analyze_meal_forced_failure_compensation_never_negative(
    client, auth_and_db_overrides, valid_image_upload
):
    fake_conn = auth_and_db_overrides
    _runtime_settings().MEALS_ANALYZE_FORCE_FAIL_AFTER_RESERVE = 1

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "idem-forced-fail-1"},
    )

    assert_error_envelope(response, 500, "INTERNAL_ERROR")
    assert fake_conn.photos_used_today(MOCK_USER["id"]) == 0
    assert fake_conn.meal_count(MOCK_USER["id"]) == 0
    req = fake_conn.request_state(MOCK_USER["id"], "idem-forced-fail-1")
    assert req is not None
    assert req["status"] == "failed"

    retry = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "idem-forced-fail-1"},
    )
    assert_error_envelope(retry, 409, "IDEMPOTENCY_CONFLICT")
    assert fake_conn.photos_used_today(MOCK_USER["id"]) == 0
    assert fake_conn.meal_count(MOCK_USER["id"]) == 0


@pytest.mark.asyncio
async def test_analyze_meal_forced_failure_compensates_without_meal_or_daily_stats_corruption(
    client, auth_and_db_overrides, valid_image_upload
):
    fake_conn = auth_and_db_overrides
    today = datetime.now(timezone.utc).date()
    fake_conn.daily_stats[(MOCK_USER["id"], today)] = {
        "calories_kcal": 100.0,
        "protein_g": 10.0,
        "fat_g": 5.0,
        "carbs_g": 12.0,
        "meals_count": 1,
    }
    before_daily_stats = dict(fake_conn.daily_stats)
    _runtime_settings().MEALS_ANALYZE_FORCE_FAIL_AFTER_RESERVE = 1

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "idem-forced-fail-atomicity-1"},
    )

    assert_error_envelope(response, 500, "INTERNAL_ERROR")
    assert fake_conn.photos_used_today(MOCK_USER["id"]) == 0
    assert fake_conn.meal_count(MOCK_USER["id"]) == 0
    assert fake_conn.daily_stats == before_daily_stats


@pytest.mark.asyncio
async def test_analyze_meal_insert_failure_rolls_back_completion_state(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    fake_conn = auth_and_db_overrides
    fake_conn.fail_meal_insert = True

    async def fake_analyze_image(*args, **kwargs):
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "idem-meal-insert-fail-1"},
    )

    assert_error_envelope(response, 500, "INTERNAL_ERROR")
    assert fake_conn.photos_used_today(MOCK_USER["id"]) == 0
    assert fake_conn.meal_count(MOCK_USER["id"]) == 0

    req = fake_conn.request_state(MOCK_USER["id"], "idem-meal-insert-fail-1")
    assert req is not None
    assert req["status"] == "failed"


@pytest.mark.asyncio
async def test_analyze_meal_storage_error_branch_if_present(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    source = inspect.getsource(analyze_meal)
    if "STORAGE_ERROR" not in source:
        pytest.skip("Storage stage is not implemented in current /v1/meals/analyze flow")

    fake_conn = auth_and_db_overrides

    async def fake_analyze_image(*args, **kwargs):
        raise FitAIError(
            code="STORAGE_ERROR",
            message="Ошибка хранилища",
            status_code=502,
            details={"stage": "upload"},
        )

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "idem-storage-error-1"},
    )

    assert_error_envelope(response, 502, "STORAGE_ERROR")
    assert fake_conn.photos_used_today(MOCK_USER["id"]) == 0

    req = fake_conn.request_state(MOCK_USER["id"], "idem-storage-error-1")
    assert req is not None
    assert req["status"] == "failed"


@pytest.mark.asyncio
async def test_analyze_meal_quota_precheck_blocked_user_does_not_create_idempotency_row(
    client, fake_conn, valid_image_upload, monkeypatch
):
    app.dependency_overrides[get_current_user] = lambda: BLOCKED_USER
    app.dependency_overrides[get_db] = lambda: fake_conn

    ai_called = False

    async def _should_not_be_called(*args, **kwargs):
        nonlocal ai_called
        ai_called = True
        raise AssertionError("OpenRouter must not be called when quota is exhausted")

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", _should_not_be_called)

    try:
        headers = {"Idempotency-Key": "idem-blocked-user-1"}
        response1 = await client.post("/v1/meals/analyze", files=valid_image_upload, headers=headers)
        response2 = await client.post("/v1/meals/analyze", files=valid_image_upload, headers=headers)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)

    assert_error_envelope(response1, 429, "QUOTA_EXCEEDED")
    assert response1.json()["error"]["details"] == {
        "limit": 0,
        "used": 0,
        "status": "blocked",
    }
    assert_error_envelope(response2, 429, "QUOTA_EXCEEDED")
    assert fake_conn.request_state(BLOCKED_USER["id"], "idem-blocked-user-1") is None
    assert fake_conn.photos_used_today(BLOCKED_USER["id"]) == 0
    assert fake_conn.meal_count(BLOCKED_USER["id"]) == 0
    assert ai_called is False
    assert all(event["event_type"] != "analyze_started" for event in fake_conn.events)


@pytest.mark.asyncio
async def test_analyze_meal_rate_limited_returns_429_and_does_not_insert_idempotency(
    client, fake_conn, valid_image_upload, monkeypatch
):
    rate_limited_user = {
        **MOCK_USER,
        "id": "6489db75-92ed-42bc-8b2b-87b40e6aa855",
    }
    app.dependency_overrides[get_current_user] = lambda: rate_limited_user
    app.dependency_overrides[get_db] = lambda: fake_conn
    monkeypatch.setattr(_runtime_settings(), "MEALS_ANALYZE_RATE_LIMIT_PER_MINUTE", 1)
    add_analyze_started_event(fake_conn, rate_limited_user["id"])

    try:
        response = await client.post(
            "/v1/meals/analyze",
            files=valid_image_upload,
            headers={"Idempotency-Key": "idem-rate-limited-1"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)

    assert_error_envelope(response, 429, "RATE_LIMITED")
    assert fake_conn.request_state(rate_limited_user["id"], "idem-rate-limited-1") is None


@pytest.mark.asyncio
async def test_analyze_meal_rate_limit_does_not_break_completed_idempotency_replay(
    client, fake_conn, valid_image_upload, monkeypatch
):
    replay_user = {
        **MOCK_USER,
        "id": "6489db75-92ed-42bc-8b2b-87b40e6aa855",
    }
    app.dependency_overrides[get_current_user] = lambda: replay_user
    app.dependency_overrides[get_db] = lambda: fake_conn
    monkeypatch.setattr(settings, "MEALS_ANALYZE_RATE_LIMIT_PER_MINUTE", 1)
    add_analyze_started_event(fake_conn, replay_user["id"])
    fake_conn.analyze_requests[(replay_user["id"], "idem-rate-replay-1")] = {
        "id": str(uuid4()),
        "status": "completed",
        "response_json": {"meal": {"result": VALID_AI_JSON}, "usage": {}},
    }

    try:
        response = await client.post(
            "/v1/meals/analyze",
            files=valid_image_upload,
            headers={"Idempotency-Key": "idem-rate-replay-1"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert response.json() == {"meal": {"result": VALID_AI_JSON}, "usage": {}}


@pytest.mark.asyncio
async def test_analyze_meal_get_meal_is_stable_and_daily_stats_match_jittered_total(
    client, auth_and_db_overrides, valid_image_upload, monkeypatch
):
    fake_conn = auth_and_db_overrides

    async def fake_analyze_image(*args, **kwargs):
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    analyze_response = await client.post(
        "/v1/meals/analyze",
        files=valid_image_upload,
        headers={"Idempotency-Key": "idem-stable-get-1"},
    )
    assert analyze_response.status_code == 200
    analyze_body = analyze_response.json()
    meal_id = analyze_body["meal"]["id"]

    first_get = await client.get(f"/v1/meals/{meal_id}")
    second_get = await client.get(f"/v1/meals/{meal_id}")
    assert first_get.status_code == 200
    assert second_get.status_code == 200
    assert first_get.json() == second_get.json()

    stored_result = first_get.json()["result"]
    _assert_totals_equal_items(stored_result)
    _assert_bounded_jitter(stored_result)

    stats_response = await client.get(f"/v1/stats/daily?date={datetime.now(timezone.utc).date().isoformat()}")
    assert stats_response.status_code == 200
    stats_body = stats_response.json()
    assert stats_body["calories_kcal"] == stored_result["totals"]["calories_kcal"]
    assert stats_body["protein_g"] == pytest.approx(stored_result["totals"]["protein_g"])
    assert stats_body["fat_g"] == pytest.approx(stored_result["totals"]["fat_g"])
    assert stats_body["carbs_g"] == pytest.approx(stored_result["totals"]["carbs_g"])
    assert stats_body["mealsCount"] == 1


@pytest.mark.asyncio
async def test_analyze_meal_under_rate_limit_flow_is_unchanged(
    client, fake_conn, valid_image_upload, monkeypatch
):
    under_limit_user = {
        **MOCK_USER,
        "id": "6489db75-92ed-42bc-8b2b-87b40e6aa855",
    }
    app.dependency_overrides[get_current_user] = lambda: under_limit_user
    app.dependency_overrides[get_db] = lambda: fake_conn
    monkeypatch.setattr(settings, "MEALS_ANALYZE_RATE_LIMIT_PER_MINUTE", 2)
    add_analyze_started_event(fake_conn, under_limit_user["id"])

    async def fake_analyze_image(*args, **kwargs):
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    try:
        response = await client.post(
            "/v1/meals/analyze",
            files=valid_image_upload,
            headers={"Idempotency-Key": "idem-rate-under-limit-1"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert fake_conn.photos_used_today(under_limit_user["id"]) == 1
