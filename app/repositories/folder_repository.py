"""Data access for the ``folders`` table (Repository pattern).

Same rules as the other repositories:

- the only place that queries the ``folders`` table;
- SQLAlchemy expressions only, so every query is parameterised;
- never commits: the service layer owns the transaction;
- every read is scoped to the caller's ``user_id``: another user's folder
  is reported exactly like a missing one (None or an empty list), so the
  API answers 404 and never confirms that an id exists (IDOR protection).
"""

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Folder


def in_folder(column, folder_id: uuid.UUID | None) -> ColumnElement[bool]:
    """SQL condition "``column`` designates ``folder_id``", where None means
    the root of the cloud.

    In SQL, ``folder_id = NULL`` is never true: the root needs ``IS NULL``.
    SQLAlchemy already turns ``column == None`` into ``IS NULL`` by itself
    (a mutation test confirmed it), but only for a literal None: the rule is
    spelled out here so it stays visible, and correct if the value ever
    becomes a bound parameter. ``IS NOT DISTINCT FROM`` would handle both
    cases in one expression, but PostgreSQL cannot use an index for it.
    """
    return column.is_(None) if folder_id is None else column == folder_id


class FolderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Read ----------------------------------------------------------------
    async def get_for_user(self, folder_id: uuid.UUID, user_id: uuid.UUID) -> Folder | None:
        """The folder if it exists AND belongs to ``user_id``, else None."""
        stmt = select(Folder).where(Folder.id == folder_id, Folder.user_id == user_id)
        return await self.session.scalar(stmt)

    async def list_children(self, user_id: uuid.UUID, parent_id: uuid.UUID | None) -> list[Folder]:
        """Sub-folders of ``parent_id`` (None = the root), sorted by name.

        Answered by the index of uq_folders_user_id_parent_id_name. The order
        follows the database collation (en_US.UTF-8: "apple" before "Banana",
        as a person expects, not the ASCII order of the "C" collation).
        """
        stmt = (
            select(Folder)
            .where(Folder.user_id == user_id, in_folder(Folder.parent_id, parent_id))
            .order_by(Folder.name)
        )
        result = await self.session.scalars(stmt)
        return list(result)

    async def names_starting_with(
        self, user_id: uuid.UUID, parent_id: uuid.UUID | None, prefix: str
    ) -> set[str]:
        """Names of the sub-folders of ``parent_id`` that start with ``prefix``
        (to find a free name such as "Photos (2)").

        ``autoescape=True`` escapes "%" and "_" in the prefix: a folder named
        "100%" must match literally, not act as a LIKE wildcard.
        """
        stmt = select(Folder.name).where(
            Folder.user_id == user_id,
            in_folder(Folder.parent_id, parent_id),
            Folder.name.startswith(prefix, autoescape=True),
        )
        result = await self.session.scalars(stmt)
        return set(result)

    async def lineage(
        self, folder_id: uuid.UUID, user_id: uuid.UUID, max_depth: int
    ) -> list[tuple[uuid.UUID, str]]:
        """``(id, name)`` of the folder and of all its ancestors, from the
        root down to the folder itself (the "breadcrumb"). Empty if the
        folder does not exist or belongs to someone else.

        One recursive query instead of one query per level. Roughly:

            WITH RECURSIVE lineage(id, parent_id, name, level) AS (
                SELECT id, parent_id, name, 1 FROM folders
                 WHERE id = $folder AND user_id = $user          -- start
              UNION ALL
                SELECT f.id, f.parent_id, f.name, l.level + 1    -- one level up
                  FROM folders f JOIN lineage l ON f.id = l.parent_id
                 WHERE f.user_id = $user AND l.level < $max_depth
            )
            SELECT id, name FROM lineage ORDER BY level DESC

        ``level < max_depth`` bounds the recursion: even a cycle created by
        hand in the database (A inside B inside A) cannot make PostgreSQL
        loop forever.
        """
        start = (
            select(Folder.id, Folder.parent_id, Folder.name, literal(1).label("level"))
            .where(Folder.id == folder_id, Folder.user_id == user_id)
            .cte("lineage", recursive=True)
        )
        parent = aliased(Folder)
        lineage = start.union_all(
            select(parent.id, parent.parent_id, parent.name, start.c.level + 1).where(
                parent.id == start.c.parent_id,
                parent.user_id == user_id,
                start.c.level < max_depth,
            )
        )
        stmt = select(lineage.c.id, lineage.c.name).order_by(lineage.c.level.desc())
        result = await self.session.execute(stmt)
        return [(row.id, row.name) for row in result]

    async def count_for_user(self, user_id: uuid.UUID) -> int:
        """Number of folders of the user (for the summary of the cloud)."""
        stmt = select(func.count()).select_from(Folder).where(Folder.user_id == user_id)
        return await self.session.scalar(stmt) or 0

    # --- Write ---------------------------------------------------------------
    def add(
        self, *, user_id: uuid.UUID, parent_id: uuid.UUID | None, name: str, created_at: datetime
    ) -> Folder:
        """Stage a new folder (sent to the database at the next flush/commit).

        Every value is set in Python, so the object is complete without being
        reloaded after the INSERT (a lazy reload is not allowed in async
        SQLAlchemy).
        """
        folder = Folder(
            id=uuid.uuid4(), user_id=user_id, parent_id=parent_id, name=name, created_at=created_at
        )
        self.session.add(folder)
        return folder
