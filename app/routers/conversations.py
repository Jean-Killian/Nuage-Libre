"""REST routes to browse and delete saved conversations (presentation layer).

REST conventions used here:
    GET    /api/conversations        -> list (collection)
    GET    /api/conversations/{id}   -> one resource with its messages
    DELETE /api/conversations/{id}   -> delete, answers 204 No Content

``conversation_id: uuid.UUID`` in the signature means FastAPI rejects any
id that is not a valid UUID with HTTP 422, before our code runs.

Every route acts on behalf of the caller (``get_current_user_id``): only
their own conversations are listed, and another user's conversation is
answered with 404, exactly like a conversation that does not exist.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user_id
from app.database import get_session
from app.repositories.conversation_repository import ConversationRepository
from app.schemas import ConversationDetail, ConversationSummary

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(
    # Query() validates ?limit=: between 1 and 200, so a client cannot ask
    # the server to load millions of rows at once.
    limit: int = Query(default=50, ge=1, le=200),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
):
    return await ConversationRepository(session).list_recent(user_id, limit)


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
):
    conversation = await ConversationRepository(session).get_with_messages(conversation_id, user_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return conversation


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
):
    # session.begin() wraps the DELETE in a transaction committed on success.
    async with session.begin():
        deleted = await ConversationRepository(session).delete(conversation_id, user_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
