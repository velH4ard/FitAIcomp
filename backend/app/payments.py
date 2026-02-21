import base64
import hashlib
import inspect
import json
import logging
import secrets
import time
import uuid
import ipaddress
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, AsyncContextManager, cast
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import APIRouter, Depends, Request

from .config import settings
from .db import get_db
from .deps import get_current_user
from .errors import FitAIError
from .events import write_event_best_effort
from .observability import duration_ms, log_ctx, log_ctx_json
from .schemas import (
    SubscriptionResponse,
    SubscriptionStatusResponse,
    YookassaCreatePaymentRequest,
    YookassaCreatePaymentResponse,
    YookassaRefreshPaymentRequest,
)
from .subscription import (
    build_subscription_status_view,
    get_effective_subscription_status,
    get_user_daily_limit,
)

logger = logging.getLogger("fitai-payments")

router = APIRouter(prefix="/v1/subscription", tags=["Subscription"])
_webhook_dedupe_memory: set[str] = set()
_webhook_allowlist_warned = False


def get_now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _build_yookassa_idempotence_key(request_key: Optional[str]) -> str:
    return request_key or str(uuid.uuid4())


@asynccontextmanager
async def _db_transaction(conn: Any):
    tx = getattr(conn, "transaction", None)
    if callable(tx):
        tx_ctx = tx()
        enter = getattr(tx_ctx, "__aenter__", None)
        exit_ = getattr(tx_ctx, "__aexit__", None)
        if callable(enter) and callable(exit_):
            tx_ctx_cm = cast(AsyncContextManager[Any], tx_ctx)
            async with tx_ctx_cm:
                yield
            return
    yield


def _is_production() -> bool:
    return settings.is_production()


def _get_webhook_ip_allowlist() -> set[str]:
    raw = settings.PAYMENTS_WEBHOOK_IP_ALLOWLIST.strip()
    if not raw:
        return set()

    values = set()
    for part in raw.split(","):
        value = part.strip()
        if value:
            values.add(value)
    return values


def _extract_client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first

    client = request.client
    if client and client.host:
        return client.host
    return None


def _client_ip_allowed(request: Request) -> bool:
    global _webhook_allowlist_warned

    allowlist = _get_webhook_ip_allowlist()
    if not _is_production():
        return True

    if not allowlist:
        if not _webhook_allowlist_warned:
            logger.warning(
                "WEBHOOK_IP_ALLOWLIST_NOT_CONFIGURED env=%s; relying on webhook auth only",
                settings.env_mode(),
            )
            _webhook_allowlist_warned = True
        return True

    client_ip = _extract_client_ip(request)
    if not client_ip:
        return False

    try:
        ip_obj = ipaddress.ip_address(client_ip)
    except ValueError:
        return False

    for allowed in allowlist:
        try:
            if "/" in allowed:
                if ip_obj in ipaddress.ip_network(allowed, strict=False):
                    return True
            else:
                if ip_obj == ipaddress.ip_address(allowed):
                    return True
        except ValueError:
            continue
    return False


def get_webhook_auth_mode() -> str:
    env_mode = settings.env_mode()
    bypass_effective = settings.payments_webhook_dev_bypass_enabled()
    allowlist = _get_webhook_ip_allowlist()
    allowlist_mode = "enforced" if (env_mode == "production" and allowlist) else "off"
    return (
        f"env={env_mode} basic_auth=on "
        f"dev_bypass={'on' if bypass_effective else 'off'} "
        f"ip_allowlist={allowlist_mode}"
    )


def _webhook_verification_ok(authorization: Optional[str]) -> bool:
    if not authorization or not authorization.startswith("Basic "):
        return False

    expected_username = settings.YOOKASSA_SHOP_ID
    expected_password = settings.YOOKASSA_SECRET_KEY
    if not expected_username or not expected_password:
        return False

    provided_token = authorization[len("Basic ") :].strip()
    try:
        decoded = base64.b64decode(provided_token, validate=True).decode("utf-8")
    except Exception:
        return False

    provided_username, sep, provided_password = decoded.partition(":")
    if not sep:
        return False

    username_ok = secrets.compare_digest(provided_username, expected_username)
    password_ok = secrets.compare_digest(provided_password, expected_password)
    return username_ok and password_ok


def _verify_yookassa_webhook(request: Request, payload: dict[str, Any]) -> bool:
    if not _client_ip_allowed(request):
        logger.warning("PAYMENT_WEBHOOK_IP_BLOCKED ip=%s", _extract_client_ip(request) or "unknown")
        return False

    authorization = request.headers.get("Authorization")
    if _webhook_verification_ok(authorization):
        return True

    bypass_enabled = settings.payments_webhook_dev_bypass_enabled()
    is_production = _is_production()
    has_proxy_header = bool(request.headers.get("CF-Ray") or request.headers.get("X-Forwarded-For"))

    if bypass_enabled and not is_production and has_proxy_header:
        event_type_raw = payload.get("event") if isinstance(payload, dict) else None
        event_type = event_type_raw if isinstance(event_type_raw, str) and event_type_raw else "unknown"
        logger.warning(
            "WEBHOOK_AUTH_BYPASS_ENABLED event=%s has_auth=%s",
            event_type,
            bool(authorization),
        )
        return True

    return False


