from pydantic import BaseModel, Field, StrictBool
from typing import Optional, List, Dict, Any, Literal
from uuid import UUID
from datetime import datetime

class UserProfile(BaseModel):
    gender: str = Field(..., pattern="^(male|female)$")
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


class ProfileGoalUpdateRequest(BaseModel):
    dailyGoal: int = Field(..., ge=1000, le=5000)


class ProfileGoalUpdateResponse(BaseModel):
    dailyGoal: int = Field(..., ge=1)
    autoGoal: int = Field(..., ge=1)
    override: Optional[int] = Field(default=None, ge=1000, le=5000)

class UsageResponse(BaseModel):
    date: str
    dailyLimit: int
    photosUsed: int
    remaining: int
    subscriptionStatus: str
    upgradeHint: Optional[str] = None


class DailyNutritionSummaryResponse(BaseModel):
    date: str
    calories_kcal: float = Field(..., ge=0)
    protein_g: float = Field(..., ge=0)
    fat_g: float = Field(..., ge=0)
    carbs_g: float = Field(..., ge=0)
    mealsCount: int = Field(..., ge=0)

# AI Contract Schemas
class FoodItem(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    grams: float = Field(..., ge=0)
    calories_kcal: float = Field(..., ge=0)
    protein_g: float = Field(..., ge=0)
    fat_g: float = Field(..., ge=0)
    carbs_g: float = Field(..., ge=0)
    confidence: float = Field(..., ge=0, le=1)

class FoodTotals(BaseModel):
    calories_kcal: float = Field(..., ge=0)
    protein_g: float = Field(..., ge=0)
    fat_g: float = Field(..., ge=0)
    carbs_g: float = Field(..., ge=0)

class FoodAnalysis(BaseModel):
    recognized: bool
    overall_confidence: float = Field(..., ge=0, le=1)
    totals: FoodTotals
    items: List[FoodItem]
    warnings: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)


class MealAIInfo(BaseModel):
    provider: str
    model: str
    confidence: float = Field(..., ge=0, le=1)


class MealListItem(BaseModel):
    id: UUID
    createdAt: datetime
    mealTime: str
    imageUrl: str
    totals: FoodTotals


class MealListResponse(BaseModel):
    items: List[MealListItem]
    nextCursor: Optional[str] = None


class MealDetailResponse(BaseModel):
    id: UUID
    createdAt: datetime
    mealTime: str
    imageUrl: str
    ai: MealAIInfo
    result: FoodAnalysis


class DailyStatsAfterDelete(BaseModel):
    date: str
    calories_kcal: float = Field(..., ge=0)
    protein_g: float = Field(..., ge=0)
    fat_g: float = Field(..., ge=0)
    carbs_g: float = Field(..., ge=0)
    mealsCount: int = Field(..., ge=0)


class DeleteMealResponse(BaseModel):
    deleted: bool
    mealId: UUID
    dailyStats: DailyStatsAfterDelete


class SubscriptionResponse(BaseModel):
    priceRubPerMonth: int
    status: str
    activeUntil: Optional[datetime]
    dailyLimit: int
    usedToday: int
    remainingToday: int


class SubscriptionStatusResponse(BaseModel):
    status: str
    activeUntil: Optional[datetime] = None
    daysLeft: int = Field(..., ge=0)
    willExpireSoon: bool


class PaywallContextResponse(BaseModel):
    reason: Literal["none", "soft_hint", "quota_reached", "expiring_soon", "referral_bonus_available"]
    subscriptionStatus: str
    daysLeft: int = Field(..., ge=0)
    dailyLimit: int
    remaining: int = Field(..., ge=0)
    recommendedPlan: str
    priceRub: int = Field(..., ge=0)
    priceOriginalRub: int = Field(..., ge=0)
    priceCurrentRub: int = Field(..., ge=0)


class YookassaCreatePaymentRequest(BaseModel):
    returnUrl: Optional[str] = None
    idempotencyKey: Optional[str] = Field(default=None, min_length=1, max_length=128)


class YookassaCreatePaymentResponse(BaseModel):
    paymentId: str
    confirmationUrl: str


class YookassaRefreshPaymentRequest(BaseModel):
    paymentId: str = Field(..., min_length=1)


class YookassaRefreshPaymentResponse(BaseModel):
    ok: bool
    activated: bool
    duplicate: bool
    paymentId: str
    paymentStatus: Optional[str] = None


class AdminStatsResponse(BaseModel):
    activeSubscriptions: int = Field(..., ge=0)
    mrrRubEstimate: int = Field(..., ge=0)
    todayAnalyzes: int = Field(..., ge=0)
    todayRateLimited: int = Field(..., ge=0)
    todayAiFailures: int = Field(..., ge=0)
    todayPaymentsCreated: int = Field(..., ge=0)
    todayPaymentsSucceeded: int = Field(..., ge=0)
    todaySubscriptionsActivated: int = Field(..., ge=0)


class EventListItem(BaseModel):
    id: UUID
    eventType: str
    details: Optional[Dict[str, Any]] = None
    createdAt: datetime


class EventListResponse(BaseModel):
    items: List[EventListItem]
    nextCursor: Optional[str] = None


