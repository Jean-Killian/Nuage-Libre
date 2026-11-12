"""Integration tests of the private cloud: /api/files routes, real database,
real file storage (in a temporary folder, see conftest.py).

IDs (T-FILE-xx, T-SEC-xx) match docs/TEST_PLAN.md.
"""

import logging
import os
import stat
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.auth import GHOST_USER_ID
from app.config import get_settings
from app.database import SessionLocal, engine
from app.models import StoredFile
from app.repositories.file_storage import FileStorage
from app.services.file_service import FileService, IncompleteUploadError, UploadTooLargeError
from tests.conftest import TEST_STORAGE_DIR, upload

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # PNG signature + padding


def files_on_disk() -> list[str]:
    """Every file under the test storage folder (temporary ".part" included)."""
    return sorted(p.name for p in TEST_STORAGE_DIR.rglob("*") if p.is_file())


async def count_rows() -> int:
    async with SessionLocal() as session:
        return await session.scalar(select(func.count()).select_from(StoredFile))


# --- Upload ------------------------------------------------------------------
async def test_upload_stores_bytes_on_disk_and_indexes_them(client):
    """T-FILE-01: the bytes land at <root>/<user_id>/<file_id>, mode 600; the row
    stores a RELATIVE path; the API answers 201 with a Location header."""
    content = b"%PDF-1.7 a small fake PDF"

    response = await upload(client, content, "Rapport de stage.pdf")

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "Rapport de stage.pdf"
    assert body["mime_type"] == "application/pdf"
    assert body["size"] == len(content)
    assert body["is_vectorized"] is False  # uploading only STORES the file
    assert body["previewable"] is True
    assert "file_path" not in body and "user_id" not in body  # internal details stay internal
    assert response.headers["location"] == f"/api/files/{body['id']}/download"

    on_disk = TEST_STORAGE_DIR / str(GHOST_USER_ID) / body["id"]
    assert on_disk.read_bytes() == content
    assert stat.S_IMODE(on_disk.stat().st_mode) == 0o600
    assert files_on_disk() == [body["id"]]  # no ".part" left behind

    async with SessionLocal() as session:
        row = await session.get(StoredFile, uuid.UUID(body["id"]))
    assert row.user_id == GHOST_USER_ID
    assert row.file_path == f"{GHOST_USER_ID}/{body['id']}"


async def test_empty_file_is_accepted(client):
    """T-FILE-02: a 0-byte file (e.g. ".gitkeep") is a valid file."""
    response = await upload(client, b"", ".gitkeep")

    assert response.status_code == 201
    assert response.json()["size"] == 0


async def test_mime_type_comes_from_the_extension(client):
    """T-FILE-03: the server decides the type; the client cannot declare one."""
    types = {}
    for name in ("photo.PNG", "Makefile", "backup.tar.gz"):
        types[name] = (await upload(client, b"x", name)).json()["mime_type"]

    assert types == {
        "photo.PNG": "image/png",
        "Makefile": "application/octet-stream",
        "backup.tar.gz": "application/gzip",
    }


async def test_file_name_is_cleaned(client):
    """T-FILE-04: path parts, control and bidi characters are removed; NFC applied."""
    body = (await upload(client, b"x", "../../etc/pass\r\nwd\u202etxt.exe")).json()
    accented = (await upload(client, b"x", "été.txt")).json()

    assert body["filename"] == "passwdtxt.exe"
    assert accented["filename"] == "été.txt"


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({"X-File-Name": "%FF%FE"}, 422),  # not valid UTF-8 once decoded
        ({"X-File-Name": "..%2F"}, 422),  # empty once cleaned
        ({"X-File-Name": "%2E%2E"}, 422),  # ".."
        ({"Content-Type": "text/plain"}, 415),  # not the raw-bytes protocol
    ],
)
async def test_invalid_uploads_are_rejected_without_writing(client, headers, expected_status):
    """T-FILE-05: an invalid request is refused and leaves no trace."""
    response = await upload(client, b"data", "ok.txt", **headers)

    assert response.status_code == expected_status
    assert files_on_disk() == []
    assert await count_rows() == 0


async def test_missing_file_name_header_is_rejected(client):
    """T-FILE-06: X-File-Name is required."""
    response = await client.post(
        "/api/files", content=b"data", headers={"Content-Type": "application/octet-stream"}
    )

    assert response.status_code == 422


