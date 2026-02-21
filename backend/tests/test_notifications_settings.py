import pytest
from datetime import datetime, timezone, timedelta

from app.db import get_db
from app.deps import get_current_user
from app.main import app


class NotificationSettingsConn:
    def __init__(self) -> None:
        self.enabled = False
        self.tone = "balanced"

    async def fetchrow(self, query, *args):
        if "INSERT INTO user_settings" in query:
            self.enabled = bool(args[1])
            incoming_tone = args[2]
            if incoming_tone is not None:
                self.tone = incoming_tone
            return {"notifications_enabled": self.enabled, "notification_tone": self.tone}
        return None


@pytest.mark.asyncio
async def test_patch_notifications_settings_toggle(client):
    mock_user = {
        "id": "00000000-0000-0000-0000-00000000aa01",
        "subscription_status": "active",
        "subscription_active_until": datetime.now(timezone.utc) + timedelta(days=30),
    }
    conn = NotificationSettingsConn()

    async def override_get_db():
        yield conn

    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[get_db] = override_get_db

    try:
        enable_response = await client.patch("/v1/notifications/settings", json={"enabled": True})
        assert enable_response.status_code == 200
        assert enable_response.json() == {"enabled": True, "tone": "balanced"}

        disable_response = await client.patch("/v1/notifications/settings", json={"enabled": False})
        assert disable_response.status_code == 200
        assert disable_response.json() == {"enabled": False, "tone": "balanced"}

        hard_response = await client.patch("/v1/notifications/settings", json={"enabled": True, "tone": "hard"})
        assert hard_response.status_code == 200
        assert hard_response.json() == {"enabled": True, "tone": "hard"}

        preserve_response = await client.patch("/v1/notifications/settings", json={"enabled": False})
        assert preserve_response.status_code == 200
        assert preserve_response.json() == {"enabled": False, "tone": "hard"}
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_patch_notifications_settings_validation_error(client):
    mock_user = {
        "id": "00000000-0000-0000-0000-00000000aa01",
        "subscription_status": "active",
        "subscription_active_until": datetime.now(timezone.utc) + timedelta(days=30),
    }

    app.dependency_overrides[get_current_user] = lambda: mock_user
    try:
        response = await client.patch("/v1/notifications/settings", json={"enabled": "yes"})
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
