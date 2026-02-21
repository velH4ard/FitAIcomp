import hashlib
import secrets
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends

from .db import get_db
from .deps import get_current_user
from .errors import FitAIError
from .events import write_event_best_effort
from .schemas import ReferralCodeResponse, ReferralRedeemRequest, ReferralRedeemResponse

router = APIRouter(prefix="/v1/referral", tags=["Referral"])

_REFERRAL_CODE_LENGTH = 10
_REFERRAL_REDEEM_RATE_LIMIT_PER_MINUTE = 5
_REFERRAL_BONUS_CREDITS = 1


def _deterministic_referral_code(user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest().upper()
    return digest[:_REFERRAL_CODE_LENGTH]


def _random_referral_code() -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(_REFERRAL_CODE_LENGTH))


async def _enforce_redeem_rate_limit(conn: asyncpg.Connection, user_id: str) -> None:
    row = await conn.fetchrow(
        """
        SELECT COUNT(*)::int AS attempts
        FROM events
        WHERE user_id = $1
          AND event_type = 'referral_redeem_attempt'
          AND created_at >= (NOW() - interval '1 minute')
        """,
        user_id,
    )
    attempts = int(row["attempts"] if row else 0)
    if attempts < _REFERRAL_REDEEM_RATE_LIMIT_PER_MINUTE:
        return

    raise FitAIError(
        code="RATE_LIMITED",
        message="Слишком много запросов, попробуйте позже",
        status_code=429,
        details={
            "retryAfterSeconds": 60,
            "windowSeconds": 60,
            "limit": _REFERRAL_REDEEM_RATE_LIMIT_PER_MINUTE,
            "scope": "referral_redeem",
        },
    )


async def _conn_fetchval(conn: asyncpg.Connection, query: str, *args):
    fetchval = getattr(conn, "fetchval", None)
    if callable(fetchval):
        return await fetchval(query, *args)

    row = await conn.fetchrow(query, *args)
    if row is None:
        return None

    if isinstance(row, dict):
        return next(iter(row.values()), None)

    keys = getattr(row, "keys", None)
    if callable(keys):
        row_keys = list(keys())
        if row_keys:
            return row[row_keys[0]]
    return None


async def _get_or_create_referral_code(conn: asyncpg.Connection, user_id: str) -> tuple[str, bool]:
    existing_code = await _conn_fetchval(
        conn,
        "SELECT code FROM referral_codes WHERE user_id = $1",
        user_id,
    )
    if existing_code:
        return str(existing_code), False

    first_candidate = _deterministic_referral_code(user_id)
    max_attempts = 8
    for attempt in range(max_attempts):
        candidate = first_candidate if attempt == 0 else _random_referral_code()
        try:
            inserted_code = await _conn_fetchval(
                conn,
                """
                INSERT INTO referral_codes (user_id, code)
                VALUES ($1, $2)
                RETURNING code
                """,
                user_id,
                candidate,
            )
            return str(inserted_code), True
        except asyncpg.UniqueViolationError:
            concurrent_existing = await _conn_fetchval(
                conn,
                "SELECT code FROM referral_codes WHERE user_id = $1",
                user_id,
            )
            if concurrent_existing:
                return str(concurrent_existing), False
            continue

    raise FitAIError(
        code="INTERNAL_ERROR",
        message="Внутренняя ошибка сервера",
        status_code=500,
        details={"stage": "referral_code_generation"},
    )


@router.get("/code", response_model=ReferralCodeResponse)
async def get_referral_code(
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    code, is_new = await _get_or_create_referral_code(conn, str(user["id"]))

    if is_new:
        await write_event_best_effort(
            conn,
            event_type="referral_code_generated",
            user_id=str(user["id"]),
            payload={"code": code},
        )

    return ReferralCodeResponse(code=code)


@router.post("/redeem", response_model=ReferralRedeemResponse)
async def redeem_referral_code(
    payload: ReferralRedeemRequest,
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    redeemer_user_id = str(user["id"])
    code = payload.code

    await _enforce_redeem_rate_limit(conn, redeemer_user_id)
    await write_event_best_effort(
        conn,
        event_type="referral_redeem_attempt",
        user_id=redeemer_user_id,
        payload={"code": code},
    )

    referrer_user_id: Optional[str] = None
    async with conn.transaction():
        code_row = await conn.fetchrow(
            "SELECT user_id FROM referral_codes WHERE code = $1",
            code,
        )
        if not code_row:
            raise FitAIError(
                code="INVALID_REFERRAL_CODE",
                message="Неверный реферальный код",
                status_code=400,
            )

        referrer_user_id = str(code_row["user_id"])
        if referrer_user_id == redeemer_user_id:
            raise FitAIError(
                code="REFERRAL_SELF_REDEEM",
                message="Нельзя активировать собственный реферальный код",
                status_code=409,
            )

        try:
            await conn.execute(
                """
                INSERT INTO referral_redemptions (
                    redeemer_user_id,
                    referrer_user_id,
                    code,
                    credits_granted
                )
                VALUES ($1, $2, $3, $4)
                """,
                redeemer_user_id,
                referrer_user_id,
                code,
                _REFERRAL_BONUS_CREDITS,
            )
        except asyncpg.UniqueViolationError as exc:
            raise FitAIError(
                code="REFERRAL_ALREADY_REDEEMED",
                message="Реферальный код уже был активирован",
                status_code=409,
            ) from exc

        await conn.execute(
            """
            UPDATE users
            SET referral_credits = referral_credits + $1,
                updated_at = NOW()
            WHERE id = ANY($2::uuid[])
            """,
            _REFERRAL_BONUS_CREDITS,
            [redeemer_user_id, referrer_user_id],
        )

    await write_event_best_effort(
        conn,
        event_type="referral_redeemed",
        user_id=redeemer_user_id,
        payload={
            "code": code,
            "referrerUserId": referrer_user_id,
            "bonus": _REFERRAL_BONUS_CREDITS,
        },
    )
    await write_event_best_effort(
        conn,
        event_type="referral_bonus_granted",
        user_id=redeemer_user_id,
        payload={"counterpartyUserId": referrer_user_id, "credits": _REFERRAL_BONUS_CREDITS},
    )
    await write_event_best_effort(
        conn,
        event_type="referral_bonus_granted",
        user_id=referrer_user_id,
        payload={"counterpartyUserId": redeemer_user_id, "credits": _REFERRAL_BONUS_CREDITS},
    )

    return ReferralRedeemResponse(redeemed=True)
