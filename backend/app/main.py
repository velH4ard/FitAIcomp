import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter, Depends, UploadFile, File, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from .errors import setup_error_handlers, FitAIError
from .db import db, get_db
import asyncpg
from .integrations.openrouter import openrouter_client
from .schemas import (
    AuthRequest, 
    AuthResponse, 
    UserResponse, 
    UserProfile, 
    ProfileUpdateResponse, 
    ProfileGoalUpdateRequest,
    ProfileGoalUpdateResponse,
    SubscriptionInfo,
    UsageResponse
)
from .auth import verify_telegram_init_data, create_access_token
from .deps import get_current_user
from .config import settings
from .payments import router as payments_router, get_webhook_auth_mode
from .meals import router as meals_router
from .stats import router as stats_router
from .admin import router as admin_router
from .paywall import router as paywall_router
from .referral import router as referral_router
from .streak import router as streak_router
from .share import router as share_router
from .notifications_api import router as notifications_router
from .premium import router as premium_router
from .subscription import compute_upgrade_hint, get_effective_subscription_status, get_user_daily_limit
from .goals import calculate_daily_goal_auto, normalize_gender
from .events import router as events_router, write_event_best_effort
from .jitter import apply_post_ai_error
from .observability import (
    REQUEST_ID_HEADER,
    reset_request_context,
    set_request_context,
    duration_ms,
    log_ctx,
    log_ctx_json,
    validate_request_id,
)
from datetime import datetime, timezone

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("fitai-api")
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting FitAI API...")
    if settings.is_production():
        logger.info("security mode: production strict enabled")
    logger.info("Startup security mode: %s", get_webhook_auth_mode())
    if settings.is_production() and settings.PAYMENTS_WEBHOOK_DEV_BYPASS == 1:
        logger.warning("PAYMENTS_WEBHOOK_DEV_BYPASS is ignored in production")
    logger.info(
        "Startup CORS config: origins=%s origin_regex=%s",
        settings.get_cors_allow_origins(),
        settings.get_cors_allow_origin_regex() or "",
    )
    await db.create_pool()
    yield
    # Shutdown
    logger.info("Shutting down FitAI API...")
    await db.close_pool()

app = FastAPI(
    title="FitAI API",
    description="Backend for FitAI Telegram WebApp",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_allow_origins(),
    allow_origin_regex=settings.get_cors_allow_origin_regex(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-Id"],
    expose_headers=[REQUEST_ID_HEADER],
)


@app.middleware("http")
async def request_observability_middleware(request: Request, call_next):
    started_at = time.monotonic()

    incoming_request_id = request.headers.get(REQUEST_ID_HEADER)
    if incoming_request_id is None:
        request_id = str(uuid.uuid4())
    else:
        if not validate_request_id(incoming_request_id):
            request_id = str(uuid.uuid4())
            request.state.request_id = request_id
            response = JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "VALIDATION_FAILED",
                        "message": "Некорректные данные",
                        "details": {
                            "fieldErrors": [
                                {
                                    "field": "header.X-Request-Id",
                                    "issue": "must be non-empty and <= 128 chars",
                                }
                            ]
                        },
                    }
                },
            )
            response.headers[REQUEST_ID_HEADER] = request_id
            logger.warning(
                "REQUEST_REJECTED context=%s",
                log_ctx_json(
                    log_ctx(
                        request,
                        extra={
                            "status_code": 400,
                            "duration_ms": duration_ms(started_at),
                            "reason": "invalid_x_request_id",
                        },
                    )
                ),
            )
            return response
        request_id = incoming_request_id.strip()

    request.state.request_id = request_id
    context_tokens = set_request_context(request_id=request_id, path=request.url.path)
    try:
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "REQUEST_DONE context=%s",
            log_ctx_json(
                log_ctx(
                    request,
                    extra={
                        "status_code": response.status_code,
                        "duration_ms": duration_ms(started_at),
                    },
                )
            ),
        )
        return response
    finally:
        reset_request_context(context_tokens)

# Setup custom error handlers
setup_error_handlers(app)

# API Router
v1_router = APIRouter(prefix="/v1")

import json

ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
DESCRIPTION_MAX_LEN = 500


