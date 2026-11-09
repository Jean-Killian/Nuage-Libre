"""Dependency injection wiring.

FastAPI's ``Depends()`` system builds the objects a route needs (settings,
database session, services) and passes them as arguments. Routes never
instantiate their collaborators themselves, which means:

- each layer only depends on the layer below it, through these functions;
- tests can swap any piece with ``app.dependency_overrides`` (e.g. replace
  the real Ollama client with a fake one that needs no GPU).
"""

from fastapi import Depends

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.services.chat_service import ChatService
from app.services.model_catalog import ModelCatalog
from app.services.ollama_client import OllamaClient


def get_ollama_client(settings: Settings = Depends(get_settings)) -> OllamaClient:
    return OllamaClient(settings.ollama_url, read_timeout=settings.ollama_read_timeout)


def get_model_catalog(
    ollama: OllamaClient = Depends(get_ollama_client),
    settings: Settings = Depends(get_settings),
) -> ModelCatalog:
    return ModelCatalog(ollama, default_model=settings.model)


def get_chat_service(
    ollama: OllamaClient = Depends(get_ollama_client),
    settings: Settings = Depends(get_settings),
    catalog: ModelCatalog = Depends(get_model_catalog),
) -> ChatService:
    return ChatService(ollama=ollama, session_factory=SessionLocal, settings=settings, catalog=catalog)
