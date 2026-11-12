"""Data access layer for conversations and messages (Repository pattern).

The repository is the *only* place in the application that knows how data
is stored. Routers and services call methods such as
``repo.get_with_messages(id)`` and never write SQL themselves. Benefits:

- Separation of concerns: switching the storage (or optimising a query)
  only changes this file.
- Testability: business logic can be tested with a fake repository.
- Security: every query is built with SQLAlchemy expressions, which are
  always sent to PostgreSQL as *parameterised* statements. User input is
  transmitted separately from the SQL text, so SQL injection is impossible
  by construction (no string concatenation anywhere).

Transactions: the repository does NOT commit. The caller (service layer)
decides where a transaction starts and ends, so several repository calls
can be grouped atomically (all succeed or all are rolled back).

Ownership (multi-user foundation): every method that reads or changes a
conversation takes the ``user_id`` of the caller and puts it in the WHERE
clause (``WHERE id = ? AND user_id = ?``). A conversation of another user
is therefore indistinguishable from a conversation that does not exist:
the API answers 404, never 403, so it does not even reveal that the id is
valid. This prevents IDOR attacks (Insecure Direct Object Reference, OWASP
A01 Broken Access Control): knowing or guessing an id is not enough.
"""

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Conversation, Message


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Read ----------------------------------------------------------------
    async def list_recent(self, user_id: uuid.UUID, limit: int = 50) -> list[Conversation]:
        """The user's conversations, most recently updated first.

        Generated SQL (roughly), answered by ix_conversations_user_id_updated_at:
            SELECT ... FROM conversations WHERE user_id = $1
            ORDER BY updated_at DESC LIMIT $2
        """
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        )
        result = await self.session.scalars(stmt)
        return list(result)

    async def get(self, conversation_id: uuid.UUID, user_id: uuid.UUID) -> Conversation | None:
        """The conversation if it exists AND belongs to ``user_id``, else None."""
        stmt = select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        return await self.session.scalar(stmt)

    async def get_with_messages(self, conversation_id: uuid.UUID, user_id: uuid.UUID) -> Conversation | None:
        """Load one of the user's conversations and all its messages.

        ``selectinload`` fetches the messages in a second query
        (``SELECT ... FROM messages WHERE conversation_id IN (...)``) right
        away. Without it, accessing ``conversation.messages`` would trigger a
        lazy load, which is forbidden in async mode and is also the classic
        "N+1 queries" performance trap.
        """
        stmt = (
            select(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
            .options(selectinload(Conversation.messages))
        )
        return await self.session.scalar(stmt)

    async def recent_messages(self, conversation_id: uuid.UUID, limit: int) -> list[Message]:
        """The ``limit`` last messages, returned in chronological order.

        We select the newest rows (ORDER BY created_at DESC LIMIT n) in a
        subquery, then re-sort them oldest-first for the model prompt.

        No user filter here: it is only called by ChatService.prepare()
        right after ``get(conversation_id, user_id)`` has proved, in the
        same request, that the conversation belongs to the caller.
        """
        newest = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
            .subquery()
        )
        stmt = select(Message).join(newest, Message.id == newest.c.id).order_by(Message.created_at.asc())
        result = await self.session.scalars(stmt)
        return list(result)

    # --- Write ---------------------------------------------------------------
    def add_conversation(
        self,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        title: str,
        model: str,
        *,
        created_at: datetime,
    ) -> Conversation:
        """Stage a new conversation owned by ``user_id`` (sent to the DB at
        the next flush/commit).

        ``created_at`` is the time of its first message, so a conversation is
        never dated after its own content.
        """
        conversation = Conversation(
            id=conversation_id,
            user_id=user_id,
            title=title,
            model=model,
            created_at=created_at,
            updated_at=created_at,
        )
        self.session.add(conversation)
        return conversation

    def add_message(
        self,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        *,
        created_at: datetime,
        thinking: str | None = None,
        model: str | None = None,
        token_count: int | None = None,
        generation_ms: int | None = None,
    ) -> Message:
        """Stage a new message.

        ``created_at`` is passed explicitly instead of relying on the
        database default: PostgreSQL's now() returns the *transaction start*
        time, so two messages inserted in the same transaction would get the
        exact same timestamp and their order would be undefined. The service
        layer stamps the user message when the request arrives and the
        assistant message when generation ends.
        """
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            thinking=thinking,
            model=model,
            token_count=token_count,
            generation_ms=generation_ms,
            created_at=created_at,
        )
        self.session.add(message)
        return message

    async def touch(self, conversation_id: uuid.UUID, user_id: uuid.UUID, *, model: str) -> None:
        """Record new activity on one of the user's conversations.

        - ``updated_at = now()`` moves it to the top of the sidebar;
        - ``model`` remembers the model of the latest answer, used by default
          for the next message.

        The ``user_id`` condition is defence in depth: ownership was already
        checked by ChatService.prepare(), but an UPDATE should never be able
        to touch another user's row, whoever calls it.
        """
        await self.session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
            .values(updated_at=func.now(), model=model)
        )

    async def delete(self, conversation_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Delete one of the user's conversations; its messages go with it
        (ON DELETE CASCADE).

        Returns True if a row was actually deleted, False if the id is
        unknown OR belongs to someone else (the caller cannot tell the two
        apart, which is intended).
        """
        result = await self.session.execute(
            delete(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        )
        return result.rowcount > 0
