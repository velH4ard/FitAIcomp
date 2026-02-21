import base64
import json
from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from .db import execute_named, fetch_named, fetchrow_named, get_db
from .deps import get_current_user
from .errors import FitAIError
from .schemas import (
    DailyStatsAfterDelete,
    DeleteMealResponse,
    FoodAnalysis,
    FoodTotals,
    MealAIInfo,
    MealDetailResponse,
    MealListItem,
    MealListResponse,
)

router = APIRouter(prefix="/v1/meals", tags=["Meals"])


def _parse_iso_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"fieldErrors": [{"field": "date", "issue": "must be YYYY-MM-DD"}]},
        ) from exc


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * ((4 - len(cursor) % 4) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        parsed = json.loads(payload)
    except Exception as exc:
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"fieldErrors": [{"field": "cursor", "issue": "malformed cursor"}]},
        ) from exc

    if not isinstance(parsed, dict):
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"fieldErrors": [{"field": "cursor", "issue": "malformed cursor"}]},
        )

    created_at_raw = parsed.get("createdAt")
    meal_id = parsed.get("id")
    if not isinstance(created_at_raw, str) or not isinstance(meal_id, str):
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"fieldErrors": [{"field": "cursor", "issue": "malformed cursor"}]},
        )

    try:
        created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
        UUID(meal_id)
    except Exception as exc:
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"fieldErrors": [{"field": "cursor", "issue": "malformed cursor"}]},
        ) from exc

    return created_at, meal_id


