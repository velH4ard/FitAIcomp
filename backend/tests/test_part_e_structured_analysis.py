import json
from datetime import date, timedelta

import pytest

from app.deps import get_current_user
from app.errors import FitAIError
from app.integrations.openrouter import OpenRouterClient
from app.main import app


MOCK_USER = {
    "id": "00000000-0000-0000-0000-00000000e001",
    "telegram_id": 900001,
    "subscription_status": "free",
    "subscription_active_until": None,
    "is_onboarded": True,
    "profile": "{}",
}

OTHER_USER = {
    "id": "00000000-0000-0000-0000-00000000e002",
    "telegram_id": 900002,
    "subscription_status": "free",
    "subscription_active_until": None,
    "is_onboarded": True,
    "profile": "{}",
}


STEP1_VALID_MODEL_OUTPUT = {
    "recognized": True,
    "overall_confidence": 0.82,
    "items": [
        {
            "name": "плов",
            "match_type": "exact",
            "confidence": 0.88,
            "nutrition_per_100g": {
                "calories_kcal": 180,
                "protein_g": 8.5,
                "fat_g": 6.0,
                "carbs_g": 22.0,
            },
            "default_weight_g": 250,
            "warnings": [],
        },
        {
            "name": "соус",
            "match_type": "fuzzy",
            "confidence": 0.42,
            "nutrition_per_100g": {
                "calories_kcal": 140,
                "protein_g": 2.0,
                "fat_g": 10.0,
                "carbs_g": 9.0,
            },
            "default_weight_g": None,
            "warnings": ["Нет точного совпадения, использована приблизительная категория."],
        },
    ],
    "warnings": ["Проверьте вес перед подтверждением."],
}


def _has_route(path: str, method: str) -> bool:
    method_upper = method.upper()
    for route in app.router.routes:
        if getattr(route, "path", None) != path:
            continue
        route_methods = getattr(route, "methods", set()) or set()
        if method_upper in route_methods:
            return True
    return False


def _require_endpoint(path: str, method: str) -> None:
    assert _has_route(path=path, method=method), (
        f"Missing endpoint {method.upper()} {path}. "
        "Spec requires two-step analysis workflow and foods search."
    )


def _assert_fitai_error(response, *, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    payload = response.json()
    assert "error" in payload
    assert payload["error"]["code"] == code
    assert "message" in payload["error"]
    assert "details" in payload["error"]


@pytest.fixture
def auth_user_override():
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_step1_valid_response_and_schema_conformance(client, monkeypatch, auth_user_override):
    _require_endpoint("/v1/meals/analysis-step1", "POST")

    async def _fake_classify_step1_items(*args, **kwargs):
        class _Obj:
            def model_dump(self_nonlocal):
                return STEP1_VALID_MODEL_OUTPUT

        return _Obj()

    monkeypatch.setattr("app.main.openrouter_client.classify_step1_items", _fake_classify_step1_items)

    response = await client.post(
        "/v1/meals/analysis-step1",
        files={"image": ("meal.jpg", b"fake-image", "image/jpeg")},
    )

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["analysisSessionId"], str)
    assert body["recognized"] is True
    assert 0 <= body["overallConfidence"] <= 1
    assert isinstance(body["warnings"], list)
    assert isinstance(body["expiresAt"], str)
    assert isinstance(body["items"], list)
    assert len(body["items"]) == 2

    first = body["items"][0]
    assert first["name"] == "плов"
    assert first["matchType"] in {"exact", "fuzzy", "unknown"}
    assert first["nutritionPer100g"]["calories_kcal"] >= 0


@pytest.mark.asyncio
async def test_step1_invalid_ai_schema_returns_validation_failed(client, monkeypatch, auth_user_override):
    _require_endpoint("/v1/meals/analysis-step1", "POST")

    async def _fake_classify_step1_items(*args, **kwargs):
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"schema": "step1-classifier", "issue": "missing required fields"},
        )

    monkeypatch.setattr("app.main.openrouter_client.classify_step1_items", _fake_classify_step1_items)

    response = await client.post(
        "/v1/meals/analysis-step1",
        files={"image": ("meal.jpg", b"fake-image", "image/jpeg")},
    )

    _assert_fitai_error(response, status_code=400, code="VALIDATION_FAILED")