def _load_ai_contract_schema() -> dict:
    source_path = Path(__file__).resolve()
    schema_path = None
    for parent in source_path.parents:
        candidate = parent / "docs" / "spec" / "ai-contract.md"
        if candidate.exists():
            schema_path = candidate
            break

    if schema_path is None:
        raise RuntimeError("AI contract schema file not found")

    content = schema_path.read_text(encoding="utf-8")
    marker = "```json"
    start = content.find(marker)
    if start == -1:
        raise RuntimeError("AI contract JSON schema block not found")
    start = content.find("{", start + len(marker))
    if start == -1:
        raise RuntimeError("AI contract JSON schema object not found")

    depth = 0
    end = -1
    for idx in range(start, len(content)):
        char = content[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break

    if end == -1:
        raise RuntimeError("AI contract JSON schema object is not closed")

    return json.loads(content[start:end])


AI_CONTRACT_SCHEMA = _load_ai_contract_schema()
AI_CONTRACT_VALIDATOR = Draft202012Validator(AI_CONTRACT_SCHEMA)


async def _enforce_analyze_rate_limit(conn: asyncpg.Connection, user_id: str) -> None:
    per_minute_limit = max(1, int(settings.MEALS_ANALYZE_RATE_LIMIT_PER_MINUTE))
    try:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*)::int AS events_count
            FROM events
            WHERE user_id = $1
              AND event_type = 'analyze_started'
              AND created_at >= (NOW() - interval '1 minute')
            """,
            user_id,
        )
    except Exception as exc:
        logger.warning("Rate-limit precheck failed user_id=%s reason=%s", user_id, type(exc).__name__)
        return

    recent_events = int(row["events_count"] if row else 0)
    if recent_events < per_minute_limit:
        return

    await write_event_best_effort(
        conn,
        event_type="rate_limited",
        user_id=str(user_id),
        payload={
            "scope": "analyze",
            "limitPerMinute": per_minute_limit,
            "recentEvents": recent_events,
            "retryAfterSeconds": 60,
        },
    )
    raise FitAIError(
        code="RATE_LIMITED",
        message="Слишком много запросов, попробуйте позже",
        status_code=429,
        details={"retryAfterSeconds": 60, "scope": "analyze"},
    )


def _raise_description_validation_error(issue: str) -> None:
    raise FitAIError(
        code="VALIDATION_FAILED",
        message="Некорректные данные",
        status_code=400,
        details={
            "fieldErrors": [
                {
                    "field": "description",
                    "issue": issue,
                    "maxLen": DESCRIPTION_MAX_LEN,
                }
            ],
            "maxLen": DESCRIPTION_MAX_LEN,
        },
    )


def _normalize_optional_description(raw_value: Any) -> Optional[str]:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        _raise_description_validation_error("must be a string")

    normalized = raw_value.strip()
    if normalized == "":
        return None
    if len(normalized) > DESCRIPTION_MAX_LEN:
        _raise_description_validation_error("must be <= 500 chars")
    return normalized


async def _parse_optional_description_from_multipart(request: Request) -> Optional[str]:
    try:
        form = await request.form()
    except Exception as exc:
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={
                "fieldErrors": [
                    {
                        "field": "description",
                        "issue": "invalid multipart field",
                        "maxLen": DESCRIPTION_MAX_LEN,
                    }
                ],
                "maxLen": DESCRIPTION_MAX_LEN,
            },
        ) from exc

    if "description" not in form:
        return None

    values = form.getlist("description")
    raw_value = values[-1] if values else None
    return _normalize_optional_description(raw_value)

def format_user_response(user_dict: dict, used_today: int = 0) -> UserResponse:
    # Calculate subscription info
    status = get_effective_subscription_status(
        user_dict["subscription_status"],
        user_dict["subscription_active_until"],
    )
    daily_limit = get_user_daily_limit(user_dict)
    
    subscription = SubscriptionInfo(
        status=status,
        activeUntil=user_dict["subscription_active_until"],
        priceRubPerMonth=settings.SUBSCRIPTION_PRICE_RUB,
        dailyLimit=daily_limit,
        usedToday=used_today
    )
    
    profile_data = user_dict.get("profile")
    if isinstance(profile_data, str):
        try:
            profile_data = json.loads(profile_data)
        except:
            profile_data = None
            
    profile = None
    if profile_data:
        if "gender" in profile_data:
            profile_data["gender"] = normalize_gender(profile_data.get("gender"))
        profile = UserProfile(**profile_data)
        
    return UserResponse(
        id=user_dict["id"],
        telegramId=user_dict["telegram_id"],
        username=user_dict.get("username"),
        isOnboarded=user_dict["is_onboarded"],
        profile=profile,
        subscription=subscription
    )

@v1_router.post("/auth/telegram", response_model=AuthResponse, tags=["Auth"])
async def auth_telegram(payload: AuthRequest, conn = Depends(get_db)):
    tg_user = verify_telegram_init_data(payload.initData)
    
    tg_id = tg_user["id"]
    username = tg_user.get("username")
    
    # Upsert user
    row = await conn.fetchrow(
        """
        INSERT INTO users (telegram_id, username)
        VALUES ($1, $2)
        ON CONFLICT (telegram_id) DO UPDATE 
        SET username = EXCLUDED.username, updated_at = NOW()
        RETURNING id, telegram_id, username, is_onboarded, subscription_status, subscription_active_until, referral_credits, profile, daily_goal_auto, daily_goal_override
        """,
        tg_id, username
    )
    
    user_dict = dict(row)
    access_token = create_access_token({"sub": str(user_dict["id"])})
    
    return AuthResponse(
        accessToken=access_token,
        user=format_user_response(user_dict)
    )

@v1_router.get("/me", response_model=UserResponse, tags=["User"])
async def get_me(user = Depends(get_current_user), conn = Depends(get_db)):
    today = datetime.now(timezone.utc).date()
    row = await conn.fetchrow(
        "SELECT photos_used FROM usage_daily WHERE user_id = $1 AND date = $2",
        user["id"], today
    )
    used_today = row["photos_used"] if row else 0
    return format_user_response(user, used_today=used_today)

@v1_router.put("/me/profile", response_model=ProfileUpdateResponse, tags=["User"])
async def update_profile(
    profile: UserProfile, 
    user = Depends(get_current_user),
    conn = Depends(get_db)
):
    profile_dict = profile.model_dump()
    profile_json = json.dumps(profile_dict)
    auto_goal = calculate_daily_goal_auto(profile_dict) or 2000
    
    row = await conn.fetchrow(
        """
        UPDATE users 
        SET profile = $1,
            daily_goal_auto = $2,
            is_onboarded = TRUE,
            updated_at = NOW()
        WHERE id = $3
        RETURNING id, is_onboarded, profile
        """,
        profile_json,
        auto_goal,
        user["id"]
    )
    
    if not row:
        raise FitAIError(code="NOT_FOUND", message="Пользователь не найден", status_code=404)
        
    updated_user = dict(row)
    
    return ProfileUpdateResponse(
        id=updated_user["id"],
        isOnboarded=updated_user["is_onboarded"],
        profile=profile
    )

@v1_router.get("/usage/today", response_model=UsageResponse, tags=["Usage"])
async def get_usage_today(
    user = Depends(get_current_user),
    conn = Depends(get_db)
):
    today = datetime.now(timezone.utc).date()
    
    row = await conn.fetchrow(
        "SELECT photos_used FROM usage_daily WHERE user_id = $1 AND date = $2",
        user["id"], today
    )
    
    photos_used = row["photos_used"] if row else 0
    status = get_effective_subscription_status(
        user["subscription_status"],
        user["subscription_active_until"],
    )
    daily_limit = get_user_daily_limit(user)
    remaining = max(0, daily_limit - photos_used)
    
    return UsageResponse(
        date=today.isoformat(),
        dailyLimit=daily_limit,
        photosUsed=photos_used,
        remaining=remaining,
        subscriptionStatus=status,
        upgradeHint=compute_upgrade_hint(status, remaining),
    )


@v1_router.patch("/profile/goal", response_model=ProfileGoalUpdateResponse, tags=["User"])
async def update_profile_goal(
    payload: ProfileGoalUpdateRequest,
    user=Depends(get_current_user),
    conn=Depends(get_db),
):
    row = await conn.fetchrow(
        """
        UPDATE users
        SET daily_goal_override = $1,
            updated_at = NOW()
        WHERE id = $2
        RETURNING daily_goal_auto, daily_goal_override
        """,
        int(payload.dailyGoal),
        user["id"],
    )
    if row is None:
        raise FitAIError(code="NOT_FOUND", message="Пользователь не найден", status_code=404)

    auto_goal = int(row["daily_goal_auto"])
    override_goal = row["daily_goal_override"]
    effective_goal = int(override_goal) if override_goal is not None else auto_goal

    return ProfileGoalUpdateResponse(
        dailyGoal=effective_goal,
        autoGoal=auto_goal,
        override=int(override_goal) if override_goal is not None else None,
    )

@v1_router.post("/meals/analyze", tags=["Meals"])
async def analyze_meal(
    request: Request,
    image: Optional[UploadFile] = File(None),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user = Depends(get_current_user),
    conn = Depends(get_db)
):
    request_started_at = time.monotonic()

    def _load_replay_response(raw: object) -> dict:
        res = raw
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except json.JSONDecodeError as exc:
                raise FitAIError(
                    code="INTERNAL_ERROR",
                    message="Внутренняя ошибка сервера",
                    status_code=500,
                    details={"stage": "idempotency_replay_decode"},
                ) from exc
        if isinstance(res, dict):
            return res
        raise FitAIError(
            code="INTERNAL_ERROR",
            message="Внутренняя ошибка сервера",
            status_code=500,
            details={"stage": "idempotency_replay_shape"},
        )

    actual_file = image
    if actual_file is None:
        try:
            form = await request.form()
        except Exception:
            form = None
        legacy_file = form.get("file") if form is not None else None
        if legacy_file is not None and hasattr(legacy_file, "read") and hasattr(legacy_file, "content_type"):
            actual_file = legacy_file

    if actual_file is None:
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"fieldErrors": [{"field": "image", "issue": "Field required"}]},
        )

    content_type = (actual_file.content_type or "").lower()
    if content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"fieldErrors": [{"field": "image", "issue": "Only jpg/png/webp are allowed"}]},
        )

    image_bytes = await actual_file.read(settings.MEALS_ANALYZE_MAX_IMAGE_BYTES + 1)
    if not image_bytes:
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"fieldErrors": [{"field": "image", "issue": "File must not be empty"}]},
        )

    if len(image_bytes) > settings.MEALS_ANALYZE_MAX_IMAGE_BYTES:
        raise FitAIError(
            code="PAYLOAD_TOO_LARGE",
            message="Файл слишком большой",
            status_code=413,
            details={
                "maxBytes": settings.MEALS_ANALYZE_MAX_IMAGE_BYTES,
                "receivedBytes": len(image_bytes),
            },
        )

    normalized_description = await _parse_optional_description_from_multipart(request)

    if not user["is_onboarded"]:
        raise FitAIError(
            code="ONBOARDING_REQUIRED",
            message="Заполните анкету перед использованием",
            status_code=409
        )

    if idempotency_key:
        existing_req = await conn.fetchrow(
            "SELECT id, status, response_json FROM analyze_requests WHERE user_id = $1 AND idempotency_key = $2",
            user["id"],
            idempotency_key,
        )
        if existing_req:
            if existing_req["status"] == "completed" and existing_req["response_json"] is not None:
                return _load_replay_response(existing_req["response_json"])

            raise FitAIError(
                code="IDEMPOTENCY_CONFLICT",
                message="Запрос с таким ключом уже обрабатывается или завершился ошибкой",
                status_code=409,
            )

    await _enforce_analyze_rate_limit(conn, str(user["id"]))

    analyze_started_at_utc = datetime.now(timezone.utc)
    today = analyze_started_at_utc.date()
    status = get_effective_subscription_status(
        user["subscription_status"],
        user["subscription_active_until"],
    )
    daily_limit = get_user_daily_limit(user)
    quota_row = await conn.fetchrow(
        "SELECT photos_used FROM usage_daily WHERE user_id = $1 AND date = $2",
        user["id"],
        today,
    )
    photos_used = quota_row["photos_used"] if quota_row else 0
    if photos_used >= daily_limit:
        await write_event_best_effort(
            conn,
            event_type="quota_exceeded",
            user_id=str(user["id"]),
            payload={"limit": daily_limit, "used": photos_used, "status": status, "stage": "precheck"},
        )
        raise FitAIError(
            code="QUOTA_EXCEEDED",
            message="Достигнут дневной лимит фото",
            status_code=429,
            details={"limit": daily_limit, "used": photos_used, "status": status},
        )

    quota_reserved = False
    usage_incremented = False
    reserved_photos_used: Optional[int] = None
    analyze_request_id = None
    analyze_started_emitted = False

    try:
        # 1. Insert idempotency (processing) only when key provided.
        if idempotency_key:
            try:
                inserted_req = await conn.fetchrow(
                    """
                    INSERT INTO analyze_requests (user_id, idempotency_key, status)
                    VALUES ($1, $2, 'processing')
                    RETURNING id
                    """,
                    user["id"], idempotency_key
                )
                analyze_request_id = inserted_req["id"] if inserted_req else None
            except asyncpg.UniqueViolationError:
                row = await conn.fetchrow(
                    "SELECT id, status, response_json FROM analyze_requests WHERE user_id = $1 AND idempotency_key = $2",
                    user["id"],
                    idempotency_key,
                )
                if row:
                    if row["status"] == "completed" and row["response_json"] is not None:
                        return _load_replay_response(row["response_json"])
                    analyze_request_id = row["id"]

                raise FitAIError(
                    code="IDEMPOTENCY_CONFLICT",
                    message="Запрос с таким ключом уже обрабатывается или завершился ошибкой",
                    status_code=409
                )

        # 2. Reserve quota (short transaction, increment happens here)
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO usage_daily (user_id, date, photos_used)
                VALUES ($1, $2, 0)
                ON CONFLICT (user_id, date) DO NOTHING
                """,
                user["id"], today
            )
            
            row = await conn.fetchrow(
                "SELECT photos_used FROM usage_daily WHERE user_id = $1 AND date = $2 FOR UPDATE",
                user["id"], today
            )
            
            if row["photos_used"] >= daily_limit:
                await write_event_best_effort(
                    conn,
                    event_type="quota_exceeded",
                    user_id=str(user["id"]),
                    payload={
                        "limit": daily_limit,
                        "used": row["photos_used"],
                        "status": status,
                        "stage": "reserve",
                    },
                )
                raise FitAIError(
                    code="QUOTA_EXCEEDED",
                    message="Достигнут дневной лимит фото",
                    status_code=429,
                    details={"limit": daily_limit, "used": row["photos_used"], "status": status}
                )

            await conn.execute(
                "UPDATE usage_daily SET photos_used = photos_used + 1 WHERE user_id = $1 AND date = $2",
                user["id"],
                today,
            )
            usage_update = await conn.fetchrow(
                "SELECT photos_used FROM usage_daily WHERE user_id = $1 AND date = $2",
                user["id"],
                today,
            )
            if usage_update is None:
                raise FitAIError(
                    code="INTERNAL_ERROR",
                    message="Внутренняя ошибка сервера",
                    status_code=500,
                    details={"stage": "usage_reserve_increment"},
                )
            reserved_photos_used = int(usage_update["photos_used"])
            usage_incremented = True
            quota_reserved = True
            
        # Forced failure for testing compensation (RFC-006)
        if settings.meals_analyze_force_fail_after_reserve_enabled():
            raise FitAIError(
                code="INTERNAL_ERROR",
                message="Forced failure after quota reserve",
                status_code=500
            )

        # 3. Execute AI (outside transaction)
        await write_event_best_effort(
            conn,
            event_type="analyze_started",
            user_id=str(user["id"]),
            payload={
                "model": settings.OPENROUTER_MODEL,
                "contentType": content_type,
                "descriptionPresent": normalized_description is not None,
                "descriptionLength": len(normalized_description) if normalized_description is not None else 0,
            },
        )
        analyze_started_emitted = True

        started_at = time.monotonic()
        raw_output = await openrouter_client.analyze_image(
            image_bytes=image_bytes,
            content_type=content_type,
            schema_hint=AI_CONTRACT_SCHEMA,
            description=normalized_description,
        )
        latency_ms = int((time.monotonic() - started_at) * 1000)

        try:
            parsed_output = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise FitAIError(
                code="VALIDATION_FAILED",
                message="Некорректные данные",
                status_code=400,
                details={"schema": "ai-contract", "issue": f"invalid_json: {exc.msg}"},
            ) from exc

        if not isinstance(parsed_output, dict):
            raise FitAIError(
                code="VALIDATION_FAILED",
                message="Некорректные данные",
                status_code=400,
                details={"schema": "ai-contract", "issue": "root must be object"},
            )

        try:
            AI_CONTRACT_VALIDATOR.validate(parsed_output)
        except JsonSchemaValidationError as exc:
            field_path = ".".join(str(p) for p in exc.path) or "$"
            raise FitAIError(
                code="VALIDATION_FAILED",
                message="Некорректные данные",
                status_code=400,
                details={"schema": "ai-contract", "issue": f"{field_path}: {exc.message}"},
            ) from exc

        meal_request_id = analyze_request_id or uuid.uuid4()
        meal_id = uuid.uuid4()
        perturbation_seed = str(analyze_request_id) if analyze_request_id is not None else str(meal_id)
        response_data = apply_post_ai_error(parsed_output, seed=perturbation_seed)

        # 4. Finalize success atomically: meal row + daily stats (+ idempotency completion if keyed)
        async with conn.transaction():
            if reserved_photos_used is None:
                raise FitAIError(
                    code="INTERNAL_ERROR",
                    message="Внутренняя ошибка сервера",
                    status_code=500,
                    details={"stage": "usage_reserve_missing"},
                )

            image_path = f"analyze/{user['id']}/{today.isoformat()}/{meal_request_id}.bin"
            meal_row = await conn.fetchrow(
                """
                INSERT INTO meals (
                    id,
                    user_id,
                    created_at,
                    meal_time,
                    description,
                    image_path,
                    ai_provider,
                    ai_model,
                    ai_confidence,
                    result_json,
                    idempotency_key,
                    analyze_request_id
                )
                VALUES (
                    $1,
                    $2,
                    $3,
                    'unknown',
                    $4,
                    $5,
                    'openrouter',
                    $6,
                    $7,
                    $8::jsonb,
                    $9,
                    $10
                )
                ON CONFLICT (analyze_request_id) DO NOTHING
                RETURNING id, created_at
                """,
                meal_id,
                user["id"],
                analyze_started_at_utc,
                normalized_description,
                image_path,
                settings.OPENROUTER_MODEL,
                float(response_data.get("overall_confidence") or 0),
                json.dumps(response_data),
                idempotency_key,
                meal_request_id,
            )

            if meal_row is None:
                raise FitAIError(
                    code="INTERNAL_ERROR",
                    message="Внутренняя ошибка сервера",
                    status_code=500,
                    details={"stage": "meal_insert"},
                )

            meal_dict = dict(meal_row)

            await conn.execute(
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
                VALUES ($1, $2, $3, $4, $5, $6, 1, NOW())
                ON CONFLICT (user_id, date)
                DO UPDATE SET
                    calories_kcal = daily_stats.calories_kcal + EXCLUDED.calories_kcal,
                    protein_g = daily_stats.protein_g + EXCLUDED.protein_g,
                    fat_g = daily_stats.fat_g + EXCLUDED.fat_g,
                    carbs_g = daily_stats.carbs_g + EXCLUDED.carbs_g,
                    meals_count = daily_stats.meals_count + 1,
                    updated_at = NOW()
                """,
                user["id"],
                today,
                float(response_data.get("totals", {}).get("calories_kcal") or 0),
                float(response_data.get("totals", {}).get("protein_g") or 0),
                float(response_data.get("totals", {}).get("fat_g") or 0),
                float(response_data.get("totals", {}).get("carbs_g") or 0),
            )

            created_at_iso = meal_dict["created_at"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

            response_payload = {
                "meal": {
                    "id": str(meal_dict["id"]),
                    "createdAt": created_at_iso,
                    "mealTime": "unknown",
                    "imageUrl": image_path,
                    "ai": {
                        "provider": "openrouter",
                        "model": settings.OPENROUTER_MODEL,
                        "confidence": float(response_data.get("overall_confidence") or 0),
                    },
                    "result": response_data,
                },
                "usage": {
                    "date": today.isoformat(),
                    "dailyLimit": daily_limit,
                    "photosUsed": int(reserved_photos_used),
                    "remaining": max(0, daily_limit - int(reserved_photos_used)),
                    "subscriptionStatus": status,
                },
            }

            if idempotency_key and analyze_request_id is not None:
                updated_req = await conn.fetchrow(
                    """
                    UPDATE analyze_requests
                    SET status = 'completed', response_json = $1::jsonb, updated_at = NOW()
                    WHERE id = $2 AND status = 'processing'
                    RETURNING id
                    """,
                    json.dumps(response_payload),
                    analyze_request_id,
                )

                if updated_req is None:
                    raise FitAIError(
                        code="INTERNAL_ERROR",
                        message="Внутренняя ошибка сервера",
                        status_code=500,
                        details={"stage": "request_complete"},
                    )

        logger.info(
            "MEAL_ANALYZE_OK context=%s",
            log_ctx_json(
                log_ctx(
                    request,
                    user_id=user["id"],
                    idempotency_key=idempotency_key,
                        extra={
                            "status_code": 200,
                            "duration_ms": duration_ms(request_started_at),
                            "model": settings.OPENROUTER_MODEL,
                            "latency_ms": latency_ms,
                            "description_present": normalized_description is not None,
                            "description_length": len(normalized_description) if normalized_description is not None else 0,
                        },
                    )
                ),
        )

        await write_event_best_effort(
            conn,
            event_type="analyze_completed",
            user_id=str(user["id"]),
            payload={
                "model": settings.OPENROUTER_MODEL,
                "latencyMs": latency_ms,
                "descriptionPresent": normalized_description is not None,
                "descriptionLength": len(normalized_description) if normalized_description is not None else 0,
            },
        )
        
        return response_payload

    except Exception as e:
        # Compensation
        if usage_incremented:
            try:
                await conn.execute(
                    "UPDATE usage_daily SET photos_used = GREATEST(0, photos_used - 1) WHERE user_id = $1 AND date = $2",
                    user["id"], today
                )
            except Exception as quota_err:
                logger.error(f"Failed to rollback quota: {quota_err}")

        if analyze_request_id is not None:
            try:
                await conn.execute(
                    "UPDATE analyze_requests SET status = 'failed', updated_at = NOW() WHERE id = $1 AND status = 'processing'",
                    analyze_request_id,
                )
            except Exception as req_err:
                logger.error(f"Failed to mark request as failed: {req_err}")
        
        if isinstance(e, FitAIError):
            if analyze_started_emitted:
                await write_event_best_effort(
                    conn,
                    event_type="analyze_failed",
                    user_id=str(user["id"]),
                    payload={"code": e.code, "model": settings.OPENROUTER_MODEL},
                )
            logger.warning(
                "MEAL_ANALYZE_FAIL context=%s",
                log_ctx_json(
                    log_ctx(
                        request,
                        user_id=user["id"],
                        idempotency_key=idempotency_key,
                        extra={
                            "status_code": e.status_code,
                            "duration_ms": duration_ms(request_started_at),
                            "code": e.code,
                            "model": settings.OPENROUTER_MODEL,
                        },
                    )
                ),
            )
            raise e

        if analyze_started_emitted:
            await write_event_best_effort(
                conn,
                event_type="analyze_failed",
                user_id=str(user["id"]),
                payload={"code": "INTERNAL_ERROR", "model": settings.OPENROUTER_MODEL},
            )
        
        logger.error(
            "MEAL_ANALYZE_FAIL context=%s",
            log_ctx_json(
                log_ctx(
                    request,
                    user_id=user["id"],
                    idempotency_key=idempotency_key,
                    extra={
                        "status_code": 500,
                        "duration_ms": duration_ms(request_started_at),
                        "code": "INTERNAL_ERROR",
                        "model": settings.OPENROUTER_MODEL,
                    },
                )
            ),
        )
        logger.error("Error in analyze_meal", exc_info=True)
        raise FitAIError(code="INTERNAL_ERROR", message="Внутренняя ошибка сервера", status_code=500)


@app.get("/health", tags=["Health"])
@v1_router.get("/health", tags=["Health"])
async def health_check():
    db_status = await db.db_check()
    return {
        "status": "ok",
        "service": "fitai-api",
        "version": "0.1.0",
        "db": db_status
    }

app.include_router(v1_router)
app.include_router(meals_router)
app.include_router(stats_router)
app.include_router(payments_router)
app.include_router(admin_router)
app.include_router(paywall_router)
app.include_router(events_router)
app.include_router(referral_router)
app.include_router(streak_router)
app.include_router(share_router)
app.include_router(notifications_router)
app.include_router(premium_router)