def verify_yookassa_webhook(request: Request, payload: dict[str, Any]) -> bool:
    return _verify_yookassa_webhook(request, payload)


def _webhook_dedupe_key(payload: dict[str, Any]) -> str:
    event = str(payload.get("event") or "")
    object_raw = payload.get("object")
    payment_object: dict[str, Any] = object_raw if isinstance(object_raw, dict) else {}
    payment_id = str(payment_object.get("id") or "")

    if event == "payment.succeeded" and payment_id:
        source = _payment_success_dedupe_source(payment_id)
    else:
        explicit_event_id = payload.get("event_id") or payload.get("id")
        if explicit_event_id:
            source = f"event_id:{explicit_event_id}"
        else:
            payment_status = payment_object.get("status") or ""
            created_at = payment_object.get("created_at") or payload.get("created_at") or ""
            source = f"fallback:{event}|{payment_id}|{payment_status}|{created_at}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _payment_success_dedupe_source(payment_id: str) -> str:
    return f"payment_success:{payment_id}"


def _payment_success_dedupe_key(payment_id: str) -> str:
    source = _payment_success_dedupe_source(payment_id)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


async def _log_event(
    conn: asyncpg.Connection,
    event_type: str,
    user_id: Optional[str],
    payload: Optional[dict[str, Any]],
) -> None:
    await write_event_best_effort(conn=conn, event_type=event_type, user_id=user_id, payload=payload)


async def _store_payment_user_mapping(
    conn: asyncpg.Connection,
    *,
    payment_id: str,
    user_id: str,
    idempotence_key: str,
    status: str = "created",
) -> None:
    persisted_status = status if status in {"created", "succeeded", "canceled", "refunded"} else "created"
    await conn.execute(
        """
        INSERT INTO yookassa_payments (payment_id, user_id, idempotence_key, status, created_at, updated_at)
        VALUES ($1, $2::uuid, $3, $4, NOW(), NOW())
        ON CONFLICT (payment_id)
        DO UPDATE SET
            user_id = EXCLUDED.user_id,
            idempotence_key = EXCLUDED.idempotence_key,
            status = EXCLUDED.status,
            updated_at = NOW()
        """,
        payment_id,
        user_id,
        idempotence_key,
        persisted_status,
    )


async def _resolve_user_id_for_payment(
    conn: asyncpg.Connection,
    *,
    payment_object: dict[str, Any],
    payment_id: Optional[str],
) -> Optional[str]:
    metadata_raw = payment_object.get("metadata")
    metadata: dict[str, Any] = metadata_raw if isinstance(metadata_raw, dict) else {}
    user_id = metadata.get("user_id")
    if isinstance(user_id, str) and user_id:
        return user_id

    if payment_id:
        row = await conn.fetchrow(
            "SELECT user_id FROM yookassa_payments WHERE payment_id = $1",
            str(payment_id),
        )
        if row and row.get("user_id"):
            return str(row["user_id"])

    return None


async def _get_override_user_state(request: Request, user_id: str) -> Optional[dict[str, Any]]:
    override = request.app.dependency_overrides.get(get_current_user)
    if not override:
        return None

    maybe_user = override()
    if inspect.isawaitable(maybe_user):
        maybe_user = await maybe_user
    if isinstance(maybe_user, dict) and str(maybe_user.get("id")) == str(user_id):
        return maybe_user
    return None


def _is_successful_payment_event(event_name: str, payment_object: dict[str, Any]) -> bool:
    if event_name == "payment.succeeded":
        return True
    return (
        payment_object.get("status") == "succeeded"
        and payment_object.get("paid") is True
        and payment_object.get("captured") is True
    )


def _is_successful_provider_payment_for_refresh(payment_object: dict[str, Any]) -> bool:
    if payment_object.get("status") != "succeeded":
        return False

    # Some provider responses may omit paid/captured fields in GET /payments,
    # but explicit False must still block activation.
    paid = payment_object.get("paid")
    captured = payment_object.get("captured")
    if paid is False or captured is False:
        return False

    return True


def _is_blocking_payment_event(event_name: str, payment_object: dict[str, Any]) -> bool:
    if event_name in {"refund.succeeded", "payment.canceled"}:
        return True
    return payment_object.get("status") in {"canceled"}


async def _try_insert_user_daily_flag(
    conn: asyncpg.Connection,
    user_id: str,
    flag: str,
) -> bool:
    row = await conn.fetchrow(
        """
        INSERT INTO user_daily_flags (user_id, flag, date)
        VALUES ($1::uuid, $2, CURRENT_DATE)
        ON CONFLICT (user_id, flag, date) DO NOTHING
        RETURNING user_id
        """,
        str(user_id),
        flag,
    )
    return row is not None


