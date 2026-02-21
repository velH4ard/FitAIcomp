from fastapi import APIRouter, Depends

from .config import settings
from .db import get_db
from .deps import get_current_user
from .events import write_event_best_effort
from .payments import _emit_subscription_expiring_soon_once_per_day, get_now_utc
from .schemas import PaywallContextResponse
from .subscription import (
    compute_upgrade_hint,
    build_subscription_status_view,
    get_effective_subscription_status,
    get_referral_credits,
    get_user_daily_limit,
)


router = APIRouter(prefix="/v1/paywall", tags=["Paywall"])


async def _try_insert_user_daily_flag(conn, user_id: str, flag: str) -> bool:
    row = await conn.fetchrow(
        """
        INSERT INTO user_daily_flags (user_id, flag, date)
        VALUES ($1::uuid, $2, CURRENT_DATE)
        ON CONFLICT (user_id, flag, date) DO NOTHING
        RETURNING user_id
        """,
        user_id,
        flag,
    )
    return row is not None


async def _emit_referral_bonus_available_once_per_day(conn, user_id: str, referral_credits: int) -> None:
    try:
        inserted = await _try_insert_user_daily_flag(
            conn,
            user_id=str(user_id),
            flag="referral_bonus_available_shown",
        )
    except Exception:
        return

    if not inserted:
        return

    await write_event_best_effort(
        conn,
        event_type="referral_bonus_available_shown",
        user_id=str(user_id),
        payload={"referralCredits": int(max(0, referral_credits))},
    )


@router.get("/context", response_model=PaywallContextResponse)
async def get_paywall_context(
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    now_utc = get_now_utc()
    today = now_utc.date()

    usage_subscription_status = get_effective_subscription_status(
        user["subscription_status"],
        user["subscription_active_until"],
        now=now_utc,
    )
    daily_limit = get_user_daily_limit(user, now=now_utc)
    referral_credits = get_referral_credits(user)

    usage_row = await conn.fetchrow(
        "SELECT photos_used FROM usage_daily WHERE user_id = $1 AND date = $2",
        user["id"],
        today,
    )
    photos_used = int(usage_row["photos_used"]) if usage_row else 0
    remaining = max(0, daily_limit - photos_used)

    _, _, days_left, will_expire_soon = build_subscription_status_view(
        user["subscription_status"],
        user["subscription_active_until"],
        now=now_utc,
    )

    upgrade_hint = compute_upgrade_hint(usage_subscription_status, remaining)
    has_referral_bonus_available = usage_subscription_status in {"free", "expired"} and referral_credits > 0

    if usage_subscription_status == "blocked":
        reason = "quota_reached" if remaining == 0 else "none"
    elif remaining == 0:
        reason = "quota_reached"
    elif usage_subscription_status == "active" and will_expire_soon:
        reason = "expiring_soon"
    elif has_referral_bonus_available:
        reason = "referral_bonus_available"
    elif upgrade_hint == "soft":
        reason = "soft_hint"
    else:
        reason = "none"

    if usage_subscription_status == "active" and days_left < 3:
        try:
            await _emit_subscription_expiring_soon_once_per_day(
                conn,
                user_id=str(user["id"]),
                days_left=days_left,
                active_until=user["subscription_active_until"],
                now_utc=now_utc,
            )
        except Exception:
            pass

    if reason == "referral_bonus_available":
        try:
            await _emit_referral_bonus_available_once_per_day(
                conn,
                user_id=str(user["id"]),
                referral_credits=referral_credits,
            )
        except Exception:
            pass

    return PaywallContextResponse(
        reason=reason,
        subscriptionStatus=usage_subscription_status,
        daysLeft=days_left,
        dailyLimit=daily_limit,
        remaining=remaining,
        recommendedPlan="monthly",
        priceRub=settings.SUBSCRIPTION_PRICE_RUB,
        priceOriginalRub=1499,
        priceCurrentRub=499,
    )
