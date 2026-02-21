from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db import get_db
from app.deps import get_current_user
from app.main import app


MOCK_USER = {
    "id": "00000000-0000-0000-0000-000000000001",
    "telegram_id": 123456789,
    "subscription_status": "free",
    "subscription_active_until": None,
    "is_onboarded": True,
    "profile": "{}",
}


def _result_json(calories: float, protein: float, fat: float, carbs: float):
    return {
        "recognized": True,
        "overall_confidence": 0.7,
        "totals": {
            "calories_kcal": calories,
            "protein_g": protein,
            "fat_g": fat,
            "carbs_g": carbs,
        },
        "items": [],
        "warnings": [],
        "assumptions": [],
    }


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeMealsConn:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.user_id = MOCK_USER["id"]
        self.other_user_id = "00000000-0000-0000-0000-000000000002"
        self.meals = [
            {
                "id": str(uuid4()),
                "user_id": self.user_id,
                "created_at": now - timedelta(minutes=5),
                "meal_time": "dinner",
                "image_url": "https://img/dinner.jpg",
                "image_path": None,
                "ai_provider": "openrouter",
                "ai_model": "google/gemini-3.0-flash-preview",
                "ai_confidence": 0.9,
                "result_json": _result_json(700, 30, 25, 80),
            },
            {
                "id": str(uuid4()),
                "user_id": self.user_id,
                "created_at": now - timedelta(hours=1),
                "meal_time": "lunch",
                "image_url": "https://img/lunch.jpg",
                "image_path": None,
                "ai_provider": "openrouter",
                "ai_model": "google/gemini-3.0-flash-preview",
                "ai_confidence": 0.8,
                "result_json": _result_json(500, 20, 15, 60),
            },
            {
                "id": str(uuid4()),
                "user_id": self.other_user_id,
                "created_at": now - timedelta(hours=2),
                "meal_time": "breakfast",
                "image_url": "https://img/other.jpg",
                "image_path": None,
                "ai_provider": "openrouter",
                "ai_model": "google/gemini-3.0-flash-preview",
                "ai_confidence": 0.5,
                "result_json": _result_json(300, 10, 10, 30),
            },
        ]
        self.daily_stats = {}

    def transaction(self):
        return _Tx()

    async def fetch(self, query, *args):
        if "FROM meals" not in query or "ORDER BY created_at DESC, id DESC" not in query:
            return []

        user_id = str(args[0])
        limit = int(args[-1])
        filter_date = None
        cursor_created_at = None
        cursor_id = None

        for arg in args[1:-1]:
            if isinstance(arg, date) and not isinstance(arg, datetime):
                filter_date = arg
            elif isinstance(arg, datetime):
                cursor_created_at = arg
            elif isinstance(arg, str) and len(arg) == 36:
                cursor_id = arg

        filtered = [m for m in self.meals if m["user_id"] == user_id]
        if filter_date is not None:
            filtered = [m for m in filtered if m["created_at"].date() == filter_date]

        filtered.sort(key=lambda x: (x["created_at"], x["id"]), reverse=True)

        if cursor_created_at is not None and cursor_id is not None:
            filtered = [
                m
                for m in filtered
                if (m["created_at"], m["id"]) < (cursor_created_at, cursor_id)
            ]

        result = []
        for meal in filtered[:limit]:
            totals = meal["result_json"]["totals"]
            result.append(
                {
                    "id": meal["id"],
                    "created_at": meal["created_at"],
                    "meal_time": meal["meal_time"],
                    "image_url": meal["image_url"],
                    "calories_kcal": totals["calories_kcal"],
                    "protein_g": totals["protein_g"],
                    "fat_g": totals["fat_g"],
                    "carbs_g": totals["carbs_g"],
                }
            )
        return result

    async def fetchrow(self, query, *args):
        if "FROM meals" in query and "FOR UPDATE" in query:
            meal_id = str(args[0])
            user_id = str(args[1])
            for meal in self.meals:
                if meal["id"] == meal_id and meal["user_id"] == user_id:
                    return {"id": meal_id, "meal_date": meal["created_at"].date()}
            return None

        if "COUNT(*)::int AS meals_count" in query:
            user_id = str(args[0])
            meal_date = args[1]
            day_meals = [
                m for m in self.meals if m["user_id"] == user_id and m["created_at"].date() == meal_date
            ]
            return {
                "calories_kcal": sum(m["result_json"]["totals"]["calories_kcal"] for m in day_meals),
                "protein_g": sum(m["result_json"]["totals"]["protein_g"] for m in day_meals),
                "fat_g": sum(m["result_json"]["totals"]["fat_g"] for m in day_meals),
                "carbs_g": sum(m["result_json"]["totals"]["carbs_g"] for m in day_meals),
                "meals_count": len(day_meals),
            }

        if "FROM meals" in query and "result_json" in query:
            meal_id = str(args[0])
            user_id = str(args[1])
            for meal in self.meals:
                if meal["id"] == meal_id and meal["user_id"] == user_id:
                    return {
                        "id": meal["id"],
                        "created_at": meal["created_at"],
                        "meal_time": meal["meal_time"],
                        "image_url": meal["image_url"],
                        "ai_provider": meal["ai_provider"],
                        "ai_model": meal["ai_model"],
                        "ai_confidence": meal["ai_confidence"],
                        "result_json": meal["result_json"],
                    }
            return None

        return None

    async def execute(self, query, *args):
        if "DELETE FROM meals" in query:
            meal_id = str(args[0])
            user_id = str(args[1])
            self.meals = [m for m in self.meals if not (m["id"] == meal_id and m["user_id"] == user_id)]
            return "DELETE 1"

        if "INSERT INTO daily_stats" in query:
            user_id = str(args[0])
            meal_date = args[1]
            self.daily_stats[(user_id, meal_date)] = {
                "calories_kcal": float(args[2]),
                "protein_g": float(args[3]),
                "fat_g": float(args[4]),
                "carbs_g": float(args[5]),
                "meals_count": int(args[6]),
            }
            return "INSERT 0 1"

        return "OK"


