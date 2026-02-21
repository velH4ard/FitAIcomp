import json
from datetime import date, datetime, timezone
from uuid import uuid4

import asyncpg
import pytest

from app.db import get_db
from app.deps import get_current_user
from app.main import app


FREE_USER = {
    "id": "00000000-0000-0000-0000-000000000901",
    "telegram_id": 900001,
    "subscription_status": "free",
    "subscription_active_until": None,
    "is_onboarded": True,
    "profile": "{}",
}

BLOCKED_USER = {
    **FREE_USER,
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


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSprint2Conn:
    def __init__(self):
        self.usage_daily = {}
        self.analyze_requests = {}
        self.meals = []
        self.daily_stats = {}
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

        if "INSERT INTO daily_stats" in query:
            user_id = str(args[0])
            meal_date = args[1]
            calories = float(args[2]) if len(args) > 2 else 0.0
            protein = float(args[3]) if len(args) > 3 else 0.0
            fat = float(args[4]) if len(args) > 4 else 0.0
            carbs = float(args[5]) if len(args) > 5 else 0.0
            meals_delta = int(args[6]) if len(args) > 6 else 1

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

            if "DO UPDATE SET" in query:
                self.daily_stats[key] = {
                    "calories_kcal": current["calories_kcal"] + calories,
                    "protein_g": current["protein_g"] + protein,
                    "fat_g": current["fat_g"] + fat,
                    "carbs_g": current["carbs_g"] + carbs,
                    "meals_count": current["meals_count"] + meals_delta,
                }
            else:
                self.daily_stats[key] = {
                    "calories_kcal": calories,
                    "protein_g": protein,
                    "fat_g": fat,
                    "carbs_g": carbs,
                    "meals_count": meals_delta,
                }
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
            meal_id = str(meal_id)
            result_json = json.loads(response_json) if isinstance(response_json, str) else response_json
            self.meals.append(
                {
                    "id": meal_id,
                    "user_id": str(user_id),
                    "created_at": created_at,
                    "description": description,
                    "image_path": image_path,
                    "ai_model": ai_model,
                    "ai_confidence": ai_confidence,
                    "result_json": result_json,
                    "idempotency_key": idempotency_key,
                    "analyze_request_id": str(analyze_request_id),
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
        if "FROM daily_stats" in query:
            user_id = str(args[0])
            from_date = None
            to_date = None
            for arg in args[1:]:
                if isinstance(arg, date):
                    if from_date is None:
                        from_date = arg
                    else:
                        to_date = arg
            if from_date is None or to_date is None:
                return []

            rows = []
            for (uid, stat_date), totals in self.daily_stats.items():
                if uid != user_id:
                    continue
                if from_date <= stat_date <= to_date:
                    rows.append(
                        {
                            "date": stat_date,
                            "calories_kcal": totals["calories_kcal"],
                            "protein_g": totals["protein_g"],
                            "fat_g": totals["fat_g"],
                            "carbs_g": totals["carbs_g"],
                            "meals_count": totals["meals_count"],
                        }
                    )
            rows.sort(key=lambda row: row["date"])
            return rows

        return []


@pytest.fixture
def fake_sprint2_conn():
    return FakeSprint2Conn()


@pytest.fixture
def valid_image_upload():
    return {"file": ("meal.jpg", b"fake-image-content", "image/jpeg")}


def _set_overrides(user, conn):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: conn


def _clear_overrides():
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


def _value_from_keys(payload, *keys):
    for key in keys:
        if key in payload:
            return payload[key]
    raise AssertionError(f"None of keys present: {keys}")


def _meal_totals(response_json: dict) -> dict:
    return response_json["meal"]["result"]["totals"]


@pytest.mark.asyncio
async def test_daily_stats_after_analyze(client, fake_sprint2_conn, valid_image_upload, monkeypatch):
    async def fake_analyze_image(*args, **kwargs):
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    _set_overrides(FREE_USER, fake_sprint2_conn)
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        stats_before = await client.get(f"/v1/stats/daily?date={today}")
        assert stats_before.status_code == 200
        assert stats_before.json() == {
            "date": today,
            "calories_kcal": 0.0,
            "protein_g": 0.0,
            "fat_g": 0.0,
            "carbs_g": 0.0,
            "mealsCount": 0,
        }

        first = await client.post(
            "/v1/meals/analyze",
            files=valid_image_upload,
            headers={"Idempotency-Key": "s2-stats-1"},
        )
        second = await client.post(
            "/v1/meals/analyze",
            files=valid_image_upload,
            headers={"Idempotency-Key": "s2-stats-2"},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        first_totals = _meal_totals(first.json())
        second_totals = _meal_totals(second.json())

        stats_response = await client.get(f"/v1/stats/daily?date={today}")
        assert stats_response.status_code == 200

        body = stats_response.json()
        calories = _value_from_keys(body, "calories_kcal")
        protein = _value_from_keys(body, "protein_g")
        fat = _value_from_keys(body, "fat_g")
        carbs = _value_from_keys(body, "carbs_g")
        meals_count = _value_from_keys(body, "mealsCount")

        assert isinstance(calories, (int, float))
        assert isinstance(protein, (int, float))
        assert isinstance(fat, (int, float))
        assert isinstance(carbs, (int, float))
        assert isinstance(meals_count, int)
        assert body["date"] == today
        assert calories == pytest.approx(first_totals["calories_kcal"] + second_totals["calories_kcal"])
        assert protein == pytest.approx(first_totals["protein_g"] + second_totals["protein_g"])
        assert fat == pytest.approx(first_totals["fat_g"] + second_totals["fat_g"])
        assert carbs == pytest.approx(first_totals["carbs_g"] + second_totals["carbs_g"])
        assert meals_count == 2
    finally:
        _clear_overrides()


@pytest.mark.asyncio
async def test_daily_stats_idempotency_replay_does_not_double_count(client, fake_sprint2_conn, valid_image_upload, monkeypatch):
    call_count = {"n": 0}

    async def fake_analyze_image(*args, **kwargs):
        call_count["n"] += 1
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    _set_overrides(FREE_USER, fake_sprint2_conn)
    try:
        headers = {"Idempotency-Key": "s2-stats-replay-1"}
        first = await client.post("/v1/meals/analyze", files=valid_image_upload, headers=headers)
        second = await client.post("/v1/meals/analyze", files=valid_image_upload, headers=headers)
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json() == second.json()
        replay_totals = _meal_totals(first.json())

        stats_response = await client.get("/v1/stats/daily")
        assert stats_response.status_code == 200
        body = stats_response.json()

        assert _value_from_keys(body, "calories_kcal") == pytest.approx(replay_totals["calories_kcal"])
        assert _value_from_keys(body, "protein_g") == pytest.approx(replay_totals["protein_g"])
        assert _value_from_keys(body, "fat_g") == pytest.approx(replay_totals["fat_g"])
        assert _value_from_keys(body, "carbs_g") == pytest.approx(replay_totals["carbs_g"])
        assert _value_from_keys(body, "mealsCount") == 1
        assert body["date"] == datetime.now(timezone.utc).date().isoformat()
        assert call_count["n"] == 1
    finally:
        _clear_overrides()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "photos_used,expected_hint",
    [
        (2, "hard"),
        (1, "soft"),
        (0, "soft"),
    ],
)
async def test_upgrade_hint_soft_hard(client, fake_sprint2_conn, photos_used, expected_hint):
    today = datetime.now(timezone.utc).date()
    fake_sprint2_conn.usage_daily[(FREE_USER["id"], today)] = photos_used

    _set_overrides(FREE_USER, fake_sprint2_conn)
    try:
        response = await client.get("/v1/usage/today")
        assert response.status_code == 200

        body = response.json()
        assert body["remaining"] == max(0, 2 - photos_used)
        assert "upgradeHint" in body
        assert body["upgradeHint"] == expected_hint
    finally:
        _clear_overrides()


@pytest.mark.asyncio
async def test_events_emitted_on_analyze_and_quota(
    client,
    fake_sprint2_conn,
    valid_image_upload,
    monkeypatch,
):
    async def fake_analyze_image(*args, **kwargs):
        return json.dumps(VALID_AI_JSON)

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", fake_analyze_image)

    _set_overrides(FREE_USER, fake_sprint2_conn)
    try:
        success_response = await client.post(
            "/v1/meals/analyze",
            files=valid_image_upload,
            headers={"Idempotency-Key": "s2-events-success-1"},
        )
        assert success_response.status_code == 200

        _set_overrides(BLOCKED_USER, fake_sprint2_conn)
        quota_response = await client.post(
            "/v1/meals/analyze",
            files=valid_image_upload,
            headers={"Idempotency-Key": "s2-events-quota-1"},
        )
        assert quota_response.status_code == 429
        assert quota_response.json()["error"]["code"] == "QUOTA_EXCEEDED"

        event_types = [event["event_type"] for event in fake_sprint2_conn.events]
        assert "analyze_started" in event_types
        assert "analyze_completed" in event_types
        assert "quota_exceeded" in event_types

        for event in fake_sprint2_conn.events:
            assert isinstance(event.get("event_type"), str)
            assert isinstance(event.get("payload"), dict)
    finally:
        _clear_overrides()
