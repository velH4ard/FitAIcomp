import logging
import time
import asyncpg
from typing import Optional
from .config import settings
from .observability import current_request_context

logger = logging.getLogger("fitai-db")


def _statement_timeout_ms() -> int:
    return max(0, int(settings.DB_STATEMENT_TIMEOUT_MS))


def _slow_query_threshold_ms() -> int:
    return max(0, int(settings.DB_SLOW_QUERY_MS))


def _log_slow_query(query_name: str, started_at: float) -> None:
    threshold_ms = _slow_query_threshold_ms()
    if threshold_ms <= 0:
        return

    duration = int((time.monotonic() - started_at) * 1000)
    if duration < threshold_ms:
        return

    context = current_request_context()
    payload = {
        "request_id": context.get("request_id", ""),
        "path": context.get("path", ""),
        "query_name": query_name,
        "duration_ms": duration,
        "threshold_ms": threshold_ms,
    }
    logger.warning("DB_SLOW_QUERY context=%s", payload)


async def fetch_named(conn: asyncpg.Connection, query_name: str, query: str, *args):
    started_at = time.monotonic()
    try:
        return await conn.fetch(query, *args)
    finally:
        _log_slow_query(query_name=query_name, started_at=started_at)


async def fetchrow_named(conn: asyncpg.Connection, query_name: str, query: str, *args):
    started_at = time.monotonic()
    try:
        return await conn.fetchrow(query, *args)
    finally:
        _log_slow_query(query_name=query_name, started_at=started_at)


async def fetchval_named(conn: asyncpg.Connection, query_name: str, query: str, *args):
    started_at = time.monotonic()
    try:
        return await conn.fetchval(query, *args)
    finally:
        _log_slow_query(query_name=query_name, started_at=started_at)


async def execute_named(conn: asyncpg.Connection, query_name: str, query: str, *args):
    started_at = time.monotonic()
    try:
        return await conn.execute(query, *args)
    finally:
        _log_slow_query(query_name=query_name, started_at=started_at)

