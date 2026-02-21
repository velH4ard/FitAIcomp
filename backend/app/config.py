from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(raw_value: str) -> list[str]:
    return [part.strip() for part in raw_value.split(",") if part.strip()]


def _normalize_env(raw_value: str) -> str:
    normalized = raw_value.strip().lower()
    return "production" if normalized == "production" else "development"


def _is_permissive_origin(value: str) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return False
    if normalized == "*":
        return True
    return "://*" in normalized


def _is_permissive_origin_regex(value: str) -> bool:
    normalized = value.strip().replace(" ", "")
    if not normalized:
        return False
    permissive_patterns = {".*", "^.*$", "^(.*)$", "^https?://.*$", "^https://.*$"}
    return normalized in permissive_patterns


class Settings(BaseSettings):
    APP_ENV: str = "development"
    FITAI_ENV: str = ""
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "info"

    # CORS
    CORS_ALLOW_ORIGINS: str = ""
    CORS_ALLOW_ORIGIN_REGEX: str = ""

    # Auth
    BOT_TOKEN: str
    TELEGRAM_BOT_TOKEN: str = ""
    JWT_SECRET: str
    AUTH_INITDATA_MAX_AGE_SEC: int = 86400
    TELEGRAM_INITDATA_MAX_AGE_SECONDS: Optional[int] = None
    JWT_EXPIRES_SEC: int = 604800  # 7 days

    # AI (OpenRouter)
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    OPENROUTER_MODEL: str = "google/gemini-3.0-flash-preview"
    OPENROUTER_CONNECT_TIMEOUT_SEC: float = 5.0
    OPENROUTER_READ_TIMEOUT_SEC: float = 40.0
    OPENROUTER_MAX_RETRIES: int = 1

    # Supabase
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    SUPABASE_DATABASE_URL: str = ""
    SUPABASE_STORAGE_BUCKET: str = "meals"
    DB_STATEMENT_TIMEOUT_MS: int = 5000
    DB_SLOW_QUERY_MS: int = 300

    # YooKassa
    YOOKASSA_SHOP_ID: str = ""
    YOOKASSA_SECRET_KEY: str = ""
    YOOKASSA_WEBHOOK_USERNAME: str = ""
    YOOKASSA_WEBHOOK_PASSWORD: str = ""
    YOOKASSA_RETURN_URL_DEFAULT: str = ""
    YOOKASSA_API_BASE_URL: str = "https://api.yookassa.ru/v3"
    YOOKASSA_CONNECT_TIMEOUT_SEC: float = 5.0
    YOOKASSA_READ_TIMEOUT_SEC: float = 20.0
    YOOKASSA_MAX_RETRIES: int = 1
    PAYMENTS_WEBHOOK_DEV_BYPASS: int = 0
    PAYMENTS_WEBHOOK_IP_ALLOWLIST: str = ""

    # Business
    SUBSCRIPTION_PRICE_RUB: int = 499
    SUBSCRIPTION_DURATION_DAYS: int = 30
    MEALS_ANALYZE_MAX_IMAGE_BYTES: int = 10485760
    MEALS_ANALYZE_RATE_LIMIT_PER_MINUTE: int = 10

    # Dev/Test switches
    MEALS_ANALYZE_FORCE_FAIL_AFTER_RESERVE: int = 0

    # Internal admin access
    ADMIN_USER_IDS: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def env_mode(self) -> str:
        if self.FITAI_ENV.strip():
            return _normalize_env(self.FITAI_ENV)
        return _normalize_env(self.APP_ENV)

    def is_production(self) -> bool:
        return self.env_mode() == "production"

    def get_cors_allow_origins(self) -> list[str]:
        configured = _split_csv(self.CORS_ALLOW_ORIGINS)
        if configured:
            if self.is_production() and any(_is_permissive_origin(origin) for origin in configured):
                raise ValueError("Permissive CORS origin is not allowed in production")
            return configured

        if self.is_production():
            return []

        return [
            "http://localhost",
            "http://localhost:3000",
            "http://localhost:5173",
            "http://localhost:5174",
            "http://127.0.0.1",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:5174",
        ]

    def get_cors_allow_origin_regex(self) -> Optional[str]:
        configured = self.CORS_ALLOW_ORIGIN_REGEX.strip()
        if configured:
            if self.is_production() and _is_permissive_origin_regex(configured):
                raise ValueError("Permissive CORS origin regex is not allowed in production")
            return configured

        if self.is_production():
            return None

        return r"^https://.*\.trycloudflare\.com$"

    def get_telegram_initdata_max_age_sec(self) -> int:
        if self.TELEGRAM_INITDATA_MAX_AGE_SECONDS is not None:
            return max(1, int(self.TELEGRAM_INITDATA_MAX_AGE_SECONDS))
        return max(1, int(self.AUTH_INITDATA_MAX_AGE_SEC))

    def payments_webhook_dev_bypass_enabled(self) -> bool:
        return bool(self.PAYMENTS_WEBHOOK_DEV_BYPASS == 1 and not self.is_production())

    def meals_analyze_force_fail_after_reserve_enabled(self) -> bool:
        return bool(self.MEALS_ANALYZE_FORCE_FAIL_AFTER_RESERVE == 1 and not self.is_production())


settings = Settings()
