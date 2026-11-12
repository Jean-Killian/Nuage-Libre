"""Application entry point.

Builds the FastAPI application: registers the routers (one per feature)
and serves the static web interface. Start it with:

    uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

Interactive API documentation is generated automatically at /docs.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.database import engine
from app.routers import chat, conversations, files, health, models

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Code run once at startup (before ``yield``) and once at shutdown (after).

    On shutdown we close every pooled database connection cleanly, so
    PostgreSQL does not keep orphan sessions open.
    """
    yield
    await engine.dispose()


app = FastAPI(
    title="Nuage Libre API",
    description="Self-hosted private cloud and AI chat backed by a local Ollama engine.",
    version="0.5.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(conversations.router)
app.include_router(models.router)
app.include_router(files.router)

# Static assets (JS/CSS/images) under /static, and the single-page UI at "/".
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
