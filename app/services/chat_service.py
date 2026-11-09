"""Chat business logic.

This service orchestrates one chat exchange:

1. ``prepare()``  - runs *before* streaming starts, inside the HTTP request:
   checks the conversation exists and loads its recent history. Errors here
   (unknown conversation) can still be returned as a normal HTTP 404.
2. ``stream_reply()`` - an async generator consumed by FastAPI's
   ``StreamingResponse``: it relays the model's answer to the browser in
   real time and, once the answer is complete, saves everything.

Persistence strategy - one atomic transaction at the end
---------------------------------------------------------
The user message, the assistant answer (and, for a new chat, the
conversation itself) are written in a *single* transaction, only after the
model has finished answering:

- If generation fails (Ollama down, GPU out of memory...), nothing is
  written: the database never contains a question without an answer, and
  the user can simply retry.
- If the database write fails, the transaction is rolled back as a whole:
  there is never a half-saved exchange.

This is the "A" (Atomicity) of ACID. The trade-off: if the browser
disconnects in the middle of an answer, the exchange is not saved.

Event protocol sent to the browser (NDJSON, one JSON object per line):

    {"type": "conversation", "id": "...", "title": "...", "is_new": true, "model": "..."}
    {"type": "thinking", "text": "..."}   # model reasoning, 0..N times
    {"type": "content",  "text": "..."}   # answer text, 0..N times
    {"type": "error",    "text": "..."}   # something went wrong (ends the stream)
    {"type": "done", "message_id": "...", "model": "...", "token_count": 42, "generation_ms": 1234}

Model selection
---------------
The client may send the name of the model to use (drop-down menu). It is
checked against the installed models (``ModelCatalog``, allow-list). When no
model is sent, an existing conversation continues with the model of its
latest answer, and a new one uses the default model (``NUAGE_MODEL``).
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.repositories.conversation_repository import ConversationRepository
from app.services.model_catalog import ModelCatalog
from app.services.ollama_client import OllamaClient, OllamaError

logger = logging.getLogger(__name__)

TITLE_MAX_LENGTH = 60


class ConversationNotFoundError(Exception):
    """Raised when the client refers to a conversation id that does not exist."""


@dataclass
class ChatContext:
    """Everything ``stream_reply`` needs, computed by ``prepare``."""

    conversation_id: uuid.UUID
    is_new: bool
    title: str
    model: str
    user_message: str
    user_created_at: datetime
    # Full prompt sent to the model: history + the new user message.
    prompt: list[dict[str, str]]


def make_title(message: str) -> str:
    """Derive a conversation title from its first message (first line, truncated)."""
    first_line = message.strip().splitlines()[0] if message.strip() else "New conversation"
    if len(first_line) <= TITLE_MAX_LENGTH:
        return first_line
    return first_line[: TITLE_MAX_LENGTH - 1].rstrip() + "…"


def to_event(event_type: str, **fields: object) -> str:
    """Serialise one event as a single NDJSON line."""
    return json.dumps({"type": event_type, **fields}, default=str) + "\n"


class ChatService:
    def __init__(
        self,
        ollama: OllamaClient,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        catalog: ModelCatalog,
    ) -> None:
        self.ollama = ollama
        self.catalog = catalog
        # A *factory* rather than a session: the streaming generator outlives
        # the request handler, so it opens its own short-lived session at
        # the end instead of reusing the request's one.
        self.session_factory = session_factory
        self.settings = settings

    async def _resolve_model(self, requested: str | None, fallback: str) -> str:
        """Pick the model for this exchange.

        An explicitly requested model must be installed (raises
        UnknownModelError otherwise). The fallback (default model or the
        conversation's last model) is trusted: it comes from our own
        configuration or database, not from the client.
        """
        if requested is None:
            return fallback
        return await self.catalog.validate(requested)

    async def prepare(
        self,
        session: AsyncSession,
        conversation_id: uuid.UUID | None,
        message: str,
        model: str | None = None,
    ) -> ChatContext:
        """Validate the request against the database and build the model prompt.

        Read-only: nothing is written here (see module docstring).
        Raises ConversationNotFoundError, UnknownModelError, or OllamaError
        (Ollama unreachable while checking the requested model).
        """
        repo = ConversationRepository(session)
        now = datetime.now(UTC)

        if conversation_id is None:
            # New conversation: the id is generated here, in Python, so it
            # can be sent to the browser immediately, before it exists in
            # the database.
            return ChatContext(
                conversation_id=uuid.uuid4(),
                is_new=True,
                title=make_title(message),
                model=await self._resolve_model(model, self.settings.model),
                user_message=message,
                user_created_at=now,
                prompt=[{"role": "user", "content": message}],
            )

        conversation = await repo.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(str(conversation_id))

        # Keep only the most recent messages so the prompt fits in the
        # context window (-1 leaves room for the new user message).
        history = await repo.recent_messages(conversation_id, self.settings.max_history_messages - 1)
        # Only the final answers are sent back to the model, not the stored
        # "thinking": reasoning from previous turns is not useful context
        # and would waste a lot of tokens.
        prompt = [{"role": m.role, "content": m.content} for m in history]
        prompt.append({"role": "user", "content": message})

        return ChatContext(
            conversation_id=conversation.id,
            is_new=False,
            title=conversation.title,
            # Continue with the model of the latest answer unless another
            # one was picked in the menu.
            model=await self._resolve_model(model, conversation.model),
            user_message=message,
            user_created_at=now,
            prompt=prompt,
        )

    async def stream_reply(self, ctx: ChatContext) -> AsyncIterator[str]:
        """Stream the model's answer as NDJSON events, then persist the exchange."""
        yield to_event(
            "conversation", id=ctx.conversation_id, title=ctx.title, is_new=ctx.is_new, model=ctx.model
        )

        thinking_parts: list[str] = []
        content_parts: list[str] = []
        token_count: int | None = None
        generation_ms: int | None = None

        # --- 1. Relay the model's answer ---------------------------------------
        try:
            async for chunk in self.ollama.stream_chat(ctx.model, ctx.prompt, self.settings.num_ctx):
                if chunk.thinking:
                    thinking_parts.append(chunk.thinking)
                    yield to_event("thinking", text=chunk.thinking)
                if chunk.content:
                    content_parts.append(chunk.content)
                    yield to_event("content", text=chunk.content)
                if chunk.done:
                    token_count = chunk.token_count
                    generation_ms = chunk.total_ms
        except OllamaError as exc:
            # Expected failure: tell the user, persist nothing.
            logger.warning("Generation failed for conversation %s: %s", ctx.conversation_id, exc)
            yield to_event("error", text=str(exc))
            return

        # --- 2. Persist the whole exchange atomically --------------------------
        try:
            async with self.session_factory() as session, session.begin():
                # session.begin() opens a transaction that is COMMITted when
                # the block exits normally, or ROLLed BACK on any exception.
                repo = ConversationRepository(session)
                if ctx.is_new:
                    repo.add_conversation(
                        ctx.conversation_id, ctx.title, ctx.model, created_at=ctx.user_created_at
                    )
                else:
                    await repo.touch(ctx.conversation_id, model=ctx.model)
                repo.add_message(
                    ctx.conversation_id, "user", ctx.user_message, created_at=ctx.user_created_at
                )
                answer = repo.add_message(
                    ctx.conversation_id,
                    "assistant",
                    "".join(content_parts),
                    created_at=datetime.now(UTC),
                    thinking="".join(thinking_parts) or None,
                    model=ctx.model,
                    token_count=token_count,
                    generation_ms=generation_ms,
                )
                # flush() sends the INSERTs now (still inside the transaction)
                # so PostgreSQL returns the generated id of the answer.
                await session.flush()
                message_id = answer.id
        except SQLAlchemyError:
            # logger.exception records the full stack trace in the server
            # logs, but the browser only gets a generic message: internal
            # details (table names, SQL) must never be exposed to clients.
            logger.exception("Could not save exchange for conversation %s", ctx.conversation_id)
            yield to_event("error", text="The answer was generated but could not be saved.")
            return

        yield to_event(
            "done",
            message_id=message_id,
            model=ctx.model,
            token_count=token_count,
            generation_ms=generation_ms,
        )