@pytest.mark.asyncio
async def test_foods_search_fuzzy_pg_trgm_path_returns_known_food(client, auth_user_override):
    _require_endpoint("/v1/foods/search", "GET")

    response = await client.get("/v1/foods/search", params={"q": "плв"})
    assert response.status_code == 200
    payload = response.json()
    items = payload["items"] if isinstance(payload, dict) and "items" in payload else payload
    assert any("плов" in str(item.get("name", "")).lower() for item in items if isinstance(item, dict))


@pytest.mark.asyncio
async def test_foods_search_ilike_fallback_path_returns_known_food(client, auth_user_override):
    _require_endpoint("/v1/foods/search", "GET")

    response = await client.get("/v1/foods/search", params={"q": "пло"})
    assert response.status_code == 200
    payload = response.json()
    items = payload["items"] if isinstance(payload, dict) and "items" in payload else payload
    assert any("плов" in str(item.get("name", "")).lower() for item in items if isinstance(item, dict))


@pytest.mark.asyncio
async def test_step2_aggregation_correctness(client, auth_user_override):
    from datetime import datetime, timedelta, timezone
    from app.main import ANALYSIS_SESSION_CACHE

    _require_endpoint("/v1/meals/analysis-step2", "POST")

    session_id = "11111111-1111-1111-1111-111111111111"
    now = datetime.now(timezone.utc)
    ANALYSIS_SESSION_CACHE[session_id] = {
        "id": session_id,
        "user_id": MOCK_USER["id"],
        "recognized": True,
        "overall_confidence": 0.74,
        "items": [
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
                "default_weight_g": 250,
                "warnings": [],
                "metadata": {},
                "original_name": "плов",
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
                "default_weight_g": None,
                "warnings": ["Нет точного совпадения, использована приблизительная категория."],
                "metadata": {},
                "original_name": "соус",
            },
        ],
        "warnings": [],
        "image_path": f"analysis-step1/{MOCK_USER['id']}/{session_id}.bin",
        "created_at": now,
        "expires_at": now + timedelta(minutes=15),
        "consumed": False,
    }
    try:
        response = await client.post(
            "/v1/meals/analysis-step2",
            json={
                "analysisSessionId": session_id,
                "mealTime": "lunch",
                "items": [
                    {"clientItemId": "item_1", "weight_g": 280},
                    {"clientItemId": "item_2", "weight_g": 35},
                ],
            },
            headers={"Idempotency-Key": "step2-agg-1"},
        )
        print("STATUS:", response.status_code); print("USER:", MOCK_USER["id"]); print("CACHE_ID:", id(ANALYSIS_SESSION_CACHE))
        print("BODY:", response.text)
    finally:
        ANALYSIS_SESSION_CACHE.pop(session_id, None)

    assert response.status_code == 200
    body = response.json()
    result = body["meal"]["result"]
    items = result["items"]
    assert len(items) == 2

    calories_sum = round(sum(float(item["calories_kcal"]) for item in items), 2)
    protein_sum = round(sum(float(item["protein_g"]) for item in items), 2)
    fat_sum = round(sum(float(item["fat_g"]) for item in items), 2)
    carbs_sum = round(sum(float(item["carbs_g"]) for item in items), 2)

    assert float(result["totals"]["calories_kcal"]) == calories_sum
    assert float(result["totals"]["protein_g"]) == protein_sum
    assert float(result["totals"]["fat_g"]) == fat_sum
    assert float(result["totals"]["carbs_g"]) == carbs_sum


