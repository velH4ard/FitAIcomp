import logging
import sys
from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter, Depends, Body, UploadFile, File, Header
from .errors import setup_error_handlers, FitAIError
from .db import db, get_db
import asyncpg
from .schemas import (
    AuthRequest, 
    AuthResponse, 
    UserResponse, 
    UserProfile, 
    ProfileUpdateResponse, 
    SubscriptionInfo,
    UsageResponse
)
from .auth import verify_telegram_init_data, create_access_token
from .deps import get_current_user
from .config import settings
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

# Setup custom error handlers
setup_error_handlers(app)

# API Router
v1_router = APIRouter(prefix="/v1")

import json

def get_daily_limit(status: str) -> int:
    if status == "active":
        return 20
    if status == "blocked":
        return 0
    return 2 # free, expired

def format_user_response(user_dict: dict, used_today: int = 0) -> UserResponse:
    # Calculate subscription info
    status = user_dict["subscription_status"]
    daily_limit = get_daily_limit(status)
    
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
        RETURNING id, telegram_id, username, is_onboarded, subscription_status, subscription_active_until, profile
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
    # Store profile as JSON string for asyncpg to handle correctly as JSONB
    profile_json = json.dumps(profile.model_dump())
    
    row = await conn.fetchrow(
        """
        UPDATE users 
        SET profile = $1, is_onboarded = TRUE, updated_at = NOW()
        WHERE id = $2
        RETURNING id, is_onboarded, profile
        """,
        profile_json,
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
    status = user["subscription_status"]
    daily_limit = get_daily_limit(status)
    
    return UsageResponse(
        date=today.isoformat(),
        dailyLimit=daily_limit,
        photosUsed=photos_used,
        remaining=max(0, daily_limit - photos_used),
        subscriptionStatus=status
    )

@v1_router.post("/meals/analyze", tags=["Meals"])
async def analyze_meal(
    file: Optional[UploadFile] = File(None),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user = Depends(get_current_user),
    conn = Depends(get_db)
):
    if not idempotency_key:
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"fieldErrors": [{"field": "header.Idempotency-Key", "issue": "Field required"}]},
        )

    if file is None:
        raise FitAIError(
            code="VALIDATION_FAILED",
            message="Некорректные данные",
            status_code=400,
            details={"fieldErrors": [{"field": "body.file", "issue": "Field required"}]},
        )

    if not user["is_onboarded"]:
        raise FitAIError(
            code="ONBOARDING_REQUIRED",
            message="Заполните анкету перед использованием",
            status_code=403
        )

    # Idempotency begin
    try:
        await conn.execute(
            """
            INSERT INTO analyze_requests (user_id, idempotency_key, status)
            VALUES ($1, $2, 'processing')
            """,
            user["id"], idempotency_key
        )
    except asyncpg.UniqueViolationError:
        row = await conn.fetchrow(
            "SELECT status, response_json FROM analyze_requests WHERE user_id = $1 AND idempotency_key = $2",
            user["id"], idempotency_key
        )
        if row and row["status"] == "completed" and row["response_json"] is not None:
            # asyncpg returns JSONB as dict/list
            return row["response_json"]
        raise FitAIError(
            code="IDEMPOTENCY_CONFLICT",
            message="Запрос с таким ключом уже обрабатывается или завершился ошибкой",
            status_code=409
        )

    today = datetime.now(timezone.utc).date()
    status = user["subscription_status"]
    daily_limit = get_daily_limit(status)
    
    quota_reserved = False
    try:
        # Quota reserve in transaction
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
                await conn.execute(
                    "UPDATE analyze_requests SET status = 'failed', updated_at = NOW() WHERE user_id = $1 AND idempotency_key = $2",
                    user["id"], idempotency_key
                )
                raise FitAIError(
                    code="QUOTA_EXCEEDED",
                    message="Достигнут дневной лимит фото",
                    status_code=403,
                    details={"limit": daily_limit, "used": row["photos_used"], "status": status}
                )
            
            await conn.execute(
                "UPDATE usage_daily SET photos_used = photos_used + 1 WHERE user_id = $1 AND date = $2",
                user["id"], today
            )
            quota_reserved = True
            
        response_data = {
            "mealName": "Test Meal",
            "calories": 500,
            "protein": 30,
            "fat": 20,
            "carbs": 50,
        }
        
        await conn.execute(
            """
            UPDATE analyze_requests 
            SET status = 'completed', response_json = $1, updated_at = NOW() 
            WHERE user_id = $2 AND idempotency_key = $3
            """,
            response_data, user["id"], idempotency_key
        )
        
        return response_data

    except Exception as e:
        if quota_reserved:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE usage_daily SET photos_used = GREATEST(0, photos_used - 1) WHERE user_id = $1 AND date = $2",
                    user["id"], today
                )
                await conn.execute(
                    "UPDATE analyze_requests SET status = 'failed', updated_at = NOW() WHERE user_id = $1 AND idempotency_key = $2",
                    user["id"], idempotency_key
                )
        else:
            # If failed before quota reserve, still mark request as failed
            await conn.execute(
                "UPDATE analyze_requests SET status = 'failed', updated_at = NOW() WHERE user_id = $1 AND idempotency_key = $2",
                user["id"], idempotency_key
            )
        
        if isinstance(e, FitAIError):
            raise e
        
        logger.error(f"Error in analyze_meal: {e}")
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
