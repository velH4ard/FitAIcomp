from datetime import datetime, timedelta, timezone
import json
import logging
import time
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from .config import settings
from .db import fetch_named, fetchrow_named, fetchval_named, get_db
from .deps import get_current_user
from .errors import FitAIError
from .events import build_created_at_bounds, decode_keyset_cursor, encode_keyset_cursor
from .observability import duration_ms, log_ctx, log_ctx_json
from .schemas import (
    AdminEventListItem,
    AdminEventListResponse,
    AdminReferralRedemptionItem,
    AdminReferralRedemptionsResponse,
    AdminReferralStatsResponse,
    AdminReferralTotalsAllTime,
    AdminStatsResponse,
)


router = APIRouter(prefix="/v1/admin", tags=["Admin"])
logger = logging.getLogger("fitai-admin")


def _payload_as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise FitAIError(
                code="INTERNAL_ERROR",
                message="Внутренняя ошибка сервера",
                status_code=500,
            ) from exc
        if isinstance(parsed, dict):
            return parsed
    raise FitAIError(
        code="INTERNAL_ERROR",
        message="Внутренняя ошибка сервера",
        status_code=500,
    )


def _admin_allowlist() -> set[str]:
    raw = settings.ADMIN_USER_IDS.strip()
    if not raw:
        return set()
    values: set[str] = set()
    for part in raw.split(","):
        candidate = part.strip()
        if candidate:
            values.add(candidate)
    return values


def _require_admin_user(user: dict) -> None:
    allowlist = _admin_allowlist()
    if not allowlist:
        raise FitAIError(
            code="FORBIDDEN",
            message="Недостаточно прав",
            status_code=403,
        )
    if str(user.get("id")) not in allowlist:
        raise FitAIError(
            code="FORBIDDEN",
            message="Недостаточно прав",
            status_code=403,
        )


def require_admin_user(user=Depends(get_current_user)) -> dict:
    _require_admin_user(user)
    return user


async def _get_active_subscriptions(conn) -> int:
    count = await fetchval_named(
        conn,
        "admin.active_subscriptions",
        """
        SELECT COUNT(*)::int
        FROM users
        WHERE subscription_status = 'active'
          AND subscription_active_until > NOW()
        """
    )
    return int(count or 0)


async def _get_today_analyzes(conn, day_utc) -> int:
    total = await fetchval_named(
        conn,
        "admin.today_analyzes",
        """
        SELECT COALESCE(SUM(photos_used), 0)::int
        FROM usage_daily
        WHERE date = $1::date
        """,
        day_utc,
    )
    return int(total or 0)


async def _get_today_event_counters(conn, start_utc: datetime, end_utc: datetime) -> dict[str, int]:
    row = await fetchrow_named(
        conn,
        "admin.today_event_counters",
        """
        SELECT
            COUNT(*) FILTER (WHERE event_type = 'rate_limited')::int AS today_rate_limited,
            COUNT(*) FILTER (
                WHERE event_type = 'analyze_failed'
                  AND COALESCE(payload->>'code', 'AI_PROVIDER_ERROR') = 'AI_PROVIDER_ERROR'
            )::int AS today_ai_failures,
            COUNT(*) FILTER (WHERE event_type = 'payment_created')::int AS today_payments_created,
            COUNT(*) FILTER (WHERE event_type = 'payment_succeeded')::int AS today_payments_succeeded,
            COUNT(*) FILTER (WHERE event_type = 'subscription_activated')::int AS today_subscriptions_activated
        FROM events
        WHERE created_at >= $1
          AND created_at < $2
          AND event_type IN (
              'rate_limited',
              'analyze_failed',
              'payment_created',
              'payment_succeeded',
              'subscription_activated'
          )
        """,
        start_utc,
        end_utc,
    )
    return {
        "today_rate_limited": int(row["today_rate_limited"] if row else 0),
        "today_ai_failures": int(row["today_ai_failures"] if row else 0),
        "today_payments_created": int(row["today_payments_created"] if row else 0),
        "today_payments_succeeded": int(row["today_payments_succeeded"] if row else 0),
        "today_subscriptions_activated": int(row["today_subscriptions_activated"] if row else 0),
    }


