import json

import pytest

from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.structured_analysis import (
    RU_FALLBACK_WARNING,
    RU_FUZZY_WARNING,
    RU_UNKNOWN_WARNING,
    build_step2_result_from_snapshot,
    resolve_food_candidate,
)
from tests.test_meals_analyze_rfc006 import FakeAnalyzeConn


MOCK_USER = {
    "id": "00000000-0000-0000-0000-00000000f001",
    "telegram_id": 990001,
    "subscription_status": "free",
    "subscription_active_until": None,
    "is_onboarded": True,
    "profile": "{}",
}


class _FakeResolveConn:
    """Fake connection matching actual SQL queries from resolve_food_candidate()."""

    def __init__(
        self,
        *,
        exact_name_row=None,
        alias_row=None,
        trgm_row=None,
        ilike_row=None,
        base_row=None,
    ):
        self.exact_name_row = exact_name_row
        self.alias_row = alias_row
        self.trgm_row = trgm_row
        self.ilike_row = ilike_row
        self.base_row = base_row

    async def fetchrow(self, query, *args):
        if "calories_per_100g IS NOT NULL" in query:
            return self.base_row
        if "WHERE normalized_name = $1" in query:
            return self.exact_name_row
        if "normalized_aliases @>" in query:
            return self.alias_row
        if "WITH candidates AS" in query:
            return self.trgm_row
        if "ILIKE" in query:
            return self.ilike_row
        raise AssertionError(f"Unexpected query in _FakeResolveConn: {query[:100]}")


def _food_row(*, name="плов", calories=180, protein=8, fat=6, carbs=22, score=None, base_name=None, state=None):  # noqa: E501
    row = {
        "id": "food-1",
        "name": name,
        "normalized_name": name.strip().lower().replace("ё", "е"),
        "base_name": base_name,
        "normalized_base_name": base_name.strip().lower().replace("ё", "е") if base_name else None,
        "state": state,
        "calories_per_100g": calories,
        "protein_per_100g": protein,
        "fat_per_100g": fat,
        "carbs_per_100g": carbs,
    }
    if score is not None:
        row["score"] = score
    return row


@pytest.mark.asyncio
async def test_food_matching_exact_name_match_is_deterministic():
    conn = _FakeResolveConn(exact_name_row=_food_row(name="плов"))

    resolved = await resolve_food_candidate(
        conn,
        name="плов",
        ai_match_type="unknown",
        ai_confidence=None,
        ai_nutrition={"calories_kcal": 1, "protein_g": 1, "fat_g": 1, "carbs_g": 1},
        ai_default_weight=250,
        ai_warnings=[],
    )

    assert resolved["name"] == "плов"
    assert resolved["match_type"] == "exact"
    assert resolved["nutrition_per_100g"] == {
        "calories_kcal": 180.0,
        "protein_g": 8.0,
        "fat_g": 6.0,
        "carbs_g": 22.0,
    }
    assert resolved["warnings"] == []


@pytest.mark.asyncio
async def test_food_matching_exact_alias_match_is_deterministic():
    conn = _FakeResolveConn(alias_row=_food_row(name="плов"))

    resolved = await resolve_food_candidate(
        conn,
        name="plov",
        ai_match_type="unknown",
        ai_confidence=None,
        ai_nutrition={"calories_kcal": 1, "protein_g": 1, "fat_g": 1, "carbs_g": 1},
        ai_default_weight=230,
        ai_warnings=[],
    )

    assert resolved["name"] == "плов"
    assert resolved["match_type"] == "exact"


@pytest.mark.asyncio
async def test_food_matching_fuzzy_threshold_035_path():
    conn = _FakeResolveConn(trgm_row=_food_row(name="плов", score=0.35))

    resolved = await resolve_food_candidate(
        conn,
        name="плв",
        ai_match_type="unknown",
        ai_confidence=None,
        ai_nutrition={"calories_kcal": 1, "protein_g": 1, "fat_g": 1, "carbs_g": 1},
        ai_default_weight=None,
        ai_warnings=[],
    )

    assert resolved["name"] == "плов"
    assert resolved["match_type"] == "fuzzy"
    assert RU_FUZZY_WARNING in resolved["warnings"]


@pytest.mark.asyncio
async def test_food_matching_ilike_fallback_path_when_similarity_not_found():
    conn = _FakeResolveConn(
        ilike_row=_food_row(name="плов домашний", calories=170, protein=7, fat=5, carbs=23),
    )

    resolved = await resolve_food_candidate(
        conn,
        name="дом",
        ai_match_type="unknown",
        ai_confidence=None,
        ai_nutrition={"calories_kcal": 99, "protein_g": 99, "fat_g": 99, "carbs_g": 99},
        ai_default_weight=200,
        ai_warnings=[],
    )

    assert resolved["name"] == "плов домашний"
    assert resolved["match_type"] == "fuzzy"
    assert resolved["nutrition_per_100g"]["calories_kcal"] == 170.0
    assert RU_FUZZY_WARNING in resolved["warnings"]


