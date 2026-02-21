import pytest


@pytest.mark.asyncio
async def test_request_id_generated_when_header_missing(client):
    response = await client.get("/health")

    assert response.status_code == 200
    request_id = response.headers.get("X-Request-Id")
    assert request_id is not None
    assert request_id.strip() != ""


@pytest.mark.asyncio
async def test_request_id_echoed_when_header_provided(client):
    response = await client.get("/health", headers={"X-Request-Id": "abc"})

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id") == "abc"


@pytest.mark.asyncio
async def test_request_id_present_on_unauthorized_error(client):
    response = await client.get("/v1/usage/today")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    request_id = response.headers.get("X-Request-Id")
    assert request_id is not None
    assert request_id.strip() != ""