@pytest.fixture
def fake_conn():
    return FakeMealsConn()


@pytest.fixture
def auth_and_db_overrides(fake_conn):
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER
    app.dependency_overrides[get_db] = lambda: fake_conn
    yield fake_conn
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_list_meals_returns_ordered_items_and_next_cursor(client, auth_and_db_overrides):
    response = await client.get("/v1/meals?limit=1")
    assert response.status_code == 200

    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["mealTime"] == "dinner"
    assert body["items"][0]["totals"]["calories_kcal"] == 700
    assert isinstance(body["nextCursor"], str)


@pytest.mark.asyncio
async def test_list_meals_cursor_paginates_without_duplicates(client, auth_and_db_overrides):
    page1 = await client.get("/v1/meals?limit=1")
    cursor = page1.json()["nextCursor"]
    page2 = await client.get(f"/v1/meals?limit=1&cursor={cursor}")

    assert page1.status_code == 200
    assert page2.status_code == 200
    assert page1.json()["items"][0]["id"] != page2.json()["items"][0]["id"]
    assert page2.json()["items"][0]["mealTime"] == "lunch"


@pytest.mark.asyncio
async def test_list_meals_cursor_stable_when_created_at_ties(client, auth_and_db_overrides):
    shared_created_at = datetime(2026, 2, 13, 10, 0, tzinfo=timezone.utc)
    auth_and_db_overrides.meals = [
        {
            **auth_and_db_overrides.meals[0],
            "id": "00000000-0000-0000-0000-0000000000aa",
            "created_at": shared_created_at,
            "meal_time": "breakfast",
        },
        {
            **auth_and_db_overrides.meals[1],
            "id": "00000000-0000-0000-0000-0000000000ab",
            "created_at": shared_created_at,
            "meal_time": "lunch",
        },
    ]

    page1 = await client.get("/v1/meals?limit=1")
    assert page1.status_code == 200
    cursor = page1.json()["nextCursor"]

    page2 = await client.get(f"/v1/meals?limit=1&cursor={cursor}")
    assert page2.status_code == 200

    assert page1.json()["items"][0]["id"] == "00000000-0000-0000-0000-0000000000ab"
    assert page2.json()["items"][0]["id"] == "00000000-0000-0000-0000-0000000000aa"


