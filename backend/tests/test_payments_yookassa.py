import importlib
import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

import asyncpg
import pytest

from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.config import settings
from app import payments


USER_ID = "00000000-0000-0000-0000-000000000101"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000202"
TELEGRAM_ID = 101010101


def assert_error_response(response, status_code: int, error_code: str) -> None:
    assert response.status_code == status_code
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == error_code
    assert "message" in body["error"]
    assert "details" in body["error"]


def _patch_optional(monkeypatch, module_name: str, attr_name: str, value) -> None:
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, attr_name, value, raising=False)


@pytest.fixture(autouse=True)
def clear_webhook_dedupe_memory():
    payments._webhook_dedupe_memory.clear()
    yield
    payments._webhook_dedupe_memory.clear()


@pytest.fixture
def auth_user_free():
    return {
        "id": USER_ID,
        "telegram_id": TELEGRAM_ID,
        "username": "payment-user",
        "is_onboarded": True,
        "subscription_status": "free",
        "subscription_active_until": None,
        "profile": {},
    }


@pytest.fixture
def auth_user_active_future():
    return {
        "id": USER_ID,
        "telegram_id": TELEGRAM_ID,
        "username": "payment-user",
        "is_onboarded": True,
        "subscription_status": "active",
        "subscription_active_until": datetime(2099, 1, 1, tzinfo=timezone.utc),
        "profile": {},
    }


@pytest.fixture
def auth_user_active_past():
    return {
        "id": USER_ID,
        "telegram_id": TELEGRAM_ID,
        "username": "payment-user",
        "is_onboarded": True,
        "subscription_status": "active",
        "subscription_active_until": datetime(2000, 1, 1, tzinfo=timezone.utc),
        "profile": {},
    }


@pytest.fixture
def auth_user_other_free():
    return {
        "id": OTHER_USER_ID,
        "telegram_id": 202020202,
        "username": "other-user",
        "is_onboarded": True,
        "subscription_status": "free",
        "subscription_active_until": None,
        "profile": {},
    }


@pytest.fixture
def override_db_for_payments():
    class NoopConn:
        async def fetchrow(self, *args, **kwargs):
            return None

        async def execute(self, *args, **kwargs):
            return "OK"

    conn = NoopConn()

    async def _override_get_db():
        yield conn

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield conn
    finally:
        app.dependency_overrides.pop(get_db, None)


class ExtensionCaptureConn:
    def __init__(self, users: dict[str, dict[str, Any]]):
        self.users = users
        self.last_extension_query: Optional[str] = None
        self.last_extension_params: Optional[Tuple[Any, ...]] = None

    async def fetchrow(self, query, *args):
        if "RETURNING subscription_active_until" in query:
            self.last_extension_query = query
            self.last_extension_params = args

            user_id = str(args[0])
            duration_days = args[1]

            if not isinstance(duration_days, int):
                raise AssertionError("duration_days must be int")

            current_until = self.users[user_id]["subscription_active_until"]
            now_utc = datetime.now(timezone.utc)
            if isinstance(current_until, datetime) and current_until > now_utc:
                base = current_until
            else:
                base = now_utc

            new_until = base + timedelta(days=duration_days)
            self.users[user_id]["subscription_active_until"] = new_until
            self.users[user_id]["subscription_status"] = "active"
            return {"subscription_active_until": new_until}

        return None

    async def execute(self, query, *args):
        return "OK"


