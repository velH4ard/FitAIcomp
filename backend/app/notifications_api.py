from fastapi import APIRouter, Depends
from typing import Literal

from .db import get_db
from .deps import get_current_user
from .premium_access import ensure_premium_access
from .schemas import NotificationSettingsRequest, NotificationSettingsResponse


router = APIRouter(prefix="/v1/notifications", tags=["Notifications"])


@router.patch("/settings", response_model=NotificationSettingsResponse)
async def update_notification_settings(
    payload: NotificationSettingsRequest,
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    ensure_premium_access(user, feature="notifications.settings")

    row = await conn.fetchrow(
        """
        INSERT INTO user_settings (user_id, notifications_enabled, notification_tone)
        VALUES ($1::uuid, $2, COALESCE($3, 'balanced'))
        ON CONFLICT (user_id)
        DO UPDATE SET
            notifications_enabled = EXCLUDED.notifications_enabled,
            notification_tone = COALESCE($3, user_settings.notification_tone),
            updated_at = NOW()
        RETURNING notifications_enabled, notification_tone
        """,
        str(user["id"]),
        bool(payload.enabled),
        payload.tone,
    )
    enabled = bool(row["notifications_enabled"]) if row else bool(payload.enabled)
    raw_tone = row["notification_tone"] if row else (payload.tone or "balanced")
    tone: Literal["soft", "hard", "balanced"] = "balanced"
    if raw_tone in {"soft", "hard", "balanced"}:
        tone = raw_tone
    return NotificationSettingsResponse(enabled=enabled, tone=tone)
