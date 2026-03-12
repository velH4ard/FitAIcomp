import asyncio
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from .goals import resolve_effective_goal
from .streak_logic import calculate_streak_metrics


logger = logging.getLogger("fitai-reminders")

REMINDER_TYPE_DAILY_PROGRESS = "daily_progress"
REMINDER_TYPE_WEEKLY_REPORT = "weekly_report"
REMINDER_TYPE_MONTHLY_REPORT = "monthly_report"
REMINDER_TYPE_INACTIVITY_2D = "inactivity_2d"
ALLOWED_NOTIFICATION_TONES = {"soft", "hard", "balanced"}

DAILY_NO_ENTRIES_MESSAGES = [
    "Сегодня еще нет записей по еде. Добавь первый прием пищи, чтобы держать ритм.",
    "Пока 0 ккал за сегодня. Сделай первый шаг и зафиксируй прием пищи.",
    "День только начался в FitAI: добавь еду, чтобы увидеть прогресс.",
]

DAILY_NO_ENTRIES_MESSAGES_SOFT = [
    "Сегодня еще нет записей по еде. Начни с маленького шага — добавь первый прием пищи.",
    "Пока 0 ккал за сегодня. Мягко возвращаемся в ритм: зафиксируй первый прием пищи.",
]

DAILY_NO_ENTRIES_MESSAGES_HARD = [
    "Сегодня нет записей. Верни контроль: добавь первый прием пищи прямо сейчас.",
    "Пока 0 ккал за день. Дисциплина начинается с первого записанного приема пищи.",
]

DAILY_UNDER_GOAL_MESSAGES = [
    "Сегодня {today} из {goal} ккал. Осталось {remaining} ккал до цели.",
    "Пока {today}/{goal} ккал. До цели осталось {remaining} ккал.",
    "Хороший старт: {today} ккал из {goal}. Нужно еще {remaining} ккал.",
]

DAILY_UNDER_GOAL_MESSAGES_SOFT = [
    "Ты на {today} из {goal} ккал. Осталось {remaining} ккал — спокойный финиш в цель.",
    "Пока {today}/{goal} ккал. Осталось {remaining} ккал, ты хорошо идешь.",
]

DAILY_UNDER_GOAL_MESSAGES_HARD = [
    "Сейчас {today}/{goal} ккал. Закрой дефицит: добери {remaining} ккал до цели.",
    "Отставание от цели: {today}/{goal} ккал. Нужно еще {remaining} ккал.",
]

DAILY_ON_TARGET_MESSAGES = [
    "Отличная работа, сегодня ты в целевом диапазоне по калориям.",
    "Ты держишь цель по калориям сегодня. Продолжай в том же ритме.",
    "Супер: дневная калорийность сейчас ровно в нужном диапазоне.",
]

DAILY_ON_TARGET_MESSAGES_SOFT = [
    "Отличная динамика: ты в целевом диапазоне по калориям.",
    "Ты в целевом диапазоне — продолжай в комфортном темпе.",
]

DAILY_ON_TARGET_MESSAGES_HARD = [
    "Ты в целевом диапазоне. Зафиксируй результат до конца дня.",
    "Целевой диапазон удержан. Не сбавляй темп.",
]

DAILY_OVER_GOAL_MESSAGES = [
    "Сегодня калорийность выше цели. Заверши день более легким приемом пищи.",
    "Есть превышение цели по калориям. Можно выровнять баланс легким ужином.",
    "Ты выше целевого диапазона. Попробуй сделать следующий прием пищи легче.",
]

DAILY_OVER_GOAL_MESSAGES_SOFT = [
    "Сегодня ты выше цели. Легкий следующий прием пищи поможет выровнять баланс.",
    "Небольшое превышение цели по калориям. Можно мягко скорректировать оставшийся день.",
]

DAILY_OVER_GOAL_MESSAGES_HARD = [
    "Сегодня выше цели. Сократи следующий прием пищи и вернись в диапазон.",
    "Цель превышена. Нужна корректировка рациона до конца дня.",
]

