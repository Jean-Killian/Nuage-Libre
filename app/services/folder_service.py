"""Folders of the private cloud: business logic.

Folders are purely logical (they only exist in the database, see the
Folder model), so creating one involves no disk access at all. The rules
enforced here, before anything is written:

1. the name is cleaned and checked (services/file_policy.py);
2. the parent exists AND belongs to the caller (else: "not found");
3. the new folder would not be nested deeper than MAX_FOLDER_DEPTH;
4. no sibling has the same name: either the request is refused (409, a
   folder typed by the user) or a free name is chosen, "Photos (2)" (a
   folder UPLOADED from the computer, which must never merge into an
   existing one).

The database enforces the same invariants on its own (composite foreign
keys, UNIQUE NULLS NOT DISTINCT): if two requests race, the second INSERT
fails and is reported as "name taken" instead of creating a duplicate.

Errors are raised as domain exceptions; the router maps them to HTTP codes.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Folder
from app.repositories.file_repository import FileRepository
from app.repositories.folder_repository import FolderRepository
from app.services.file_policy import MAX_FOLDER_DEPTH, clean_folder_name, first_free_name

# SQLSTATE of a UNIQUE violation (PostgreSQL error code).
_UNIQUE_VIOLATION = "23505"

OnConflict = Literal["fail", "rename"]


# --- Domain errors ---------------------------------------------------------
class FolderNotFoundError(Exception):
    """No such folder for this user (it does not exist OR belongs to someone else)."""


class FolderNameTakenError(Exception):
    """A folder with this name already exists in the same parent."""

    def __init__(self, name: str) -> None:
        super().__init__(f"A folder named {name!r} already exists here.")
        self.name = name


class FolderTooDeepError(Exception):
    """The new folder would be nested deeper than MAX_FOLDER_DEPTH."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"Folders cannot be nested more than {limit} levels deep.")
        self.limit = limit


@dataclass(frozen=True)
class FolderView:
    """A folder and its path from the root (for the breadcrumb)."""

    folder: Folder
    path: list[tuple[uuid.UUID, str]]  # (id, name), root first, the folder itself last


@dataclass(frozen=True)
class CloudSummary:
    """Totals over the whole cloud of one user."""

    files: int
    folders: int
    total_size: int
    in_ai_memory: int


class FolderService:
    async def create(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        raw_name: str,
        parent_id: uuid.UUID | None,
        *,
        on_conflict: OnConflict = "fail",
    ) -> Folder:
        """Create a folder for ``user_id`` inside ``parent_id`` (None = the root).

        Raises InvalidFolderNameError, FolderNotFoundError (unknown parent),
        FolderTooDeepError or FolderNameTakenError.
        """
        name = clean_folder_name(raw_name)
        try:
            async with session.begin():
                repo = FolderRepository(session)
                if parent_id is not None:
                    parent_path = await repo.lineage(parent_id, user_id, MAX_FOLDER_DEPTH)
                    if not parent_path:
                        raise FolderNotFoundError(str(parent_id))
                    # The parent is at depth len(parent_path): its child one deeper.
                    if len(parent_path) + 1 > MAX_FOLDER_DEPTH:
                        raise FolderTooDeepError(MAX_FOLDER_DEPTH)

                taken = await repo.names_starting_with(user_id, parent_id, name)
                if name in taken:
                    if on_conflict == "fail":
                        raise FolderNameTakenError(name)
                    name = first_free_name(name, taken)

                return repo.add(user_id=user_id, parent_id=parent_id, name=name, created_at=datetime.now(UTC))
        except IntegrityError as exc:
            # Another request created the same name between our check and
            # our INSERT: the UNIQUE constraint caught it. Anything else is
            # unexpected and must not be hidden behind a "name taken".
            if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
                raise FolderNameTakenError(name) from exc
            raise

    async def get(self, session: AsyncSession, user_id: uuid.UUID, folder_id: uuid.UUID) -> FolderView:
        """One of the user's folders, with its path. Raises FolderNotFoundError."""
        repo = FolderRepository(session)
        folder = await repo.get_for_user(folder_id, user_id)
        if folder is None:
            raise FolderNotFoundError(str(folder_id))
        path = await repo.lineage(folder_id, user_id, MAX_FOLDER_DEPTH)
        return FolderView(folder=folder, path=path)

    async def list_children(
        self, session: AsyncSession, user_id: uuid.UUID, parent_id: uuid.UUID | None
    ) -> list[Folder]:
        """Sub-folders of ``parent_id`` (None = the root), by name.

        An unknown parent is an error (404), not an empty list: the client
        must learn that the folder it shows no longer exists.
        """
        repo = FolderRepository(session)
        if parent_id is not None and await repo.get_for_user(parent_id, user_id) is None:
            raise FolderNotFoundError(str(parent_id))
        return await repo.list_children(user_id, parent_id)

    async def summary(self, session: AsyncSession, user_id: uuid.UUID) -> CloudSummary:
        """Totals over the whole cloud (all folders): shown in the header of
        the file manager and used by the RAG status line."""
        totals = await FileRepository(session).totals_for_user(user_id)
        folders = await FolderRepository(session).count_for_user(user_id)
        return CloudSummary(
            files=totals.count, folders=folders, total_size=totals.total_size, in_ai_memory=totals.vectorized
        )
