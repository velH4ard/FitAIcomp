import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from .db import fetch_named, get_db
from .deps import get_current_user
from .errors import FitAIError
from .goals import resolve_effective_goal
from .premium_access import ensure_premium_access
from .schemas import (
    MonthlyReportAggregates,
    MonthlyReportResponse,
    WeeklyReportDay,
    WeeklyReportResponse,
    WeeklyReportTotals,
    WeeklyWeightForecast,
    WeightChartItem,
    WeightChartResponse,
    WhyNotLosingInsight,
    WhyNotLosingResponse,
)


router = APIRouter(prefix="/v1", tags=["Premium"])


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _resolve_goal_or_default(user: dict) -> int:
    resolved = resolve_effective_goal(user)
    return int(resolved) if resolved is not None and int(resolved) > 0 else 2000


async def _load_daily_calories_map(conn, user_id: str, start_date: date, end_date: date) -> dict[date, float]:
    rows = await fetch_named(
        conn,
        "premium.daily_calories_range",
        """
        SELECT date, calories_kcal
        FROM daily_stats
        WHERE user_id = $1
          AND date >= $2
          AND date <= $3
        """,
        user_id,
        start_date,
        end_date,
    )
    return {row["date"]: float(row["calories_kcal"] or 0) for row in rows}


