import json
from typing import Any, Optional


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_gender(value: Any) -> str:
    if str(value).lower() == "male":
        return "male"
    return "female"


def parse_profile(profile: Any) -> dict[str, Any]:
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except Exception:
            return {}
    if isinstance(profile, dict):
        return profile
    return {}


def calculate_daily_goal_auto(profile: Any) -> Optional[int]:
    profile_dict = parse_profile(profile)
    if not profile_dict:
        return None

    age = _safe_int(profile_dict.get("age"))
    height_cm = _safe_float(profile_dict.get("heightCm"))
    weight_kg = _safe_float(profile_dict.get("weightKg"))
    goal = str(profile_dict.get("goal") or "")

    if age is None or height_cm is None or weight_kg is None:
        return None
    if age < 10 or age > 120:
        return None
    if height_cm < 80 or height_cm > 250:
        return None
    if weight_kg < 20 or weight_kg > 400:
        return None

    gender = normalize_gender(profile_dict.get("gender"))
    if gender == "male":
        bmr = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age + 5.0
    else:
        bmr = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age - 161.0

    tdee = bmr * 1.4
    adjustment = 0.0
    if goal == "lose_weight":
        adjustment = -300.0
    elif goal == "gain_weight":
        adjustment = 300.0

    return int(round(tdee + adjustment))


def calculate_daily_goal_legacy(profile: Any) -> Optional[int]:
    profile_dict = parse_profile(profile)
    if not profile_dict:
        return None

    age = _safe_int(profile_dict.get("age"))
    height_cm = _safe_float(profile_dict.get("heightCm"))
    weight_kg = _safe_float(profile_dict.get("weightKg"))
    goal = str(profile_dict.get("goal") or "")

    if age is None or height_cm is None or weight_kg is None:
        return None
    if age < 10 or age > 120:
        return None
    if height_cm < 80 or height_cm > 250:
        return None
    if weight_kg < 20 or weight_kg > 400:
        return None

    gender = normalize_gender(profile_dict.get("gender"))
    if gender == "male":
        bmr = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age + 5.0
    else:
        bmr = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age - 161.0

    tdee = bmr * 1.2
    if goal == "lose_weight":
        daily_goal = tdee - 500.0
    elif goal == "gain_weight":
        daily_goal = tdee + 300.0
    else:
        daily_goal = tdee

    return int(max(1200.0, daily_goal))


def resolve_effective_goal(user: dict[str, Any]) -> Optional[int]:
    override_goal = _safe_int(user.get("daily_goal_override"))
    if override_goal is not None:
        return override_goal

    auto_goal = _safe_int(user.get("daily_goal_auto"))
    if auto_goal is not None and auto_goal > 0:
        return auto_goal

    profile_goal = _safe_int(parse_profile(user.get("profile")).get("dailyGoal"))
    if profile_goal is not None and profile_goal > 0:
        return profile_goal

    fallback_goal = calculate_daily_goal_legacy(user.get("profile"))
    if fallback_goal is not None:
        return fallback_goal

    return calculate_daily_goal_auto(user.get("profile"))
