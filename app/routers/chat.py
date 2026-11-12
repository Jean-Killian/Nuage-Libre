"""HTTP route for chatting with the model (presentation layer).

A router only deals with HTTP concerns: parsing/validating the request,
calling the service, and turning results or errors into HTTP responses.
All the actual logic lives in ``app/services/chat_service.py``.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user_id
from app.config import Settings, get_settings
from app.database import get_session
from app.dependencies import get_chat_service
from app.schemas import ChatRequest
from app.services.chat_service import ChatService, ConversationNotFoundError
from app.services.model_catalog import UnknownModelError
from app.services.ollama_client import OllamaError

router = APIRouter(prefix="/api", tags=["chat"])


@router.post(
    "/chat",
    # Documented in /docs: the body is a stream of JSON lines, not one JSON.
    response_class=StreamingResponse,
    responses={
        200: {"content": {"application/x-ndjson": {}}, "description": "Stream of NDJSON events"},
        404: {"description": "Conversation not found"},
        422: {"description": "Invalid body or model not installed"},
        503: {"description": "AI engine unreachable"},
    },
)
async def chat(
    payload: ChatRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    service: ChatService = Depends(get_chat_service),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Send a message and receive the model's answer as a live stream.

    - ``conversation_id`` omitted -> a new conversation is created for the caller.
    - ``conversation_id`` given  -> the message is added to that conversation
      (404 if it does not exist or belongs to another user).
    - ``model`` (optional) -> model to answer with; must be installed.
    """
    if len(payload.message) > settings.max_message_length:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Message too long (max {settings.max_message_length} characters).",
        )

    try:
        ctx = await service.prepare(session, user_id, payload.conversation_id, payload.message, payload.model)
    except ConversationNotFoundError:
        # "from None": the internal exception is an expected case, not a bug;
        # do not chain it to the HTTP error in the logs.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.") from None
    except UnknownModelError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="This model is not installed."
        ) from None
    except OllamaError as exc:
        # Could not even check the model list: the AI engine is down.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None

    return StreamingResponse(
        service.stream_reply(ctx),
        media_type="application/x-ndjson",
        # Ask reverse proxies (nginx, Caddy...) not to buffer the stream,
        # otherwise tokens would arrive all at once at the end.
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
