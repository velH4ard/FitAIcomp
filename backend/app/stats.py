from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from .db import fetch_named, fetchrow_named, get_db
from .deps import get_current_user
from .errors import FitAIError
from .schemas import DailyNutritionSummaryResponse, WeeklyStatsDay, WeeklyStatsResponse, WeeklyStatsTotals


router = APIRouter(prefix="/v1/stats", tags=["Stats"])


def _parse_stats_date(raw_date: Optional[str]) -> date:
    if raw_date is None:
        return datetime.now(timezone.utc).date()
    try:
        return date.fromisoformat(raw_date)
    except ValueError as exc:
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"fieldErrors": [{"field": "date", "issue": "must be YYYY-MM-DD"}]},
        ) from exc


def _parse_weekly_end_date(raw_date: Optional[str]) -> date:
    if raw_date is None:
        return datetime.now(timezone.utc).date()
    try:
        return date.fromisoformat(raw_date)
    except ValueError as exc:
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"fieldErrors": [{"field": "endDate", "issue": "must be YYYY-MM-DD"}]},
        ) from exc


@router.get("/daily", response_model=DailyNutritionSummaryResponse)
async def get_daily_stats(
    date_filter: Optional[str] = Query(default=None, alias="date"),
    user=Depends(get_current_user),
    conn=Depends(get_db),
): 
    selected_date = _parse_stats_date(date_filter)
    row = await fetchrow_named(
        conn,
        "stats.daily",
        """
        SELECT
            calories_kcal,
            protein_g,
            fat_g,
            carbs_g,
            meals_count
        FROM daily_stats
        WHERE user_id = $1
          AND date = $2::date
        """,
        user["id"],
        selected_date,
    )
    row_dict = dict(row) if row else {}
    return DailyNutritionSummaryResponse(
        date=selected_date.isoformat(),
        calories_kcal=float(row_dict.get("calories_kcal") or 0),
        protein_g=float(row_dict.get("protein_g") or 0),
        fat_g=float(row_dict.get("fat_g") or 0),
        carbs_g=float(row_dict.get("carbs_g") or 0),
        mealsCount=int(row_dict.get("meals_count") or 0),
    )


@router.get("/weekly", response_model=WeeklyStatsResponse)
async def get_weekly_stats(
    end_date_raw: Optional[str] = Query(default=None, alias="endDate"),
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    end_date = _parse_weekly_end_date(end_date_raw)
    start_date = end_date - timedelta(days=6)
    start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc)

    rows = await fetch_named(
        conn,
        "stats.weekly",
        """
        SELECT
            created_at::date AS date,
            COALESCE(SUM((result_json->'totals'->>'calories_kcal')::double precision), 0) AS calories,
            COALESCE(SUM((result_json->'totals'->>'protein_g')::double precision), 0) AS protein,
            COALESCE(SUM((result_json->'totals'->>'fat_g')::double precision), 0) AS fat,
            COALESCE(SUM((result_json->'totals'->>'carbs_g')::double precision), 0) AS carbs,
            COUNT(*)::int AS meals_count
        FROM meals
        WHERE user_id = $1
          AND created_at >= $2::date
          AND created_at < ($3::date + interval '1 day')
        GROUP BY created_at::date
        ORDER BY created_at::date ASC
        """,
        user["id"],
        start_dt,
        end_dt,
    )

    by_date: dict[date, dict] = {}
    for row in rows:
        row_dict = dict(row)
        by_date[row_dict["date"]] = row_dict

    days: list[WeeklyStatsDay] = []
    totals_calories = 0.0
    totals_protein = 0.0
    totals_fat = 0.0
    totals_carbs = 0.0
    totals_meals_count = 0

    for offset in range(7):
        day = start_date + timedelta(days=offset)
        row_dict = by_date.get(day, {})

        calories = float(row_dict.get("calories") or 0)
        protein = float(row_dict.get("protein") or 0)
        fat = float(row_dict.get("fat") or 0)
        carbs = float(row_dict.get("carbs") or 0)
        meals_count = int(row_dict.get("meals_count") or 0)

        totals_calories += calories
        totals_protein += protein
        totals_fat += fat
        totals_carbs += carbs
        totals_meals_count += meals_count

        days.append(
            WeeklyStatsDay(
                date=day.isoformat(),
                calories_kcal=calories,
                protein_g=protein,
                fat_g=fat,
                carbs_g=carbs,
                mealsCount=meals_count,
            )
        )

    return WeeklyStatsResponse(
        startDate=start_date.isoformat(),
        endDate=end_date.isoformat(),
        days=days,
        totals=WeeklyStatsTotals(
            calories_kcal=totals_calories,
            protein_g=totals_protein,
            fat_g=totals_fat,
            carbs_g=totals_carbs,
            mealsCount=totals_meals_count,
        ),
    )
