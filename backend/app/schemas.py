from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime

class UserProfile(BaseModel):
    gender: str = Field(..., pattern="^(male|female|other)$")
    age: int = Field(..., ge=10, le=120)
    heightCm: int = Field(..., ge=80, le=250)
    weightKg: float = Field(..., ge=20, le=400)
    goal: str = Field(..., pattern="^(lose_weight|maintain|gain_weight)$")

class SubscriptionInfo(BaseModel):
    status: str
    activeUntil: Optional[datetime]
    priceRubPerMonth: int
    dailyLimit: int
    usedToday: int

class UserResponse(BaseModel):
    id: UUID
    telegramId: int
    username: Optional[str] = None
    isOnboarded: bool
    profile: Optional[UserProfile] = None
    subscription: Optional[SubscriptionInfo] = None

class AuthRequest(BaseModel):
    initData: str

class AuthResponse(BaseModel):
    accessToken: str
    user: UserResponse

class ProfileUpdateResponse(BaseModel):
    id: UUID
    isOnboarded: bool
    profile: UserProfile
