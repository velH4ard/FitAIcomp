from datetime import date, datetime, timedelta
from typing import Optional


def _is_consecutive(prev_date: date, curr_date: date) -> bool:
    return (curr_date - prev_date).days == 1


def normalize_stats_rows(rows: list[dict]) -> dict[date, float]:
    by_date: dict[date, float] = {}
    for row in rows:
        raw_date = row.get("date")
        if isinstance(raw_date, datetime):
            raw_date = raw_date.date()
        if not isinstance(raw_date, date):
            continue
        by_date[raw_date] = float(row.get("calories_kcal") or 0)
    return by_date


def calculate_streak_metrics(
    rows: list[dict],
    *,
    today: date,
    effective_goal: int,
) -> tuple[int, int, Optional[date]]:
    if effective_goal <= 0:
        return 0, 0, None

    threshold = float(effective_goal) * 0.7
    stats_by_date = normalize_stats_rows(rows)
    if not stats_by_date:
        return 0, 0, None

    current_streak = 0
    last_completed_date: Optional[date] = None

    if today in stats_by_date:
        check_date = today
        prev_date_in_streak: Optional[date] = None
        while True:
            calories = stats_by_date.get(check_date)
            if calories is None:
                break
            if calories < threshold:
                break

            if prev_date_in_streak is None:
                current_streak = 1
                last_completed_date = check_date
            elif _is_consecutive(check_date, prev_date_in_streak):
                current_streak += 1
                last_completed_date = check_date
            else:
                break

            prev_date_in_streak = check_date
            check_date -= timedelta(days=1)

    best_streak = 0
    current_run = 0
    prev_completed_date: Optional[date] = None
    for row in rows:
        raw_date = row.get("date")
        if isinstance(raw_date, datetime):
            raw_date = raw_date.date()
        if not isinstance(raw_date, date):
            continue

        calories = float(row.get("calories_kcal") or 0)
        if calories >= threshold:
            if prev_completed_date is None:
                current_run = 1
            elif _is_consecutive(prev_completed_date, raw_date):
                current_run += 1
            else:
                current_run = 1
            prev_completed_date = raw_date
            if current_run > best_streak:
                best_streak = current_run
        else:
            current_run = 0
            prev_completed_date = None

    return current_streak, best_streak, last_completed_date
