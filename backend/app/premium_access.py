from .errors import FitAIError
from .subscription import get_effective_subscription_status


PAYWALL_PRICES = {"original": 1499, "current": 499}


def ensure_premium_access(user: dict, *, feature: str) -> None:
    status = get_effective_subscription_status(
        str(user.get("subscription_status") or "free"),
        user.get("subscription_active_until"),
    )
    if status == "active":
        return

    raise FitAIError(
        code="PAYWALL_BLOCKED",
        message="Доступно только в Premium",
        status_code=402,
        details={"feature": feature, "prices": PAYWALL_PRICES},
    )
