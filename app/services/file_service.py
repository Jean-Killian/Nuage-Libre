"""Private cloud business logic: upload, list and download files.

The service coordinates two storage systems that cannot share a
transaction: the disk (FileStorage, the bytes) and PostgreSQL
(FileRepository, the index). The order of operations is what keeps them
consistent.

Upload order: disk first, database last
---------------------------------------
1. Check the request against the rules (name, size limit, free space,
   destination folder) BEFORE reading a single byte of the body.
2. Stream the bytes to disk (atomic write: complete file or nothing).
3. Only then INSERT the row, in its own transaction.
4. If the INSERT fails, delete the bytes again (compensating action).

The database is the source of truth for "which files exist": a row only
appears once its bytes are safely on disk, so the file list never shows a
file that cannot be downloaded. The opposite order (row first) could leave
visible rows pointing to missing files after a crash. The remaining failure
mode, a crash between steps 2 and 3, leaves an orphan file without a row:
invisible to users, it only wastes space and can be detected by a future
clean-up job comparing the folder with the table.

Errors are raised as domain exceptions (below); the router maps them to
HTTP status codes. The service never builds HTTP responses itself.
"""

import logging
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import StoredFile
from app.repositories.file_repository import FileRepository
from app.repositories.file_storage import FileStorage, UnsafePathError
from app.repositories.folder_repository import FolderRepository
from app.services.file_policy import clean_filename, guess_mime_type, inline_media_type, needs_sandbox
from app.services.folder_service import FolderNotFoundError

logger = logging.getLogger(__name__)


# --- Domain errors ---------------------------------------------------------
class CloudFileNotFoundError(Exception):
    """No such file for this user (it does not exist OR belongs to someone else)."""


