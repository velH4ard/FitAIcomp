from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from .db import fetch_named, get_db
from .deps import get_current_user
from .goals import resolve_effective_goal
from .schemas import StreakResponse
from .streak_logic import calculate_streak_metrics


router = APIRouter(prefix="/v1/streak", tags=["Streak"])


@router.get("", response_model=StreakResponse)
async def get_streak(
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    """
    Get user's streak information for calorie goal completion.
    
    A day is considered "completed" if total calories >= 70% of user's daily goal.
    """
    effective_goal = resolve_effective_goal(user)
    if effective_goal is None:
        return StreakResponse(
            currentStreak=0,
            bestStreak=0,
            lastCompletedDate=None,
        )

    # Fetch all daily_stats for this user ordered by date ASC
    rows = await fetch_named(
        conn,
        "streak.all_stats",
        """
        SELECT date, calories_kcal
        FROM daily_stats
        WHERE user_id = $1
        ORDER BY date ASC
        """,
        user["id"],
    )
    
    if not rows:
        return StreakResponse(
            currentStreak=0,
            bestStreak=0,
            lastCompletedDate=None,
        )
    
    stats = [dict(row) for row in rows]
    today = datetime.now(timezone.utc).date()
    current_streak, best_streak, last_completed_date = calculate_streak_metrics(
        stats,
        today=today,
        effective_goal=effective_goal,
    )
    
    return StreakResponse(
        currentStreak=current_streak,
        bestStreak=best_streak,
        lastCompletedDate=last_completed_date.isoformat() if last_completed_date else None,
    )