@pytest.mark.asyncio
async def test_step1_to_step2_e2e_canonical_meal_usage_and_increment_once(client, monkeypatch, auth_user_override):
    _require_endpoint("/v1/meals/analysis-step1", "POST")
    _require_endpoint("/v1/meals/analysis-step2", "POST")

    async def _fake_classify_step1_items(*args, **kwargs):
        class _Obj:
            def model_dump(self_nonlocal):
                return STEP1_VALID_MODEL_OUTPUT

        return _Obj()

    monkeypatch.setattr("app.main.openrouter_client.classify_step1_items", _fake_classify_step1_items)

    step1_resp = await client.post(
        "/v1/meals/analysis-step1",
        files={"image": ("meal.jpg", b"fake-image", "image/jpeg")},
    )
    assert step1_resp.status_code == 200
    session_id = step1_resp.json()["analysisSessionId"]

    idem = "step2-e2e-idem"
    step2_resp_first = await client.post(
        "/v1/meals/analysis-step2",
        json={
            "analysisSessionId": session_id,
            "mealTime": "lunch",
            "items": [
                {"clientItemId": "item_1", "weight_g": 250},
                {"clientItemId": "item_2", "weight_g": 30},
            ],
        },
        headers={"Idempotency-Key": idem},
    )
    assert step2_resp_first.status_code == 200

    first_body = step2_resp_first.json()
    assert set(first_body.keys()) == {"meal", "usage"}
    assert set(first_body["meal"].keys()) >= {"id", "createdAt", "mealTime", "imageUrl", "ai", "result"}
    assert set(first_body["usage"].keys()) >= {
        "date",
        "dailyLimit",
        "photosUsed",
        "remaining",
        "subscriptionStatus",
    }

    step2_resp_second = await client.post(
        "/v1/meals/analysis-step2",
        json={
            "analysisSessionId": session_id,
            "mealTime": "lunch",
            "items": [
                {"clientItemId": "item_1", "weight_g": 250},
                {"clientItemId": "item_2", "weight_g": 30},
            ],
        },
        headers={"Idempotency-Key": idem},
    )
    assert step2_resp_second.status_code == 200
    second_body = step2_resp_second.json()

    assert second_body["meal"]["id"] == first_body["meal"]["id"]
    assert second_body["usage"]["photosUsed"] == first_body["usage"]["photosUsed"]


@pytest.mark.asyncio
async def test_step2_expired_or_not_owned_session_returns_not_found(client):
    _require_endpoint("/v1/meals/analysis-step2", "POST")

    app.dependency_overrides[get_current_user] = lambda: OTHER_USER
    try:
        response = await client.post(
            "/v1/meals/analysis-step2",
            json={
                "analysisSessionId": "22222222-2222-2222-2222-222222222222",
                "mealTime": "dinner",
                "items": [{"clientItemId": "item_1", "weight_g": 200}],
            },
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    _assert_fitai_error(response, status_code=404, code="NOT_FOUND")


@pytest.mark.asyncio
async def test_part_e_openrouter_step1_classifier_deterministic_success(monkeypatch):
    expected = {
        "recognized": True,
        "overall_confidence": 0.81,
        "items": [
            {
                "name": "плов",
                "match_type": "exact",
                "confidence": 0.81,
                "nutrition_per_100g": {
                    "calories_kcal": 180,
                    "protein_g": 8.0,
                    "fat_g": 6.0,
                    "carbs_g": 22.0,
                },
                "default_weight_g": 250,
                "warnings": [],
            },
            {
                "name": "салат",
                "match_type": "fuzzy",
                "confidence": 0.44,
                "nutrition_per_100g": {
                    "calories_kcal": 90,
                    "protein_g": 2.1,
                    "fat_g": 5.2,
                    "carbs_g": 8.0,
                },
                "default_weight_g": None,
                "warnings": ["Нет точного совпадения, использована приблизительная категория."],
            },
        ],
        "warnings": ["Проверьте названия и вес перед подтверждением."],
    }

    class _DummyResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(expected, ensure_ascii=False)}}]}

    class _DummyAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            return _DummyResponse()

    monkeypatch.setattr("app.integrations.openrouter.httpx.AsyncClient", lambda *args, **kwargs: _DummyAsyncClient())

    client = OpenRouterClient()
    one = await client.classify_step1_items(
        image_bytes=b"img",
        content_type="image/jpeg",
        description="plate",
    )
    two = await client.classify_step1_items(
        image_bytes=b"img",
        content_type="image/jpeg",
        description="plate",
    )

    assert one.model_dump() == expected
    assert two.model_dump() == expected


@pytest.mark.asyncio
async def test_part_e_openrouter_step1_classifier_invalid_schema_returns_validation_failed(monkeypatch):
    invalid_payload = {"recognized": True}

    class _DummyResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(invalid_payload)}}]}

    class _DummyAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            return _DummyResponse()

    monkeypatch.setattr("app.integrations.openrouter.httpx.AsyncClient", lambda *args, **kwargs: _DummyAsyncClient())

    client = OpenRouterClient()
    with pytest.raises(FitAIError) as exc_info:
        await client.classify_step1_items(
            image_bytes=b"img",
            content_type="image/jpeg",
            description=None,
        )

    err = exc_info.value
    assert err.code == "VALIDATION_FAILED"
    assert err.status_code == 400
    assert err.details.get("schema") == "step1-classifier"