async def _emit_subscription_expiring_soon_once_per_day(
    conn: asyncpg.Connection,
    user_id: str,
    *,
    days_left: int,
    active_until: Optional[datetime],
    now_utc: datetime,
) -> None:
    try:
        inserted = await _try_insert_user_daily_flag(
            conn,
            user_id=user_id,
            flag="subscription_expiring_soon",
        )
    except Exception as exc:
        logger.warning(
            "SUBSCRIPTION_EXPIRING_SOON_FLAG_FAIL user_id=%s reason=%s",
            user_id,
            type(exc).__name__,
        )
        return

    if not inserted:
        return

    await write_event_best_effort(
        conn,
        event_type="subscription_expiring_soon",
        user_id=str(user_id),
        payload={
            "daysLeft": days_left,
            "activeUntil": active_until.isoformat() if active_until else None,
        },
    )


async def _create_payment_with_retries(
    payload: dict[str, Any],
    idempotence_key: str,
) -> dict[str, Any]:
    timeout = httpx.Timeout(
        connect=settings.YOOKASSA_CONNECT_TIMEOUT_SEC,
        read=settings.YOOKASSA_READ_TIMEOUT_SEC,
        write=settings.YOOKASSA_READ_TIMEOUT_SEC,
        pool=settings.YOOKASSA_CONNECT_TIMEOUT_SEC,
    )

    max_attempts = settings.YOOKASSA_MAX_RETRIES + 1
    last_error: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, base_url=settings.YOOKASSA_API_BASE_URL) as client:
                response = await client.post(
                    "/payments",
                    auth=(settings.YOOKASSA_SHOP_ID, settings.YOOKASSA_SECRET_KEY),
                    headers={"Idempotence-Key": idempotence_key},
                    json=payload,
                )

            if response.status_code in {429, 500, 502, 503, 504} and attempt < max_attempts:
                continue

            if response.status_code >= 400:
                raise FitAIError(
                    code="PAYMENT_PROVIDER_ERROR",
                    message="Ошибка платежного провайдера",
                    status_code=502,
                    details={
                        "stage": "create_payment",
                        "providerStatus": response.status_code,
                    },
                )

            return response.json()
        except FitAIError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc
            if attempt < max_attempts:
                continue
        except Exception as exc:
            last_error = exc
            break

    raise FitAIError(
        code="PAYMENT_PROVIDER_ERROR",
        message="Ошибка платежного провайдера",
        status_code=502,
        details={
            "stage": "create_payment",
            "providerStatus": None,
        },
    ) from last_error


async def _create_yookassa_payment(payload: dict[str, Any], idempotence_key: str) -> dict[str, Any]:
    return await _create_payment_with_retries(payload=payload, idempotence_key=idempotence_key)


async def yookassa_create_payment(payload: dict[str, Any], idempotence_key: str) -> dict[str, Any]:
    return await _create_yookassa_payment(payload=payload, idempotence_key=idempotence_key)


async def _fetch_yookassa_payment_with_retries(payment_id: str) -> dict[str, Any]:
    timeout = httpx.Timeout(
        connect=settings.YOOKASSA_CONNECT_TIMEOUT_SEC,
        read=settings.YOOKASSA_READ_TIMEOUT_SEC,
        write=settings.YOOKASSA_READ_TIMEOUT_SEC,
        pool=settings.YOOKASSA_CONNECT_TIMEOUT_SEC,
    )

    max_attempts = settings.YOOKASSA_MAX_RETRIES + 1
    last_error: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, base_url=settings.YOOKASSA_API_BASE_URL) as client:
                response = await client.get(
                    f"/payments/{payment_id}",
                    auth=(settings.YOOKASSA_SHOP_ID, settings.YOOKASSA_SECRET_KEY),
                )

            if response.status_code in {429, 500, 502, 503, 504} and attempt < max_attempts:
                continue

            if response.status_code >= 400:
                raise FitAIError(
                    code="PAYMENT_PROVIDER_ERROR",
                    message="Ошибка платежного провайдера",
                    status_code=502,
                    details={
                        "stage": "fetch_payment",
                        "providerStatus": response.status_code,
                    },
                )

            body = response.json()
            if not isinstance(body, dict):
                raise FitAIError(
                    code="PAYMENT_PROVIDER_ERROR",
                    message="Ошибка платежного провайдера",
                    status_code=502,
                    details={"stage": "fetch_payment"},
                )
            return body
        except FitAIError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc
            if attempt < max_attempts:
                continue
        except Exception as exc:
            last_error = exc
            break

    raise FitAIError(
        code="PAYMENT_PROVIDER_ERROR",
        message="Ошибка платежного провайдера",
        status_code=502,
        details={
            "stage": "fetch_payment",
            "providerStatus": None,
        },
    ) from last_error


