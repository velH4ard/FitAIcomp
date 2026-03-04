import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from .errors import FitAIError
from .events import write_event_best_effort
from .subscription import get_effective_subscription_status, get_user_daily_limit

logger = logging.getLogger("fitai-structured-analysis")

STEP1_AI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["recognized", "overall_confidence", "items", "warnings"],
    "properties": {
        "recognized": {"type": "boolean"},
        "overall_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "items": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "name",
                    "match_type",
                    "confidence",
                    "nutrition_per_100g",
                    "default_weight_g",
                    "warnings",
                ],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 120},
                    "match_type": {"type": "string", "enum": ["exact", "fuzzy", "unknown"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "nutrition_per_100g": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["calories_kcal", "protein_g", "fat_g", "carbs_g"],
                        "properties": {
                            "calories_kcal": {"type": "number", "minimum": 0},
                            "protein_g": {"type": "number", "minimum": 0},
                            "fat_g": {"type": "number", "minimum": 0},
                            "carbs_g": {"type": "number", "minimum": 0},
                        },
                    },
                    "default_weight_g": {"type": ["number", "null"], "exclusiveMinimum": 0},
                    "warnings": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {"type": "string", "minLength": 1, "maxLength": 240},
                    },
                },
            },
        },
        "warnings": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 240},
        },
    },
}

STEP1_AI_VALIDATOR = Draft202012Validator(STEP1_AI_SCHEMA)

EXACT_SIMILARITY_THRESHOLD = 0.90
FUZZY_SIMILARITY_THRESHOLD = 0.35

RU_FUZZY_WARNING = "Нет точного совпадения, использована приблизительная категория."
RU_UNKNOWN_WARNING = "Не найдено в базе продуктов, использована приблизительная пищевая ценность."
RU_FALLBACK_WARNING = "Часть нутриентов заполнена консервативным fallback-значением."
RU_BASE_FALLBACK_WARNING = "Для варианта блюда нет точного КБЖУ, использованы значения базового продукта."

_MULTISPACE_RE = re.compile(r"\s+")


def ensure_step1_ai_payload(raw_output: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"schema": "step1-classifier", "issue": f"invalid_json: {exc.msg}"},
        ) from exc

    if not isinstance(parsed, dict):
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"schema": "step1-classifier", "issue": "root must be object"},
        )

    try:
        STEP1_AI_VALIDATOR.validate(parsed)
    except JsonSchemaValidationError as exc:
        field_path = ".".join(str(p) for p in exc.path) or "$"
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"schema": "step1-classifier", "issue": f"{field_path}: {exc.message}"},
        ) from exc

    return parsed


def _round2(value: float) -> float:
    return round(float(value), 2)


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def normalize_food_text(value: str) -> str:
    normalized = (value or "").strip().lower().replace("ё", "е")
    normalized = re.sub(r"[^\w\s-]+", " ", normalized)
    return _MULTISPACE_RE.sub(" ", normalized)


def compact_food_text(value: str) -> str:
    return normalize_food_text(value).replace(" ", "")


def adjust_candidate_confidence(
    *,
    ai_confidence: Optional[float],
    match_type: str,
    match_source: str,
    match_score: float,
) -> float:
    source = match_source or "unknown"
    score = _clamp(float(match_score), 0.0, 1.0)

    if ai_confidence is None:
        base = {
            "exact": 0.95 if source == "name_exact" else 0.82,
            "fuzzy": 0.58 if source != "ilike" else 0.44,
            "unknown": 0.2,
        }.get(match_type, 0.2)
    else:
        base = _clamp(float(ai_confidence), 0.0, 1.0)

    if match_type == "exact":
        if source == "name_exact":
            return _round2(_clamp(max(base, 0.90), 0.90, 1.00))
        return _round2(_clamp(max(base * 0.92, 0.75), 0.75, 0.89))

    if match_type == "fuzzy":
        if source == "ilike":
            return _round2(_clamp(max(base * 0.60, 0.35), 0.35, 0.55))
        boosted = max(base * 0.70, 0.35 + (score - FUZZY_SIMILARITY_THRESHOLD) * 0.4)
        return _round2(_clamp(boosted, 0.35, 0.74))

    return _round2(_clamp(min(base * 0.40, 0.34), 0.0, 0.34))


def _nutrition_fallback() -> dict[str, float]:
    return {
        "calories_kcal": 180.0,
        "protein_g": 8.0,
        "fat_g": 8.0,
        "carbs_g": 18.0,
    }


