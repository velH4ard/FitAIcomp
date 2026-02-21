import json

import pytest

from app.integrations.openrouter import OpenRouterClient


class _DummyResponse:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": json.dumps({"ok": True})}}]}


@pytest.mark.asyncio
async def test_openrouter_prompt_includes_user_notes_only_when_description_present(monkeypatch):
    captured_payload = {}

    class _DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            captured_payload["json"] = json
            return _DummyResponse()

    monkeypatch.setattr("app.integrations.openrouter.httpx.AsyncClient", _DummyAsyncClient)

    client = OpenRouterClient()
    await client.analyze_image(
        image_bytes=b"img",
        content_type="image/jpeg",
        schema_hint={"type": "object"},
        description="notes from user",
    )

    content = captured_payload["json"]["messages"][1]["content"]
    text_parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
    assert any(part == "User notes: notes from user" for part in text_parts)
    assert not any("Additional user context:" in part for part in text_parts)


@pytest.mark.asyncio
async def test_openrouter_prompt_omits_user_notes_when_description_absent(monkeypatch):
    captured_payload = {}

    class _DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            captured_payload["json"] = json
            return _DummyResponse()

    monkeypatch.setattr("app.integrations.openrouter.httpx.AsyncClient", _DummyAsyncClient)

    client = OpenRouterClient()
    await client.analyze_image(
        image_bytes=b"img",
        content_type="image/jpeg",
        schema_hint={"type": "object"},
        description=None,
    )

    content = captured_payload["json"]["messages"][1]["content"]
    text_parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
    assert not any(part.startswith("User notes:") for part in text_parts)
