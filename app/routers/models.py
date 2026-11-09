"""Route listing the models available in the drop-down menu (presentation layer)."""

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import get_model_catalog
from app.schemas import ModelOut
from app.services.model_catalog import ModelCatalog
from app.services.ollama_client import OllamaError

router = APIRouter(prefix="/api", tags=["models"])


@router.get("/models", response_model=list[ModelOut])
async def list_models(catalog: ModelCatalog = Depends(get_model_catalog)) -> list[ModelOut]:
    """Installed chat models, smallest first. The default model is flagged."""
    try:
        models = await catalog.chat_models()
    except OllamaError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None
    return [
        ModelOut(
            name=m.name,
            family=m.family,
            parameter_size=m.parameter_size,
            quantization=m.quantization,
            size_bytes=m.size_bytes,
            thinking="thinking" in m.capabilities,
            vision="vision" in m.capabilities,
            is_default=m.name == catalog.default_model,
        )
        for m in models
    ]
