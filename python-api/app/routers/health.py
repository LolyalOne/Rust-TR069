"""Healthcheck router answering Docker container healthcheck."""

from fastapi import APIRouter, status
from sqlalchemy import text

from app.config import settings
from app.database import AsyncSessionLocal

router = APIRouter(tags=["Health"])


@router.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    """
    Healthcheck endpoint answering docker-compose healthcheck.
    Performs fast DB verification.
    """
    db_status = "ok"
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {e}"

    return {
        "status": "healthy" if db_status == "ok" else "degraded",
        "database": db_status,
        "mqtt_broker": f"{settings.MQTT_HOST}:{settings.MQTT_PORT}",
        "version": "1.0.0",
    }
