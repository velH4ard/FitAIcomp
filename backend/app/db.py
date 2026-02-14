import logging
import asyncpg
from typing import Optional
from .config import settings

logger = logging.getLogger("fitai-db")

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
                    profile JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
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