class UploadTooLargeError(Exception):
    """The file is larger than NUAGE_MAX_UPLOAD_BYTES."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"File too large (max {limit} bytes).")
        self.limit = limit


class InsufficientStorageError(Exception):
    """Accepting the file would leave less than NUAGE_MIN_FREE_DISK_BYTES free."""


class IncompleteUploadError(Exception):
    """The body ended before the announced number of bytes was received."""


class UploadNotSavedError(Exception):
    """The bytes were received but the database refused or failed to index them."""


@dataclass(frozen=True)
class DownloadPlan:
    """Everything the router needs to send a file, decided by the rules."""

    path: Path  # absolute path, already checked to be inside the storage root
    filename: str  # display name, for the Content-Disposition header
    media_type: str  # Content-Type to send (text/* is sent as text/plain inline)
    inline: bool  # True: display in the browser; False: force a download
    sandbox: bool  # add "Content-Security-Policy: sandbox"


async def _limit_size(chunks: AsyncIterable[bytes], max_bytes: int) -> AsyncIterator[bytes]:
    """Pass the chunks through, but stop as soon as more than ``max_bytes``
    have been received.

    The Content-Length header was already checked before reading, so this
    is defence in depth: the limit holds even if the header were missing or
    wrong, and the excess bytes are never written to disk.
    """
    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if total > max_bytes:
            raise UploadTooLargeError(max_bytes)
        yield chunk


class FileService:
    def __init__(self, storage: FileStorage, settings: Settings) -> None:
        self.storage = storage
        self.settings = settings

    # --- Upload ------------------------------------------------------------
    async def upload(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        raw_filename: str,
        chunks: AsyncIterable[bytes],
        *,
        declared_size: int | None,
        folder_id: uuid.UUID | None = None,
    ) -> StoredFile:
        """Store one file for ``user_id`` in ``folder_id`` (None = the root)
        and return its database row.

        ``chunks`` is the request body, consumed as a stream: the file is
        written to disk piece by piece and never held in memory as a whole.
        The folder only changes where the file is SHOWN: on disk, the bytes
        go to "<user_id>/<file_id>" as always.
        Raises InvalidFileNameError, FolderNotFoundError, UploadTooLargeError,
        InsufficientStorageError, IncompleteUploadError, UploadNotSavedError,
        or whatever ``chunks`` raises (e.g. the client disconnecting).
        """
        # 1. Rules checked before reading the body: a refused upload costs
        #    no bandwidth, no disk write and no database access.
        filename = clean_filename(raw_filename)
        max_bytes = self.settings.max_upload_bytes
        if declared_size is not None and declared_size > max_bytes:
            raise UploadTooLargeError(max_bytes)
        needed = (declared_size or 0) + self.settings.min_free_disk_bytes
        if await self.storage.free_bytes() < needed:
            raise InsufficientStorageError()
        if folder_id is not None:
            # A short read-only transaction, closed BEFORE the body is
            # streamed (a 2 GB upload must not hold a database connection).
            # The composite foreign key (folder_id, user_id) checks again at
            # INSERT time, should the folder disappear in the meantime.
            async with session.begin():
                folder = await FolderRepository(session).get_for_user(folder_id, user_id)
            if folder is None:
                raise FolderNotFoundError(str(folder_id))

        # 2. Bytes to disk. The id is generated here because it is also the
        #    file's name on disk, written before the row exists.
        file_id = uuid.uuid4()
        relative_path = self.storage.relative_path_for(user_id, file_id)
        size = await self.storage.save(relative_path, _limit_size(chunks, max_bytes))
        if declared_size is not None and size != declared_size:
            # Fewer bytes than announced: the transfer was cut. A truncated
            # file is worse than no file (a broken PDF, a corrupt archive).
            self.storage.delete(relative_path)
            raise IncompleteUploadError()

        # 3. Row last, in its own short transaction.
        try:
            async with session.begin():
                stored = FileRepository(session).add(
                    file_id=file_id,
                    user_id=user_id,
                    folder_id=folder_id,
                    filename=filename,
                    file_path=relative_path,
                    mime_type=guess_mime_type(filename),
                    size=size,
                    created_at=datetime.now(UTC),
                )
        except SQLAlchemyError as exc:
            # 4. Compensating action: the transaction was rolled back, so the
            #    bytes must go too, otherwise they would be an orphan. The
            #    full error goes to the server logs only; the client gets a
            #    generic message (no table names or SQL leaked).
            self.storage.delete(relative_path)
            logger.exception("Could not index file %s for user %s", file_id, user_id)
            raise UploadNotSavedError() from exc
        except BaseException:
            # Same compensation for any other interruption (e.g. cancellation).
            self.storage.delete(relative_path)
            raise

        # Privacy by design: ids and sizes are logged, never file names
        # (a name such as "medical_results.pdf" is personal data).
        logger.info("Stored file %s (%d bytes) for user %s", file_id, size, user_id)
        return stored

    # --- Read --------------------------------------------------------------
    async def list_files(
        self, session: AsyncSession, user_id: uuid.UUID, folder_id: uuid.UUID | None, limit: int
    ) -> list[StoredFile]:
        """The user's files in ``folder_id`` (None = the root), newest first.

        An unknown folder (or someone else's) is an error, not an empty
        list, so the client learns that the folder it shows is gone.
        """
        if folder_id is not None and await FolderRepository(session).get_for_user(folder_id, user_id) is None:
            raise FolderNotFoundError(str(folder_id))
        return await FileRepository(session).list_for_user(user_id, folder_id, limit)

    async def plan_download(
        self, session: AsyncSession, user_id: uuid.UUID, file_id: uuid.UUID, *, inline: bool
    ) -> DownloadPlan:
        """Find one of the user's files and decide HOW it may be sent.

        ``inline`` is what the client asked for ("view" rather than
        "download"); it is granted only if the type is in the safe
        allow-list, otherwise the file is sent as an attachment.
        Raises CloudFileNotFoundError.
        """
        stored = await FileRepository(session).get_for_user(file_id, user_id)
        if stored is None:
            raise CloudFileNotFoundError(str(file_id))

        try:
            path = await self.storage.locate(stored.file_path)
        except UnsafePathError:
            # Can only happen if the database was altered: a security event.
            logger.error("Refused to serve file %s: its path leaves the storage folder", file_id)
            raise CloudFileNotFoundError(str(file_id)) from None
        if path is None:
            # Row without bytes (e.g. deleted by hand through the Samba
            # share): an integrity problem worth a trace in the logs.
            logger.error("File %s is in the database but missing on disk", file_id)
            raise CloudFileNotFoundError(str(file_id))

        inline_type = inline_media_type(stored.mime_type) if inline else None
        media_type = inline_type or stored.mime_type
        return DownloadPlan(
            path=path,
            filename=stored.filename,
            media_type=media_type,
            inline=inline_type is not None,
            sandbox=needs_sandbox(media_type),
        )