async def _fetch_yookassa_payment(payment_id: str) -> dict[str, Any]:
    return await _fetch_yookassa_payment_with_retries(payment_id=payment_id)


async def _build_subscription_response_for_user(
    conn: asyncpg.Connection,
    *,
    user_id: str,
    fallback_user: dict[str, Any],
) -> SubscriptionResponse:
    user_row = await conn.fetchrow(
        """
        SELECT id, subscription_status, subscription_active_until, referral_credits
        FROM users
        WHERE id = $1::uuid
        """,
        user_id,
    )

    user_data: dict[str, Any]
    if user_row:
        user_data = dict(user_row)
    else:
        user_data = fallback_user

    effective_status = get_effective_subscription_status(
        str(user_data.get("subscription_status") or "free"),
        user_data.get("subscription_active_until"),
    )
    daily_limit = get_user_daily_limit(user_data)
    today = get_now_utc().date()

    usage_row = await conn.fetchrow(
        "SELECT photos_used FROM usage_daily WHERE user_id = $1 AND date = $2",
        user_id,
        today,
    )
    used_today = int(usage_row["photos_used"] if usage_row else 0)

    return SubscriptionResponse(
        priceRubPerMonth=settings.SUBSCRIPTION_PRICE_RUB,
        status=effective_status,
        activeUntil=user_data.get("subscription_active_until"),
        dailyLimit=daily_limit,
        usedToday=used_today,
        remainingToday=max(0, daily_limit - used_today),
    )


async def _has_local_payment_success_signal(
    conn: asyncpg.Connection,
    *,
    user_id: str,
    payment_id: str,
) -> bool:
    status_row = await conn.fetchrow(
        """
        SELECT status
        FROM yookassa_payments
        WHERE payment_id = $1
          AND user_id = $2::uuid
        """,
        payment_id,
        user_id,
    )
    if status_row and str(status_row.get("status") or "") == "succeeded":
        return True

    dedupe_row = await conn.fetchrow(
        """
        SELECT dedupe_key
        FROM payment_webhook_events
        WHERE dedupe_key = $1
          AND status = 'completed'
        """,
        _payment_success_dedupe_key(payment_id),
    )
    if dedupe_row is not None:
        return True

    event_row = await conn.fetchrow(
        """
        SELECT id
        FROM events
        WHERE user_id = $1::uuid
          AND event_type = 'payment_succeeded'
          AND payload ->> 'paymentId' = $2
        ORDER BY created_at DESC
        LIMIT 1
        """,
        user_id,
        payment_id,
    )
    return event_row is not None


@router.get("", response_model=SubscriptionResponse)
async def get_subscription(
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    effective_status = get_effective_subscription_status(
        user["subscription_status"], user["subscription_active_until"]
    )
    daily_limit = get_user_daily_limit(user)
    today = get_now_utc().date()

    usage_row = await conn.fetchrow(
        "SELECT photos_used FROM usage_daily WHERE user_id = $1 AND date = $2",
        user["id"],
        today,
    )
    used_today = usage_row["photos_used"] if usage_row else 0

    return SubscriptionResponse(
        priceRubPerMonth=settings.SUBSCRIPTION_PRICE_RUB,
        status=effective_status,
        activeUntil=user["subscription_active_until"],
        dailyLimit=daily_limit,
        usedToday=used_today,
        remainingToday=max(0, daily_limit - used_today),
    )


@router.get("/status", response_model=SubscriptionStatusResponse)
async def get_subscription_status(user=Depends(get_current_user), conn=Depends(get_db)):
    now_utc = get_now_utc()
    status, active_until, days_left, will_expire_soon = build_subscription_status_view(
        user["subscription_status"],
        user["subscription_active_until"],
        now=now_utc,
    )

    if status == "active" and days_left < 3:
        await _emit_subscription_expiring_soon_once_per_day(
            conn,
            user_id=str(user["id"]),
            days_left=days_left,
            active_until=active_until,
            now_utc=now_utc,
        )

    return SubscriptionStatusResponse(
        status=status,
        activeUntil=active_until,
        daysLeft=days_left,
        willExpireSoon=will_expire_soon,
    )


@router.post("/yookassa/create", response_model=YookassaCreatePaymentResponse)
async def create_yookassa_payment(
    request: Request,
    request_body: YookassaCreatePaymentRequest,
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    started_at = time.monotonic()
    return_url = request_body.returnUrl or settings.YOOKASSA_RETURN_URL_DEFAULT
    if not return_url:
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"fieldErrors": [{"field": "returnUrl", "issue": "Field required"}]},
        )

    idempotence_key = _build_yookassa_idempotence_key(request_body.idempotencyKey)

    yookassa_payload = {
        "amount": {
            "value": f"{settings.SUBSCRIPTION_PRICE_RUB:.2f}",
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": return_url,
        },
        "description": "FitAI subscription 1 month",
        "metadata": {
            "user_id": str(user["id"]),
            "telegram_id": str(user["telegram_id"]),
            "plan": "monthly_499",
        },
    }

    try:
        provider_response = await _create_yookassa_payment(
            payload=yookassa_payload,
            idempotence_key=idempotence_key,
        )
    except FitAIError as exc:
        await _log_event(
            conn,
            event_type="payment_create_failed",
            user_id=str(user["id"]),
            payload={
                "provider": "yookassa",
                "code": exc.code,
            },
        )
        logger.warning(
            "PAYMENT_CREATE_FAIL context=%s",
            log_ctx_json(
                log_ctx(
                    request,
                    user_id=user["id"],
                    idempotency_key=idempotence_key,
                    extra={
                        "status_code": exc.status_code,
                        "duration_ms": duration_ms(started_at),
                        "code": exc.code,
                    },
                )
            ),
        )
        raise

    payment_id = provider_response.get("id") or provider_response.get("paymentId")
    confirmation = provider_response.get("confirmation")
    confirmation_url = (
        confirmation.get("confirmation_url") if isinstance(confirmation, dict) else None
    ) or provider_response.get("confirmationUrl")
    if not payment_id or not confirmation_url:
        await _log_event(
            conn,
            event_type="payment_create_failed",
            user_id=str(user["id"]),
            payload={"provider": "yookassa", "issue": "invalid_provider_response"},
        )
        raise FitAIError(
            code="PAYMENT_PROVIDER_ERROR",
            message="Ошибка платежного провайдера",
            status_code=502,
            details={"stage": "create_payment"},
        )

    await _log_event(
        conn,
        event_type="payment_created",
        user_id=str(user["id"]),
        payload={
            "provider": "yookassa",
            "paymentId": payment_id,
            "idempotenceKey": idempotence_key,
        },
    )
    await _store_payment_user_mapping(
        conn,
        payment_id=str(payment_id),
        user_id=str(user["id"]),
        idempotence_key=idempotence_key,
        status=str(provider_response.get("status") or "created"),
    )
    logger.info(
        "PAYMENT_CREATE_OK context=%s",
        log_ctx_json(
            log_ctx(
                request,
                user_id=user["id"],
                idempotency_key=idempotence_key,
                extra={
                    "status_code": 200,
                    "duration_ms": duration_ms(started_at),
                    "payment_id": payment_id,
                },
            )
        ),
    )

    return YookassaCreatePaymentResponse(
        paymentId=payment_id,
        confirmationUrl=confirmation_url,
    )