class PaymentMappingConn:
    def __init__(self, users: dict[str, dict[str, Any]]):
        self.users = users
        self.payment_map: dict[str, str] = {}
        self.payment_status: dict[str, str] = {}
        self.payment_event_status: dict[str, str] = {}
        self.payment_mapping_inserts: list[tuple[str, str, str]] = []
        self.events: list[dict[str, Any]] = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self_nonlocal):
                return self

            async def __aexit__(self_nonlocal, exc_type, exc, tb):
                return False

        return _Tx()

    async def fetchrow(self, query, *args):
        if "SELECT photos_used FROM usage_daily" in query:
            return None

        if "FROM users" in query and "WHERE id = $1::uuid" in query and "FOR UPDATE" not in query:
            user_id = str(args[0])
            user = self.users.get(user_id)
            if not user:
                return None
            return {
                "id": user_id,
                "subscription_status": user.get("subscription_status", "free"),
                "subscription_active_until": user.get("subscription_active_until"),
                "referral_credits": user.get("referral_credits", 0),
            }

        if "FROM yookassa_payments" in query and "AND user_id = $2::uuid" in query:
            payment_id = str(args[0])
            expected_user_id = str(args[1])
            user_id = self.payment_map.get(payment_id)
            if not user_id or user_id != expected_user_id:
                return None
            return {
                "user_id": user_id,
                "status": self.payment_status.get(payment_id, "created"),
            }

        if "SELECT user_id FROM yookassa_payments" in query:
            payment_id = str(args[0])
            user_id = self.payment_map.get(payment_id)
            if not user_id:
                return None
            return {"user_id": user_id}

        if "SELECT status FROM yookassa_payments" in query:
            payment_id = str(args[0])
            status = self.payment_status.get(payment_id)
            if status is None:
                return None
            return {"status": status}

        if "FROM payment_webhook_events" in query and "WHERE dedupe_key = $1" in query:
            dedupe_key = str(args[0])
            status = self.payment_event_status.get(dedupe_key)
            if status is None:
                return None
            return {"dedupe_key": dedupe_key, "status": status}

        if "FROM events" in query and "event_type = 'payment_succeeded'" in query:
            user_id = str(args[0])
            payment_id = str(args[1])
            for event in reversed(self.events):
                if (
                    event.get("user_id") == user_id
                    and event.get("event_type") == "payment_succeeded"
                    and str((event.get("payload") or {}).get("paymentId") or "") == payment_id
                ):
                    return {"id": "evt-local-payment-succeeded"}
            return None

        if "INSERT INTO payment_webhook_events" in query and "ON CONFLICT (dedupe_key) DO NOTHING" in query:
            dedupe_key = str(args[0])
            if dedupe_key in self.payment_event_status:
                return None
            self.payment_event_status[dedupe_key] = "processing"
            return {"dedupe_key": dedupe_key}

        if "SELECT subscription_active_until FROM users" in query and "FOR UPDATE" in query:
            user_id = str(args[0])
            user = self.users.get(user_id)
            if not user:
                return None
            return {"subscription_active_until": user.get("subscription_active_until")}

        return None

    async def execute(self, query, *args):
        if "INSERT INTO yookassa_payments" in query:
            payment_id = str(args[0])
            user_id = str(args[1])
            idempotence_key = str(args[2])
            status = str(args[3]) if len(args) > 3 else "created"
            self.payment_map[payment_id] = user_id
            self.payment_status[payment_id] = status
            self.payment_mapping_inserts.append((payment_id, user_id, idempotence_key))
            return "OK"

        if "INSERT INTO payment_webhook_events" in query:
            dedupe_key = str(args[0])
            if dedupe_key in self.payment_event_status:
                raise asyncpg.UniqueViolationError("duplicate")
            self.payment_event_status[dedupe_key] = "processing"
            return "OK"

        if "INSERT INTO events" in query:
            user_id = str(args[0])
            event_type = str(args[1])
            payload_raw = args[2]
            if isinstance(payload_raw, str):
                payload = json.loads(payload_raw)
            elif isinstance(payload_raw, dict):
                payload = payload_raw
            else:
                payload = {}
            self.events.append(
                {
                    "user_id": user_id,
                    "event_type": event_type,
                    "payload": payload,
                }
            )
            return "OK"

        if "UPDATE payment_webhook_events" in query and "SET status = 'completed'" in query:
            dedupe_key = str(args[0])
            if dedupe_key in self.payment_event_status:
                self.payment_event_status[dedupe_key] = "completed"
            return "OK"

        if "UPDATE users" in query and "subscription_active_until" in query:
            user_id = str(args[0])
            new_until = args[1]
            user = self.users.get(user_id)
            if user is not None:
                user["subscription_status"] = "active"
                user["subscription_active_until"] = new_until
            return "OK"

        if "UPDATE yookassa_payments" in query:
            payment_id = str(args[0])
            if "status = 'succeeded'" in query:
                self.payment_status[payment_id] = "succeeded"
            elif "status = 'canceled'" in query:
                self.payment_status[payment_id] = "canceled"
            elif "status = 'created'" in query:
                self.payment_status[payment_id] = "created"
            return "OK"

        if "DELETE FROM payment_webhook_events" in query:
            dedupe_key = str(args[0])
            self.payment_event_status.pop(dedupe_key, None)
            return "OK"

        return "OK"


@pytest.fixture
def override_db_capture_extension(auth_user_active_future, auth_user_active_past):
    users = {str(auth_user_active_future["id"]): auth_user_active_future}
    conn = ExtensionCaptureConn(users=users)

    async def _override_get_db():
        yield conn

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield conn
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def override_db_with_payment_mapping(auth_user_free):
    users = {str(auth_user_free["id"]): auth_user_free}
    conn = PaymentMappingConn(users=users)

    async def _override_get_db():
        yield conn

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield conn
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def override_db_with_payment_mapping_two_users(auth_user_free, auth_user_other_free):
    users = {
        str(auth_user_free["id"]): auth_user_free,
        str(auth_user_other_free["id"]): auth_user_other_free,
    }
    conn = PaymentMappingConn(users=users)

    async def _override_get_db():
        yield conn

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield conn
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def mock_yookassa_create_success(monkeypatch):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "paymentId": "pay_test_001",
            "confirmationUrl": "https://yookassa.test/confirm/pay_test_001",
        }

    for module_name in ("app.main", "app.payments"):
        _patch_optional(monkeypatch, module_name, "create_yookassa_payment", _fake_create_payment)
        _patch_optional(monkeypatch, module_name, "yookassa_create_payment", _fake_create_payment)
        _patch_optional(monkeypatch, module_name, "_create_yookassa_payment", _fake_create_payment)


@pytest.fixture
def mock_yookassa_create_failure(monkeypatch):
    from app.errors import FitAIError

    async def _fake_create_payment(*args, **kwargs):
        raise FitAIError(
            code="PAYMENT_PROVIDER_ERROR",
            message="Ошибка платежного провайдера",
            status_code=502,
            details={"stage": "create_payment"},
        )

    for module_name in ("app.main", "app.payments"):
        _patch_optional(monkeypatch, module_name, "create_yookassa_payment", _fake_create_payment)
        _patch_optional(monkeypatch, module_name, "yookassa_create_payment", _fake_create_payment)
        _patch_optional(monkeypatch, module_name, "_create_yookassa_payment", _fake_create_payment)


@pytest.fixture
def mock_webhook_verified(monkeypatch):
    def _fake_verify(*args, **kwargs):
        return True

    for module_name in ("app.main", "app.payments"):
        _patch_optional(monkeypatch, module_name, "verify_yookassa_webhook", _fake_verify)
        _patch_optional(monkeypatch, module_name, "verify_webhook_signature", _fake_verify)
        _patch_optional(monkeypatch, module_name, "_verify_yookassa_webhook", _fake_verify)