WEEKLY_EMPTY_MESSAGES = [
    "📊 На этой неделе пока нет записей. Начни с одного приема пищи сегодня — и ритм вернется 💪",
    "📊 За последние 7 дней еще нет данных. Добавь первый прием пищи и запусти новый стрик 💪",
]

MONTHLY_EMPTY_MESSAGE = (
    "📆 Отчёт за месяц\n\n"
    "В этом месяце записей не было.\n"
    "Давай вернёмся в ритм — начни с одного приёма пищи сегодня 🔥"
)

INACTIVITY_2D_MESSAGES = [
    "Мы не видим записей уже 2 дня. Вернись и продолжи прогресс 🔥",
    "Пауза затянулась 🙂 Добавь один приём пищи — и стрик снова пойдёт.",
    "Загляни в FitAI: один снимок — и ты снова в ритме 💪",
]


@dataclass
class ReminderRunStats:
    total_scanned: int = 0
    eligible: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0


def _pick_message(choice_fn: Callable[[list[str]], str], pool: list[str]) -> str:
    return choice_fn(pool)


def _normalize_notification_tone(raw_tone: Any) -> str:
    normalized = str(raw_tone or "").strip().lower()
    if normalized in ALLOWED_NOTIFICATION_TONES:
        return normalized
    return "balanced"


def _get_daily_message_pools(tone: str) -> tuple[list[str], list[str], list[str], list[str]]:
    if tone == "soft":
        return (
            DAILY_NO_ENTRIES_MESSAGES_SOFT,
            DAILY_UNDER_GOAL_MESSAGES_SOFT,
            DAILY_ON_TARGET_MESSAGES_SOFT,
            DAILY_OVER_GOAL_MESSAGES_SOFT,
        )
    if tone == "hard":
        return (
            DAILY_NO_ENTRIES_MESSAGES_HARD,
            DAILY_UNDER_GOAL_MESSAGES_HARD,
            DAILY_ON_TARGET_MESSAGES_HARD,
            DAILY_OVER_GOAL_MESSAGES_HARD,
        )
    return (
        DAILY_NO_ENTRIES_MESSAGES,
        DAILY_UNDER_GOAL_MESSAGES,
        DAILY_ON_TARGET_MESSAGES,
        DAILY_OVER_GOAL_MESSAGES,
    )


def _build_daily_message(
    *,
    today_calories: float,
    effective_goal: int,
    tone: str,
    choice_fn: Callable[[list[str]], str],
) -> str:
    no_entries_pool, under_goal_pool, on_target_pool, over_goal_pool = _get_daily_message_pools(tone)
    if today_calories == 0:
        return _pick_message(choice_fn, no_entries_pool)

    progress = today_calories / float(effective_goal)
    if progress < 0.9:
        remaining = max(0, int(round(effective_goal - today_calories)))
        template = _pick_message(choice_fn, under_goal_pool)
        return template.format(
            today=int(round(today_calories)),
            goal=effective_goal,
            remaining=remaining,
        )
    if progress <= 1.1:
        return _pick_message(choice_fn, on_target_pool)
    return _pick_message(choice_fn, over_goal_pool)


def _build_weekly_report_message(
    *,
    avg_calories: int,
    days_within_target: int,
    current_streak: int,
) -> str:
    return (
        "📊 Твой недельный отчёт:\n\n"
        f"🔥 Стрик: {current_streak} дней\n"
        f"🍽 Среднее: {avg_calories} ккал\n"
        f"🎯 В цель попал(а): {days_within_target} из 7 дней\n\n"
        "Продолжай в том же ритме 💪"
    )


def _build_monthly_report_message(
    *,
    avg_calories: int,
    days_tracked: int,
    days_in_target: int,
) -> str:
    return (
        "📆 Твой отчёт за месяц\n\n"
        f"📊 Дней с записями: {days_tracked}\n"
        f"🍽 Среднее: {avg_calories} ккал\n"
        f"🎯 В цель: {days_in_target} из {days_tracked}\n\n"
        "Хороший темп. Продолжай 💪"
    )


