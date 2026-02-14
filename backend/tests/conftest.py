import pytest
import pytest_asyncio
import os

# Set dummy environment variables for testing before importing the app
os.environ["BOT_TOKEN"] = "fake_bot_token"
os.environ["DATABASE_URL"] = "postgresql://user:pass@localhost:5432/db"
os.environ["JWT_SECRET"] = "fake_jwt_secret"
os.environ["OPENROUTER_API_KEY"] = "fake_openrouter_key"
os.environ["SUPABASE_URL"] = "https://fake.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "fake_key"
os.environ["SUPABASE_STORAGE_BUCKET"] = "meals"

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.db import db

@pytest_asyncio.fixture
async def client():
    # Use ASGITransport for FastAPI testing
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

@pytest_asyncio.fixture(autouse=True)
async def mock_db_pool(monkeypatch):
    """Mock database pool to avoid real connections during tests."""
    class MockPool:
        def acquire(self, timeout=None):
            class MockAcquireContext:
                async def __aenter__(self):
                    class MockConn:
                        async def execute(self, query, *args):
                            return "OK"
                        async def fetchrow(self, query, *args):
                            return None
                    return MockConn()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            return MockAcquireContext()
        async def close(self):
            pass
    
    mock_pool = MockPool()
    monkeypatch.setattr(db, "pool", mock_pool)
    return mock_pool
