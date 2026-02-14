import logging
import sys
from fastapi import FastAPI, APIRouter
from .errors import setup_error_handlers

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("fitai-api")

app = FastAPI(
    title="FitAI API",
    description="Backend for FitAI Telegram WebApp",
    version="0.1.0",
)

# Setup custom error handlers
setup_error_handlers(app)

# API Router
v1_router = APIRouter(prefix="/v1")

@app.get("/health", tags=["Health"])
@v1_router.get("/health", tags=["Health"])
async def health_check():
    return {
        "status": "ok",
        "service": "fitai-api",
        "version": "0.1.0"
    }

app.include_router(v1_router)

@app.on_event("startup")
async def startup_event():
    logger.info("Starting FitAI API...")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down FitAI API...")