def _calculate_best_tracked_streak(rows: list[dict[str, Any]]) -> int:
    # Deterministic definition: longest run of consecutive calendar days
    # that have at least one daily_stats row in the report month.
    unique_days = sorted({item.get("date") for item in rows if isinstance(item.get("date"), date)})
    if not unique_days:
        return 0

    best = 1
    current = 1
    for idx in range(1, len(unique_days)):
        if unique_days[idx] == unique_days[idx - 1] + timedelta(days=1):
            current += 1
            if current > best:
                best = current
        else:
            current = 1
    return best


async def _fetch_last_tracked_date(conn: Any, *, user_id: str) -> Optional[date]:
    row = await conn.fetchrow(
        """
        SELECT MAX(date) AS last_tracked_date
        FROM daily_stats
        WHERE user_id = $1::uuid
        """,
        str(user_id),
    )
    if not row:
        return None
    value = row.get("last_tracked_date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


async def _fetch_last_delivery_date(
    conn: Any,
    *,
    user_id: str,
    reminder_type: str,
) -> Optional[date]:
    row = await conn.fetchrow(
        """
        SELECT MAX(date) AS last_delivery_date
        FROM reminder_deliveries
        WHERE user_id = $1::uuid
          AND reminder_type = $2
        """,
        str(user_id),
        reminder_type,
    )
    if not row:
        return None
    value = row.get("last_delivery_date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


async def _reserve_delivery(
    conn: Any,
    *,
    user_id: str,
    run_date: date,
    reminder_type: str,
) -> bool:
    row = await conn.fetchrow(
        """
        INSERT INTO reminder_deliveries (id, user_id, date, reminder_type)
        VALUES ($1::uuid, $2::uuid, $3, $4)
        ON CONFLICT (user_id, date, reminder_type) DO NOTHING
        RETURNING id
        """,
        str(uuid.uuid4()),
        str(user_id),
        run_date,
        reminder_type,
    )
    return row is not None


async def _release_delivery_reservation(
    conn: Any,
    *,
    user_id: str,
    run_date: date,
    reminder_type: str,
) -> None:
    await conn.execute(
        """
        DELETE FROM reminder_deliveries
        WHERE user_id = $1::uuid
          AND date = $2
          AND reminder_type = $3
        """,
        str(user_id),
        run_date,
        reminder_type,
    )


async def _fetch_user_stats_for_window(
    conn: Any,
    *,
    user_id: str,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT date, calories_kcal
        FROM daily_stats
        WHERE user_id = $1::uuid
          AND date >= $2
          AND date <= $3
        ORDER BY date ASC
        """,
        str(user_id),
        start_date,
        end_date,
    )
    return [dict(row) for row in rows]


async def _fetch_user_all_stats(conn: Any, *, user_id: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT date, calories_kcal
        FROM daily_stats
        WHERE user_id = $1::uuid
        ORDER BY date ASC
        """,
        str(user_id),
    )
    return [dict(row) for row in rows]


async def run_daily_reminders(
    conn: Any,
    *,
    sender: Callable[[int, str], Awaitable[None]],
    run_date: Optional[date] = None,
    job_run_id: Optional[str] = None,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_fn: Callable[[], float] = random.random,
    choice_fn: Callable[[list[str]], str] = random.choice,
) -> ReminderRunStats:
    now_utc = datetime.now(timezone.utc)
    target_date = run_date or now_utc.date()
    run_id = job_run_id or str(uuid.uuid4())

    rows = await conn.fetch(
        """
        SELECT
            u.id,
            u.telegram_id,
            u.profile,
            u.daily_goal_auto,
            u.daily_goal_override,
            us.notification_tone,
            COALESCE(ds.calories_kcal, 0) AS calories_kcal
        FROM users u
        JOIN user_settings us
          ON us.user_id = u.id
         AND us.notifications_enabled = TRUE
        LEFT JOIN daily_stats ds
          ON ds.user_id = u.id
         AND ds.date = $1
        WHERE u.subscription_status <> 'blocked'
          AND u.telegram_id IS NOT NULL
        """,
        target_date,
    )

    stats = ReminderRunStats(total_scanned=len(rows))

    for row in rows:
        record = dict(row)
        user_id = str(record["id"])
        effective_goal = resolve_effective_goal(record)
        tone = _normalize_notification_tone(record.get("notification_tone"))
        if effective_goal is None or effective_goal <= 0:
            stats.skipped += 1
            continue

        stats.eligible += 1
        calories = float(record.get("calories_kcal") or 0)

        reserved = await _reserve_delivery(
            conn,
            user_id=user_id,
            run_date=target_date,
            reminder_type=REMINDER_TYPE_DAILY_PROGRESS,
        )
        if not reserved:
            stats.skipped += 1
            continue

        chat_id = int(record["telegram_id"])
        message_text = _build_daily_message(
            today_calories=calories,
            effective_goal=effective_goal,
            tone=tone,
            choice_fn=choice_fn,
        )

        try:
            await sender(chat_id, message_text)
            stats.sent += 1
            await sleep_fn(0.03 + (max(0.0, min(1.0, random_fn())) * 0.05))
        except Exception:
            stats.failed += 1
            await _release_delivery_reservation(
                conn,
                user_id=user_id,
                run_date=target_date,
                reminder_type=REMINDER_TYPE_DAILY_PROGRESS,
            )
            logger.warning("REMINDER_SEND_FAIL job_run_id=%s user_id=%s type=%s", run_id, user_id, REMINDER_TYPE_DAILY_PROGRESS)

    logger.info(
        "DAILY_REMINDER_JOB_DONE job_run_id=%s date=%s total_scanned=%s eligible=%s sent=%s skipped=%s failed=%s",
        run_id,
        target_date.isoformat(),
        stats.total_scanned,
        stats.eligible,
        stats.sent,
        stats.skipped,
        stats.failed,
    )
    return stats


async def run_weekly_reports(
    conn: Any,
    *,
    sender: Callable[[int, str], Awaitable[None]],
    run_date: Optional[date] = None,
    job_run_id: Optional[str] = None,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_fn: Callable[[], float] = random.random,
    choice_fn: Callable[[list[str]], str] = random.choice,
) -> ReminderRunStats:
    now_utc = datetime.now(timezone.utc)
    end_date = run_date or now_utc.date()
    start_date = end_date - timedelta(days=6)
    run_id = job_run_id or str(uuid.uuid4())

    rows = await conn.fetch(
        """
        SELECT
            u.id,
            u.telegram_id,
            u.profile,
            u.daily_goal_auto,
            u.daily_goal_override
        FROM users u
        JOIN user_settings us
          ON us.user_id = u.id
         AND us.notifications_enabled = TRUE
        WHERE u.subscription_status <> 'blocked'
          AND u.telegram_id IS NOT NULL
        """,
    )

    stats = ReminderRunStats(total_scanned=len(rows))

    for row in rows:
        record = dict(row)
        user_id = str(record["id"])
        effective_goal = resolve_effective_goal(record)
        if effective_goal is None or effective_goal <= 0:
            stats.skipped += 1
            continue

        stats.eligible += 1
        reserved = await _reserve_delivery(
            conn,
            user_id=user_id,
            run_date=end_date,
            reminder_type=REMINDER_TYPE_WEEKLY_REPORT,
        )
        if not reserved:
            stats.skipped += 1
            continue

        weekly_rows = await _fetch_user_stats_for_window(
            conn,
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
        )

        calories_by_day: dict[date, float] = {
            row_item["date"]: float(row_item.get("calories_kcal") or 0) for row_item in weekly_rows
        }
        seven_days = [start_date + timedelta(days=offset) for offset in range(7)]
        total_calories = sum(calories_by_day.get(day, 0.0) for day in seven_days)
        avg_calories = int(round(total_calories / 7.0))

        days_within_target = 0
        for day in seven_days:
            day_calories = calories_by_day.get(day, 0.0)
            progress = day_calories / float(effective_goal)
            if 0.9 <= progress <= 1.1:
                days_within_target += 1

        if total_calories <= 0:
            message_text = _pick_message(choice_fn, WEEKLY_EMPTY_MESSAGES)
        else:
            all_rows = await _fetch_user_all_stats(conn, user_id=user_id)
            current_streak, _, _ = calculate_streak_metrics(
                all_rows,
                today=end_date,
                effective_goal=effective_goal,
            )
            message_text = _build_weekly_report_message(
                avg_calories=avg_calories,
                days_within_target=days_within_target,
                current_streak=current_streak,
            )

        chat_id = int(record["telegram_id"])
        try:
            await sender(chat_id, message_text)
            stats.sent += 1
            await sleep_fn(0.03 + (max(0.0, min(1.0, random_fn())) * 0.05))
        except Exception:
            stats.failed += 1
            await _release_delivery_reservation(
                conn,
                user_id=user_id,
                run_date=end_date,
                reminder_type=REMINDER_TYPE_WEEKLY_REPORT,
            )
            logger.warning("REMINDER_SEND_FAIL job_run_id=%s user_id=%s type=%s", run_id, user_id, REMINDER_TYPE_WEEKLY_REPORT)

    logger.info(
        "WEEKLY_REPORT_JOB_DONE job_run_id=%s start_date=%s end_date=%s total_scanned=%s eligible=%s sent=%s skipped=%s failed=%s",
        run_id,
        start_date.isoformat(),
        end_date.isoformat(),
        stats.total_scanned,
        stats.eligible,
        stats.sent,
        stats.skipped,
        stats.failed,
    )
    return stats


async def run_monthly_reports(
    conn: Any,
    *,
    sender: Callable[[int, str], Awaitable[None]],
    run_date: Optional[date] = None,
    job_run_id: Optional[str] = None,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_fn: Callable[[], float] = random.random,
    choice_fn: Callable[[list[str]], str] = random.choice,
) -> ReminderRunStats:
    now_utc = datetime.now(timezone.utc)
    target_date = run_date or now_utc.date()
    month_end = target_date.replace(day=1) - timedelta(days=1)
    month_start = month_end.replace(day=1)
    run_id = job_run_id or str(uuid.uuid4())

    rows = await conn.fetch(
        """
        SELECT
            u.id,
            u.telegram_id,
            u.profile,
            u.daily_goal_auto,
            u.daily_goal_override
        FROM users u
        JOIN user_settings us
          ON us.user_id = u.id
         AND us.notifications_enabled = TRUE
        WHERE u.subscription_status <> 'blocked'
          AND u.telegram_id IS NOT NULL
        """,
    )

    stats = ReminderRunStats(total_scanned=len(rows))

    for row in rows:
        record = dict(row)
        user_id = str(record["id"])
        effective_goal = resolve_effective_goal(record)

        stats.eligible += 1
        reserved = await _reserve_delivery(
            conn,
            user_id=user_id,
            run_date=target_date,
            reminder_type=REMINDER_TYPE_MONTHLY_REPORT,
        )
        if not reserved:
            stats.skipped += 1
            continue

        monthly_rows = await _fetch_user_stats_for_window(
            conn,
            user_id=user_id,
            start_date=month_start,
            end_date=month_end,
        )

        days_tracked = len({item.get("date") for item in monthly_rows if isinstance(item.get("date"), date)})
        if days_tracked == 0:
            message_text = MONTHLY_EMPTY_MESSAGE
            total_calories = 0
            avg_calories = 0
            days_in_target = 0
            best_streak_month = 0
        else:
            total_calories = int(round(sum(float(item.get("calories_kcal") or 0) for item in monthly_rows)))
            avg_calories = int(round(total_calories / days_tracked)) if days_tracked > 0 else 0
            days_in_target = 0
            if effective_goal is not None and effective_goal > 0:
                for item in monthly_rows:
                    day_calories = float(item.get("calories_kcal") or 0)
                    progress = day_calories / float(effective_goal)
                    if 0.9 <= progress <= 1.1:
                        days_in_target += 1

            best_streak_month = _calculate_best_tracked_streak(monthly_rows)

            message_text = _build_monthly_report_message(
                avg_calories=avg_calories,
                days_tracked=days_tracked,
                days_in_target=days_in_target,
            )

        logger.info(
            "MONTHLY_REPORT_METRICS job_run_id=%s user_id=%s period_start=%s period_end=%s total_calories=%s avg_calories=%s days_tracked=%s days_in_target=%s best_streak_month=%s",
            run_id,
            user_id,
            month_start.isoformat(),
            month_end.isoformat(),
            total_calories,
            avg_calories,
            days_tracked,
            days_in_target,
            best_streak_month,
        )

        chat_id = int(record["telegram_id"])
        try:
            await sender(chat_id, message_text)
            stats.sent += 1
            await sleep_fn(0.05 + (max(0.0, min(1.0, random_fn())) * 0.05))
        except Exception:
            stats.failed += 1
            await _release_delivery_reservation(
                conn,
                user_id=user_id,
                run_date=target_date,
                reminder_type=REMINDER_TYPE_MONTHLY_REPORT,
            )
            logger.warning("REMINDER_SEND_FAIL job_run_id=%s user_id=%s type=%s", run_id, user_id, REMINDER_TYPE_MONTHLY_REPORT)

    logger.info(
        "MONTHLY_REPORT_JOB_DONE job_run_id=%s report_month_start=%s report_month_end=%s delivery_date=%s total_scanned=%s eligible=%s sent=%s skipped=%s failed=%s",
        run_id,
        month_start.isoformat(),
        month_end.isoformat(),
        target_date.isoformat(),
        stats.total_scanned,
        stats.eligible,
        stats.sent,
        stats.skipped,
        stats.failed,
    )
    return stats


async def run_inactivity_2d_reminders(
    conn: Any,
    *,
    sender: Callable[[int, str], Awaitable[None]],
    run_date: Optional[date] = None,
    job_run_id: Optional[str] = None,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_fn: Callable[[], float] = random.random,
    choice_fn: Callable[[list[str]], str] = random.choice,
) -> ReminderRunStats:
    now_utc = datetime.now(timezone.utc)
    target_date = run_date or now_utc.date()
    day_minus_1 = target_date - timedelta(days=1)
    day_minus_2 = target_date - timedelta(days=2)
    run_id = job_run_id or str(uuid.uuid4())

    rows = await conn.fetch(
        """
        SELECT
            u.id,
            u.telegram_id
        FROM users u
        JOIN user_settings us
          ON us.user_id = u.id
         AND us.notifications_enabled = TRUE
        WHERE u.subscription_status <> 'blocked'
          AND u.telegram_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM daily_stats ds
              WHERE ds.user_id = u.id
                AND ds.date IN ($1, $2)
          )
        """,
        day_minus_2,
        day_minus_1,
    )

    stats = ReminderRunStats(total_scanned=len(rows))

    for row in rows:
        record = dict(row)
        user_id = str(record["id"])

        last_tracked_date = await _fetch_last_tracked_date(conn, user_id=user_id)
        last_inactivity_delivery = await _fetch_last_delivery_date(
            conn,
            user_id=user_id,
            reminder_type=REMINDER_TYPE_INACTIVITY_2D,
        )
        if last_inactivity_delivery is not None and (
            last_tracked_date is None or last_tracked_date <= last_inactivity_delivery
        ):
            stats.skipped += 1
            continue

        stats.eligible += 1
        reserved = await _reserve_delivery(
            conn,
            user_id=user_id,
            run_date=target_date,
            reminder_type=REMINDER_TYPE_INACTIVITY_2D,
        )
        if not reserved:
            stats.skipped += 1
            continue

        chat_id = int(record["telegram_id"])
        try:
            await sender(chat_id, _pick_message(choice_fn, INACTIVITY_2D_MESSAGES))
            stats.sent += 1
            await sleep_fn(0.05 + (max(0.0, min(1.0, random_fn())) * 0.05))
        except Exception:
            stats.failed += 1
            await _release_delivery_reservation(
                conn,
                user_id=user_id,
                run_date=target_date,
                reminder_type=REMINDER_TYPE_INACTIVITY_2D,
            )
            logger.warning("REMINDER_SEND_FAIL job_run_id=%s user_id=%s type=%s", run_id, user_id, REMINDER_TYPE_INACTIVITY_2D)

    logger.info(
        "INACTIVITY_2D_JOB_DONE job_run_id=%s day_minus_2=%s day_minus_1=%s total_scanned=%s eligible=%s sent=%s skipped=%s failed=%s",
        run_id,
        day_minus_2.isoformat(),
        day_minus_1.isoformat(),
        stats.total_scanned,
        stats.eligible,
        stats.sent,
        stats.skipped,
        stats.failed,
    )
    return stats