@router.get("/stats", response_model=AdminStatsResponse)
async def get_admin_stats(request: Request, user=Depends(require_admin_user), conn=Depends(get_db)):
    started_at = time.monotonic()

    now_utc = datetime.now(timezone.utc)
    start_utc = datetime.combine(now_utc.date(), datetime.min.time(), tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(days=1)

    active_subscriptions = await _get_active_subscriptions(conn)
    today_analyzes = await _get_today_analyzes(conn, start_utc.date())
    event_counters = await _get_today_event_counters(conn, start_utc, end_utc)

    response = AdminStatsResponse(
        activeSubscriptions=active_subscriptions,
        mrrRubEstimate=active_subscriptions * int(settings.SUBSCRIPTION_PRICE_RUB),
        todayAnalyzes=today_analyzes,
        todayRateLimited=event_counters["today_rate_limited"],
        todayAiFailures=event_counters["today_ai_failures"],
        todayPaymentsCreated=event_counters["today_payments_created"],
        todayPaymentsSucceeded=event_counters["today_payments_succeeded"],
        todaySubscriptionsActivated=event_counters["today_subscriptions_activated"],
    )
    logger.info(
        "ADMIN_STATS_OK context=%s",
        log_ctx_json(
            log_ctx(
                request,
                user_id=user.get("id"),
                extra={
                    "status_code": 200,
                    "duration_ms": duration_ms(started_at),
                },
            )
        ),
    )
    return response


@router.get("/events", response_model=AdminEventListResponse)
async def list_admin_events(
    user=Depends(require_admin_user),
    conn=Depends(get_db),
    event_type: Optional[str] = Query(default=None, alias="eventType"),
    user_id: Optional[UUID] = Query(default=None, alias="userId"),
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    since_date, until_date = build_created_at_bounds(since, until)

    args: list[Any] = []
    query = """
        SELECT id, user_id, event_type, payload, created_at
        FROM events
        WHERE TRUE
    """

    if event_type is not None:
        args.append(event_type)
        query += f" AND event_type = ${len(args)}"

    if user_id is not None:
        args.append(str(user_id))
        query += f" AND user_id = ${len(args)}::uuid"

    if since_date is not None:
        args.append(since_date)
        query += f" AND created_at >= ${len(args)}::date"

    if until_date is not None:
        args.append(until_date + timedelta(days=1))
        query += f" AND created_at < ${len(args)}::date"

    if cursor is not None:
        cursor_created_at, cursor_id = decode_keyset_cursor(cursor)
        args.extend([cursor_created_at, cursor_id])
        created_idx = len(args) - 1
        id_idx = len(args)
        query += f" AND (created_at, id) < (${created_idx}::timestamptz, ${id_idx}::uuid)"

    args.append(limit + 1)
    query += f" ORDER BY created_at DESC, id DESC LIMIT ${len(args)}"

    rows = await fetch_named(conn, "admin.events.list", query, *args)

    has_more = len(rows) > limit
    visible_rows = rows[:limit]
    items: list[AdminEventListItem] = []
    for row in visible_rows:
        row_dict = dict(row)
        raw_user_id = row_dict.get("user_id")
        if raw_user_id is None:
            raise FitAIError(
                code="INTERNAL_ERROR",
                message="Внутренняя ошибка сервера",
                status_code=500,
            )

        items.append(
            AdminEventListItem(
                id=row_dict["id"],
                userId=raw_user_id,
                eventType=row_dict["event_type"],
                details=_payload_as_dict(row_dict["payload"]) if row_dict.get("payload") is not None else None,
                createdAt=row_dict["created_at"],
            )
        )

    next_cursor = None
    if has_more and visible_rows:
        last = dict(visible_rows[-1])
        next_cursor = encode_keyset_cursor(last["created_at"], str(last["id"]))

    return AdminEventListResponse(items=items, nextCursor=next_cursor)


@router.get(
    "/referral/stats",
    response_model=AdminReferralStatsResponse,
    response_model_exclude_none=True,
)
async def get_admin_referral_stats(
    include_totals_all_time: bool = Query(default=False, alias="includeTotalsAllTime"),
    user=Depends(require_admin_user),
    conn=Depends(get_db),
):
    now_utc = datetime.now(timezone.utc)
    start_utc = datetime.combine(now_utc.date(), datetime.min.time(), tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(days=1)

    today_row = await fetchrow_named(
        conn,
        "admin.referral.stats.today",
        """
        SELECT
            (SELECT COUNT(*) FROM referral_codes WHERE created_at >= $1 AND created_at < $2)::int AS today_codes_issued,
            (SELECT COUNT(*) FROM referral_redemptions WHERE created_at >= $1 AND created_at < $2)::int AS today_redeems,
            (
                SELECT COUNT(DISTINCT redeemer_user_id)
                FROM referral_redemptions
                WHERE created_at >= $1 AND created_at < $2
            )::int AS today_unique_redeemers,
            (
                SELECT COALESCE(SUM(credits_granted), 0)
                FROM referral_redemptions
                WHERE created_at >= $1 AND created_at < $2
            )::int AS today_credits_granted
        """,
        start_utc,
        end_utc,
    )

    today_codes_issued = int(today_row["today_codes_issued"] if today_row else 0)
    today_redeems = int(today_row["today_redeems"] if today_row else 0)
    today_unique_redeemers = int(today_row["today_unique_redeemers"] if today_row else 0)
    today_credits_granted = int(today_row["today_credits_granted"] if today_row else 0)

    totals_all_time: Optional[AdminReferralTotalsAllTime] = None
    if include_totals_all_time:
        totals_row = await fetchrow_named(
            conn,
            "admin.referral.stats.totals_all_time",
            """
            SELECT
                (SELECT COUNT(*) FROM referral_codes)::int AS codes_issued,
                (SELECT COUNT(*) FROM referral_redemptions)::int AS redeems,
                (SELECT COALESCE(SUM(credits_granted), 0) FROM referral_redemptions)::int AS credits_granted
            """,
        )
        totals_codes_issued = int(totals_row["codes_issued"] if totals_row else 0)
        totals_redeems = int(totals_row["redeems"] if totals_row else 0)
        totals_credits_granted = int(totals_row["credits_granted"] if totals_row else 0)
        totals_all_time = AdminReferralTotalsAllTime(
            codesIssued=totals_codes_issued,
            redeems=totals_redeems,
            creditsGranted=totals_credits_granted,
        )

    return AdminReferralStatsResponse(
        todayCodesIssued=today_codes_issued,
        todayRedeems=today_redeems,
        todayUniqueRedeemers=today_unique_redeemers,
        todayCreditsGranted=today_credits_granted,
        totalsAllTime=totals_all_time,
    )


@router.get("/referral/redemptions", response_model=AdminReferralRedemptionsResponse)
async def list_admin_referral_redemptions(
    user=Depends(require_admin_user),
    conn=Depends(get_db),
    user_id: Optional[UUID] = Query(default=None, alias="userId"),
    referrer_user_id: Optional[UUID] = Query(default=None, alias="referrerUserId"),
    date_from: Optional[str] = Query(default=None, alias="dateFrom"),
    date_to: Optional[str] = Query(default=None, alias="dateTo"),
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    since_date, until_date = build_created_at_bounds(
        date_from,
        date_to,
        since_field="dateFrom",
        until_field="dateTo",
    )

    args: list[Any] = []
    query = """
        SELECT
            id,
            created_at,
            redeemer_user_id,
            referrer_user_id,
            code,
            credits_granted
        FROM referral_redemptions
        WHERE TRUE
    """

    if user_id is not None:
        args.append(str(user_id))
        query += f" AND redeemer_user_id = ${len(args)}::uuid"

    if referrer_user_id is not None:
        args.append(str(referrer_user_id))
        query += f" AND referrer_user_id = ${len(args)}::uuid"

    if since_date is not None:
        args.append(since_date)
        query += f" AND created_at >= ${len(args)}::date"

    if until_date is not None:
        args.append(until_date + timedelta(days=1))
        query += f" AND created_at < ${len(args)}::date"

    if cursor is not None:
        cursor_created_at, cursor_id = decode_keyset_cursor(cursor)
        args.extend([cursor_created_at, cursor_id])
        created_idx = len(args) - 1
        id_idx = len(args)
        query += f" AND (created_at, id) < (${created_idx}::timestamptz, ${id_idx}::uuid)"

    args.append(limit + 1)
    query += f" ORDER BY created_at DESC, id DESC LIMIT ${len(args)}"

    rows = await fetch_named(conn, "admin.referral.redemptions.list", query, *args)

    has_more = len(rows) > limit
    visible_rows = rows[:limit]
    items: list[AdminReferralRedemptionItem] = []
    for row in visible_rows:
        row_dict = dict(row)
        items.append(
            AdminReferralRedemptionItem(
                id=row_dict["id"],
                createdAt=row_dict["created_at"],
                redeemerUserId=row_dict["redeemer_user_id"],
                referrerUserId=row_dict["referrer_user_id"],
                code=row_dict["code"],
                creditsGranted=int(row_dict["credits_granted"]),
            )
        )

    next_cursor = None
    if has_more and visible_rows:
        last = dict(visible_rows[-1])
        next_cursor = encode_keyset_cursor(last["created_at"], str(last["id"]))

    return AdminReferralRedemptionsResponse(items=items, nextCursor=next_cursor)