@pytest.fixture
def mock_webhook_invalid(monkeypatch):
    def _fake_verify(*args, **kwargs):
        return False

    for module_name in ("app.main", "app.payments"):
        _patch_optional(monkeypatch, module_name, "verify_yookassa_webhook", _fake_verify)
        _patch_optional(monkeypatch, module_name, "verify_webhook_signature", _fake_verify)
        _patch_optional(monkeypatch, module_name, "_verify_yookassa_webhook", _fake_verify)


def _paid_webhook_payload(event_id: str, user_id: str = USER_ID, payment_id: str = "payment-001"):
    return {
        "id": event_id,
        "event": "payment.succeeded",
        "object": {
            "id": payment_id,
            "status": "succeeded",
            "paid": True,
            "metadata": {
                "user_id": user_id,
                "telegram_id": str(TELEGRAM_ID),
                "plan": "monthly_499",
            },
        },
    }


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    raw = f"{username}:{password}".encode("utf-8")
    token = base64.b64encode(raw).decode("ascii")
    return {"Authorization": f"Basic {token}"}


@pytest.mark.asyncio
async def test_create_payment_success_returns_payment_id_and_confirmation_url(
    client,
    override_db_for_payments,
    auth_user_free,
    mock_yookassa_create_success,
):
    app.dependency_overrides[get_current_user] = lambda: auth_user_free
    try:
        response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-create-success-1",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["paymentId"]
        assert body["confirmationUrl"].startswith("https://")
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_create_payment_failure_returns_payment_provider_error(
    client,
    override_db_for_payments,
    auth_user_free,
    mock_yookassa_create_failure,
):
    app.dependency_overrides[get_current_user] = lambda: auth_user_free
    try:
        response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-create-fail-1",
            },
        )

        assert_error_response(response, 502, "PAYMENT_PROVIDER_ERROR")
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_webhook_success_activates_and_extends_subscription_by_30_days(
    client,
    override_db_for_payments,
    auth_user_active_future,
    monkeypatch,
):
    app.dependency_overrides[get_current_user] = lambda: auth_user_active_future
    try:
        monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
        monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

        before_until = auth_user_active_future["subscription_active_until"]
        expected_until = before_until + timedelta(days=30)

        response = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=_paid_webhook_payload("evt-success-1"),
            headers=_basic_auth_header("fitai-shop-id", "fitai-secret"),
        )

        assert response.status_code == 200
        assert response.json().get("ok") is True

        subscription_response = await client.get("/v1/subscription")
        assert subscription_response.status_code == 200
        data = subscription_response.json()
        assert data["status"] == "active"
        assert data["dailyLimit"] == 20
        actual_until = datetime.fromisoformat(data["activeUntil"].replace("Z", "+00:00"))
        assert actual_until == expected_until
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_webhook_subscription_extension_query_uses_int_interval_multiplier(
    client,
    override_db_capture_extension,
    auth_user_active_future,
    monkeypatch,
):
    app.dependency_overrides[get_current_user] = lambda: auth_user_active_future
    monkeypatch.setattr(settings, "SUBSCRIPTION_DURATION_DAYS", 30)
    monkeypatch.setattr(payments, "verify_yookassa_webhook", lambda *_args, **_kwargs: True)
    override_db_capture_extension.users[str(auth_user_active_future["id"])] = auth_user_active_future

    before_until = auth_user_active_future["subscription_active_until"]
    expected_until = before_until + timedelta(days=30)
    try:
        response = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=_paid_webhook_payload("evt-int-duration-query-1"),
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}

        assert auth_user_active_future["subscription_active_until"] == expected_until
        assert auth_user_active_future["subscription_status"] == "active"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_webhook_extension_for_expired_subscription_uses_now_plus_int_days(
    client,
    override_db_capture_extension,
    auth_user_active_past,
    monkeypatch,
):
    app.dependency_overrides[get_current_user] = lambda: auth_user_active_past
    monkeypatch.setattr(settings, "SUBSCRIPTION_DURATION_DAYS", 30)
    monkeypatch.setattr(payments, "verify_yookassa_webhook", lambda *_args, **_kwargs: True)
    override_db_capture_extension.users[str(auth_user_active_past["id"])] = auth_user_active_past

    before = datetime.now(timezone.utc)
    try:
        response = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=_paid_webhook_payload("evt-int-duration-query-2"),
        )
        after = datetime.now(timezone.utc)

        assert response.status_code == 200
        assert response.json() == {"ok": True}

        expected_min = before + timedelta(days=30)
        expected_max = after + timedelta(days=30)
        assert expected_min <= auth_user_active_past["subscription_active_until"] <= expected_max
        assert auth_user_active_past["subscription_status"] == "active"

    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_webhook_duplicate_same_event_is_idempotent_and_not_double_extended(
    client,
    override_db_for_payments,
    auth_user_active_future,
    monkeypatch,
):
    app.dependency_overrides[get_current_user] = lambda: auth_user_active_future
    try:
        monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
        monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

        payload = _paid_webhook_payload("evt-duplicate-1")

        first = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=payload,
            headers=_basic_auth_header("fitai-shop-id", "fitai-secret"),
        )
        assert first.status_code == 200
        assert first.json().get("ok") is True

        after_first = await client.get("/v1/subscription")
        assert after_first.status_code == 200
        first_until = after_first.json()["activeUntil"]

        second = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=payload,
            headers=_basic_auth_header("fitai-shop-id", "fitai-secret"),
        )
        assert second.status_code == 200
        assert second.json().get("ok") is True

        after_second = await client.get("/v1/subscription")
        assert after_second.status_code == 200
        second_until = after_second.json()["activeUntil"]

        assert second_until == first_until
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_webhook_duplicate_payment_succeeded_with_new_event_id_is_idempotent(
    client,
    override_db_for_payments,
    auth_user_active_future,
    monkeypatch,
):
    app.dependency_overrides[get_current_user] = lambda: auth_user_active_future
    try:
        monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
        monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

        first = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=_paid_webhook_payload("evt-dup-new-id-1", payment_id="payment-dup-001"),
            headers=_basic_auth_header("fitai-shop-id", "fitai-secret"),
        )
        assert first.status_code == 200

        after_first = await client.get("/v1/subscription")
        first_until = after_first.json()["activeUntil"]

        second = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=_paid_webhook_payload("evt-dup-new-id-2", payment_id="payment-dup-001"),
            headers=_basic_auth_header("fitai-shop-id", "fitai-secret"),
        )
        assert second.status_code == 200

        after_second = await client.get("/v1/subscription")
        second_until = after_second.json()["activeUntil"]

        assert second_until == first_until
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_webhook_invalid_basic_auth_returns_payment_webhook_invalid(
    client,
    override_db_for_payments,
    monkeypatch,
):
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

    response = await client.post(
        "/v1/subscription/yookassa/webhook",
        json=_paid_webhook_payload("evt-invalid-signature-1"),
        headers=_basic_auth_header("fitai-shop-id", "wrong-secret"),
    )

    assert_error_response(response, 401, "PAYMENT_WEBHOOK_INVALID")


