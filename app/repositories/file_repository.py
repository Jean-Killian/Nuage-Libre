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
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StoredFile
from app.repositories.folder_repository import in_folder


@dataclass(frozen=True)
class FileTotals:
    """Aggregates over all the files of one user."""

    count: int
    total_size: int
    vectorized: int


class FileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Read ----------------------------------------------------------------
    async def list_for_user(
        self, user_id: uuid.UUID, folder_id: uuid.UUID | None, limit: int
    ) -> list[StoredFile]:
        """The user's files in ``folder_id`` (None = the root), newest first
        (answered by ix_files_user_id_folder_id_created_at).

        Generated SQL (roughly):
            SELECT ... FROM files WHERE user_id = $1 AND folder_id = $2
            ORDER BY created_at DESC LIMIT $3
        """
        stmt = (
            select(StoredFile)
            .where(StoredFile.user_id == user_id, in_folder(StoredFile.folder_id, folder_id))
            .order_by(StoredFile.created_at.desc())
            .limit(limit)
        )
        result = await self.session.scalars(stmt)
        return list(result)

    async def totals_for_user(self, user_id: uuid.UUID) -> FileTotals:
        """Number of files, total size and files in the AI memory, over the
        WHOLE cloud of the user (all folders), in one aggregate query:

            SELECT count(*), coalesce(sum(size), 0),
                   count(*) FILTER (WHERE is_vectorized)
            FROM files WHERE user_id = $1
        """
        stmt = select(
            func.count(),
            func.coalesce(func.sum(StoredFile.size), 0),
            func.count().filter(StoredFile.is_vectorized),
        ).where(StoredFile.user_id == user_id)
        count, total_size, vectorized = (await self.session.execute(stmt)).one()
        return FileTotals(count=count, total_size=int(total_size), vectorized=vectorized)

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
        folder_id: uuid.UUID | None,
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
            folder_id=folder_id,
            filename=filename,
            file_path=file_path,
            mime_type=mime_type,
            size=size,
            is_vectorized=False,
            created_at=created_at,
        )
        self.session.add(stored)
        return stored