@pytest.mark.asyncio
async def test_list_meals_pagination_stable_for_same_created_at_with_id_tiebreak(
    client,
    auth_and_db_overrides,
):
    same_created_at = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)
    auth_and_db_overrides.meals = [
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "user_id": MOCK_USER["id"],
            "created_at": same_created_at,
            "meal_time": "breakfast",
            "image_url": "https://img/2.jpg",
            "image_path": None,
            "ai_provider": "openrouter",
            "ai_model": "google/gemini-3.0-flash-preview",
            "ai_confidence": 0.7,
            "result_json": _result_json(200, 10, 8, 20),
        },
        {
            "id": "00000000-0000-0000-0000-000000000003",
            "user_id": MOCK_USER["id"],
            "created_at": same_created_at,
            "meal_time": "lunch",
            "image_url": "https://img/3.jpg",
            "image_path": None,
            "ai_provider": "openrouter",
            "ai_model": "google/gemini-3.0-flash-preview",
            "ai_confidence": 0.7,
            "result_json": _result_json(300, 12, 10, 30),
        },
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "user_id": MOCK_USER["id"],
            "created_at": same_created_at,
            "meal_time": "dinner",
            "image_url": "https://img/1.jpg",
            "image_path": None,
            "ai_provider": "openrouter",
            "ai_model": "google/gemini-3.0-flash-preview",
            "ai_confidence": 0.7,
            "result_json": _result_json(100, 8, 5, 10),
        },
        {
            "id": "00000000-0000-0000-0000-000000000000",
            "user_id": MOCK_USER["id"],
            "created_at": datetime(2026, 2, 1, 11, 59, 59, tzinfo=timezone.utc),
            "meal_time": "snack",
            "image_url": "https://img/0.jpg",
            "image_path": None,
            "ai_provider": "openrouter",
            "ai_model": "google/gemini-3.0-flash-preview",
            "ai_confidence": 0.7,
            "result_json": _result_json(50, 2, 1, 6),
        },
    ]

    page1 = await client.get("/v1/meals?limit=2")
    assert page1.status_code == 200
    cursor = page1.json()["nextCursor"]
    assert isinstance(cursor, str) and cursor

    page2 = await client.get(f"/v1/meals?limit=2&cursor={cursor}")
    assert page2.status_code == 200

    page1_ids = [item["id"] for item in page1.json()["items"]]
    page2_ids = [item["id"] for item in page2.json()["items"]]

    assert page1_ids == [
        "00000000-0000-0000-0000-000000000003",
        "00000000-0000-0000-0000-000000000002",
    ]
    assert page2_ids == [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000000",
    ]
    assert set(page1_ids).isdisjoint(set(page2_ids))


@pytest.mark.asyncio
async def test_list_meals_invalid_cursor_returns_validation_failed(client, auth_and_db_overrides):
    response = await client.get("/v1/meals?cursor=not-a-valid-cursor")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_get_meal_not_owned_returns_not_found(client, auth_and_db_overrides):
    other_meal_id = auth_and_db_overrides.meals[-1]["id"]
    response = await client.get(f"/v1/meals/{other_meal_id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_delete_meal_recalculates_daily_stats_and_returns_payload(client, auth_and_db_overrides):
    meal_to_delete = auth_and_db_overrides.meals[0]
    response = await client.delete(f"/v1/meals/{meal_to_delete['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True
    assert body["mealId"] == meal_to_delete["id"]
    assert body["dailyStats"]["mealsCount"] == 1
    assert body["dailyStats"]["calories_kcal"] == 500

    key = (MOCK_USER["id"], meal_to_delete["created_at"].date())
    assert auth_and_db_overrides.daily_stats[key]["meals_count"] == 1


@pytest.mark.asyncio
async def test_delete_meal_not_owned_returns_not_found(client, auth_and_db_overrides):
    other_meal_id = auth_and_db_overrides.meals[-1]["id"]
    response = await client.delete(f"/v1/meals/{other_meal_id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
