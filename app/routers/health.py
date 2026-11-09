"""Health check route: is every dependency of the application reachable?

Used by the web UI (green/red status dot) and, later, by container
orchestration (a HEALTHCHECK can restart the app if it stops responding).
"""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import get_session
from app.dependencies import get_ollama_client
from app.schemas import HealthOut
from app.services.ollama_client import OllamaClient, OllamaError

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health(
    session: AsyncSession = Depends(get_session),
    ollama: OllamaClient = Depends(get_ollama_client),
    settings: Settings = Depends(get_settings),
) -> HealthOut:
    try:
        await session.execute(text("SELECT 1"))  # cheapest possible query
        database = "up"
    except (SQLAlchemyError, OSError):
        database = "down"

    try:
        version = await ollama.version()
        ollama_status = "up"
    except OllamaError:
        version, ollama_status = None, "down"

    return HealthOut(ollama=ollama_status, database=database, ollama_version=version, model=settings.model)
