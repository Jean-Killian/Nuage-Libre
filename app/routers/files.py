"""HTTP routes of the private cloud (presentation layer).

    POST /api/files                  upload one file           -> 201 Created
    GET  /api/files                  the caller's files, newest first
    GET  /api/files/{id}/download    download it (or view it with ?inline=true)

Upload protocol: the request body IS the file
----------------------------------------------
The usual FastAPI approach (``UploadFile``, multipart/form-data) was
measured as a poor fit for this server: Starlette first copies every
uploaded file into a temporary file under /tmp, and on Bazzite /tmp is a
*tmpfs*, i.e. RAM (16 GB, shared with the LLM). A 4 GB video would occupy
4 GB of RAM, then be copied a second time to the data disk.

Instead, like Amazon S3 (PutObject) or Google Drive (uploadType=media), the
client sends the raw bytes of the file as the body:

    POST /api/files
    Content-Type: application/octet-stream
    Content-Length: 182734
    X-File-Name: Rapport%20de%20stage.pdf      (percent-encoded UTF-8)

    <the bytes of the file>

The router hands ``request.stream()`` to the service, which writes each
chunk straight to its final disk, as it arrives: constant memory whatever
the file size, one single write, and the size limit can be enforced
before the first byte is read (Content-Length).

Side benefit, CSRF protection by construction: a page from another
website can only send "simple" cross-origin requests without the
browser's permission (CORS preflight), and a simple request cannot carry a
custom header such as X-File-Name nor the application/octet-stream type.
A multipart/form-data upload, on the contrary, is a simple request that
any website could submit on the user's behalf.
"""

import uuid
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import ClientDisconnect

from app.auth import get_current_user_id
from app.database import get_session
from app.dependencies import get_file_service
from app.schemas import FileOut
from app.services.file_policy import InvalidFileNameError
from app.services.file_service import (
    CloudFileNotFoundError,
    FileService,
    IncompleteUploadError,
    InsufficientStorageError,
    UploadNotSavedError,
    UploadTooLargeError,
)

router = APIRouter(prefix="/api/files", tags=["files"])

# Sent with every downloaded or viewed file except PDF, audio and video
# (see file_policy.needs_sandbox). "sandbox" turns the response into an isolated,
# script-less origin; default-src 'none' forbids loading anything else.
# style-src 'unsafe-inline' is needed by the browser's own image viewer
# page (it centres the picture with inline styles), and img/media-src
# 'self' let the browser display the image or play the media itself.
SANDBOX_POLICY = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox"