@pytest.mark.asyncio
async def test_food_matching_unknown_path_adds_ru_warning():
    conn = _FakeResolveConn()

    resolved = await resolve_food_candidate(
        conn,
        name="непонятное блюдо",
        ai_match_type="unknown",
        ai_confidence=None,
        ai_nutrition={"calories_kcal": 180, "protein_g": 8, "fat_g": 8, "carbs_g": 18},
        ai_default_weight=None,
        ai_warnings=[],
    )

    assert resolved["name"] == "непонятное блюдо"
    assert resolved["match_type"] == "unknown"
    assert RU_UNKNOWN_WARNING in resolved["warnings"]


@pytest.mark.asyncio
async def test_food_matching_partial_nutrition_uses_fallback_and_warning():
    conn = _FakeResolveConn(
        exact_name_row=_food_row(
            name="плов",
            calories=120,
            protein=None,
            fat=None,
            carbs=None,
        ),
    )

    resolved = await resolve_food_candidate(
        conn,
        name="плов",
        ai_match_type="exact",
        ai_confidence=None,
        ai_nutrition={"calories_kcal": 10, "protein_g": 10, "fat_g": 10, "carbs_g": 10},
        ai_default_weight=210,
        ai_warnings=[],
    )

    # Per spec: no base_name available + partial nutrition -> match_type becomes "unknown"
    assert resolved["match_type"] == "unknown"
    assert resolved["nutrition_per_100g"] == {
        "calories_kcal": 120.0,
        "protein_g": 8.0,
        "fat_g": 8.0,
        "carbs_g": 18.0,
    }
    assert RU_FALLBACK_WARNING in resolved["warnings"]


@pytest.mark.asyncio
async def test_e2e_step1_user_correction_step2_finalize_deterministic():
    snapshot_items = [
        {
            "client_item_id": "item_1",
            "name": "плов",
            "match_type": "exact",
            "confidence": 0.9,
            "nutrition_per_100g": {
                "calories_kcal": 180.0,
                "protein_g": 8.0,
                "fat_g": 6.0,
                "carbs_g": 22.0,
            },
            "warnings": [],
        },
        {
            "client_item_id": "item_2",
            "name": "соус",
            "match_type": "fuzzy",
            "confidence": 0.42,
            "nutrition_per_100g": {
                "calories_kcal": 140.0,
                "protein_g": 2.0,
                "fat_g": 10.0,
                "carbs_g": 9.0,
            },
            "warnings": [RU_FUZZY_WARNING],
        },
    ]
    corrected_weights = {"item_1": 280.0, "item_2": 35.0}

    result = build_step2_result_from_snapshot(snapshot_items, corrected_weights, overall_confidence=0.66)

    assert result["recognized"] is True
    assert result["totals"] == {
        "calories_kcal": 553.0,
        "protein_g": 23.1,
        "fat_g": 20.3,
        "carbs_g": 64.75,
    }
    assert result["items"][0]["grams"] == 280.0
    assert result["items"][1]["grams"] == 35.0
    assert "Расчет выполнен по значениям на 100 г из шага 1." in result["assumptions"]
    assert any("fuzzy match" in warning for warning in result["warnings"])
    assert RU_FUZZY_WARNING in result["warnings"]


@pytest.fixture
def _legacy_overrides():
    fake_conn = FakeAnalyzeConn()
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER
    app.dependency_overrides[get_db] = lambda: fake_conn
    try:
        yield fake_conn
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_legacy_analyze_regression_happy_path(client, monkeypatch, _legacy_overrides):
    async def _fake_analyze_image(*args, **kwargs):
        return json.dumps(
            {
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
                        "name": "плов",
                        "grams": 300,
                        "calories_kcal": 540,
                        "protein_g": 28,
                        "fat_g": 19,
                        "carbs_g": 60,
                        "confidence": 0.62,
                    }
                ],
                "warnings": [],
                "assumptions": [],
            }
        )

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", _fake_analyze_image)

    response = await client.post(
        "/v1/meals/analyze",
        files={"image": ("meal.jpg", b"fake-image", "image/jpeg")},
        headers={"Idempotency-Key": "legacy-regression-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"meal", "usage"}
    assert body["meal"]["result"]["recognized"] is True


@pytest.mark.asyncio
async def test_legacy_analyze_regression_idempotency_replay(client, monkeypatch, _legacy_overrides):
    call_count = {"n": 0}

    async def _fake_analyze_image(*args, **kwargs):
        call_count["n"] += 1
        return json.dumps(
            {
                "recognized": True,
                "overall_confidence": 0.6,
                "totals": {
                    "calories_kcal": 300,
                    "protein_g": 20,
                    "fat_g": 10,
                    "carbs_g": 20,
                },
                "items": [
                    {
                        "name": "рис",
                        "grams": 200,
                        "calories_kcal": 300,
                        "protein_g": 20,
                        "fat_g": 10,
                        "carbs_g": 20,
                        "confidence": 0.6,
                    }
                ],
                "warnings": [],
                "assumptions": [],
            }
        )

    monkeypatch.setattr("app.main.openrouter_client.analyze_image", _fake_analyze_image)

    headers = {"Idempotency-Key": "legacy-regression-2"}
    first = await client.post(
        "/v1/meals/analyze",
        files={"image": ("meal.jpg", b"fake-image", "image/jpeg")},
        headers=headers,
    )
    second = await client.post(
        "/v1/meals/analyze",
        files={"image": ("meal.jpg", b"fake-image", "image/jpeg")},
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert call_count["n"] == 1
