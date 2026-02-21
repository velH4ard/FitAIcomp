from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from app.reminders import (
    REMINDER_TYPE_INACTIVITY_2D,
    REMINDER_TYPE_MONTHLY_REPORT,
    run_inactivity_2d_reminders,
    run_monthly_reports,
)


async def _no_sleep(_: float) -> None:
    return None


class FakeJobsConn:
    def __init__(self, users: list[dict], daily_stats: dict[tuple[str, date], float]):
        self.users = users
        self.daily_stats = daily_stats
        self.deliveries: set[tuple[str, date, str]] = set()

    async def fetch(self, query, *args):
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
                        "daily_goal_auto": user.get("daily_goal_auto", 2000),
                        "daily_goal_override": user.get("daily_goal_override"),
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


@pytest.mark.asyncio
async def test_monthly_enabled_user_with_prev_month_stats_sends_and_inserts_delivery():
    run_date = date(2026, 3, 1)
    user_id = "u-monthly-send"
    users = [{"id": user_id, "telegram_id": 10101, "notifications_enabled": True, "daily_goal_auto": 2000}]
    daily_stats = {
        (user_id, date(2026, 2, 10)): 1800,
        (user_id, date(2026, 2, 11)): 2100,
    }
    conn = FakeJobsConn(users, daily_stats)
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
    assert stats.eligible == 1
    assert stats.sent == 1
    assert sender.await_count == 1
    assert (user_id, run_date, REMINDER_TYPE_MONTHLY_REPORT) in conn.deliveries


@pytest.mark.asyncio
async def test_monthly_same_run_date_is_idempotent_and_sends_once():
    run_date = date(2026, 3, 1)
    user_id = "u-monthly-idem"
    users = [{"id": user_id, "telegram_id": 10102, "notifications_enabled": True, "daily_goal_auto": 2000}]
    daily_stats = {(user_id, date(2026, 2, 20)): 1900}
    conn = FakeJobsConn(users, daily_stats)
    sender = AsyncMock()

    first = await run_monthly_reports(
        conn,
        sender=sender,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )
    second = await run_monthly_reports(
        conn,
        sender=sender,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert first.sent == 1
    assert second.sent == 0
    assert second.skipped == 1
    assert sender.await_count == 1


@pytest.mark.asyncio
async def test_monthly_send_failure_removes_delivery_row_compensation():
    run_date = date(2026, 3, 1)
    user_id = "u-monthly-fail"
    users = [{"id": user_id, "telegram_id": 10103, "notifications_enabled": True, "daily_goal_auto": 2000}]
    daily_stats = {(user_id, date(2026, 2, 5)): 2000}
    conn = FakeJobsConn(users, daily_stats)

    async def failing_sender(_chat_id: int, _text: str) -> None:
        raise RuntimeError("telegram send failed")

    stats = await run_monthly_reports(
        conn,
        sender=failing_sender,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
        choice_fn=lambda pool: pool[0],
    )

    assert stats.failed == 1
    assert stats.sent == 0
    assert (user_id, run_date, REMINDER_TYPE_MONTHLY_REPORT) not in conn.deliveries


@pytest.mark.asyncio
async def test_inactivity_last_two_days_missing_sends_notification():
    run_date = date(2026, 2, 22)
    user_id = "u-inactive-send"
    users = [{"id": user_id, "telegram_id": 20201, "notifications_enabled": True}]
    conn = FakeJobsConn(users, daily_stats={})
    sender = AsyncMock()

    stats = await run_inactivity_2d_reminders(
        conn,
        sender=sender,
        run_date=run_date,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
    )

    assert stats.total_scanned == 1
    assert stats.eligible == 1
    assert stats.sent == 1
    assert sender.await_count == 1
    assert (user_id, run_date, REMINDER_TYPE_INACTIVITY_2D) in conn.deliveries


@pytest.mark.asyncio
async def test_inactivity_no_resend_without_new_stats_then_resend_after_new_gap():
    first_run = date(2026, 2, 22)
    user_id = "u-inactive-reset"
    users = [{"id": user_id, "telegram_id": 20202, "notifications_enabled": True}]
    conn = FakeJobsConn(users, daily_stats={})
    sender = AsyncMock()

    first = await run_inactivity_2d_reminders(
        conn,
        sender=sender,
        run_date=first_run,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
    )
    second_same_date = await run_inactivity_2d_reminders(
        conn,
        sender=sender,
        run_date=first_run,
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
    )

    conn.daily_stats[(user_id, date(2026, 2, 23))] = 1750

    after_activity = await run_inactivity_2d_reminders(
        conn,
        sender=sender,
        run_date=date(2026, 2, 24),
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
    )
    after_new_gap = await run_inactivity_2d_reminders(
        conn,
        sender=sender,
        run_date=date(2026, 2, 26),
        sleep_fn=_no_sleep,
        random_fn=lambda: 0.0,
    )

    assert first.sent == 1
    assert second_same_date.sent == 0
    assert second_same_date.skipped == 1
    assert after_activity.sent == 0
    assert after_new_gap.sent == 1
    assert sender.await_count == 2
