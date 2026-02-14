import logging
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter, Depends, Body
from .errors import setup_error_handlers, FitAIError
from .db import db, get_db
from .schemas import AuthRequest, AuthResponse, UserResponse, UserProfile, ProfileUpdateResponse, SubscriptionInfo
from .auth import verify_telegram_init_data, create_access_token
from .deps import get_current_user
from .config import settings
from datetime import datetime

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

def format_user_response(user_dict: dict) -> UserResponse:
    # Calculate subscription info
    subscription = SubscriptionInfo(
        status=user_dict["subscription_status"],
        activeUntil=user_dict["subscription_active_until"],
        priceRubPerMonth=settings.SUBSCRIPTION_PRICE_RUB,
        dailyLimit=20 if user_dict["subscription_status"] == "active" else 2,
        usedToday=0 # Placeholder
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
async def get_me(user = Depends(get_current_user)):
    return format_user_response(user)

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