@pytest.mark.asyncio
async def test_webhook_without_auth_bypass_off_returns_payment_webhook_invalid(
    client,
    override_db_for_payments,
    monkeypatch,
):
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "PAYMENTS_WEBHOOK_DEV_BYPASS", 0)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

    response = await client.post(
        "/v1/subscription/yookassa/webhook",
        json=_paid_webhook_payload("evt-missing-auth-bypass-off-1"),
    )

    assert_error_response(response, 401, "PAYMENT_WEBHOOK_INVALID")


@pytest.mark.asyncio
async def test_webhook_with_missing_secret_key_fails_verification_even_with_auth(
    client,
    override_db_for_payments,
    monkeypatch,
):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "PAYMENTS_WEBHOOK_DEV_BYPASS", 0)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "")

    response = await client.post(
        "/v1/subscription/yookassa/webhook",
        json=_paid_webhook_payload("evt-missing-secret-key-1"),
        headers=_basic_auth_header("fitai-shop-id", "fitai-secret"),
    )

    assert_error_response(response, 401, "PAYMENT_WEBHOOK_INVALID")


@pytest.mark.asyncio
async def test_webhook_without_auth_dev_bypass_with_cf_header_is_accepted(
    client,
    override_db_for_payments,
    auth_user_active_future,
    monkeypatch,
):
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "PAYMENTS_WEBHOOK_DEV_BYPASS", 1)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

    app.dependency_overrides[get_current_user] = lambda: auth_user_active_future
    try:
        response = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=_paid_webhook_payload("evt-dev-bypass-cf-header-1"),
            headers={"CF-Ray": "dev-ray-test"},
        )

        assert response.status_code == 200
        assert response.json().get("ok") is True
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_webhook_without_auth_production_ignores_bypass_and_returns_invalid(
    client,
    override_db_for_payments,
    monkeypatch,
):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "PAYMENTS_WEBHOOK_DEV_BYPASS", 1)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

    response = await client.post(
        "/v1/subscription/yookassa/webhook",
        json=_paid_webhook_payload("evt-prod-bypass-ignored-1"),
        headers={"CF-Ray": "prod-ray-test"},
    )

    assert_error_response(response, 401, "PAYMENT_WEBHOOK_INVALID")


@pytest.mark.asyncio
async def test_webhook_ip_allowlist_blocks_non_listed_ip_in_production(
    client,
    override_db_for_payments,
    monkeypatch,
):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "PAYMENTS_WEBHOOK_DEV_BYPASS", 0)
    monkeypatch.setattr(settings, "PAYMENTS_WEBHOOK_IP_ALLOWLIST", "203.0.113.10")
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

    response = await client.post(
        "/v1/subscription/yookassa/webhook",
        json=_paid_webhook_payload("evt-ip-block-1"),
        headers={
            **_basic_auth_header("fitai-shop-id", "fitai-secret"),
            "X-Forwarded-For": "198.51.100.7",
        },
    )

    assert_error_response(response, 401, "PAYMENT_WEBHOOK_INVALID")