def _human_size(num_bytes: int) -> str:
    """2147483648 -> "2.0 GiB" (for error messages shown to the user)."""
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=FileOut,
    summary="Upload a file to the private cloud",
    # The body is read by hand (request.stream()), so FastAPI cannot infer
    # its schema: it is described here for the /docs page, where it shows
    # a file picker.
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "The raw bytes of the file (not multipart/form-data).",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
    responses={
        400: {"description": "Upload interrupted before the end of the file"},
        411: {"description": "Content-Length header missing (e.g. chunked transfer)"},
        413: {"description": "File larger than NUAGE_MAX_UPLOAD_BYTES"},
        415: {"description": "Body is not application/octet-stream"},
        422: {"description": "X-File-Name missing, not percent-encoded UTF-8, or empty"},
        500: {"description": "The database could not index the file (nothing is kept)"},
        507: {"description": "Not enough free space left on the server's disk"},
    },
)
async def upload_file(
    request: Request,
    response: Response,
    x_file_name: str = Header(
        alias="X-File-Name",
        min_length=1,
        max_length=4096,
        description="Name of the file, percent-encoded UTF-8 (JavaScript: encodeURIComponent(file.name)).",
    ),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    service: FileService = Depends(get_file_service),
) -> FileOut:
    """Store the request body as a new file of the caller's cloud.

    Uploading only STORES the file. Adding it to the AI memory (RAG) will
    be a separate, explicit action (``is_vectorized`` stays false).
    """
    # --- 1. Protocol checks: nothing of the body has been read yet ---------
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/octet-stream":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Send the raw bytes of the file with 'Content-Type: application/octet-stream'.",
        )
    content_length = request.headers.get("content-length")
    if content_length is None:
        # Without it the size is unknown until the end: refuse rather than
        # discover too late that the file does not fit.
        raise HTTPException(
            status_code=status.HTTP_411_LENGTH_REQUIRED, detail="The Content-Length header is required."
        )
    if not (content_length.isascii() and content_length.isdigit()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length header.")
    try:
        # HTTP headers are ASCII: the name travels percent-encoded.
        # errors="strict": malformed UTF-8 is an error, not silently replaced.
        raw_filename = unquote(x_file_name, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="X-File-Name must be the percent-encoded UTF-8 name of the file.",
        ) from None

    # --- 2. Business rules and storage (service layer) ---------------------
    try:
        stored = await service.upload(
            session, user_id, raw_filename, request.stream(), declared_size=int(content_length)
        )
    except InvalidFileNameError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    except UploadTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"File too large: the limit is {_human_size(exc.limit)}.",
        ) from None
    except InsufficientStorageError:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail="Not enough free space on the server's disk.",
        ) from None
    except (ClientDisconnect, IncompleteUploadError):
        # The client is usually gone already; this mainly keeps the logs
        # clean (a 400 line instead of a stack trace).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Upload interrupted before the end of the file."
        ) from None
    except UploadNotSavedError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The file was received but could not be saved. Please try again.",
        ) from None

    # REST: a 201 Created response points to the new resource.
    response.headers["Location"] = f"/api/files/{stored.id}/download"
    return stored


@router.get("", response_model=list[FileOut])
async def list_files(
    limit: int = Query(default=200, ge=1, le=1000),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    service: FileService = Depends(get_file_service),
):
    """The caller's files, newest first (``file_path`` is never exposed)."""
    return await service.list_files(session, user_id, limit)


@router.get(
    "/{file_id}/download",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "The bytes of the file"},
        404: {"description": "File not found (or owned by another user)"},
    },
)
async def download_file(
    # Typed as a UUID: "../../etc/passwd" or any other non-UUID value is
    # rejected with 422 by FastAPI before this function even runs.
    file_id: uuid.UUID,
    inline: bool = Query(
        default=False, description="Display in the browser instead of downloading (safe types only)."
    ),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    service: FileService = Depends(get_file_service),
) -> FileResponse:
    """Send one of the caller's files.

    ``?inline=true`` asks to VIEW the file; it is honoured only for types
    the browser can display without running scripts (images, PDF, audio,
    video, text shown as plain text). Anything else, HTML and SVG
    included, is always sent as a download.
    """
    try:
        plan = await service.plan_download(session, user_id, file_id, inline=inline)
    except CloudFileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.") from None

    headers = {
        # Never guess the type from the content: a text file that looks
        # like HTML stays text, an "image" that contains a script stays an
        # (invalid) image.
        "X-Content-Type-Options": "nosniff",
        # Other websites cannot embed our files (<img src="http://nuage/...">).
        "Cross-Origin-Resource-Policy": "same-origin",
        # Links clicked inside a viewed PDF do not reveal the file's URL.
        "Referrer-Policy": "no-referrer",
        # Personal data: the browser must not keep a copy in its disk cache.
        "Cache-Control": "no-store",
    }
    if plan.sandbox:
        headers["Content-Security-Policy"] = SANDBOX_POLICY

    # FileResponse streams the file from disk in chunks (constant memory),
    # supports Range requests (seeking in a video) and builds a
    # Content-Disposition header that encodes non-ASCII names (RFC 6266 /
    # RFC 5987: filename*=utf-8''...).
    return FileResponse(
        plan.path,
        media_type=plan.media_type,
        filename=plan.filename,
        content_disposition_type="inline" if plan.inline else "attachment",
        headers=headers,
    )
