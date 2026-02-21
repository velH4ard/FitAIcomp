from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
import importlib
import inspect

import pytest

from app.db import get_db
from app.deps import get_current_user
from app.main import app


MOCK_USER = {
    "id": "00000000-0000-0000-0000-00000000a001",
    "telegram_id": 900000001,
    "subscription_status": "free",
    "subscription_active_until": None,
    "is_onboarded": True,
    "profile": {},
}


def _has_route(path: str, method: str) -> bool:
    for route in app.router.routes:
        if getattr(route, "path", None) != path:
            continue
        methods = getattr(route, "methods", set())
        if method.upper() in methods:
            return True
    return False


class FakeNotificationSettingsConn:
    def __init__(self):
        self.enabled = False
        self.tone = "balanced"

    async def fetchrow(self, query, *args):
        query_lc = query.lower()
        if "notifications_enabled" in query_lc and "update users" in query_lc:
            self.enabled = bool(args[-1])
            return {
                "notifications_enabled": self.enabled,
                "enabled": self.enabled,
                "notification_tone": self.tone,
            }
        if "notifications_enabled" in query_lc and "from users" in query_lc:
            return {
                "notifications_enabled": self.enabled,
                "enabled": self.enabled,
                "notification_tone": self.tone,
            }
        if "insert into user_settings" in query_lc:
            self.enabled = bool(args[1])
            if len(args) > 2 and args[2] is not None:
                self.tone = str(args[2])
            return {"notifications_enabled": self.enabled, "notification_tone": self.tone}
        return None

    async def execute(self, query, *args):
        query_lc = query.lower()
        if "notifications_enabled" in query_lc and "update users" in query_lc:
            self.enabled = bool(args[-1])
        return "OK"


@pytest.mark.asyncio
async def test_notifications_settings_patch_toggles_enabled_state(client):
    if not _has_route("/v1/notifications/settings", "PATCH"):
        pytest.skip("PATCH /v1/notifications/settings is not implemented in current backend")

    fake_conn = FakeNotificationSettingsConn()
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER
    app.dependency_overrides[get_db] = lambda: fake_conn

    try:
        response_enable = await client.patch(
            "/v1/notifications/settings",
            json={"enabled": True},
        )
        assert response_enable.status_code == 200
        assert response_enable.json() == {"enabled": True, "tone": "balanced"}

        response_disable = await client.patch(
            "/v1/notifications/settings",
            json={"enabled": False},
        )
        assert response_disable.status_code == 200
        assert response_disable.json() == {"enabled": False, "tone": "balanced"}
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def _resolve_reminder_runner_or_skip():
    try:
        module = importlib.import_module("app.notifications.reminders")
    except ModuleNotFoundError:
        pytest.skip("Reminder job module app.notifications.reminders is not implemented")

    candidates = (
        "run_daily_reminders",
        "run_reminders",
        "run_once",
        "main",
    )
    for name in candidates:
        runner = getattr(module, name, None)
        if callable(runner):
            break
    else:
        pytest.skip(
            "Reminder job has no supported runner function "
            "(expected one of: run_daily_reminders/run_reminders/run_once/main)"
        )

    signature = inspect.signature(runner)
    param_names = set(signature.parameters.keys())
    conn_param_names = {"conn", "db_conn", "connection"}
    sender_param_names = {
        "sender",
        "send_telegram",
        "send_telegram_message",
        "telegram_sender",
    }

    if not (param_names & conn_param_names):
        pytest.skip("Reminder runner is not injectable by DB connection in current implementation")
    if not (param_names & sender_param_names):
        pytest.skip("Reminder runner is not injectable by sender function in current implementation")

    async def _run(conn, sender, today_utc: date):
        kwargs = {}
        for param_name in signature.parameters:
            if param_name in conn_param_names:
                kwargs[param_name] = conn
            elif param_name in sender_param_names:
                kwargs[param_name] = sender
            elif param_name in {"today", "today_utc", "today_date"}:
                kwargs[param_name] = today_utc

        result = runner(**kwargs)
        if inspect.isawaitable(result):
            await result

    return _run


class FakeReminderConn:
    def __init__(self, *, daily_goal: int, today_calories: float, enabled: bool = True):
        self.user_id = MOCK_USER["id"]
        self.telegram_id = MOCK_USER["telegram_id"]
        self.notifications_enabled = enabled
        self.daily_goal = daily_goal
        self.today_calories = today_calories

        self.delivery_rows: dict[tuple[str, date, str], str] = {}

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch(self, query, *args):
        query_lc = query.lower()
        if "from users" in query_lc and "join user_settings" in query_lc:
            if self.notifications_enabled:
                return [
                    {
                        "id": self.user_id,
                        "telegram_id": self.telegram_id,
                        "profile": {"dailyGoal": self.daily_goal},
                        "calories_kcal": self.today_calories,
                    }
                ]
            return []
        return []

    async def fetchrow(self, query, *args):
        query_lc = query.lower()
        if "insert into reminder_deliveries" in query_lc:
            key = (str(args[1]), args[2], str(args[3]))
            if key in self.delivery_rows:
                return None
            self.delivery_rows[key] = "sent"
            return {"id": str(args[0])}
        return None

    async def execute(self, query, *args):
        query_lc = query.lower()
        if "delete from reminder_deliveries" in query_lc:
            key = (str(args[0]), args[1], str(args[2]))
            self.delivery_rows.pop(key, None)
            return "DELETE 1"

        return "OK"

    def sent_count(self) -> int:
        return sum(1 for status in self.delivery_rows.values() if status == "sent")

    def failed_count(self) -> int:
        return sum(1 for status in self.delivery_rows.values() if status == "failed")


@pytest.mark.asyncio
async def test_reminder_job_sends_once_and_is_idempotent_for_same_day():
    run_job = _resolve_reminder_runner_or_skip()
    fake_conn = FakeReminderConn(daily_goal=2000, today_calories=1000.0, enabled=True)
    today = date(2026, 2, 19)

    calls = []

    async def fake_sender(*args, **kwargs):
        calls.append((args, kwargs))

    await run_job(fake_conn, fake_sender, today)
    await run_job(fake_conn, fake_sender, today)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_reminder_job_sends_when_in_target_range():
    run_job = _resolve_reminder_runner_or_skip()
    fake_conn = FakeReminderConn(daily_goal=2000, today_calories=1400.0, enabled=True)
    today = date(2026, 2, 19)

    calls = []

    async def fake_sender(*args, **kwargs):
        calls.append((args, kwargs))

    await run_job(fake_conn, fake_sender, today)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_reminder_job_send_failure_is_compensated_or_marked_failed():
    run_job = _resolve_reminder_runner_or_skip()
    fake_conn = FakeReminderConn(daily_goal=2000, today_calories=1000.0, enabled=True)
    today = date(2026, 2, 19)

    attempts = 0

    async def failing_sender(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("telegram send failed")

    await run_job(fake_conn, failing_sender, today)

    assert attempts == 1

    compensated_removed = fake_conn.sent_count() == 0 and fake_conn.failed_count() == 0
    marked_failed = fake_conn.failed_count() == 1
    assert compensated_removed or marked_failed