def _encode_cursor(created_at: datetime, meal_id: str) -> str:
    payload = {
        "createdAt": created_at.astimezone(timezone.utc).isoformat(),
        "id": meal_id,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _as_dict_json(value: Any) -> dict[str, Any]:
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


@router.get("", response_model=MealListResponse)
async def list_meals(
    date_filter: Optional[str] = Query(default=None, alias="date"),
    limit: int = Query(default=20, ge=1, le=50),
    cursor: Optional[str] = Query(default=None),
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    args: list[Any] = [user["id"]]
    query = """
        SELECT
            id,
            created_at,
            meal_time,
            COALESCE(image_url, image_path) AS image_url,
            COALESCE((result_json->'totals'->>'calories_kcal')::double precision, 0) AS calories_kcal,
            COALESCE((result_json->'totals'->>'protein_g')::double precision, 0) AS protein_g,
            COALESCE((result_json->'totals'->>'fat_g')::double precision, 0) AS fat_g,
            COALESCE((result_json->'totals'->>'carbs_g')::double precision, 0) AS carbs_g
        FROM meals
        WHERE user_id = $1
    """

    if date_filter is not None:
        utc_day = _parse_iso_date(date_filter)
        args.append(utc_day)
        day_idx = len(args)
        query += (
            f" AND created_at >= ${day_idx}::date"
            f" AND created_at < (${day_idx}::date + interval '1 day')"
        )

    if cursor is not None:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        args.extend([cursor_created_at, cursor_id])
        created_idx = len(args) - 1
        id_idx = len(args)
        query += (
            f" AND (created_at, id) < (${created_idx}::timestamptz, ${id_idx}::uuid)"
        )

    args.append(limit + 1)
    query += f" ORDER BY created_at DESC, id DESC LIMIT ${len(args)}"

    rows = await fetch_named(conn, "meals.list", query, *args)

    has_more = len(rows) > limit
    visible_rows = rows[:limit]
    items: list[MealListItem] = []
    for row in visible_rows:
        row_dict = dict(row)
        image_url = row_dict.get("image_url")
        if not image_url:
            raise FitAIError(
                code="INTERNAL_ERROR",
                message="Внутренняя ошибка сервера",
                status_code=500,
            )

        items.append(
            MealListItem(
                id=row_dict["id"],
                createdAt=row_dict["created_at"],
                mealTime=row_dict.get("meal_time") or "unknown",
                imageUrl=image_url,
                totals=FoodTotals(
                    calories_kcal=float(row_dict.get("calories_kcal") or 0),
                    protein_g=float(row_dict.get("protein_g") or 0),
                    fat_g=float(row_dict.get("fat_g") or 0),
                    carbs_g=float(row_dict.get("carbs_g") or 0),
                ),
            )
        )

    next_cursor = None
    if has_more and visible_rows:
        last = dict(visible_rows[-1])
        next_cursor = _encode_cursor(last["created_at"], str(last["id"]))

    return MealListResponse(items=items, nextCursor=next_cursor)


@router.get("/{meal_id}", response_model=MealDetailResponse)
async def get_meal(
    meal_id: UUID,
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    row = await fetchrow_named(
        conn,
        "meals.get",
        """
        SELECT
            id,
            created_at,
            meal_time,
            COALESCE(image_url, image_path) AS image_url,
            COALESCE(ai_provider, 'openrouter') AS ai_provider,
            COALESCE(ai_model, '') AS ai_model,
            COALESCE(ai_confidence, (result_json->>'overall_confidence')::double precision, 0) AS ai_confidence,
            result_json
        FROM meals
        WHERE id = $1 AND user_id = $2
        """,
        meal_id,
        user["id"],
    )

    if not row:
        raise FitAIError(code="NOT_FOUND", message="Не найдено", status_code=404)

    row_dict = dict(row)
    image_url = row_dict.get("image_url")
    if not image_url:
        raise FitAIError(code="INTERNAL_ERROR", message="Внутренняя ошибка сервера", status_code=500)

    result_json = _as_dict_json(row_dict.get("result_json"))
    result = FoodAnalysis(**result_json)

    return MealDetailResponse(
        id=row_dict["id"],
        createdAt=row_dict["created_at"],
        mealTime=row_dict.get("meal_time") or "unknown",
        imageUrl=image_url,
        ai=MealAIInfo(
            provider=row_dict.get("ai_provider") or "openrouter",
            model=row_dict.get("ai_model") or "",
            confidence=float(row_dict.get("ai_confidence") or 0),
        ),
        result=result,
    )


@router.delete("/{meal_id}", response_model=DeleteMealResponse)
async def delete_meal(
    meal_id: UUID,
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    async with conn.transaction():
        row = await fetchrow_named(
            conn,
            "meals.delete.lock",
            """
            SELECT id, created_at::date AS meal_date
            FROM meals
            WHERE id = $1 AND user_id = $2
            FOR UPDATE
            """,
            meal_id,
            user["id"],
        )

        if not row:
            raise FitAIError(code="NOT_FOUND", message="Не найдено", status_code=404)

        meal_date = dict(row)["meal_date"]

        await execute_named(
            conn,
            "meals.delete.row",
            "DELETE FROM meals WHERE id = $1 AND user_id = $2",
            meal_id,
            user["id"],
        )

        recalculated = await fetchrow_named(
            conn,
            "meals.delete.recalculate_daily",
            """
            SELECT
                COALESCE(SUM((result_json->'totals'->>'calories_kcal')::double precision), 0) AS calories_kcal,
                COALESCE(SUM((result_json->'totals'->>'protein_g')::double precision), 0) AS protein_g,
                COALESCE(SUM((result_json->'totals'->>'fat_g')::double precision), 0) AS fat_g,
                COALESCE(SUM((result_json->'totals'->>'carbs_g')::double precision), 0) AS carbs_g,
                COUNT(*)::int AS meals_count
            FROM meals
            WHERE user_id = $1
              AND created_at >= $2::date
              AND created_at < ($2::date + interval '1 day')
            """,
            user["id"],
            meal_date,
        )
        recalculated_dict = dict(recalculated)

        await execute_named(
            conn,
            "meals.delete.upsert_daily_stats",
            """
            INSERT INTO daily_stats (
                user_id,
                date,
                calories_kcal,
                protein_g,
                fat_g,
                carbs_g,
                meals_count,
                updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            ON CONFLICT (user_id, date)
            DO UPDATE SET
                calories_kcal = EXCLUDED.calories_kcal,
                protein_g = EXCLUDED.protein_g,
                fat_g = EXCLUDED.fat_g,
                carbs_g = EXCLUDED.carbs_g,
                meals_count = EXCLUDED.meals_count,
                updated_at = NOW()
            """,
            user["id"],
            meal_date,
            float(recalculated_dict.get("calories_kcal") or 0),
            float(recalculated_dict.get("protein_g") or 0),
            float(recalculated_dict.get("fat_g") or 0),
            float(recalculated_dict.get("carbs_g") or 0),
            int(recalculated_dict.get("meals_count") or 0),
        )

    return DeleteMealResponse(
        deleted=True,
        mealId=meal_id,
        dailyStats=DailyStatsAfterDelete(
            date=meal_date.isoformat(),
            calories_kcal=float(recalculated_dict.get("calories_kcal") or 0),
            protein_g=float(recalculated_dict.get("protein_g") or 0),
            fat_g=float(recalculated_dict.get("fat_g") or 0),
            carbs_g=float(recalculated_dict.get("carbs_g") or 0),
            mealsCount=int(recalculated_dict.get("meals_count") or 0),
        ),
    )
