import pytest
from unittest.mock import AsyncMock, patch
from app import db as db_module
from app.db import Database

@pytest.mark.asyncio
async def test_create_pool_disables_statement_cache():
    """Regression test: ensure statement_cache_size=0 is passed to asyncpg.create_pool."""
    
    # Mock settings to have a database URL
    with patch("app.db.settings") as mock_settings:
        mock_settings.SUPABASE_DATABASE_URL = "postgresql://user:pass@localhost:5432/db"
        mock_settings.DB_STATEMENT_TIMEOUT_MS = 5000
        mock_settings.DB_SLOW_QUERY_MS = 300
        
        db_instance = Database()
        
        # We need to mock asyncpg.create_pool
        with patch("asyncpg.create_pool", new_callable=AsyncMock) as mock_create_pool:
            # Avoid calling init_db which would try to use the pool
            with patch.object(Database, "init_db", new_callable=AsyncMock):
                await db_instance.create_pool()
                
                # Verify create_pool was called with statement_cache_size=0
                _, kwargs = mock_create_pool.call_args
                assert kwargs.get("statement_cache_size") == 0


@pytest.mark.asyncio
async def test_create_pool_sets_statement_timeout_from_env():
    with patch("app.db.settings") as mock_settings:
        mock_settings.SUPABASE_DATABASE_URL = "postgresql://user:pass@localhost:5432/db"
        mock_settings.DB_STATEMENT_TIMEOUT_MS = 7000
        mock_settings.DB_SLOW_QUERY_MS = 300

        db_instance = Database()

        with patch("asyncpg.create_pool", new_callable=AsyncMock) as mock_create_pool:
            with patch.object(Database, "init_db", new_callable=AsyncMock):
                await db_instance.create_pool()

                _, kwargs = mock_create_pool.call_args
                assert kwargs.get("server_settings") == {"statement_timeout": "7000ms"}


@pytest.mark.asyncio
async def test_init_db_uses_non_partial_unique_index_for_meals_analyze_request_id():
    captured_sql = {"value": ""}

    class _AcquireCtx:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Conn:
        async def execute(self, sql):
            captured_sql["value"] = sql
            return "OK"

    class _Pool:
        def acquire(self):
            return _AcquireCtx(_Conn())

    db_instance = Database()
    db_instance.pool = _Pool()  # type: ignore[assignment]

    await db_instance.init_db()

    sql = captured_sql["value"]
    assert "DROP INDEX IF EXISTS idx_meals_analyze_request_id" in sql
    assert "ALTER COLUMN analyze_request_id SET NOT NULL" in sql
    assert "ON meals (analyze_request_id);" in sql
    assert "ON meals (analyze_request_id)\n                    WHERE analyze_request_id IS NOT NULL;" not in sql


def test_log_slow_query_emits_without_sql_text(caplog, monkeypatch):
    caplog.set_level("WARNING")
    monkeypatch.setattr(db_module.settings, "DB_SLOW_QUERY_MS", 1)
    db_module._log_slow_query("stats.daily", 0.0)
    assert any("DB_SLOW_QUERY" in record.message for record in caplog.records)
    assert all("SELECT" not in record.message for record in caplog.records)
