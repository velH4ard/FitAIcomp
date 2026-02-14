from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "info"

    # Auth
    BOT_TOKEN: str
    JWT_SECRET: str
    AUTH_INITDATA_MAX_AGE_SEC: int = 86400
    JWT_EXPIRES_SEC: int = 604800  # 7 days
    
    # AI (OpenRouter)
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    OPENROUTER_MODEL: str = "google/gemini-3.0-flash-preview"

    # Supabase
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    SUPABASE_DATABASE_URL: str = ""
    SUPABASE_STORAGE_BUCKET: str = "meals"
    
    # YooKassa
    YOOKASSA_SHOP_ID: str = ""
    YOOKASSA_SECRET_KEY: str = ""
    
    # Business
    SUBSCRIPTION_PRICE_RUB: int = 500
    SUBSCRIPTION_DURATION_DAYS: int = 30

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
