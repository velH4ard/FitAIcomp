from fastapi import FastAPI
from app.config import settings

app = FastAPI(title="FitAI API")

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "fitai-api",
        "version": "0.1.0",
        "env": settings.APP_ENV
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.APP_HOST, port=settings.APP_PORT)
