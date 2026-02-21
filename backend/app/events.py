import json
import logging
import base64
from datetime import date, datetime, timedelta, timezone
from typing import Any
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from .db import fetch_named, get_db
from .deps import get_current_user
from .errors import FitAIError
from .schemas import EventListItem, EventListResponse


logger = logging.getLogger("fitai-events")
router = APIRouter(prefix="/v1/events", tags=["Events"])

_SENSITIVE_KEYS = {
    "authorization",
    "token",
    "secret",
    "api_key",
    "apikey",
    "password",
    "initdata",
    "hash",
}


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, nested in value.items():
            key_str = str(key)
            if key_str.lower() in _SENSITIVE_KEYS:
                continue
            sanitized[key_str] = _sanitize_payload(nested)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    return value


def _validation_error(field: str, issue: str) -> FitAIError:
    return FitAIError(
        code="VALIDATION_FAILED",
        message="Некорректные данные",
        status_code=400,
        details={"fieldErrors": [{"field": field, "issue": issue}]},
    )


def _parse_iso_date(raw: Optional[str], field_name: str) -> Optional[date]:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise _validation_error(field_name, "must be YYYY-MM-DD") from exc


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


def decode_keyset_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * ((4 - len(cursor) % 4) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        parsed = json.loads(payload)
    except Exception as exc:
        raise _validation_error("cursor", "malformed cursor") from exc

    if not isinstance(parsed, dict):
        raise _validation_error("cursor", "malformed cursor")

    created_at_raw = parsed.get("createdAt")
    event_id = parsed.get("id")
    if not isinstance(created_at_raw, str) or not isinstance(event_id, str):
        raise _validation_error("cursor", "malformed cursor")

    try:
        created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
        UUID(event_id)
    except Exception as exc:
        raise _validation_error("cursor", "malformed cursor") from exc

    return created_at, event_id


def encode_keyset_cursor(created_at: datetime, event_id: str) -> str:
    payload = {
        "createdAt": created_at.astimezone(timezone.utc).isoformat(),
        "id": event_id,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def build_created_at_bounds(
    since_raw: Optional[str],
    until_raw: Optional[str],
    since_field: str = "since",
    until_field: str = "until",
) -> tuple[Optional[date], Optional[date]]:
    since_date = _parse_iso_date(since_raw, since_field)
    until_date = _parse_iso_date(until_raw, until_field)
    if since_date and until_date and since_date > until_date:
        raise _validation_error(since_field, "must be <= until")
    return since_date, until_date


@router.get("", response_model=EventListResponse)
async def list_user_events(
    event_type: Optional[str] = Query(default=None, alias="eventType"),
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    since_date, until_date = build_created_at_bounds(since, until)

    args: list[Any] = [user["id"]]
    query = """
        SELECT id, event_type, payload, created_at
        FROM events
        WHERE user_id = $1
    """

    if event_type is not None:
        args.append(event_type)
        query += f" AND event_type = ${len(args)}"

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

    rows = await fetch_named(conn, "events.list.user", query, *args)

    has_more = len(rows) > limit
    visible_rows = rows[:limit]
    items: list[EventListItem] = []
    for row in visible_rows:
        row_dict = dict(row)
        items.append(
            EventListItem(
                id=row_dict["id"],
                eventType=row_dict["event_type"],
                details=_payload_as_dict(row_dict["payload"]) if row_dict.get("payload") is not None else None,
                createdAt=row_dict["created_at"],
            )
        )

    next_cursor = None
    if has_more and visible_rows:
        last = dict(visible_rows[-1])
        next_cursor = encode_keyset_cursor(last["created_at"], str(last["id"]))

    return EventListResponse(items=items, nextCursor=next_cursor)


async def write_event_best_effort(
    conn: Any,
    event_type: str,
    user_id: Optional[str],
    payload: Optional[dict[str, Any]] = None,
) -> None:
    if not user_id:
        logger.warning("Skip event=%s due to empty user_id", event_type)
        return

    safe_payload = payload or {}
    safe_payload = _sanitize_payload(safe_payload)

    query = "INSERT INTO events (user_id, event_type, payload) VALUES ($1::uuid, $2, $3::jsonb)"
    params = (str(user_id), event_type, json.dumps(safe_payload))

    try:
        await conn.execute(query, *params)
    except Exception as exc:
        logger.warning("Failed to store event=%s reason=%s", event_type, type(exc).__name__)