async def test_multipart_upload_is_refused(client):
    """T-FILE-07: a classic multipart/form-data upload is refused (415), which
    also means a cross-site HTML form can never create a file (CSRF)."""
    response = await client.post(
        "/api/files", files={"file": ("a.txt", b"x")}, headers={"X-File-Name": "a.txt"}
    )

    assert response.status_code == 415
    assert await count_rows() == 0


async def test_upload_without_content_length_is_refused(client):
    """T-FILE-08: a chunked body (no Content-Length) is refused with 411."""

    async def chunked_body():
        yield b"some bytes"

    response = await client.post(
        "/api/files",
        content=chunked_body(),
        headers={"Content-Type": "application/octet-stream", "X-File-Name": "a.txt"},
    )

    assert response.status_code == 411
    assert files_on_disk() == []


async def test_size_limit_is_enforced_before_reading(client, override_settings):
    """T-FILE-09: a file over NUAGE_MAX_UPLOAD_BYTES gets 413 and nothing is stored;
    a file of exactly the limit is accepted."""
    override_settings(max_upload_bytes=10)

    too_big = await upload(client, b"x" * 11, "big.bin")
    just_right = await upload(client, b"x" * 10, "ok.bin")

    assert too_big.status_code == 413
    assert too_big.json() == {"detail": "File too large: the limit is 10 B."}
    assert just_right.status_code == 201
    assert await count_rows() == 1


async def test_disk_space_reserve_is_enforced(client, override_settings):
    """T-FILE-10: if the upload would eat into the free-space reserve: 507."""
    override_settings(min_free_disk_bytes=10**18)  # an exabyte: never available

    response = await upload(client)

    assert response.status_code == 507
    assert files_on_disk() == []


async def test_database_failure_removes_the_bytes(client, act_as, caplog):
    """T-FILE-11: if the INSERT fails, the file written to disk is deleted
    again (compensation) and the client gets a generic message."""
    act_as(uuid.uuid4())  # a user that does not exist: the foreign key refuses the row

    with caplog.at_level(logging.ERROR):
        response = await upload(client)

    assert response.status_code == 500
    assert response.json() == {"detail": "The file was received but could not be saved. Please try again."}
    assert files_on_disk() == []
    assert "Could not index file" in caplog.text  # details are in the server logs only


# --- Service level: failures in the middle of the stream ------------------------
async def stream(*parts: bytes):
    for part in parts:
        yield part


def file_service(**changes) -> FileService:
    settings = get_settings().model_copy(update=changes)
    return FileService(FileStorage(settings.storage_dir), settings)


async def test_interrupted_upload_leaves_nothing():
    """T-FILE-12: the client disconnecting half-way leaves no file and no row."""

    async def interrupted():
        yield b"the beginning of a large video"
        raise ConnectionResetError("client disconnected")

    async with SessionLocal() as session:
        with pytest.raises(ConnectionResetError):
            await file_service().upload(
                session, GHOST_USER_ID, "video.mp4", interrupted(), declared_size=None
            )

    assert files_on_disk() == []
    assert await count_rows() == 0


async def test_streamed_size_limit_without_content_length():
    """T-FILE-13: the limit also holds while streaming (defence in depth if the
    announced size were missing or wrong): the excess is never written."""
    async with SessionLocal() as session:
        with pytest.raises(UploadTooLargeError):
            await file_service(max_upload_bytes=8).upload(
                session, GHOST_USER_ID, "big.bin", stream(b"12345", b"67890"), declared_size=None
            )

    assert files_on_disk() == []


async def test_truncated_body_is_not_kept():
    """T-FILE-14: fewer bytes than announced = truncated file, which is discarded."""
    async with SessionLocal() as session:
        with pytest.raises(IncompleteUploadError):
            await file_service().upload(session, GHOST_USER_ID, "a.pdf", stream(b"half"), declared_size=1000)

    assert files_on_disk() == []
    assert await count_rows() == 0


# --- List --------------------------------------------------------------------
async def test_list_returns_files_newest_first(client):
    """T-FILE-15: the file manager lists the user's files, newest first."""
    first = (await upload(client, b"1", "a.txt")).json()["id"]
    second = (await upload(client, PNG_BYTES, "b.png")).json()["id"]

    response = await client.get("/api/files")

    assert response.status_code == 200
    files = response.json()
    assert [f["id"] for f in files] == [second, first]
    assert all("file_path" not in f for f in files)
    assert (await client.get("/api/files?limit=0")).status_code == 422
    assert (await client.get("/api/files?limit=1001")).status_code == 422


