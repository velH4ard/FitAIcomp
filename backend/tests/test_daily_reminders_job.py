from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from app.reminders import (
    REMINDER_TYPE_DAILY_PROGRESS,
    REMINDER_TYPE_INACTIVITY_2D,
    REMINDER_TYPE_MONTHLY_REPORT,
    REMINDER_TYPE_WEEKLY_REPORT,
    run_daily_reminders,
    run_inactivity_2d_reminders,
    run_monthly_reports,
    run_weekly_reports,
)


TEST_DATE = date(2026, 2, 22)


class FakeReminderConn:
    def __init__(self, users: list[dict], daily_stats: dict):
        self.users = users
        self.daily_stats = daily_stats
        self.deliveries: set[tuple[str, date, str]] = set()

    async def fetch(self, query, *args):
        if "LEFT JOIN daily_stats ds" in query and "JOIN user_settings us" in query:
            target_date = args[0]
            rows = []
            for user in self.users:
                if user.get("subscription_status", "free") == "blocked":
                    continue
                if not user.get("notifications_enabled", True):
                    continue
                rows.append(
                    {
                        "id": user["id"],
                        "telegram_id": user["telegram_id"],
                        "profile": user.get("profile", {}),
                        "daily_goal_auto": user.get("daily_goal_auto"),
                        "daily_goal_override": user.get("daily_goal_override"),
                        "notification_tone": user.get("notification_tone", "balanced"),
                        "calories_kcal": self.daily_stats.get((user["id"], target_date), 0),
                    }
                )
            return rows

        if "FROM users u" in query and "JOIN user_settings us" in query:
            rows = []
            for user in self.users:
                if user.get("subscription_status", "free") == "blocked":
                    continue
                if not user.get("notifications_enabled", True):
                    continue
                if "NOT EXISTS" in query:
                    window_start = args[0]
                    window_end = args[1]
                    has_recent_rows = False
                    day = window_start
                    while day <= window_end:
                        if (user["id"], day) in self.daily_stats:
                            has_recent_rows = True
                            break
                        day += timedelta(days=1)
                    if has_recent_rows:
                        continue
                rows.append(
                    {
                        "id": user["id"],
                        "telegram_id": user["telegram_id"],
                        "profile": user.get("profile", {}),
                        "daily_goal_auto": user.get("daily_goal_auto"),
                        "daily_goal_override": user.get("daily_goal_override"),
                        "notification_tone": user.get("notification_tone", "balanced"),
                    }
                )
            return rows

        if "FROM daily_stats" in query and "date >= $2" in query and "date <= $3" in query:
            user_id = str(args[0])
            start_date = args[1]
            end_date = args[2]
            rows = []
            day = start_date
            while day <= end_date:
                key = (user_id, day)
                if key in self.daily_stats:
                    rows.append({"date": day, "calories_kcal": self.daily_stats[key]})
                day += timedelta(days=1)
            return rows

        if "FROM daily_stats" in query and "ORDER BY date ASC" in query:
            user_id = str(args[0])
            rows = []
            for (uid, day), calories in sorted(self.daily_stats.items(), key=lambda item: item[0][1]):
                if uid == user_id:
                    rows.append({"date": day, "calories_kcal": calories})
            return rows

        return []

    async def fetchrow(self, query, *args):
        if "SELECT MAX(date) AS last_tracked_date" in query:
            user_id = str(args[0])
            candidates = [day for (uid, day), _ in self.daily_stats.items() if uid == user_id]
            return {"last_tracked_date": max(candidates) if candidates else None}

        if "SELECT MAX(date) AS last_delivery_date" in query:
            user_id = str(args[0])
            reminder_type = str(args[1])
            candidates = [day for uid, day, r_type in self.deliveries if uid == user_id and r_type == reminder_type]
            return {"last_delivery_date": max(candidates) if candidates else None}

        if "INSERT INTO reminder_deliveries" in query:
            key = (str(args[1]), args[2], str(args[3]))
            if key in self.deliveries:
                return None
            self.deliveries.add(key)
            return {"id": str(args[0])}
        return None

    async def execute(self, query, *args):
        if "DELETE FROM reminder_deliveries" in query:
            key = (str(args[0]), args[1], str(args[2]))
            self.deliveries.discard(key)
        return "OK"


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_daily_reminders_all_four_branches_and_sender_mocked():
    users = [
        {"id": "u-1", "telegram_id": 1001, "daily_goal_auto": 2000, "notifications_enabled": True},
        {"id": "u-2", "telegram_id": 1002, "daily_goal_auto": 2000, "notifications_enabled": True},
        {"id": "u-3", "telegram_id": 1003, "daily_goal_auto": 2000, "notifications_enabled": True},
        {"id": "u-4", "telegram_id": 1004, "daily_goal_auto": 2000, "notifications_enabled": True},
    ]
    daily_stats = {
        ("u-1", TEST_DATE): 0,
        ("u-2", TEST_DATE): 1200,
        ("u-3", TEST_DATE): 2000,
        ("u-4", TEST_DATE): 2400,
    }
    conn = FakeReminderConn(users, daily_stats)
    sender = AsyncMock()

    stats = await run_daily_reminders(
        conn,
        sender=sender,
        run_date=TEST_DATE,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert stats.total_scanned == 4
    assert stats.eligible == 4
    assert stats.sent == 4
    assert sender.await_count == 4

    texts = [call.args[1] for call in sender.await_args_list]
    assert "нет записей" in texts[0]
    assert "1200" in texts[1] and "2000" in texts[1] and "800" in texts[1]
    assert "целевом диапазоне" in texts[2]
    assert "выше цели" in texts[3]


@pytest.mark.asyncio
async def test_daily_override_goal_affects_branch_logic():
    users = [
        {
            "id": "u-ovr",
            "telegram_id": 2001,
            "daily_goal_auto": 2000,
            "daily_goal_override": 3000,
            "notifications_enabled": True,
        }
    ]
    daily_stats = {("u-ovr", TEST_DATE): 2500}
    conn = FakeReminderConn(users, daily_stats)
    sender = AsyncMock()

    await run_daily_reminders(
        conn,
        sender=sender,
        run_date=TEST_DATE,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert sender.await_count == 1
    text = sender.await_args_list[0].args[1]
    assert "2500" in text
    assert "3000" in text
    assert "500" in text


@pytest.mark.asyncio
async def test_weekly_report_metrics_are_correct():
    user_id = "u-weekly"
    users = [{"id": user_id, "telegram_id": 3001, "daily_goal_auto": 2000, "notifications_enabled": True}]
    day_values = [2000, 1800, 2200, 2100, 0, 1900, 2000]
    daily_stats = {
        (user_id, TEST_DATE - timedelta(days=6 - idx)): value
        for idx, value in enumerate(day_values)
    }
    conn = FakeReminderConn(users, daily_stats)
    sender = AsyncMock()

    stats = await run_weekly_reports(
        conn,
        sender=sender,
        run_date=TEST_DATE,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert stats.total_scanned == 1
    assert stats.sent == 1
    text = sender.await_args_list[0].args[1]
    assert "Стрик: 2 дней" in text
    assert "Среднее: 1714 ккал" in text
    assert "В цель попал(а): 6 из 7 дней" in text


@pytest.mark.asyncio
async def test_daily_idempotency_double_run_single_send():
    users = [{"id": "u-idem", "telegram_id": 4001, "daily_goal_auto": 2000, "notifications_enabled": True}]
    daily_stats = {("u-idem", TEST_DATE): 1500}
    conn = FakeReminderConn(users, daily_stats)
    sender = AsyncMock()

    first = await run_daily_reminders(
        conn,
        sender=sender,
        run_date=TEST_DATE,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )
    second = await run_daily_reminders(
        conn,
        sender=sender,
        run_date=TEST_DATE,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert first.sent == 1
    assert second.sent == 0
    assert second.skipped == 1
    assert sender.await_count == 1


@pytest.mark.asyncio
async def test_weekly_idempotency_double_run_single_send():
    user_id = "u-week-idem"
    users = [{"id": user_id, "telegram_id": 5001, "daily_goal_auto": 2000, "notifications_enabled": True}]
    daily_stats = {(user_id, TEST_DATE - timedelta(days=idx)): 2000 for idx in range(7)}
    conn = FakeReminderConn(users, daily_stats)
    sender = AsyncMock()

    first = await run_weekly_reports(
        conn,
        sender=sender,
        run_date=TEST_DATE,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )
    second = await run_weekly_reports(
        conn,
        sender=sender,
        run_date=TEST_DATE,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert first.sent == 1
    assert second.sent == 0
    assert sender.await_count == 1


@pytest.mark.asyncio
async def test_tone_segmentation_soft_hard_balanced():
    users = [
        {
            "id": "u-soft",
            "telegram_id": 6101,
            "daily_goal_auto": 2000,
            "notifications_enabled": True,
            "notification_tone": "soft",
        },
        {
            "id": "u-hard",
            "telegram_id": 6102,
            "daily_goal_auto": 2000,
            "notifications_enabled": True,
            "notification_tone": "hard",
        },
        {
            "id": "u-balanced",
            "telegram_id": 6103,
            "daily_goal_auto": 2000,
            "notifications_enabled": True,
            "notification_tone": "balanced",
        },
    ]
    daily_stats = {
        ("u-soft", TEST_DATE): 1000,
        ("u-hard", TEST_DATE): 1000,
        ("u-balanced", TEST_DATE): 1000,
    }
    conn = FakeReminderConn(users, daily_stats)
    sender = AsyncMock()

    await run_daily_reminders(
        conn,
        sender=sender,
        run_date=TEST_DATE,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert sender.await_count == 3
    messages_by_chat = {int(call.args[0]): call.args[1] for call in sender.await_args_list}
    assert "спокойный" in messages_by_chat[6101]
    assert "Закрой дефицит" in messages_by_chat[6102]
    assert "Осталось 1000 ккал" in messages_by_chat[6103]


@pytest.mark.asyncio
async def test_monthly_report_metrics_are_correct_for_previous_month():
    run_date = date(2026, 3, 1)
    user_id = "u-monthly"
    users = [{"id": user_id, "telegram_id": 7001, "daily_goal_auto": 2000, "notifications_enabled": True}]
    feb_values = [2000, 2100, 1800, 1500, 2200, 1000]
    daily_stats = {
        (user_id, date(2026, 2, 1) + timedelta(days=idx)): value
        for idx, value in enumerate(feb_values)
    }
    conn = FakeReminderConn(users, daily_stats)
    sender = AsyncMock()

    stats = await run_monthly_reports(
        conn,
        sender=sender,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert stats.total_scanned == 1
    assert stats.sent == 1
    text = sender.await_args_list[0].args[1]
    assert "Дней с записями: 6" in text
    assert "Среднее: 1767 ккал" in text
    assert "В цель: 4 из 6" in text


@pytest.mark.asyncio
async def test_monthly_report_empty_month_uses_motivational_variant():
    run_date = date(2026, 3, 1)
    users = [{"id": "u-empty", "telegram_id": 7002, "daily_goal_auto": 2000, "notifications_enabled": True}]
    conn = FakeReminderConn(users, daily_stats={})
    sender = AsyncMock()

    await run_monthly_reports(
        conn,
        sender=sender,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert sender.await_count == 1
    assert "В этом месяце записей не было" in sender.await_args_list[0].args[1]


@pytest.mark.asyncio
async def test_inactivity_2d_trigger_and_idempotency():
    users = [
        {"id": "u-inactive", "telegram_id": 8001, "daily_goal_auto": 2000, "notifications_enabled": True},
        {"id": "u-active", "telegram_id": 8002, "daily_goal_auto": 2000, "notifications_enabled": True},
    ]
    daily_stats = {
        ("u-active", TEST_DATE - timedelta(days=1)): 1500,
    }
    conn = FakeReminderConn(users, daily_stats)
    sender = AsyncMock()

    first = await run_inactivity_2d_reminders(
        conn,
        sender=sender,
        run_date=TEST_DATE,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )
    second = await run_inactivity_2d_reminders(
        conn,
        sender=sender,
        run_date=TEST_DATE,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert first.sent == 1
    assert second.sent == 0
    assert second.skipped == 1
    assert sender.await_count == 1
    assert sender.await_args_list[0].args[0] == 8001
    assert "уже 2 дня" in sender.await_args_list[0].args[1]


@pytest.mark.asyncio
async def test_monthly_and_inactivity_idempotency_double_run_single_send_each():
    run_date = date(2026, 3, 1)
    users = [{"id": "u-both", "telegram_id": 8101, "daily_goal_auto": 2000, "notifications_enabled": True}]
    daily_stats = {
        ("u-both", date(2026, 2, 15)): 2000,
    }
    conn = FakeReminderConn(users, daily_stats)
    sender_monthly = AsyncMock()
    sender_inactivity = AsyncMock()

    first_monthly = await run_monthly_reports(
        conn,
        sender=sender_monthly,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )
    second_monthly = await run_monthly_reports(
        conn,
        sender=sender_monthly,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )
    first_inactivity = await run_inactivity_2d_reminders(
        conn,
        sender=sender_inactivity,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )
    second_inactivity = await run_inactivity_2d_reminders(
        conn,
        sender=sender_inactivity,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert first_monthly.sent == 1
    assert second_monthly.sent == 0
    assert first_inactivity.sent == 1
    assert second_inactivity.sent == 0
    assert sender_monthly.await_count == 1
    assert sender_inactivity.await_count == 1


@pytest.mark.asyncio
async def test_daily_weekly_monthly_do_not_conflict_by_reminder_type():
    run_date = date(2026, 3, 1)
    users = [{"id": "u-multi", "telegram_id": 9001, "daily_goal_auto": 2000, "notifications_enabled": True}]
    daily_stats = {("u-multi", run_date): 1500}
    for idx in range(1, 7):
        daily_stats[("u-multi", run_date - timedelta(days=idx))] = 1800
    daily_stats[("u-multi", date(2026, 2, 14))] = 2000

    conn = FakeReminderConn(users, daily_stats)
    sender = AsyncMock()

    await run_daily_reminders(
        conn,
        sender=sender,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )
    await run_weekly_reports(
        conn,
        sender=sender,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )
    await run_monthly_reports(
        conn,
        sender=sender,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert sender.await_count == 3
    reminder_types = {delivery[2] for delivery in conn.deliveries}
    assert reminder_types == {
        REMINDER_TYPE_DAILY_PROGRESS,
        REMINDER_TYPE_WEEKLY_REPORT,
        REMINDER_TYPE_MONTHLY_REPORT,
    }


@pytest.mark.asyncio
async def test_inactivity_does_not_conflict_with_other_reminder_types():
    run_date = date(2026, 3, 1)
    users = [{"id": "u-inact-multi", "telegram_id": 9002, "daily_goal_auto": 2000, "notifications_enabled": True}]
    conn = FakeReminderConn(users, daily_stats={})
    sender = AsyncMock()

    await run_weekly_reports(
        conn,
        sender=sender,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )
    await run_monthly_reports(
        conn,
        sender=sender,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )
    await run_inactivity_2d_reminders(
        conn,
        sender=sender,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    reminder_types = {delivery[2] for delivery in conn.deliveries}
    assert REMINDER_TYPE_WEEKLY_REPORT in reminder_types
    assert REMINDER_TYPE_MONTHLY_REPORT in reminder_types
    assert REMINDER_TYPE_INACTIVITY_2D in reminder_types


@pytest.mark.asyncio
async def test_inactivity_2d_anti_spam_requires_new_tracking_after_previous_delivery():
    run_date = date(2026, 3, 4)
    user_id = "u-inactivity-anti-spam"
    users = [{"id": user_id, "telegram_id": 9101, "daily_goal_auto": 2000, "notifications_enabled": True}]
    daily_stats = {
        (user_id, date(2026, 2, 25)): 1600,
    }
    conn = FakeReminderConn(users, daily_stats)
    conn.deliveries.add((user_id, date(2026, 3, 1), REMINDER_TYPE_INACTIVITY_2D))
    sender = AsyncMock()

    first = await run_inactivity_2d_reminders(
        conn,
        sender=sender,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert first.sent == 0
    assert first.skipped == 1

    # User tracked again after last inactivity reminder, then stopped for 2 days.
    conn.daily_stats[(user_id, date(2026, 3, 2))] = 1700
    second_run_date = date(2026, 3, 5)
    second = await run_inactivity_2d_reminders(
        conn,
        sender=sender,
        run_date=second_run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert second.sent == 1
    assert sender.await_count == 1
