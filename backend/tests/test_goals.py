from app.goals import calculate_daily_goal_auto, resolve_effective_goal


def test_calculate_daily_goal_auto_fallback_for_legacy_other_gender():
    profile = {
        "gender": "other",
        "age": 30,
        "heightCm": 170,
        "weightKg": 70,
        "goal": "maintain",
    }
    value = calculate_daily_goal_auto(profile)
    assert value is not None
    assert value > 0


def test_resolve_effective_goal_prefers_override_then_auto():
    user = {"daily_goal_override": 2500, "daily_goal_auto": 2100, "profile": {}}
    assert resolve_effective_goal(user) == 2500

    user = {"daily_goal_override": None, "daily_goal_auto": 2100, "profile": {}}
    assert resolve_effective_goal(user) == 2100