def _normalize_nutrition(candidate: dict[str, Any], item_warnings: list[str]) -> dict[str, float]:
    raw = candidate.get("nutrition_per_100g")
    if not isinstance(raw, dict):
        raw = {}

    fallback = _nutrition_fallback()
    result: dict[str, float] = {}
    had_missing = False
    for key in ("calories_kcal", "protein_g", "fat_g", "carbs_g"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and float(value) >= 0:
            result[key] = _round2(float(value))
        else:
            had_missing = True
            result[key] = fallback[key]

    if had_missing and RU_FALLBACK_WARNING not in item_warnings:
        item_warnings.append(RU_FALLBACK_WARNING)
    return result


def _contains_warning(warnings: list[str], text: str) -> bool:
    return any(w.strip() == text for w in warnings)


def _ensure_ru_match_warning(match_type: str, warnings: list[str]) -> list[str]:
    out = [str(w) for w in warnings if isinstance(w, str) and w.strip()]
    if match_type == "fuzzy" and not _contains_warning(out, RU_FUZZY_WARNING):
        out.append(RU_FUZZY_WARNING)
    if match_type == "unknown" and not _contains_warning(out, RU_UNKNOWN_WARNING):
        out.append(RU_UNKNOWN_WARNING)
    return out[:5]


async def resolve_food_candidate(
    conn,
    *,
    name: str,
    ai_match_type: str,
    ai_confidence: Optional[float],
    ai_nutrition: dict[str, float],
    ai_default_weight: Optional[float],
    ai_warnings: list[str],
) -> dict[str, Any]:
    candidate_name = (name or "").strip()
    normalized_name = normalize_food_text(candidate_name)
    compact_name = compact_food_text(candidate_name)

    if len(normalized_name) < 2:
        warnings = _ensure_ru_match_warning("unknown", list(ai_warnings))
        return {
            "name": candidate_name or "неизвестный продукт",
            "match_type": "unknown",
            "match_score": 0.0,
            "match_source": "empty_input",
            "confidence": adjust_candidate_confidence(
                ai_confidence=ai_confidence,
                match_type="unknown",
                match_source="empty_input",
                match_score=0.0,
            ),
            "nutrition_per_100g": _nutrition_fallback(),
            "default_weight_g": _round2(float(ai_default_weight)) if isinstance(ai_default_weight, (int, float)) and float(ai_default_weight) > 0 else None,
            "warnings": warnings,
            "metadata": {
                "match_type": "unknown",
                "match_score": 0.0,
                "match_source": "empty_input",
            },
        }

    match_type = ai_match_type
    match_source = "unknown"
    match_score = 0.0

    resolved_name = candidate_name
    nutrition = _normalize_nutrition({"nutrition_per_100g": ai_nutrition}, [])
    warnings = list(ai_warnings)

    row = await conn.fetchrow(
        """
        SELECT
            id,
            name,
            normalized_name,
            base_name,
            normalized_base_name,
            state,
            calories_per_100g,
            protein_per_100g,
            fat_per_100g,
            carbs_per_100g
        FROM foods
        WHERE normalized_name = $1
        LIMIT 1
        """,
        normalized_name,
    )
    if row is not None:
        match_type = "exact"
        match_source = "name_exact"
        match_score = 1.0

    if row is None:
        row = await conn.fetchrow(
            """
            SELECT
                id,
                name,
                normalized_name,
                base_name,
                normalized_base_name,
                state,
                calories_per_100g,
                protein_per_100g,
                fat_per_100g,
                carbs_per_100g
            FROM foods
            WHERE normalized_aliases @> ARRAY[$1::text]
               OR compact_aliases @> ARRAY[$2::text]
            LIMIT 1
            """,
            normalized_name,
            compact_name,
        )
        if row is not None:
            match_type = "exact"
            match_source = "alias_exact"
            match_score = 1.0

    if row is None:
        row = await conn.fetchrow(
            """
            WITH candidates AS (
                SELECT
                    f.id,
                    f.name,
                    f.normalized_name,
                    f.base_name,
                    f.normalized_base_name,
                    f.state,
                    f.calories_per_100g,
                    f.protein_per_100g,
                    f.fat_per_100g,
                    f.carbs_per_100g,
                    GREATEST(
                        similarity(f.normalized_name, $1),
                        similarity(f.alias_search_text, $1),
                        similarity(f.compact_alias_search_text, $2)
                    ) AS score
                FROM foods f
                WHERE (
                    f.normalized_name % $1
                    OR f.alias_search_text % $1
                    OR f.compact_alias_search_text % $2
                )
            )
            SELECT *
            FROM candidates
            WHERE score >= $3
            ORDER BY score DESC, id ASC
            LIMIT 1
            """,
            normalized_name,
            compact_name,
            FUZZY_SIMILARITY_THRESHOLD,
        )
        if row is not None:
            match_type = "fuzzy"
            match_source = "trgm"
            match_score = float(row["score"] or 0)

    if row is None:
        row = await conn.fetchrow(
            """
            SELECT
                id,
                name,
                normalized_name,
                base_name,
                normalized_base_name,
                state,
                calories_per_100g,
                protein_per_100g,
                fat_per_100g,
                carbs_per_100g
            FROM foods
            WHERE normalized_name ILIKE ('%' || $1 || '%')
               OR alias_search_text ILIKE ('%' || $1 || '%')
               OR compact_alias_search_text ILIKE ('%' || $2 || '%')
            ORDER BY id ASC
            LIMIT 1
            """,
            normalized_name,
            compact_name,
        )
        if row is not None:
            match_type = "fuzzy"
            match_source = "ilike"
            match_score = FUZZY_SIMILARITY_THRESHOLD

    if row is not None:
        resolved_name = str(row["name"])
        row_nutrition = {
            "calories_kcal": row["calories_per_100g"],
            "protein_g": row["protein_per_100g"],
            "fat_g": row["fat_per_100g"],
            "carbs_g": row["carbs_per_100g"],
        }
        missing = [k for k, v in row_nutrition.items() if v is None]

        if missing and row["normalized_base_name"]:
            base_row = await conn.fetchrow(
                """
                SELECT
                    calories_per_100g,
                    protein_per_100g,
                    fat_per_100g,
                    carbs_per_100g
                FROM foods
                WHERE normalized_name = $1
                  AND calories_per_100g IS NOT NULL
                  AND protein_per_100g IS NOT NULL
                  AND fat_per_100g IS NOT NULL
                  AND carbs_per_100g IS NOT NULL
                ORDER BY (state IS NOT NULL) ASC, id ASC
                LIMIT 1
                """,
                str(row["normalized_base_name"]),
            )
            if base_row is not None:
                row_nutrition = {
                    "calories_kcal": base_row["calories_per_100g"] if row_nutrition["calories_kcal"] is None else row_nutrition["calories_kcal"],
                    "protein_g": base_row["protein_per_100g"] if row_nutrition["protein_g"] is None else row_nutrition["protein_g"],
                    "fat_g": base_row["fat_per_100g"] if row_nutrition["fat_g"] is None else row_nutrition["fat_g"],
                    "carbs_g": base_row["carbs_per_100g"] if row_nutrition["carbs_g"] is None else row_nutrition["carbs_g"],
                }
                if not _contains_warning(warnings, RU_BASE_FALLBACK_WARNING):
                    warnings.append(RU_BASE_FALLBACK_WARNING)

        nutrition = _normalize_nutrition({"nutrition_per_100g": row_nutrition}, warnings)
        if _contains_warning(warnings, RU_FALLBACK_WARNING) and missing:
            match_type = "unknown"

    if row is None:
        match_type = "unknown"

    confidence = adjust_candidate_confidence(
        ai_confidence=ai_confidence,
        match_type=match_type,
        match_source=match_source,
        match_score=match_score,
    )
    warnings = _ensure_ru_match_warning(match_type, warnings)

    default_weight: Optional[float] = None
    if isinstance(ai_default_weight, (int, float)) and float(ai_default_weight) > 0:
        default_weight = _round2(float(ai_default_weight))

    return {
        "name": resolved_name,
        "match_type": match_type,
        "match_score": _round2(match_score),
        "match_source": match_source,
        "confidence": confidence,
        "nutrition_per_100g": nutrition,
        "default_weight_g": default_weight,
        "warnings": warnings,
        "metadata": {
            "match_type": match_type,
            "match_score": _round2(match_score),
            "match_source": match_source,
        },
    }


async def reserve_daily_quota_for_step2(conn, *, user: dict[str, Any], today):
    status = get_effective_subscription_status(
        user["subscription_status"],
        user["subscription_active_until"],
    )
    daily_limit = get_user_daily_limit(user)

    await conn.execute(
        """
        INSERT INTO usage_daily (user_id, date, photos_used)
        VALUES ($1, $2, 0)
        ON CONFLICT (user_id, date) DO NOTHING
        """,
        user["id"],
        today,
    )

    row = await conn.fetchrow(
        "SELECT photos_used FROM usage_daily WHERE user_id = $1 AND date = $2 FOR UPDATE",
        user["id"],
        today,
    )
    used = int(row["photos_used"] if row else 0)
    if used >= daily_limit:
        raise FitAIError(
            code="QUOTA_EXCEEDED",
            message="Достигнут дневной лимит фото",
            status_code=429,
            details={"limit": daily_limit, "used": used, "status": status},
        )

    await conn.execute(
        "UPDATE usage_daily SET photos_used = photos_used + 1 WHERE user_id = $1 AND date = $2",
        user["id"],
        today,
    )
    updated = await conn.fetchrow(
        "SELECT photos_used FROM usage_daily WHERE user_id = $1 AND date = $2",
        user["id"],
        today,
    )
    photos_used = int(updated["photos_used"] if updated else used + 1)
    return {
        "daily_limit": daily_limit,
        "photos_used": photos_used,
        "status": status,
    }


def build_step2_result_from_snapshot(
    snapshot_items: list[dict[str, Any]],
    requested_weights: dict[str, float],
    overall_confidence: float,
) -> dict[str, Any]:
    result_items: list[dict[str, Any]] = []
    warnings_acc: list[str] = []

    total_cal = 0.0
    total_protein = 0.0
    total_fat = 0.0
    total_carbs = 0.0

    for item in snapshot_items:
        client_item_id = str(item["client_item_id"])
        grams = float(requested_weights[client_item_id])

        n = item["nutrition_per_100g"]
        calories = _round2(float(n["calories_kcal"]) * grams / 100.0)
        protein = _round2(float(n["protein_g"]) * grams / 100.0)
        fat = _round2(float(n["fat_g"]) * grams / 100.0)
        carbs = _round2(float(n["carbs_g"]) * grams / 100.0)

        total_cal += calories
        total_protein += protein
        total_fat += fat
        total_carbs += carbs

        item_warnings = item.get("warnings") or []
        kbju_source = "exact"
        if item.get("match_type") == "unknown":
            kbju_source = "fallback"
        elif _contains_warning(item_warnings, RU_BASE_FALLBACK_WARNING):
            kbju_source = "base"
        elif _contains_warning(item_warnings, RU_FALLBACK_WARNING):
            kbju_source = "fallback"

        if item.get("match_type") in {"fuzzy", "unknown"} and not _contains_warning(
            warnings_acc,
            "Для части блюда использована приблизительная категория (fuzzy match).",
        ):
            warnings_acc.append("Для части блюда использована приблизительная категория (fuzzy match).")
        for warning in item_warnings:
            if isinstance(warning, str) and warning.strip() and not _contains_warning(warnings_acc, warning.strip()):
                warnings_acc.append(warning.strip())

        result_items.append(
            {
                "name": str(item["name"]),
                "grams": _round2(grams),
                "calories_kcal": calories,
                "protein_g": protein,
                "fat_g": fat,
                "carbs_g": carbs,
                "confidence": _round2(float(item.get("confidence") or 0)),
                "_kbju_source": kbju_source,
            }
        )

    return {
        "recognized": bool(snapshot_items),
        "overall_confidence": _round2(overall_confidence),
        "totals": {
            "calories_kcal": _round2(total_cal),
            "protein_g": _round2(total_protein),
            "fat_g": _round2(total_fat),
            "carbs_g": _round2(total_carbs),
        },
        "items": result_items,
        "warnings": warnings_acc[:8],
        "assumptions": ["Расчет выполнен по значениям на 100 г из шага 1."],
    }


def step1_session_expired(created_at: datetime, *, now: Optional[datetime] = None) -> bool:
    utc_now = now or datetime.now(timezone.utc)
    created_utc = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
    return (utc_now - created_utc) > timedelta(minutes=15)


async def mark_step2_idempotency_failed(conn, request_id: UUID) -> None:
    try:
        await conn.execute(
            """
            UPDATE analyze_requests
            SET status = 'failed', updated_at = NOW()
            WHERE id = $1 AND status = 'processing'
            """,
            request_id,
        )
    except Exception:
        logger.warning("Failed to mark step2 idempotency failed request_id=%s", request_id)


async def emit_step_events(
    conn,
    *,
    user_id: str,
    ok: bool,
    step: str,
    details: Optional[dict[str, Any]] = None,
) -> None:
    await write_event_best_effort(
        conn,
        event_type=f"analysis_{step}_{'ok' if ok else 'fail'}",
        user_id=user_id,
        payload=details or {},
    )