# --- Download ----------------------------------------------------------------
async def test_download_returns_exact_bytes_with_security_headers(client):
    """T-FILE-16: the exact bytes come back as an attachment, with a UTF-8 name
    (RFC 5987) and the protective headers."""
    content = os.urandom(3 * 1024 * 1024 + 123)  # 3 MiB of binary noise
    file_id = (await upload(client, content, "données été.bin")).json()["id"]

    response = await client.get(f"/api/files/{file_id}/download")

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == (
        "attachment; filename*=utf-8''donn%C3%A9es%20%C3%A9t%C3%A9.bin"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"].endswith("sandbox")


async def test_view_inline_only_for_script_free_types(client):
    """T-FILE-17 / T-SEC-06 (stored XSS): ?inline=true displays images and text,
    but an SVG (which can carry JavaScript) is always downloaded, and HTML is
    shown as plain text, never rendered."""
    png = (await upload(client, PNG_BYTES, "photo.png")).json()["id"]
    svg = (await upload(client, b"<svg onload=alert(1)>", "logo.svg")).json()["id"]
    html = (await upload(client, b"<script>alert(1)</script>", "page.html")).json()["id"]

    png_view = await client.get(f"/api/files/{png}/download?inline=true")
    svg_view = await client.get(f"/api/files/{svg}/download?inline=true")
    html_view = await client.get(f"/api/files/{html}/download?inline=true")

    assert png_view.headers["content-disposition"] == 'inline; filename="photo.png"'
    assert png_view.headers["content-type"] == "image/png"
    assert svg_view.headers["content-disposition"].startswith("attachment")
    assert html_view.headers["content-disposition"].startswith("inline")
    assert html_view.headers["content-type"] == "text/plain; charset=utf-8"
    for response in (png_view, svg_view, html_view):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-security-policy"].endswith("sandbox")


async def test_pdf_view_is_not_sandboxed(client):
    """T-FILE-18: PDFs are viewed without the CSP sandbox (browser PDF viewers
    refuse to run in a sandboxed document) but keep nosniff."""
    file_id = (await upload(client, b"%PDF-1.7", "cours.pdf")).json()["id"]

    response = await client.get(f"/api/files/{file_id}/download?inline=true")

    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline")
    assert "content-security-policy" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_unknown_or_malformed_file_id(client):
    """T-FILE-19: unknown id -> 404; an id that is not a UUID -> 422 before any code runs."""
    assert (await client.get(f"/api/files/{uuid.uuid4()}/download")).status_code == 404
    assert (await client.get("/api/files/1%20OR%201=1/download")).status_code == 422
    assert (await client.get("/api/files/..%2F..%2Fetc%2Fpasswd/download")).status_code == 404


async def test_bytes_missing_on_disk(client, caplog):
    """T-FILE-20: a row whose bytes were deleted behind the app's back (e.g. through
    Samba) gives a clean 404 and an error in the server logs, not a crash."""
    file_id = (await upload(client)).json()["id"]
    (TEST_STORAGE_DIR / str(GHOST_USER_ID) / file_id).unlink()

    with caplog.at_level(logging.ERROR):
        response = await client.get(f"/api/files/{file_id}/download")

    assert response.status_code == 404
    assert "missing on disk" in caplog.text


# --- Path traversal: the defences that do not depend on the UUID file names ----
async def test_database_refuses_paths_leaving_the_storage(client):
    """T-SEC-07 (layer 1, database): even with direct SQL access, a row cannot
    hold an absolute path or "..": the CHECK constraint refuses it."""
    file_id = (await upload(client)).json()["id"]

    for evil in ("/etc/passwd", "../../../etc/passwd", f"{GHOST_USER_ID}/../../../etc/passwd"):
        with pytest.raises(IntegrityError, match="ck_files_path_relative"):
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE files SET file_path = :path WHERE id = :id"), {"path": evil, "id": file_id}
                )


async def test_symlink_to_a_system_file_is_not_served(client, caplog):
    """T-SEC-08 (layer 2, storage): if the bytes of a file were replaced by a
    symbolic link to a system file, the download refuses to follow it."""
    file_id = (await upload(client)).json()["id"]
    stored = TEST_STORAGE_DIR / str(GHOST_USER_ID) / file_id
    stored.unlink()
    stored.symlink_to("/etc/passwd")

    with caplog.at_level(logging.ERROR):
        response = await client.get(f"/api/files/{file_id}/download")

    assert response.status_code == 404
    assert b"root:" not in response.content
    assert "leaves the storage folder" in caplog.text