class AdminEventListItem(BaseModel):
    id: UUID
    userId: Optional[UUID] = None
    eventType: str
    details: Optional[Dict[str, Any]] = None
    createdAt: datetime


class AdminEventListResponse(BaseModel):
    items: List[AdminEventListItem]
    nextCursor: Optional[str] = None


class AdminReferralTotalsAllTime(BaseModel):
    codesIssued: int = Field(..., ge=0)
    redeems: int = Field(..., ge=0)
    creditsGranted: int = Field(..., ge=0)


class AdminReferralStatsResponse(BaseModel):
    todayCodesIssued: int = Field(..., ge=0)
    todayRedeems: int = Field(..., ge=0)
    todayUniqueRedeemers: int = Field(..., ge=0)
    todayCreditsGranted: int = Field(..., ge=0)
    totalsAllTime: Optional[AdminReferralTotalsAllTime] = None


class AdminReferralRedemptionItem(BaseModel):
    id: UUID
    createdAt: datetime
    redeemerUserId: UUID
    referrerUserId: UUID
    code: str
    creditsGranted: int = Field(..., ge=0)


class AdminReferralRedemptionsResponse(BaseModel):
    items: List[AdminReferralRedemptionItem]
    nextCursor: Optional[str] = None


class WeeklyStatsDay(BaseModel):
    date: str
    calories_kcal: float = Field(..., ge=0)
    protein_g: float = Field(..., ge=0)
    fat_g: float = Field(..., ge=0)
    carbs_g: float = Field(..., ge=0)
    mealsCount: int = Field(..., ge=0)


class WeeklyStatsTotals(BaseModel):
    calories_kcal: float = Field(..., ge=0)
    protein_g: float = Field(..., ge=0)
    fat_g: float = Field(..., ge=0)
    carbs_g: float = Field(..., ge=0)
    mealsCount: int = Field(..., ge=0)


class WeeklyStatsResponse(BaseModel):
    startDate: str
    endDate: str
    days: List[WeeklyStatsDay]
    totals: WeeklyStatsTotals


class ReferralCodeResponse(BaseModel):
    code: str = Field(..., pattern=r"^[A-Z0-9]{6,16}$")


class ReferralRedeemRequest(BaseModel):
    code: str = Field(..., pattern=r"^[A-Z0-9]{6,16}$")


class ReferralRedeemResponse(BaseModel):
    redeemed: bool


class NotificationSettingsRequest(BaseModel):
    enabled: StrictBool
    tone: Optional[Literal["soft", "hard", "balanced"]] = None


class NotificationSettingsResponse(BaseModel):
    enabled: bool
    tone: Literal["soft", "hard", "balanced"]


class StreakResponse(BaseModel):
    currentStreak: int = Field(..., ge=0)
    bestStreak: int = Field(..., ge=0)
    lastCompletedDate: Optional[str] = None  # YYYY-MM-DD or null


class ShareDataResponse(BaseModel):
    """Response for share screen with combined data."""
    streak: int = Field(..., ge=0)
    bestStreak: int = Field(..., ge=0)
    todayCalories: float = Field(..., ge=0)
    dailyGoal: int = Field(..., ge=1200)
    date: str  # YYYY-MM-DD


class WeeklyReportDay(BaseModel):
    date: str
    calories_kcal: float
    goalCalories_kcal: float
    deltaCalories_kcal: float
    balance: Literal["deficit", "surplus", "balanced"]


class WeeklyReportTotals(BaseModel):
    calories_kcal: float
    goalCalories_kcal: float
    deltaCalories_kcal: float
    deficitDays: int = Field(..., ge=0)
    surplusDays: int = Field(..., ge=0)
    balancedDays: int = Field(..., ge=0)


class WeeklyWeightForecast(BaseModel):
    method: Literal["7700kcal_per_kg"]
    periodDeltaKg: float
    projectedWeightKg: float
    confidence: Literal["low", "medium"]


class WeeklyReportResponse(BaseModel):
    startDate: str
    endDate: str
    days: List[WeeklyReportDay]
    totals: WeeklyReportTotals
    weightForecast: WeeklyWeightForecast


class MonthlyReportAggregates(BaseModel):
    calories_kcal: float
    goalCalories_kcal: float
    deltaCalories_kcal: float
    avgCaloriesPerDay: float
    trackedDays: int = Field(..., ge=0)
    deficitDays: int = Field(..., ge=0)
    surplusDays: int = Field(..., ge=0)
    balancedDays: int = Field(..., ge=0)


class MonthlyReportWeight(BaseModel):
    startWeightKg: Optional[float] = None
    endWeightKg: Optional[float] = None
    changeKg: Optional[float] = None


class MonthlyReportResponse(BaseModel):
    month: str
    startDate: str
    endDate: str
    aggregates: MonthlyReportAggregates
    weight: MonthlyReportWeight


class WhyNotLosingInsight(BaseModel):
    rule: str
    text: str
    recommendation: str


class WhyNotLosingResponse(BaseModel):
    analysisType: Literal["rule_based_v1"]
    windowDays: int = Field(..., ge=7, le=30)
    summary: str
    insights: List[WhyNotLosingInsight]


class WeightChartItem(BaseModel):
    date: str
    weight: float = Field(..., ge=20, le=400)


class WeightChartResponse(BaseModel):
    items: List[WeightChartItem]
