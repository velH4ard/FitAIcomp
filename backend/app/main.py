import logging
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter
from .errors import setup_error_handlers
from .db import db

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