@pytest.mark.asyncio
async def test_webhook_ip_allowlist_allows_listed_ip_in_production(
    client,
    override_db_for_payments,
    auth_user_active_future,
    monkeypatch,
):
    app.dependency_overrides[get_current_user] = lambda: auth_user_active_future
    try:
        monkeypatch.setattr(settings, "APP_ENV", "production")
        monkeypatch.setattr(settings, "PAYMENTS_WEBHOOK_DEV_BYPASS", 0)
        monkeypatch.setattr(settings, "PAYMENTS_WEBHOOK_IP_ALLOWLIST", "203.0.113.10")
        monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
        monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

        response = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=_paid_webhook_payload("evt-ip-allow-1"),
            headers={
                **_basic_auth_header("fitai-shop-id", "fitai-secret"),
                "X-Forwarded-For": "203.0.113.10, 10.0.0.3",
            },
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_get_subscription_computes_active_and_expired_limits_correctly(
    client,
    override_db_for_payments,
    auth_user_active_future,
    auth_user_active_past,
):
    app.dependency_overrides[get_current_user] = lambda: auth_user_active_future
    try:
        active_response = await client.get("/v1/subscription")
        assert active_response.status_code == 200
        active_data = active_response.json()
        assert active_data["status"] == "active"
        assert active_data["dailyLimit"] == 20

        app.dependency_overrides[get_current_user] = lambda: auth_user_active_past
        expired_response = await client.get("/v1/subscription")
        assert expired_response.status_code == 200
        expired_data = expired_response.json()
        assert expired_data["status"] == "expired"
        assert expired_data["dailyLimit"] == 2
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_subscription_uses_configured_price_and_create_payment_amount(
    client,
    override_db_for_payments,
    auth_user_free,
    monkeypatch,
):
    captured_payload: dict = {}

    async def _fake_create_payment(*args, **kwargs):
        payload = kwargs.get("payload")
        if payload is None and args:
            payload = args[0]
        if isinstance(payload, dict):
            captured_payload.update(payload)
        return {
            "paymentId": "pay_test_price_001",
            "confirmationUrl": "https://yookassa.test/confirm/pay_test_price_001",
        }

    monkeypatch.setattr(settings, "SUBSCRIPTION_PRICE_RUB", 10)
    _patch_optional(monkeypatch, "app.payments", "_create_yookassa_payment", _fake_create_payment)

    app.dependency_overrides[get_current_user] = lambda: auth_user_free
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-price-override-1",
            },
        )
        assert create_response.status_code == 200
        assert captured_payload["amount"]["value"] == "10.00"

        subscription_response = await client.get("/v1/subscription")
        assert subscription_response.status_code == 200
        assert subscription_response.json()["priceRubPerMonth"] == 10
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_create_payment_stores_payment_user_mapping(
    client,
    override_db_with_payment_mapping,
    auth_user_free,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-map-001",
            "confirmation": {"confirmation_url": "https://yookassa.test/confirm/pay-map-001"},
        }

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)

    app.dependency_overrides[get_current_user] = lambda: auth_user_free
    try:
        response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-map-create-001",
            },
        )

        assert response.status_code == 200
        assert override_db_with_payment_mapping.payment_mapping_inserts == [
            ("pay-map-001", str(auth_user_free["id"]), "idem-map-create-001")
        ]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_webhook_success_without_metadata_uses_stored_mapping(
    client,
    override_db_with_payment_mapping,
    auth_user_free,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-map-002",
            "confirmation": {"confirmation_url": "https://yookassa.test/confirm/pay-map-002"},
        }

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

    app.dependency_overrides[get_current_user] = lambda: auth_user_free
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-map-create-002",
            },
        )
        assert create_response.status_code == 200

        webhook_payload = {
            "id": "evt-map-002",
            "event": "payment.succeeded",
            "object": {
                "id": "pay-map-002",
                "status": "succeeded",
                "paid": True,
                "captured": True,
                "metadata": {},
            },
        }
        webhook_response = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=webhook_payload,
            headers=_basic_auth_header("fitai-shop-id", "fitai-secret"),
        )
        assert webhook_response.status_code == 200
        assert webhook_response.json() == {"ok": True}

        me_response = await client.get("/v1/me")
        assert me_response.status_code == 200
        me_data = me_response.json()
        assert me_data["subscription"]["status"] == "active"

        subscription_response = await client.get("/v1/subscription")
        assert subscription_response.status_code == 200
        subscription_data = subscription_response.json()
        assert subscription_data["status"] == "active"
        assert subscription_data["dailyLimit"] == 20
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_webhook_invalid_auth_does_not_change_subscription_state(
    client,
    override_db_with_payment_mapping,
    auth_user_free,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-map-003",
            "confirmation": {"confirmation_url": "https://yookassa.test/confirm/pay-map-003"},
        }

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

    app.dependency_overrides[get_current_user] = lambda: auth_user_free
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-map-create-003",
            },
        )
        assert create_response.status_code == 200

        webhook_payload = {
            "id": "evt-map-003",
            "event": "payment.succeeded",
            "object": {
                "id": "pay-map-003",
                "status": "succeeded",
                "paid": True,
                "captured": True,
                "metadata": {},
            },
        }
        webhook_response = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=webhook_payload,
            headers=_basic_auth_header("fitai-shop-id", "wrong-secret"),
        )
        assert_error_response(webhook_response, 401, "PAYMENT_WEBHOOK_INVALID")

        subscription_response = await client.get("/v1/subscription")
        assert subscription_response.status_code == 200
        subscription_data = subscription_response.json()
        assert subscription_data["status"] == "free"
        assert subscription_data["dailyLimit"] == 2
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_refresh_success_activates_subscription_with_succeeded_payment(
    client,
    override_db_with_payment_mapping,
    auth_user_free,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-001",
            "confirmation": {"confirmation_url": "https://yookassa.test/confirm/pay-refresh-001"},
        }

    async def _fake_fetch_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-001",
            "status": "succeeded",
            "paid": True,
            "captured": True,
            "metadata": {},
        }

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(payments, "_fetch_yookassa_payment", _fake_fetch_payment)

    app.dependency_overrides[get_current_user] = lambda: auth_user_free
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-refresh-create-001",
            },
        )
        assert create_response.status_code == 200

        refresh_response = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-refresh-001"},
        )
        assert refresh_response.status_code == 200
        body = refresh_response.json()
        assert body["status"] == "active"
        assert body["dailyLimit"] == 20
        assert override_db_with_payment_mapping.payment_status["pay-refresh-001"] == "succeeded"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_refresh_fetch_error_with_local_success_returns_active_subscription(
    client,
    override_db_with_payment_mapping,
    auth_user_free,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-fetch-fail-local-001",
            "confirmation": {
                "confirmation_url": "https://yookassa.test/confirm/pay-refresh-fetch-fail-local-001"
            },
        }

    from app.errors import FitAIError

    async def _fake_fetch_payment_fail(*args, **kwargs):
        raise FitAIError(
            code="PAYMENT_PROVIDER_ERROR",
            message="Ошибка платежного провайдера",
            status_code=502,
            details={"stage": "fetch_payment", "providerStatus": None},
        )

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(payments, "_fetch_yookassa_payment", _fake_fetch_payment_fail)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

    app.dependency_overrides[get_current_user] = lambda: auth_user_free
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-refresh-fetch-fail-local-create-001",
            },
        )
        assert create_response.status_code == 200

        webhook_response = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=_paid_webhook_payload(
                "evt-refresh-fetch-fail-local-1",
                user_id=str(auth_user_free["id"]),
                payment_id="pay-refresh-fetch-fail-local-001",
            ),
            headers=_basic_auth_header("fitai-shop-id", "fitai-secret"),
        )
        assert webhook_response.status_code == 200

        refresh_response = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-refresh-fetch-fail-local-001"},
        )
        assert refresh_response.status_code == 200
        body = refresh_response.json()
        assert body["status"] == "active"
        assert body["dailyLimit"] == 20
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_refresh_pending_returns_current_subscription_unchanged(
    client,
    override_db_with_payment_mapping,
    auth_user_free,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-pending-001",
            "confirmation": {"confirmation_url": "https://yookassa.test/confirm/pay-refresh-pending-001"},
        }

    async def _fake_fetch_payment_pending(*args, **kwargs):
        return {
            "id": "pay-refresh-pending-001",
            "status": "pending",
            "paid": False,
            "captured": False,
            "metadata": {},
        }

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(payments, "_fetch_yookassa_payment", _fake_fetch_payment_pending)

    app.dependency_overrides[get_current_user] = lambda: auth_user_free
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-refresh-pending-create-001",
            },
        )
        assert create_response.status_code == 200

        before_response = await client.get("/v1/subscription")
        assert before_response.status_code == 200
        before_body = before_response.json()

        refresh_response = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-refresh-pending-001"},
        )
        assert refresh_response.status_code == 200
        refresh_body = refresh_response.json()
        assert refresh_body["status"] == before_body["status"]
        assert refresh_body["activeUntil"] == before_body["activeUntil"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_refresh_canceled_returns_predictable_provider_error_details(
    client,
    override_db_with_payment_mapping,
    auth_user_free,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-canceled-001",
            "confirmation": {"confirmation_url": "https://yookassa.test/confirm/pay-refresh-canceled-001"},
        }

    async def _fake_fetch_payment_canceled(*args, **kwargs):
        return {
            "id": "pay-refresh-canceled-001",
            "status": "canceled",
            "paid": False,
            "captured": False,
            "metadata": {},
        }

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(payments, "_fetch_yookassa_payment", _fake_fetch_payment_canceled)

    app.dependency_overrides[get_current_user] = lambda: auth_user_free
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-refresh-canceled-create-001",
            },
        )
        assert create_response.status_code == 200

        refresh_response = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-refresh-canceled-001"},
        )
        assert_error_response(refresh_response, 502, "PAYMENT_PROVIDER_ERROR")
        error = refresh_response.json()["error"]
        assert error["details"]["stage"] == "refresh_payment_status"
        assert error["details"]["paymentStatus"] == "canceled"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_refresh_after_webhook_fetch_error_does_not_double_extend_subscription(
    client,
    override_db_with_payment_mapping,
    auth_user_active_future,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-after-webhook-fetch-fail-001",
            "confirmation": {
                "confirmation_url": "https://yookassa.test/confirm/pay-refresh-after-webhook-fetch-fail-001"
            },
        }

    from app.errors import FitAIError

    async def _fake_fetch_payment_fail(*args, **kwargs):
        raise FitAIError(
            code="PAYMENT_PROVIDER_ERROR",
            message="Ошибка платежного провайдера",
            status_code=502,
            details={"stage": "fetch_payment", "providerStatus": None},
        )

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(payments, "_fetch_yookassa_payment", _fake_fetch_payment_fail)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

    override_db_with_payment_mapping.users[str(auth_user_active_future["id"])] = auth_user_active_future
    app.dependency_overrides[get_current_user] = lambda: auth_user_active_future
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-refresh-after-webhook-fetch-fail-create-001",
            },
        )
        assert create_response.status_code == 200

        webhook_response = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=_paid_webhook_payload(
                "evt-refresh-after-webhook-fetch-fail-1",
                user_id=str(auth_user_active_future["id"]),
                payment_id="pay-refresh-after-webhook-fetch-fail-001",
            ),
            headers=_basic_auth_header("fitai-shop-id", "fitai-secret"),
        )
        assert webhook_response.status_code == 200

        subscription_after_webhook = await client.get("/v1/subscription")
        assert subscription_after_webhook.status_code == 200
        until_after_webhook = subscription_after_webhook.json()["activeUntil"]

        refresh_response = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-refresh-after-webhook-fetch-fail-001"},
        )
        assert refresh_response.status_code == 200
        assert refresh_response.json()["activeUntil"] == until_after_webhook
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "local_proof_mode",
    ["payment_status_succeeded", "completed_marker"],
)
async def test_refresh_provider_fetch_failure_with_local_success_proof_returns_active_subscription(
    client,
    override_db_with_payment_mapping,
    auth_user_active_future,
    monkeypatch,
    local_proof_mode,
):
    from app.errors import FitAIError

    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-provider-fail-001",
            "confirmation": {
                "confirmation_url": "https://yookassa.test/confirm/pay-refresh-provider-fail-001"
            },
        }

    async def _fake_fetch_payment(*args, **kwargs):
        raise FitAIError(
            code="PAYMENT_PROVIDER_ERROR",
            message="Ошибка платежного провайдера",
            status_code=502,
            details={"stage": "fetch_payment", "providerStatus": 502},
        )

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(payments, "_fetch_yookassa_payment", _fake_fetch_payment)

    override_db_with_payment_mapping.users[str(auth_user_active_future["id"])] = auth_user_active_future
    app.dependency_overrides[get_current_user] = lambda: auth_user_active_future
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": f"idem-refresh-provider-fail-create-{local_proof_mode}",
            },
        )
        assert create_response.status_code == 200

        if local_proof_mode == "payment_status_succeeded":
            override_db_with_payment_mapping.payment_status["pay-refresh-provider-fail-001"] = "succeeded"
        else:
            dedupe_key = payments._payment_success_dedupe_key("pay-refresh-provider-fail-001")
            override_db_with_payment_mapping.payment_event_status[dedupe_key] = "completed"

        refresh_response = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-refresh-provider-fail-001"},
        )
        assert refresh_response.status_code == 200
        assert refresh_response.json()["status"] == "active"
        assert refresh_response.json()["dailyLimit"] == 20
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_refresh_pending_returns_200_without_subscription_activation(
    client,
    override_db_with_payment_mapping,
    auth_user_free,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-pending-001",
            "confirmation": {"confirmation_url": "https://yookassa.test/confirm/pay-refresh-pending-001"},
        }

    async def _fake_fetch_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-pending-001",
            "status": "pending",
            "paid": False,
            "captured": False,
            "metadata": {},
        }

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(payments, "_fetch_yookassa_payment", _fake_fetch_payment)

    app.dependency_overrides[get_current_user] = lambda: auth_user_free
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-refresh-pending-create-001",
            },
        )
        assert create_response.status_code == 200

        refresh_response = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-refresh-pending-001"},
        )
        assert refresh_response.status_code == 200
        body = refresh_response.json()
        assert body["status"] == "free"
        assert body["dailyLimit"] == 2
        assert body["activeUntil"] is None
        assert override_db_with_payment_mapping.payment_status["pay-refresh-pending-001"] == "created"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_refresh_canceled_returns_spec_aligned_payment_provider_error(
    client,
    override_db_with_payment_mapping,
    auth_user_free,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-canceled-001",
            "confirmation": {"confirmation_url": "https://yookassa.test/confirm/pay-refresh-canceled-001"},
        }

    async def _fake_fetch_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-canceled-001",
            "status": "canceled",
            "paid": False,
            "captured": False,
            "metadata": {},
        }

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(payments, "_fetch_yookassa_payment", _fake_fetch_payment)

    app.dependency_overrides[get_current_user] = lambda: auth_user_free
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-refresh-canceled-create-001",
            },
        )
        assert create_response.status_code == 200

        refresh_response = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-refresh-canceled-001"},
        )
        assert_error_response(refresh_response, 502, "PAYMENT_PROVIDER_ERROR")
        assert refresh_response.json()["error"]["details"]["providerStatus"] == "canceled"
        assert override_db_with_payment_mapping.payment_status["pay-refresh-canceled-001"] == "canceled"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_refresh_not_found_for_other_user_payment_id(
    client,
    override_db_with_payment_mapping_two_users,
    auth_user_free,
    auth_user_other_free,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-foreign-001",
            "confirmation": {"confirmation_url": "https://yookassa.test/confirm/pay-refresh-foreign-001"},
        }

    async def _fake_fetch_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-foreign-001",
            "status": "succeeded",
            "paid": True,
            "captured": True,
            "metadata": {},
        }

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(payments, "_fetch_yookassa_payment", _fake_fetch_payment)

    app.dependency_overrides[get_current_user] = lambda: auth_user_free
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-refresh-foreign-create-001",
            },
        )
        assert create_response.status_code == 200

        app.dependency_overrides[get_current_user] = lambda: auth_user_other_free
        refresh_response = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-refresh-foreign-001"},
        )
        assert_error_response(refresh_response, 404, "NOT_FOUND")
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_refresh_second_call_is_idempotent_and_does_not_double_extend(
    client,
    override_db_with_payment_mapping,
    auth_user_active_future,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-idem-001",
            "confirmation": {"confirmation_url": "https://yookassa.test/confirm/pay-refresh-idem-001"},
        }

    async def _fake_fetch_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-idem-001",
            "status": "succeeded",
            "paid": True,
            "captured": True,
            "metadata": {},
        }

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(payments, "_fetch_yookassa_payment", _fake_fetch_payment)

    override_db_with_payment_mapping.users[str(auth_user_active_future["id"])] = auth_user_active_future
    app.dependency_overrides[get_current_user] = lambda: auth_user_active_future
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-refresh-idem-create-001",
            },
        )
        assert create_response.status_code == 200

        first_refresh = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-refresh-idem-001"},
        )
        assert first_refresh.status_code == 200
        first_until = first_refresh.json()["activeUntil"]

        second_refresh = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-refresh-idem-001"},
        )
        assert second_refresh.status_code == 200
        second_until = second_refresh.json()["activeUntil"]
        assert second_until == first_until
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_refresh_and_webhook_do_not_double_extend_same_payment(
    client,
    override_db_with_payment_mapping,
    auth_user_active_future,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-webhook-001",
            "confirmation": {"confirmation_url": "https://yookassa.test/confirm/pay-refresh-webhook-001"},
        }

    async def _fake_fetch_payment(*args, **kwargs):
        return {
            "id": "pay-refresh-webhook-001",
            "status": "succeeded",
            "paid": True,
            "captured": True,
            "metadata": {},
        }

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(payments, "_fetch_yookassa_payment", _fake_fetch_payment)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

    override_db_with_payment_mapping.users[str(auth_user_active_future["id"])] = auth_user_active_future
    app.dependency_overrides[get_current_user] = lambda: auth_user_active_future
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-refresh-webhook-create-001",
            },
        )
        assert create_response.status_code == 200

        webhook_response = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=_paid_webhook_payload(
                "evt-refresh-webhook-1",
                user_id=str(auth_user_active_future["id"]),
                payment_id="pay-refresh-webhook-001",
            ),
            headers=_basic_auth_header("fitai-shop-id", "fitai-secret"),
        )
        assert webhook_response.status_code == 200

        subscription_after_webhook = await client.get("/v1/subscription")
        assert subscription_after_webhook.status_code == 200
        until_after_webhook = subscription_after_webhook.json()["activeUntil"]

        refresh_response = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-refresh-webhook-001"},
        )
        assert refresh_response.status_code == 200
        assert refresh_response.json()["activeUntil"] == until_after_webhook
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_webhook_after_refresh_is_idempotent_for_same_payment(
    client,
    override_db_with_payment_mapping,
    auth_user_active_future,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-webhook-after-refresh-001",
            "confirmation": {
                "confirmation_url": "https://yookassa.test/confirm/pay-webhook-after-refresh-001"
            },
        }

    async def _fake_fetch_payment(*args, **kwargs):
        return {
            "id": "pay-webhook-after-refresh-001",
            "status": "succeeded",
            "paid": True,
            "captured": True,
            "metadata": {},
        }

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(payments, "_fetch_yookassa_payment", _fake_fetch_payment)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

    override_db_with_payment_mapping.users[str(auth_user_active_future["id"])] = auth_user_active_future
    app.dependency_overrides[get_current_user] = lambda: auth_user_active_future
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-webhook-after-refresh-create-001",
            },
        )
        assert create_response.status_code == 200

        refresh_response = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-webhook-after-refresh-001"},
        )
        assert refresh_response.status_code == 200

        subscription_after_refresh = await client.get("/v1/subscription")
        assert subscription_after_refresh.status_code == 200
        until_after_refresh = subscription_after_refresh.json()["activeUntil"]

        webhook_response = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=_paid_webhook_payload(
                "evt-webhook-after-refresh-1",
                user_id=str(auth_user_active_future["id"]),
                payment_id="pay-webhook-after-refresh-001",
            ),
            headers=_basic_auth_header("fitai-shop-id", "fitai-secret"),
        )
        assert webhook_response.status_code == 200

        subscription_after_webhook = await client.get("/v1/subscription")
        assert subscription_after_webhook.status_code == 200
        assert subscription_after_webhook.json()["activeUntil"] == until_after_refresh
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_duplicate_webhook_after_refresh_success_does_not_double_extend_active_until(
    client,
    override_db_with_payment_mapping,
    auth_user_active_future,
    monkeypatch,
):
    async def _fake_create_payment(*args, **kwargs):
        return {
            "id": "pay-webhook-after-refresh-dup-001",
            "confirmation": {
                "confirmation_url": "https://yookassa.test/confirm/pay-webhook-after-refresh-dup-001"
            },
        }

    async def _fake_fetch_payment(*args, **kwargs):
        return {
            "id": "pay-webhook-after-refresh-dup-001",
            "status": "succeeded",
            "paid": True,
            "captured": True,
            "metadata": {},
        }

    monkeypatch.setattr(payments, "_create_yookassa_payment", _fake_create_payment)
    monkeypatch.setattr(payments, "_fetch_yookassa_payment", _fake_fetch_payment)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "fitai-shop-id")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "fitai-secret")

    override_db_with_payment_mapping.users[str(auth_user_active_future["id"])] = auth_user_active_future
    app.dependency_overrides[get_current_user] = lambda: auth_user_active_future
    try:
        create_response = await client.post(
            "/v1/subscription/yookassa/create",
            json={
                "returnUrl": "https://t.me/fitai_bot/app",
                "idempotencyKey": "idem-webhook-after-refresh-dup-create-001",
            },
        )
        assert create_response.status_code == 200

        refresh_response = await client.post(
            "/v1/subscription/yookassa/refresh",
            json={"paymentId": "pay-webhook-after-refresh-dup-001"},
        )
        assert refresh_response.status_code == 200
        until_after_refresh = refresh_response.json()["activeUntil"]

        webhook_payload = _paid_webhook_payload(
            "evt-webhook-after-refresh-dup-1",
            user_id=str(auth_user_active_future["id"]),
            payment_id="pay-webhook-after-refresh-dup-001",
        )
        first_webhook = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=webhook_payload,
            headers=_basic_auth_header("fitai-shop-id", "fitai-secret"),
        )
        assert first_webhook.status_code == 200

        second_webhook = await client.post(
            "/v1/subscription/yookassa/webhook",
            json=webhook_payload,
            headers=_basic_auth_header("fitai-shop-id", "fitai-secret"),
        )
        assert second_webhook.status_code == 200

        subscription_response = await client.get("/v1/subscription")
        assert subscription_response.status_code == 200
        assert subscription_response.json()["activeUntil"] == until_after_refresh
    finally:
        app.dependency_overrides.pop(get_current_user, None)
