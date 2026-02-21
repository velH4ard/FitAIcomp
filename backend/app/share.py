"""
Share data endpoint for generating share screen content.

Returns combined data: streak, today's calories, daily goal.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends

from .db import fetch_named, get_db
from .deps import get_current_user
from .schemas import ShareDataResponse

router = APIRouter(prefix="/v1/share-data", tags=["Share"])


def _calculate_daily_goal(profile: Optional[dict]) -> int:
    """
    Calculate daily calorie goal from user profile using Mifflin-St Jeor equation.
    
    Returns 2000 as default if profile is incomplete.
    """
    DEFAULT_GOAL = 2000
    
    if not profile:
        return DEFAULT_GOAL
    
    gender = profile.get("gender")
    age = profile.get("age")
    height_cm = profile.get("heightCm")
    weight_kg = profile.get("weightKg")
    goal = profile.get("goal")
    
    # Check all required fields are present
    if not all([gender, age is not None, height_cm, weight_kg]):
        return DEFAULT_GOAL
    
    # Cast to proper types after validation
    try:
        age_val = int(age)
        height_val = float(height_cm)
        weight_val = float(weight_kg)
    except (TypeError, ValueError):
        return DEFAULT_GOAL
    
    # Validate ranges
    if age_val < 10 or age_val > 120:
        return DEFAULT_GOAL
    if height_val < 80 or height_val > 250:
        return DEFAULT_GOAL
    if weight_val < 20 or weight_val > 400:
        return DEFAULT_GOAL
    
    # Mifflin-St Jeor BMR calculation
    if gender == "male":
        bmr = 10 * weight_val + 6.25 * height_val - 5 * age_val + 5
    else:  # female or other
        bmr = 10 * weight_val + 6.25 * height_val - 5 * age_val - 161
    
    # Activity multiplier (assuming moderate for share screen)
    tdee = bmr * 1.55
    
    # Adjust based on goal
    if goal == "lose_weight":
        daily_goal = tdee - 500  # ~0.5kg/week deficit
    elif goal == "gain_weight":
        daily_goal = tdee + 300  # mild surplus
    else:  # maintain
        daily_goal = tdee
    
    # Round to nearest 50 and clamp between 1200-4000
    daily_goal = round(daily_goal / 50) * 50
    daily_goal = max(1200, min(4000, daily_goal))
    
    return int(daily_goal)


def _is_consecutive(prev_date: date, curr_date: date) -> bool:
    """Check if curr_date is exactly one day after prev_date."""
    return (curr_date - prev_date).days == 1


@router.get("", response_model=ShareDataResponse)
async def get_share_data(
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    """
    Get combined data for share screen.
    
    Returns streak info, today's calories, and calculated daily goal.
    """
    today = datetime.now(timezone.utc).date()
    
    # Get user profile for daily goal calculation
    profile = user.get("profile")
    daily_goal = _calculate_daily_goal(profile)
    threshold = daily_goal * 0.7
    
    # Fetch today's stats
    today_row = await conn.fetchrow(
        """
        SELECT calories_kcal
        FROM daily_stats
        WHERE user_id = $1 AND date = $2
        """,
        user["id"],
        today,
    )
    today_calories = float(today_row["calories_kcal"]) if today_row else 0.0
    
    # Fetch all daily_stats for streak calculation
    rows = await fetch_named(
        conn,
        "share.all_stats",
        """
        SELECT date, calories_kcal
        FROM daily_stats
        WHERE user_id = $1
        ORDER BY date ASC
        """,
        user["id"],
    )
    
    # Default streak values
    current_streak = 0
    best_streak = 0
    
    if rows:
        # Convert to list of dicts for processing
        stats = [dict(row) for row in rows]
        
        # Build a dict for quick lookup
        stats_by_date: dict[date, float] = {}
        for stat in stats:
            stat_date = stat["date"]
            if isinstance(stat_date, datetime):
                stat_date = stat_date.date()
            stats_by_date[stat_date] = float(stat.get("calories_kcal") or 0)
        
        # Calculate current streak (from today backwards)
        # Edge case: if today has no entry yet, currentStreak = 0
        if today in stats_by_date:
            check_date = today
            prev_date_in_streak: Optional[date] = None
            
            while True:
                if check_date not in stats_by_date:
                    # Gap in the streak - stop counting
                    break
                
                calories = stats_by_date[check_date]
                
                if calories >= threshold:
                    if prev_date_in_streak is None:
                        # First completed day (today)
                        current_streak = 1
                    elif _is_consecutive(check_date, prev_date_in_streak):
                        # Consecutive day
                        current_streak += 1
                    else:
                        # Gap in dates - streak broken
                        break
                    
                    prev_date_in_streak = check_date
                    check_date -= timedelta(days=1)
                else:
                    # Didn't meet threshold - streak broken
                    break
        
        # Calculate best streak (scan all history)
        current_run = 0
        prev_completed_date: Optional[date] = None
        
        for stat in stats:
            stat_date = stat["date"]
            if isinstance(stat_date, datetime):
                stat_date = stat_date.date()
            
            calories = float(stat.get("calories_kcal") or 0)
            
            if calories >= threshold:
                if prev_completed_date is None:
                    # First completed day ever
                    current_run = 1
                elif _is_consecutive(prev_completed_date, stat_date):
                    # Consecutive day
                    current_run += 1
                else:
                    # Gap - start new run
                    current_run = 1
                
                best_streak = max(best_streak, current_run)
                prev_completed_date = stat_date
            else:
                # Didn't meet threshold - reset
                current_run = 0
                prev_completed_date = None
    
    return ShareDataResponse(
        streak=current_streak,
        bestStreak=best_streak,
        todayCalories=today_calories,
        dailyGoal=daily_goal,
        date=today.isoformat(),
    )