@router.post("/yookassa/refresh", response_model=SubscriptionResponse)
async def refresh_yookassa_payment(
    request: Request,
    request_body: YookassaRefreshPaymentRequest,
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    started_at = time.monotonic()
    payment_id = request_body.paymentId.strip()

    ownership_row = await conn.fetchrow(
        """
        SELECT user_id
        FROM yookassa_payments
        WHERE payment_id = $1
          AND user_id = $2::uuid
        """,
        payment_id,
        str(user["id"]),
    )
    if ownership_row is None:
        raise FitAIError(
            code="NOT_FOUND",
            message="Не найдено",
            status_code=404,
        )

    try:
        provider_payment = await _fetch_yookassa_payment(payment_id)
    except FitAIError as exc:
        if exc.code == "PAYMENT_PROVIDER_ERROR":
            if await _has_local_payment_success_signal(
                conn,
                user_id=str(user["id"]),
                payment_id=payment_id,
            ):
                logger.info(
                    "PAYMENT_REFRESH_OK context=%s",
                    log_ctx_json(
                        log_ctx(
                            request,
                            user_id=user["id"],
                            extra={
                                "status_code": 200,
                                "duration_ms": duration_ms(started_at),
                                "payment_id": payment_id,
                                "fallback_local_success": True,
                            },
                        )
                    ),
                )
                return await _build_subscription_response_for_user(
                    conn,
                    user_id=str(user["id"]),
                    fallback_user=user,
                )
        raise
    payment_status = str(provider_payment.get("status") or "")

    if _is_successful_provider_payment_for_refresh(provider_payment):
        dedupe_key = _payment_success_dedupe_key(payment_id)
        inserted = False
        try:
            async with _db_transaction(conn):
                inserted_row = await conn.fetchrow(
                    """
                    INSERT INTO payment_webhook_events (dedupe_key, status, event_type, payment_id, payload)
                    VALUES ($1, 'processing', 'payment.refresh', $2, $3::jsonb)
                    ON CONFLICT (dedupe_key) DO NOTHING
                    RETURNING dedupe_key
                    """,
                    dedupe_key,
                    payment_id,
                    json.dumps(
                        {
                            "event": "payment.refresh",
                            "payment_id": payment_id,
                            "status": payment_status,
                        }
                    ),
                )
                inserted = inserted_row is not None

                if inserted:
                    user_subscription_row = await conn.fetchrow(
                        "SELECT subscription_active_until FROM users WHERE id = $1::uuid FOR UPDATE",
                        str(user["id"]),
                    )
                    if not user_subscription_row:
                        raise FitAIError(
                            code="PAYMENT_PROVIDER_ERROR",
                            message="Ошибка платежного провайдера",
                            status_code=502,
                            details={"stage": "refresh_resolve_user"},
                        )

                    old_until = user_subscription_row["subscription_active_until"]
                    now_utc = get_now_utc()
                    if isinstance(old_until, datetime) and old_until > now_utc:
                        base = old_until
                    else:
                        base = now_utc
                    new_until = base + timedelta(days=settings.SUBSCRIPTION_DURATION_DAYS)

                    await conn.execute(
                        """
                        UPDATE users
                        SET
                            subscription_status = 'active',
                            subscription_active_until = $2,
                            updated_at = NOW()
                        WHERE id = $1::uuid
                        """,
                        str(user["id"]),
                        new_until,
                    )

                    await conn.execute(
                        """
                        UPDATE yookassa_payments
                        SET status = 'succeeded', updated_at = NOW()
                        WHERE payment_id = $1
                        """,
                        payment_id,
                    )

                    await _log_event(
                        conn,
                        event_type="payment_succeeded",
                        user_id=str(user["id"]),
                        payload={
                            "event": "payment.refresh",
                            "paymentId": payment_id,
                            "status": "active",
                        },
                    )
                    await _log_event(
                        conn,
                        event_type="subscription_activated",
                        user_id=str(user["id"]),
                        payload={
                            "paymentId": payment_id,
                            "durationDays": settings.SUBSCRIPTION_DURATION_DAYS,
                            "source": "refresh",
                        },
                    )

                    await conn.execute(
                        """
                        UPDATE payment_webhook_events
                        SET status = 'completed', updated_at = NOW()
                        WHERE dedupe_key = $1
                        """,
                        dedupe_key,
                    )

                    logger.info(
                        "PAYMENT_REFRESH_APPLY context=%s",
                        log_ctx_json(
                            log_ctx(
                                request,
                                user_id=user["id"],
                                extra={
                                    "status_code": 200,
                                    "duration_ms": duration_ms(started_at),
                                    "payment_id": payment_id,
                                    "applied": True,
                                    "old_until": old_until.isoformat() if isinstance(old_until, datetime) else None,
                                    "new_until": new_until.isoformat(),
                                },
                            )
                        ),
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE yookassa_payments
                        SET status = 'succeeded', updated_at = NOW()
                        WHERE payment_id = $1
                        """,
                        payment_id,
                    )
        except Exception:
            if inserted:
                await conn.execute(
                    "DELETE FROM payment_webhook_events WHERE dedupe_key = $1 AND status = 'processing'",
                    dedupe_key,
                )
            raise

        logger.info(
            "PAYMENT_REFRESH_OK context=%s",
            log_ctx_json(
                log_ctx(
                    request,
                    user_id=user["id"],
                    extra={
                        "status_code": 200,
                        "duration_ms": duration_ms(started_at),
                        "payment_id": payment_id,
                        "dedup": not inserted,
                    },
                )
            ),
        )
        return await _build_subscription_response_for_user(
            conn,
            user_id=str(user["id"]),
            fallback_user=user,
        )

    if payment_status in {"pending", "waiting_for_capture"}:
        await conn.execute(
            """
            UPDATE yookassa_payments
            SET status = 'created', updated_at = NOW()
            WHERE payment_id = $1
            """,
            payment_id,
        )
        return await _build_subscription_response_for_user(
            conn,
            user_id=str(user["id"]),
            fallback_user=user,
        )

    if payment_status in {"canceled"}:
        await conn.execute(
            """
            UPDATE yookassa_payments
            SET status = 'canceled', updated_at = NOW()
            WHERE payment_id = $1
            """,
            payment_id,
        )

    raise FitAIError(
        code="PAYMENT_PROVIDER_ERROR",
        message="Ошибка платежного провайдера",
        status_code=502,
        details={
            "stage": "refresh_payment_status",
            "paymentStatus": payment_status or None,
            "providerStatus": payment_status or None,
        },
    )


@router.post("/yookassa/webhook")
async def yookassa_webhook(
    request: Request,
    conn=Depends(get_db),
):
    started_at = time.monotonic()
    try:
        payload = await request.json()
    except Exception as exc:
        await _log_event(
            conn,
            event_type="PAYMENT_WEBHOOK_FAIL",
            user_id=None,
            payload={"reason": "invalid_json"},
        )
        raise FitAIError(
            code="PAYMENT_PROVIDER_ERROR",
            message="Ошибка платежного провайдера",
            status_code=502,
            details={"stage": "webhook_parse"},
        ) from exc

    if not isinstance(payload, dict):
        raise FitAIError(
            code="PAYMENT_PROVIDER_ERROR",
            message="Ошибка платежного провайдера",
            status_code=502,
            details={"stage": "webhook_parse"},
        )

    if not verify_yookassa_webhook(request, payload):
        logger.info(
            "PAYMENT_WEBHOOK_RECEIVED context=%s",
            log_ctx_json(
                log_ctx(
                    request,
                    extra={
                        "status_code": 401,
                        "duration_ms": duration_ms(started_at),
                        "verify_ok": False,
                        "event_id": str(payload.get("event_id") or payload.get("id") or ""),
                        "payment_id": str((payload.get("object") or {}).get("id") or ""),
                        "payment_status": str((payload.get("object") or {}).get("status") or ""),
                    },
                )
            ),
        )
        await _log_event(
            conn,
            event_type="PAYMENT_WEBHOOK_FAIL",
            user_id=None,
            payload={"reason": "invalid_verification"},
        )
        raise FitAIError(
            code="PAYMENT_WEBHOOK_INVALID",
            message="Неверная подпись вебхука",
            status_code=401,
        )

    event_type = payload.get("event") or "unknown"
    object_raw = payload.get("object")
    payment_object: dict[str, Any] = object_raw if isinstance(object_raw, dict) else {}
    payment_id = payment_object.get("id")
    dedupe_key = _webhook_dedupe_key(payload)
    logger.info(
        "PAYMENT_WEBHOOK_RECEIVED context=%s",
        log_ctx_json(
            log_ctx(
                request,
                extra={
                    "status_code": 200,
                    "duration_ms": duration_ms(started_at),
                    "verify_ok": True,
                    "event_id": str(payload.get("event_id") or payload.get("id") or ""),
                    "event": str(event_type),
                    "payment_id": str(payment_id or ""),
                    "payment_status": str(payment_object.get("status") or ""),
                },
            )
        ),
    )

    if dedupe_key in _webhook_dedupe_memory:
        logger.info(
            "PAYMENT_WEBHOOK_OK context=%s",
            log_ctx_json(
                log_ctx(
                    request,
                    extra={
                        "status_code": 200,
                        "duration_ms": duration_ms(started_at),
                        "duplicate": True,
                        "dedupe_key": dedupe_key,
                    },
                )
            ),
        )
        return {"ok": True}

    inserted = False
    try:
        async with _db_transaction(conn):
            try:
                await conn.execute(
                    """
                    INSERT INTO payment_webhook_events (dedupe_key, status, event_type, payment_id, payload)
                    VALUES ($1, 'processing', $2, $3, $4::jsonb)
                    """,
                    dedupe_key,
                    event_type,
                    payment_id,
                    json.dumps(
                        {
                            "event": event_type,
                            "payment_id": payment_id,
                            "status": payment_object.get("status"),
                        }
                    ),
                )
                inserted = True
            except asyncpg.UniqueViolationError:
                logger.info(
                    "PAYMENT_WEBHOOK_OK context=%s",
                    log_ctx_json(
                        log_ctx(
                            request,
                            extra={
                                "status_code": 200,
                                "duration_ms": duration_ms(started_at),
                                "duplicate": True,
                                "dedupe_key": dedupe_key,
                            },
                        )
                    ),
                )
                return {"ok": True}

            if _is_successful_payment_event(event_type, payment_object):
                user_id = await _resolve_user_id_for_payment(
                    conn,
                    payment_object=payment_object,
                    payment_id=str(payment_id) if payment_id else None,
                )
                if not user_id:
                    raise FitAIError(
                        code="PAYMENT_PROVIDER_ERROR",
                        message="Ошибка платежного провайдера",
                        status_code=502,
                        details={"stage": "webhook_resolve_user"},
                    )

                user_subscription_row = await conn.fetchrow(
                    "SELECT subscription_active_until FROM users WHERE id = $1::uuid FOR UPDATE",
                    user_id,
                )

                if not user_subscription_row:
                    override_user = await _get_override_user_state(request, user_id)
                    if override_user is None:
                        raise FitAIError(
                            code="PAYMENT_PROVIDER_ERROR",
                            message="Ошибка платежного провайдера",
                            status_code=502,
                            details={"stage": "webhook_resolve_user"},
                        )
                    current_until = override_user.get("subscription_active_until")
                    now_utc = get_now_utc()
                    if isinstance(current_until, datetime):
                        base = current_until if current_until > now_utc else now_utc
                    else:
                        base = now_utc
                    new_until = base + timedelta(days=settings.SUBSCRIPTION_DURATION_DAYS)
                    override_user["subscription_active_until"] = new_until
                    override_user["subscription_status"] = "active"
                    old_until = current_until if isinstance(current_until, datetime) else None
                else:
                    old_until = user_subscription_row["subscription_active_until"]
                    now_utc = get_now_utc()
                    if isinstance(old_until, datetime) and old_until > now_utc:
                        base = old_until
                    else:
                        base = now_utc
                    new_until = base + timedelta(days=settings.SUBSCRIPTION_DURATION_DAYS)

                    await conn.execute(
                        """
                        UPDATE users
                        SET
                            subscription_status = 'active',
                            subscription_active_until = $2,
                            updated_at = NOW()
                        WHERE id = $1::uuid
                        """,
                        user_id,
                        new_until,
                    )

                if payment_id:
                    await conn.execute(
                        """
                        UPDATE yookassa_payments
                        SET status = 'succeeded', updated_at = NOW()
                        WHERE payment_id = $1
                        """,
                        str(payment_id),
                    )

                logger.info(
                    "PAYMENT_WEBHOOK_APPLY context=%s",
                    log_ctx_json(
                        log_ctx(
                            request,
                            user_id=user_id,
                            extra={
                                "status_code": 200,
                                "duration_ms": duration_ms(started_at),
                                "event": event_type,
                                "payment_id": payment_id,
                                "applied": True,
                                "dedup": False,
                                "old_until": old_until.isoformat() if isinstance(old_until, datetime) else None,
                                "new_until": new_until.isoformat(),
                            },
                        )
                    ),
                )

                await _log_event(
                    conn,
                    event_type="payment_succeeded",
                    user_id=user_id,
                    payload={
                        "event": event_type,
                        "paymentId": payment_id,
                        "status": "active",
                    },
                )
                await _log_event(
                    conn,
                    event_type="subscription_activated",
                    user_id=user_id,
                    payload={
                        "paymentId": payment_id,
                        "durationDays": settings.SUBSCRIPTION_DURATION_DAYS,
                    },
                )
                logger.info(
                    "PAYMENT_WEBHOOK_OK context=%s",
                    log_ctx_json(
                        log_ctx(
                            request,
                            user_id=user_id,
                            extra={
                                "status_code": 200,
                                "duration_ms": duration_ms(started_at),
                                "event": event_type,
                                "payment_id": payment_id,
                            },
                        )
                    ),
                )

            elif _is_blocking_payment_event(event_type, payment_object):
                metadata_raw = payment_object.get("metadata")
                metadata: dict[str, Any] = metadata_raw if isinstance(metadata_raw, dict) else {}
                user_id = metadata.get("user_id")
                if user_id:
                    await conn.execute(
                        """
                        UPDATE users
                        SET subscription_status = 'blocked', updated_at = NOW()
                        WHERE id = $1
                        """,
                        user_id,
                    )
                    await _log_event(
                        conn,
                        event_type="payment_blocked",
                        user_id=user_id,
                        payload={
                            "event": event_type,
                            "paymentId": payment_id,
                            "status": "blocked",
                        },
                    )

            await conn.execute(
                """
                UPDATE payment_webhook_events
                SET status = 'completed', updated_at = NOW()
                WHERE dedupe_key = $1
                """,
                dedupe_key,
            )
        _webhook_dedupe_memory.add(dedupe_key)
        if not _is_successful_payment_event(event_type, payment_object):
            logger.info(
                "PAYMENT_WEBHOOK_OK context=%s",
                log_ctx_json(
                    log_ctx(
                        request,
                        extra={
                            "status_code": 200,
                            "duration_ms": duration_ms(started_at),
                            "event": event_type,
                            "payment_id": payment_id,
                        },
                    )
                ),
            )
        return {"ok": True}

    except FitAIError as exc:
        if inserted:
            await conn.execute(
                "DELETE FROM payment_webhook_events WHERE dedupe_key = $1 AND status = 'processing'",
                dedupe_key,
            )
        await _log_event(
            conn,
            event_type="PAYMENT_WEBHOOK_FAIL",
            user_id=None,
            payload={"event": event_type, "code": exc.code},
        )
        logger.warning(
            "PAYMENT_WEBHOOK_FAIL context=%s",
            log_ctx_json(
                log_ctx(
                    request,
                    extra={
                        "status_code": exc.status_code,
                        "duration_ms": duration_ms(started_at),
                        "event": event_type,
                        "code": exc.code,
                        "payment_id": payment_id,
                    },
                )
            ),
        )
        _webhook_dedupe_memory.discard(dedupe_key)
        raise
    except Exception as exc:
        if inserted:
            await conn.execute(
                "DELETE FROM payment_webhook_events WHERE dedupe_key = $1 AND status = 'processing'",
                dedupe_key,
            )
        await _log_event(
            conn,
            event_type="PAYMENT_WEBHOOK_FAIL",
            user_id=None,
            payload={"event": event_type, "code": "INTERNAL_ERROR"},
        )
        logger.error(
            "PAYMENT_WEBHOOK_FAIL context=%s",
            log_ctx_json(
                log_ctx(
                    request,
                    extra={
                        "status_code": 500,
                        "duration_ms": duration_ms(started_at),
                        "event": event_type,
                        "code": "INTERNAL_ERROR",
                        "payment_id": payment_id,
                    },
                )
            ),
            exc_info=True,
        )
        _webhook_dedupe_memory.discard(dedupe_key)
        raise FitAIError(
            code="INTERNAL_ERROR",
            message="Внутренняя ошибка сервера",
            status_code=500,
        ) from exc
