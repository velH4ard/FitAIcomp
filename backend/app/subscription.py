from datetime import datetime, timezone
from typing import Any, Optional


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_effective_subscription_status(
    raw_status: str,
    active_until: Optional[datetime],
    *,
    now: Optional[datetime] = None,
) -> str:
    if raw_status == "blocked":
        return "blocked"

    if active_until is None:
        return "free"

    now_utc = _to_utc(now) if now else datetime.now(timezone.utc)
    active_until_utc = _to_utc(active_until)
    return "active" if active_until_utc >= now_utc else "expired"


def get_daily_limit_for_status(status: str) -> int:
    if status == "blocked":
        return 0
    return 20 if status == "active" else 2


def get_referral_credits(user: dict[str, Any]) -> int:
    raw_value = user.get("referral_credits", 0)
    try:
        credits = int(raw_value)
    except (TypeError, ValueError):
        return 0
    return max(0, credits)


def get_user_daily_limit(user: dict[str, Any], *, now: Optional[datetime] = None) -> int:
    status = get_effective_subscription_status(
        str(user.get("subscription_status") or "free"),
        user.get("subscription_active_until"),
        now=now,
    )
    base_limit = get_daily_limit_for_status(status)
    return base_limit + get_referral_credits(user)


def compute_days_left(active_until: Optional[datetime], *, now: Optional[datetime] = None) -> int:
    if active_until is None:
        return 0

    now_utc = _to_utc(now) if now else datetime.now(timezone.utc)
    active_until_utc = _to_utc(active_until)
    remaining_seconds = (active_until_utc - now_utc).total_seconds()
    if remaining_seconds <= 0:
        return 0
    return max(1, int((remaining_seconds + 86399) // 86400))


def build_subscription_status_view(
    raw_status: str,
    active_until: Optional[datetime],
    *,
    now: Optional[datetime] = None,
) -> tuple[str, Optional[datetime], int, bool]:
    now_utc = _to_utc(now) if now else datetime.now(timezone.utc)
    effective_status = get_effective_subscription_status(raw_status, active_until, now=now_utc)

    if effective_status == "blocked":
        return "blocked", None, 0, False

    if effective_status != "active":
        return "free", None, 0, False

    days_left = compute_days_left(active_until, now=now_utc)
    return "active", active_until, days_left, days_left < 3


def compute_upgrade_hint(subscription_status: str, remaining: int) -> Optional[str]:
    if subscription_status == "active":
        return None
    if remaining == 0:
        return "hard"
    return "soft"
