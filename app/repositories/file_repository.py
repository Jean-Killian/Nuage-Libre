"""Data access for the ``files`` table (Repository pattern).

Same rules as ConversationRepository:

- the only place that queries the ``files`` table;
- SQLAlchemy expressions only, so every query is parameterised;
- never commits: the service layer owns the transaction;
- every read is scoped to the caller's ``user_id``. Another user's file is
  reported exactly like a missing one (None), so the API answers 404 and
  never confirms that an id exists (IDOR protection).

The bytes of the files are NOT handled here but by ``FileStorage``
(app/repositories/file_storage.py): one class per storage technology.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StoredFile


class FileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Read ----------------------------------------------------------------
    async def list_for_user(self, user_id: uuid.UUID, limit: int) -> list[StoredFile]:
        """The user's files, newest first (answered by ix_files_user_id_created_at).

        Generated SQL (roughly):
            SELECT ... FROM files WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2
        """
        stmt = (
            select(StoredFile)
            .where(StoredFile.user_id == user_id)
            .order_by(StoredFile.created_at.desc())
            .limit(limit)
        )
        result = await self.session.scalars(stmt)
        return list(result)

    async def get_for_user(self, file_id: uuid.UUID, user_id: uuid.UUID) -> StoredFile | None:
        """The file if it exists AND belongs to ``user_id``, else None."""
        stmt = select(StoredFile).where(StoredFile.id == file_id, StoredFile.user_id == user_id)
        return await self.session.scalar(stmt)

    # --- Write ---------------------------------------------------------------
    def add(
        self,
        *,
        file_id: uuid.UUID,
        user_id: uuid.UUID,
        filename: str,
        file_path: str,
        mime_type: str,
        size: int,
        created_at: datetime,
    ) -> StoredFile:
        """Stage a new row (sent to the database at the next flush/commit).

        Every value, including ``id``, ``is_vectorized`` and ``created_at``,
        is set in Python: the object is then complete without reloading it
        from the database after the INSERT (a lazy reload is not allowed in
        async SQLAlchemy).
        """
        stored = StoredFile(
            id=file_id,
            user_id=user_id,
            filename=filename,
            file_path=file_path,
            mime_type=mime_type,
            size=size,
            is_vectorized=False,
            created_at=created_at,
        )
        self.session.add(stored)
        return stored
