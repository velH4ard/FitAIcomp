import uuid

import pytest


@pytest.mark.asyncio
async def test_request_id_generated_when_missing(client):
    response = await client.get("/health")

    assert response.status_code == 200
    request_id = response.headers.get("X-Request-Id")
    assert request_id
    uuid.UUID(request_id)


@pytest.mark.asyncio
async def test_request_id_reused_when_valid_header_provided(client):
    request_id = "req-observability-123"
    response = await client.get("/health", headers={"X-Request-Id": request_id})

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id") == request_id


@pytest.mark.asyncio
async def test_request_id_invalid_header_returns_validation_failed(client):
    response = await client.get("/health", headers={"X-Request-Id": "   "})

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert response.headers.get("X-Request-Id")


@pytest.mark.asyncio
async def test_request_id_present_on_error_response(client):
    request_id = "req-unauthorized-1"
    response = await client.get("/v1/me", headers={"X-Request-Id": request_id})

    assert response.status_code == 401
    assert response.headers.get("X-Request-Id") == request_id
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