class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    async def create_pool(self):
        if not settings.SUPABASE_DATABASE_URL:
            logger.warning("SUPABASE_DATABASE_URL is not set, database pool will not be created.")
            return

        try:
            # Best practices for asyncpg pool
            self.pool = await asyncpg.create_pool(
                dsn=settings.SUPABASE_DATABASE_URL,
                min_size=2,
                max_size=10,
                max_queries=50000,
                max_inactive_connection_lifetime=300.0,
                command_timeout=60.0,
                statement_cache_size=0,
                server_settings={"statement_timeout": f"{_statement_timeout_ms()}ms"},
            )
            logger.info("Database pool created.")
            
            # Initialize tables
            await self.init_db()
        except Exception as e:
            logger.error(f"Failed to create database pool: {e}")
            self.pool = None

    async def init_db(self):
        if not self.pool:
            return
            
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    is_onboarded BOOLEAN DEFAULT FALSE,
                    subscription_status TEXT DEFAULT 'free',
                    subscription_active_until TIMESTAMPTZ,
                    referral_credits INT NOT NULL DEFAULT 0,
                    daily_goal_auto INT NOT NULL DEFAULT 2000,
                    daily_goal_override INT,
                    profile JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );

                ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS referral_credits INT NOT NULL DEFAULT 0;

                ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS daily_goal_auto INT NOT NULL DEFAULT 2000;

                ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS daily_goal_override INT;

                UPDATE users
                SET daily_goal_auto = 2000
                WHERE daily_goal_auto IS NULL OR daily_goal_auto <= 0;

                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'users_daily_goal_override_range'
                    ) THEN
                        ALTER TABLE users
                            ADD CONSTRAINT users_daily_goal_override_range
                            CHECK (
                                daily_goal_override IS NULL
                                OR (daily_goal_override >= 1000 AND daily_goal_override <= 5000)
                            );
                    END IF;
                END $$;

                UPDATE users
                SET referral_credits = 0
                WHERE referral_credits < 0;

                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'users_referral_credits_non_negative'
                    ) THEN
                        ALTER TABLE users
                            ADD CONSTRAINT users_referral_credits_non_negative
                            CHECK (referral_credits >= 0);
                    END IF;
                END $$;

                CREATE TABLE IF NOT EXISTS usage_daily (
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    date DATE NOT NULL,
                    photos_used INT NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, date)
                );

                CREATE TABLE IF NOT EXISTS meals (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    meal_time TEXT NOT NULL DEFAULT 'unknown',
                    description TEXT,
                    image_path TEXT,
                    image_url TEXT,
                    ai_provider TEXT,
                    ai_model TEXT,
                    ai_confidence DOUBLE PRECISION,
                    result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    idempotency_key TEXT
                );

                ALTER TABLE meals
                    ADD COLUMN IF NOT EXISTS description TEXT;

                CREATE TABLE IF NOT EXISTS daily_stats (
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    date DATE NOT NULL,
                    calories_kcal DOUBLE PRECISION NOT NULL DEFAULT 0,
                    protein_g DOUBLE PRECISION NOT NULL DEFAULT 0,
                    fat_g DOUBLE PRECISION NOT NULL DEFAULT 0,
                    carbs_g DOUBLE PRECISION NOT NULL DEFAULT 0,
                    meals_count INT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, date)
                );

                CREATE TABLE IF NOT EXISTS weight_logs (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    date DATE NOT NULL,
                    weight_kg DOUBLE PRECISION NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'weight_logs_weight_range'
                    ) THEN
                        ALTER TABLE weight_logs
                            ADD CONSTRAINT weight_logs_weight_range
                            CHECK (weight_kg >= 20 AND weight_kg <= 400);
                    END IF;
                END $$;

                CREATE INDEX IF NOT EXISTS idx_weight_logs_user_date
                    ON weight_logs (user_id, date ASC);

                CREATE TABLE IF NOT EXISTS analyze_requests (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES users(id),
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'failed')),
                    response_json JSONB NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(user_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                DELETE FROM events WHERE user_id IS NULL;

                ALTER TABLE events
                    ALTER COLUMN user_id SET NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_events_user_created
                    ON events (user_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_events_user_type_created
                    ON events (user_id, event_type, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_events_type_created
                    ON events (event_type, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_events_user_created_id
                    ON events (user_id, created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_events_user_type_created_id
                    ON events (user_id, event_type, created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_events_created_id
                    ON events (created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_events_type_created_id
                    ON events (event_type, created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_users_subscription_active_until
                    ON users (subscription_status, subscription_active_until);

                CREATE INDEX IF NOT EXISTS idx_usage_daily_date
                    ON usage_daily (date);

                CREATE TABLE IF NOT EXISTS payment_webhook_events (
                    dedupe_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (status IN ('processing', 'completed')),
                    event_type TEXT NOT NULL,
                    payment_id TEXT,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS yookassa_payments (
                    payment_id TEXT PRIMARY KEY,
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    idempotence_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('created', 'succeeded', 'canceled', 'refunded')) DEFAULT 'created',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_yookassa_payments_user_created
                    ON yookassa_payments (user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS user_daily_flags (
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    flag TEXT NOT NULL,
                    date DATE NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, flag, date)
                );

                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    notifications_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    notification_tone TEXT NOT NULL DEFAULT 'balanced',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                ALTER TABLE user_settings
                    ADD COLUMN IF NOT EXISTS notification_tone TEXT NOT NULL DEFAULT 'balanced';

                UPDATE user_settings
                SET notification_tone = 'balanced'
                WHERE notification_tone IS NULL
                   OR notification_tone NOT IN ('soft', 'hard', 'balanced');

                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'user_settings_notification_tone_allowed'
                    ) THEN
                        ALTER TABLE user_settings
                            ADD CONSTRAINT user_settings_notification_tone_allowed
                            CHECK (notification_tone IN ('soft', 'hard', 'balanced'));
                    END IF;
                END $$;

                CREATE TABLE IF NOT EXISTS reminder_deliveries (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    date DATE NOT NULL,
                    reminder_type TEXT NOT NULL DEFAULT 'daily_progress',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(user_id, date, reminder_type)
                );

                ALTER TABLE reminder_deliveries
                    ALTER COLUMN reminder_type SET DEFAULT 'daily_progress';

                CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_date
                    ON reminder_deliveries (date DESC);

                CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_user_date
                    ON reminder_deliveries (user_id, date DESC);

                CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_user_type_date
                    ON reminder_deliveries (user_id, reminder_type, date DESC);

                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_indexes
                        WHERE schemaname = 'public'
                          AND tablename = 'daily_stats'
                          AND indexdef ILIKE '%(user_id, date)%'
                    ) THEN
                        CREATE INDEX idx_daily_stats_user_date ON daily_stats (user_id, date);
                    END IF;
                END $$;

                CREATE TABLE IF NOT EXISTS referral_codes (
                    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    code TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS referral_redemptions (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    redeemer_user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                    referrer_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    credits_granted INT NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                ALTER TABLE referral_redemptions
                    ADD COLUMN IF NOT EXISTS id UUID;

                UPDATE referral_redemptions
                SET id = gen_random_uuid()
                WHERE id IS NULL;

                ALTER TABLE referral_redemptions
                    ALTER COLUMN id SET DEFAULT gen_random_uuid();

                ALTER TABLE referral_redemptions
                    ALTER COLUMN id SET NOT NULL;

                ALTER TABLE referral_redemptions
                    ADD COLUMN IF NOT EXISTS credits_granted INT;

                UPDATE referral_redemptions
                SET credits_granted = 1
                WHERE credits_granted IS NULL;

                UPDATE referral_redemptions
                SET credits_granted = 0
                WHERE credits_granted < 0;

                ALTER TABLE referral_redemptions
                    ALTER COLUMN credits_granted SET DEFAULT 1;

                ALTER TABLE referral_redemptions
                    ALTER COLUMN credits_granted SET NOT NULL;

                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'referral_redemptions_credits_granted_non_negative'
                    ) THEN
                        ALTER TABLE referral_redemptions
                            ADD CONSTRAINT referral_redemptions_credits_granted_non_negative
                            CHECK (credits_granted >= 0);
                    END IF;
                END $$;

                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM pg_constraint c
                        JOIN pg_attribute a
                          ON a.attrelid = c.conrelid
                         AND a.attnum = ANY(c.conkey)
                        WHERE c.conrelid = 'referral_redemptions'::regclass
                          AND c.contype = 'p'
                          AND a.attname = 'redeemer_user_id'
                    ) THEN
                        ALTER TABLE referral_redemptions
                            DROP CONSTRAINT referral_redemptions_pkey;
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint c
                        JOIN pg_attribute a
                          ON a.attrelid = c.conrelid
                         AND a.attnum = ANY(c.conkey)
                        WHERE c.conrelid = 'referral_redemptions'::regclass
                          AND c.contype = 'p'
                          AND a.attname = 'id'
                    ) THEN
                        ALTER TABLE referral_redemptions
                            ADD CONSTRAINT referral_redemptions_pkey PRIMARY KEY (id);
                    END IF;
                END $$;

                CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_redemptions_redeemer_unique
                    ON referral_redemptions (redeemer_user_id);

                CREATE INDEX IF NOT EXISTS idx_meals_user_created_id
                    ON meals (user_id, created_at DESC, id DESC);

                DROP INDEX IF EXISTS idx_meals_user_created;

                CREATE UNIQUE INDEX IF NOT EXISTS idx_meals_user_idempotency
                    ON meals (user_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;

                ALTER TABLE meals
                    ADD COLUMN IF NOT EXISTS analyze_request_id UUID;

                DROP INDEX IF EXISTS idx_meals_analyze_request_id;

                ALTER TABLE meals
                    ALTER COLUMN analyze_request_id SET NOT NULL;

                CREATE UNIQUE INDEX IF NOT EXISTS idx_meals_analyze_request_id
                    ON meals (analyze_request_id);

                CREATE INDEX IF NOT EXISTS idx_referral_redemptions_referrer_created
                    ON referral_redemptions (referrer_user_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_referral_redemptions_referrer_created_id
                    ON referral_redemptions (referrer_user_id, created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_referral_redemptions_redeemer_created_id
                    ON referral_redemptions (redeemer_user_id, created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_referral_redemptions_created
                    ON referral_redemptions (created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_referral_redemptions_created_id
                    ON referral_redemptions (created_at DESC, id DESC);

                DROP INDEX IF EXISTS idx_referral_redemptions_created_redeemer;

                CREATE INDEX IF NOT EXISTS idx_referral_codes_created
                    ON referral_codes (created_at DESC);
            """)
            logger.info("Database tables initialized.")

    async def close_pool(self):
        if self.pool:
            await self.pool.close()
            logger.info("Database pool closed.")

    async def db_check(self) -> str:
        if not settings.SUPABASE_DATABASE_URL:
            return "disabled"
        
        if not self.pool:
            # Try to initialize if not initialized but URL is present
            # This handles cases where DB might have been down during startup
            await self.create_pool()
            if not self.pool:
                return "fail"
        
        try:
            async with self.pool.acquire(timeout=5.0) as conn:
                await conn.execute("SELECT 1")
            return "ok"
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return "fail"

db = Database()

async def get_db():
    if not db.pool:
        # Try one last time to init
        if settings.SUPABASE_DATABASE_URL:
            await db.create_pool()
        
        if not db.pool:
            raise RuntimeError("Database pool is not initialized and SUPABASE_DATABASE_URL is missing or invalid")
            
    async with db.pool.acquire() as conn:
        yield conn
