"""Shared test fixtures (pytest loads this file automatically).

Test strategy
-------------
- Integration tests: requests go through the real FastAPI app, the real
  service and repository layers, and a real PostgreSQL database
  (``nuage_libre_test``, separate from the real data).
- The only thing replaced is Ollama, by ``FakeOllama``: tests run without a
  GPU, in milliseconds, and always get the same answer (deterministic).
- Uploaded files go to a temporary folder created for the test session,
  never to the real storage folder on the Lexar disk.
- Every test starts with empty tables and an empty storage folder, so tests
  never depend on each other.

Run:  venv/bin/pytest -v
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
_env = dotenv_values(ROOT / ".env")

# IMPORTANT: this must run BEFORE any "app" module is imported, because
# app/database.py creates the engine from NUAGE_DATABASE_URL at import time.
# We point the application at the test database, never the real one.
TEST_DB_URL = os.environ.get("NUAGE_TEST_DATABASE_URL") or _env.get("NUAGE_TEST_DATABASE_URL")
TEST_MIGRATION_URL = os.environ.get("NUAGE_TEST_MIGRATION_DATABASE_URL") or _env.get(
    "NUAGE_TEST_MIGRATION_DATABASE_URL"
)
if not TEST_DB_URL or not TEST_MIGRATION_URL:
    raise RuntimeError("Set NUAGE_TEST_DATABASE_URL and NUAGE_TEST_MIGRATION_DATABASE_URL (see .env.example)")
if not TEST_DB_URL.rsplit("/", 1)[-1].endswith("_test"):
    # Safety net: refuse to run (and wipe tables) on a non-test database.
    raise RuntimeError(f"Refusing to run tests on a database not ending in '_test': {TEST_DB_URL}")
os.environ["NUAGE_DATABASE_URL"] = TEST_DB_URL
# Same idea for the private cloud: a fresh temporary folder (removed at the
# end of the session), so tests can never write to, or wipe, real files.
TEST_STORAGE_DIR = Path(tempfile.mkdtemp(prefix="nuage-libre-test-storage-"))
os.environ["NUAGE_STORAGE_DIR"] = str(TEST_STORAGE_DIR)

import httpx  # noqa: E402  (imports below must come after the environment setup)
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.auth import GHOST_USER_ID, get_current_user_id  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import engine  # noqa: E402
from app.dependencies import get_ollama_client  # noqa: E402
from app.main import app  # noqa: E402
from app.services.ollama_client import ChatChunk, ModelInfo, OllamaError  # noqa: E402


class FakeOllama:
    """Stand-in for OllamaClient with the same public methods.

    - ``chunks``: what the "model" will stream back.
    - ``error``: if set, ``stream_chat`` raises it (simulates Ollama down / OOM).
    - ``calls``: every prompt received, so tests can check what was sent.
    """

    def __init__(self) -> None:
        self.chunks = [
            ChatChunk(thinking="Let me think."),
            ChatChunk(content="Hello "),
            ChatChunk(content="Killian!"),
            ChatChunk(done=True, token_count=3, total_ms=150),
        ]
        self.error: OllamaError | None = None
        self.calls: list[dict] = []
        # Installed models: the configured default, a small one, and an
        # embedding-only model that must never appear in the chat menu.
        self.models = [
            ModelInfo(
                get_settings().model, 17_000_000_000, "qwen35", "26.9B", "Q4_K_M", ("completion", "thinking")
            ),
            ModelInfo("qwen3:4b", 2_500_000_000, "qwen3", "4.0B", "Q4_K_M", ("completion", "thinking")),
            ModelInfo("nomic-embed-text:latest", 270_000_000, "nomic-bert", "137M", "F16", ("embedding",)),
        ]

    async def list_models(self) -> list[ModelInfo]:
        if self.error:
            raise self.error
        return self.models

    async def version(self) -> str:
        return "fake-1.0"

    async def stream_chat(self, model, messages, num_ctx):
        self.calls.append({"model": model, "messages": messages, "num_ctx": num_ctx})
        if self.error:
            raise self.error
        for chunk in self.chunks:
            yield chunk


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    """Create/upgrade the test schema once, with the real Alembic migrations.

    Running the migrations (rather than Base.metadata.create_all) also tests
    that the migration scripts themselves are correct.
    """
    os.environ["ALEMBIC_DATABASE_URL"] = TEST_MIGRATION_URL
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    yield
    shutil.rmtree(TEST_STORAGE_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
async def clean_tables():
    """Reset the database before each test.

    Order matters because of the foreign keys: files first (ON DELETE
    RESTRICT would refuse to delete their owner or their folder), then
    folders (sub-folders follow by CASCADE), then conversations (messages
    follow by CASCADE), then every user except the Ghost User, who is part
    of the schema (seeded by the migration).
    """
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM files"))
        await conn.execute(text("DELETE FROM folders"))
        await conn.execute(text("DELETE FROM conversations"))
        await conn.execute(text("DELETE FROM users WHERE id <> :ghost"), {"ghost": GHOST_USER_ID})
    yield


@pytest.fixture(autouse=True)
def clean_storage():
    """Empty the temporary storage folder before each test (a plain, sync
    fixture: blocking file operations do not belong in async code)."""
    for child in TEST_STORAGE_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    yield


@pytest.fixture
def fake_ollama():
    fake = FakeOllama()
    app.dependency_overrides[get_ollama_client] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def override_settings():
    """Change settings for one test: ``override_settings(max_history_messages=3)``."""

    def _override(**changes):
        settings = get_settings().model_copy(update=changes)
        app.dependency_overrides[get_settings] = lambda: settings
        return settings

    yield _override
    app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
async def client(fake_ollama):
    """HTTP client calling the app in-process (no network, no running server)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def parse_events(response: httpx.Response) -> list[dict]:
    """Split an NDJSON response body into a list of event dicts."""
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


@pytest.fixture
def act_as():
    """Make the API believe the caller is another user: ``act_as(user_id)``.

    It replaces ``get_current_user_id`` exactly as JWT authentication will
    in v0.6, which proves the routes rely on that single seam.
    """

    def _act_as(user_id):
        app.dependency_overrides[get_current_user_id] = lambda: user_id

    yield _act_as
    app.dependency_overrides.pop(get_current_user_id, None)


async def create_user(username: str):
    """Insert a second user directly in the database and return its id."""
    async with engine.begin() as conn:
        return await conn.scalar(
            text("INSERT INTO users (username) VALUES (:name) RETURNING id"), {"name": username}
        )


async def upload(client, content: bytes = b"hello cloud", name: str = "notes.txt", folder_id=None, **headers):
    """Upload ``content`` the way the web UI does (raw body + X-File-Name),
    at the root of the cloud or into ``folder_id``."""
    from urllib.parse import quote

    return await client.post(
        "/api/files",
        content=content,
        params={"folder_id": str(folder_id)} if folder_id else None,
        headers={"Content-Type": "application/octet-stream", "X-File-Name": quote(name), **headers},
    )


async def create_folder(client, name: str, parent_id=None, **extra):
    """Create a folder through the API and return the response."""
    body = {"name": name, "parent_id": str(parent_id) if parent_id else None, **extra}
    return await client.post("/api/folders", json=body)
