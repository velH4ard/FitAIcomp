import importlib
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.errors import FitAIError


@pytest_asyncio.fixture
async def strict_mode_client(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "fake_bot_token")
    monkeypatch.setenv("JWT_SECRET", "fake_jwt_secret")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("CORS_ALLOW_ORIGIN_REGEX", "")

    config_module = importlib.import_module("app.config")
    importlib.reload(config_module)
    main_module = importlib.import_module("app.main")
    main_module = importlib.reload(main_module)

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, main_module

    # Restore default test module state after reload in strict-mode tests.
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://localhost:5174,http://localhost:8000")
    monkeypatch.setenv("CORS_ALLOW_ORIGIN_REGEX", r"^https://[-a-z0-9]+\.trycloudflare\.com$")
    importlib.reload(config_module)
    importlib.reload(main_module)


def _assert_fitai_error_envelope(payload: dict, code: str) -> None:
    assert "error" in payload
    assert payload["error"]["code"] == code
    assert "message" in payload["error"]
    assert "details" in payload["error"]


@pytest.mark.asyncio
async def test_cors_preflight_allowed_origin_includes_expected_headers(strict_mode_client):
    client, _ = strict_mode_client

    response = await client.options(
        "/v1/me",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )

    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") == "https://app.example.com"
    assert "GET" in response.headers.get("access-control-allow-methods", "")


@pytest.mark.asyncio
async def test_cors_preflight_disallowed_origin_has_no_allow_origin_in_production_strict(strict_mode_client):
    client, _ = strict_mode_client

    response = await client.options(
        "/v1/me",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code in (200, 400, 204)
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_auth_telegram_invalid_initdata_returns_fitai_error_envelope(strict_mode_client):
    client, main_module = strict_mode_client

    with patch.object(main_module, "verify_telegram_init_data") as mock_verify:
        mock_verify.side_effect = FitAIError(
            code="AUTH_INVALID_INITDATA",
            message="Invalid initData",
            status_code=401,
        )

        response = await client.post("/v1/auth/telegram", json={"initData": "invalid"})

    assert response.status_code == 401
    _assert_fitai_error_envelope(response.json(), "AUTH_INVALID_INITDATA")
    assert response.headers.get("X-Request-Id")


@pytest.mark.asyncio
async def test_protected_endpoint_without_token_returns_unauthorized_envelope(strict_mode_client):
    client, _ = strict_mode_client

    response = await client.get("/v1/me")

    assert response.status_code == 401
    _assert_fitai_error_envelope(response.json(), "UNAUTHORIZED")
    assert response.headers.get("X-Request-Id")