@router.get("/reports/weekly", response_model=WeeklyReportResponse)
async def get_weekly_report(
    end_date_query: Optional[date] = Query(default=None, alias="endDate"),
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    ensure_premium_access(user, feature="reports.weekly")

    end_date = end_date_query or _utc_today()
    start_date = end_date - timedelta(days=6)
    goal = _resolve_goal_or_default(user)

    calories_by_date = await _load_daily_calories_map(conn, str(user["id"]), start_date, end_date)
    days: list[WeeklyReportDay] = []
    total_calories = 0.0
    total_goal = 0.0
    deficit_days = 0
    surplus_days = 0
    balanced_days = 0

    for offset in range(7):
        day = start_date + timedelta(days=offset)
        calories = float(calories_by_date.get(day, 0.0))
        goal_for_day = float(goal)
        delta = calories - goal_for_day

        if delta < 0:
            balance = "deficit"
            deficit_days += 1
        elif delta > 0:
            balance = "surplus"
            surplus_days += 1
        else:
            balance = "balanced"
            balanced_days += 1

        days.append(
            WeeklyReportDay(
                date=day.isoformat(),
                calories_kcal=round(calories, 2),
                goalCalories_kcal=round(goal_for_day, 2),
                deltaCalories_kcal=round(delta, 2),
                balance=balance,
            )
        )
        total_calories += calories
        total_goal += goal_for_day

    period_delta_kg = (total_calories - total_goal) / 7700.0
    profile = user.get("profile") if isinstance(user, dict) else {}
    projected_weight = None
    if isinstance(profile, dict):
        try:
            projected_weight = float(profile.get("weightKg")) + period_delta_kg
        except (TypeError, ValueError):
            projected_weight = None
    if projected_weight is None:
        projected_weight = period_delta_kg

    return WeeklyReportResponse(
        startDate=start_date.isoformat(),
        endDate=end_date.isoformat(),
        days=days,
        totals=WeeklyReportTotals(
            calories_kcal=round(total_calories, 2),
            goalCalories_kcal=round(total_goal, 2),
            deltaCalories_kcal=round(total_calories - total_goal, 2),
            deficitDays=deficit_days,
            surplusDays=surplus_days,
            balancedDays=balanced_days,
        ),
        weightForecast=WeeklyWeightForecast(
            method="7700kcal_per_kg",
            periodDeltaKg=round(period_delta_kg, 2),
            projectedWeightKg=round(projected_weight, 2),
            confidence="low" if abs(period_delta_kg) < 0.5 else "medium",
        ),
    )


@router.get("/reports/monthly", response_model=MonthlyReportResponse)
async def get_monthly_report(
    month_query: Optional[str] = Query(default=None, alias="month"),
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    ensure_premium_access(user, feature="reports.monthly")

    if month_query:
        try:
            year_s, month_s = month_query.split("-", 1)
            year = int(year_s)
            month = int(month_s)
            start_date = date(year, month, 1)
        except Exception as exc:
            raise FitAIError(
                code="VALIDATION_FAILED",
                message="Некорректные данные",
                status_code=400,
                details={"fieldErrors": [{"field": "month", "issue": "must be YYYY-MM"}]},
            ) from exc
    else:
        today = _utc_today()
        start_date = today.replace(day=1)

    end_day = calendar.monthrange(start_date.year, start_date.month)[1]
    end_date = start_date.replace(day=end_day)
    goal = _resolve_goal_or_default(user)

    calories_by_date = await _load_daily_calories_map(conn, str(user["id"]), start_date, end_date)
    period_days = (end_date - start_date).days + 1
    daily_values = [float(calories_by_date.get(start_date + timedelta(days=offset), 0.0)) for offset in range(period_days)]
    total_calories = float(sum(daily_values))
    avg_calories = total_calories / float(period_days)
    total_goal = float(goal) * period_days
    delta_calories = total_calories - total_goal

    tracked_days = sum(1 for calories in daily_values if calories > 0)
    deficit_days = sum(1 for calories in daily_values if calories < goal)
    surplus_days = sum(1 for calories in daily_values if calories > goal)
    balanced_days = period_days - deficit_days - surplus_days

    weight_rows = await fetch_named(
        conn,
        "premium.monthly_weight",
        """
        SELECT date, weight_kg
        FROM weight_logs
        WHERE user_id = $1
          AND date >= $2
          AND date <= $3
        ORDER BY date ASC
        """,
        str(user["id"]),
        start_date,
        end_date,
    )
    start_weight = float(weight_rows[0]["weight_kg"]) if weight_rows else None
    end_weight = float(weight_rows[-1]["weight_kg"]) if weight_rows else None
    change_kg = round(end_weight - start_weight, 2) if (start_weight is not None and end_weight is not None) else None

    return MonthlyReportResponse(
        month=f"{start_date.year:04d}-{start_date.month:02d}",
        startDate=start_date.isoformat(),
        endDate=end_date.isoformat(),
        aggregates=MonthlyReportAggregates(
            calories_kcal=round(total_calories, 2),
            goalCalories_kcal=round(total_goal, 2),
            deltaCalories_kcal=round(delta_calories, 2),
            avgCaloriesPerDay=round(avg_calories, 2),
            trackedDays=tracked_days,
            deficitDays=deficit_days,
            surplusDays=surplus_days,
            balancedDays=balanced_days,
        ),
        weight={
            "startWeightKg": start_weight,
            "endWeightKg": end_weight,
            "changeKg": change_kg,
        },
    )


@router.get("/analysis/why-not-losing", response_model=WhyNotLosingResponse)
async def get_why_not_losing(
    window_days: int = Query(default=14, alias="windowDays", ge=7, le=30),
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    ensure_premium_access(user, feature="analysis.why_not_losing")

    end_date = _utc_today()
    start_date = end_date - timedelta(days=window_days - 1)
    goal = _resolve_goal_or_default(user)

    rows = await fetch_named(
        conn,
        "premium.why_not_losing_window",
        """
        SELECT date, calories_kcal, meals_count
        FROM daily_stats
        WHERE user_id = $1
          AND date >= $2
          AND date <= $3
        """,
        str(user["id"]),
        start_date,
        end_date,
    )

    calories_by_date: dict[date, float] = {}
    logging_days = 0
    for row in rows:
        calories = float(row["calories_kcal"] or 0)
        meals_count = int(row["meals_count"] or 0)
        calories_by_date[row["date"]] = calories
        if meals_count > 0:
            logging_days += 1

    daily_values = [float(calories_by_date.get(start_date + timedelta(days=offset), 0.0)) for offset in range(window_days)]
    logged_values = [value for value in daily_values if value > 0]
    average_calories = (sum(logged_values) / float(len(logged_values))) if logged_values else 0.0
    surplus_days = sum(1 for calories in daily_values if calories > goal * 1.1)
    avg_deficit = goal - average_calories

    frequent_surpluses = surplus_days >= 3
    low_logging = logging_days < max(4, int(window_days * 0.6))
    low_deficit = 0 <= avg_deficit < 150

    insights: list[WhyNotLosingInsight] = []
    if frequent_surpluses:
        insights.append(
            WhyNotLosingInsight(
                rule="FREQUENT_SURPLUSES",
                text="За период много дней с калорийностью выше цели.",
                recommendation="Снизьте калорийность ужинов и перекусов в ближайшие 7 дней.",
            )
        )
    if low_logging:
        insights.append(
            WhyNotLosingInsight(
                rule="LOW_LOGGING_FREQUENCY",
                text="Недостаточно заполненных дней для точной оценки динамики.",
                recommendation="Логируйте питание ежедневно минимум неделю подряд.",
            )
        )
    if low_deficit:
        insights.append(
            WhyNotLosingInsight(
                rule="LOW_DEFICIT",
                text="Средний дефицит слишком мал для устойчивого снижения веса.",
                recommendation="Уменьшите дневную цель на 100-150 ккал и проверьте динамику через 7 дней.",
            )
        )

    summary = (
        "За выбранный период есть факторы, мешающие устойчивому снижению веса."
        if insights
        else "Критичных факторов не найдено. Продолжайте стабильный режим и регулярный учет."
    )

    return WhyNotLosingResponse(
        analysisType="rule_based_v1",
        windowDays=window_days,
        summary=summary,
        insights=insights,
    )


@router.get("/charts/weight", response_model=WeightChartResponse)
async def get_weight_chart(
    date_from: Optional[date] = Query(default=None, alias="dateFrom"),
    date_to: Optional[date] = Query(default=None, alias="dateTo"),
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    ensure_premium_access(user, feature="charts.weight")

    if date_from is None and date_to is None:
        end_date = _utc_today()
        start_date = end_date - timedelta(days=29)
    else:
        if date_from is None or date_to is None:
            raise FitAIError(
                code="VALIDATION_FAILED",
                message="Некорректные данные",
                status_code=400,
                details={
                    "fieldErrors": [
                        {"field": "dateFrom", "issue": "must be provided together with dateTo"},
                        {"field": "dateTo", "issue": "must be provided together with dateFrom"},
                    ]
                },
            )
        if date_from > date_to:
            raise FitAIError(
                code="VALIDATION_FAILED",
                message="Некорректные данные",
                status_code=400,
                details={"fieldErrors": [{"field": "dateFrom", "issue": "must be <= dateTo"}]},
            )
        start_date = date_from
        end_date = date_to

    rows = await fetch_named(
        conn,
        "premium.weight_chart",
        """
        SELECT date, weight_kg
        FROM weight_logs
        WHERE user_id = $1
          AND date >= $2
          AND date <= $3
        ORDER BY date ASC
        """,
        str(user["id"]),
        start_date,
        end_date,
    )

    items = [WeightChartItem(date=row["date"].isoformat(), weight=float(row["weight_kg"])) for row in rows]
    return WeightChartResponse(items=items)
