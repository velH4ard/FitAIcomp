import importlib
import os
import sys

import pytest


def test_import_app_main_has_all_required_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: app.main import must not fail on missing deps."""
    monkeypatch.setenv("BOT_TOKEN", os.getenv("BOT_TOKEN", "test_bot_token"))
    monkeypatch.setenv("JWT_SECRET", os.getenv("JWT_SECRET", "test_jwt_secret"))

    sys.modules.pop("app.main", None)

    try:
        importlib.import_module("app.main")
    except ModuleNotFoundError as exc:
        pytest.fail(f"Importing app.main failed with ModuleNotFoundError: {exc}")


@pytest.mark.parametrize(
    "module_name",
    [
        "backend.scripts.send_monthly_reports",
        "backend.scripts.send_inactivity_reminders",
        "app.notifications.monthly_reports",
        "app.notifications.inactivity_reminders",
    ],
)
def test_retention_job_modules_importable(module_name: str) -> None:
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"Importing {module_name} failed with ModuleNotFoundError: {exc}")
