"""Multi-user foundation: the Ghost User and the isolation between users.

There is no login yet, but the data model and every query are already
multi-user. These tests prove it by impersonating a second user through the
same seam that JWT authentication will use (``get_current_user_id``).
"""

import importlib.util
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth import GHOST_USER_ID, GHOST_USERNAME
from app.database import engine
from tests.conftest import ROOT, create_user, parse_events, upload


def load_ghost_migration():
    """Import the migration that seeds the Ghost User, as a plain module."""
    (path,) = (ROOT / "migrations" / "versions").glob("*-c3e9a1d4b7f2_*.py")
    spec = importlib.util.spec_from_file_location("ghost_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_ghost_user_is_seeded_with_the_hard_coded_id():
    """T-USER-01: the Ghost User row exists, and the id frozen in the migration
    is still the one hard-coded in app/auth.py."""
    async with engine.connect() as conn:
        username = await conn.scalar(text("SELECT username FROM users WHERE id = :id"), {"id": GHOST_USER_ID})

    assert username == GHOST_USERNAME
    assert load_ghost_migration().GHOST_USER_ID == GHOST_USER_ID


async def test_new_data_belongs_to_the_ghost_user(client):
    """T-USER-02: without authentication, conversations and files are owned by the Ghost User."""
    conversation_id = parse_events(await client.post("/api/chat", json={"message": "Hi"}))[0]["id"]
    file_id = (await upload(client)).json()["id"]

    async with engine.connect() as conn:
        conversation_owner = await conn.scalar(
            text("SELECT user_id FROM conversations WHERE id = :id"), {"id": conversation_id}
        )
        file_owner = await conn.scalar(text("SELECT user_id FROM files WHERE id = :id"), {"id": file_id})

    assert conversation_owner == file_owner == GHOST_USER_ID


async def test_users_cannot_reach_each_others_conversations(client, act_as):
    """T-USER-03 / T-SEC-09 (IDOR): another user's conversation cannot be listed,
    read, continued or deleted, even with its exact id. The answer is 404, as
    for an id that does not exist, so its existence is not even revealed."""
    ghost_conversation = parse_events(await client.post("/api/chat", json={"message": "my secret"}))[0]["id"]
    act_as(await create_user("intruder"))

    assert (await client.get("/api/conversations")).json() == []
    assert (await client.get(f"/api/conversations/{ghost_conversation}")).status_code == 404
    continued = await client.post("/api/chat", json={"conversation_id": ghost_conversation, "message": "hi"})
    assert continued.status_code == 404
    assert (await client.delete(f"/api/conversations/{ghost_conversation}")).status_code == 404
    intruder_conversation = parse_events(await client.post("/api/chat", json={"message": "mine"}))[0]["id"]
    assert [c["id"] for c in (await client.get("/api/conversations")).json()] == [intruder_conversation]

    act_as(GHOST_USER_ID)
    # Untouched, and the intruder's own conversation is not visible here either.
    assert [c["id"] for c in (await client.get("/api/conversations")).json()] == [ghost_conversation]


async def test_users_cannot_reach_each_others_files(client, act_as):
    """T-USER-04 / T-SEC-09 (IDOR): another user's file is neither listed nor downloadable."""
    file_id = (await upload(client, b"private diary", "diary.txt")).json()["id"]
    act_as(await create_user("intruder"))

    assert (await client.get("/api/files")).json() == []
    response = await client.get(f"/api/files/{file_id}/download")
    assert response.status_code == 404
    assert b"private diary" not in response.content


async def test_database_refuses_data_without_owner():
    """T-USER-05 ("fail closed"): a conversation inserted without an owner is
    refused by PostgreSQL itself (NOT NULL, no default value)."""
    with pytest.raises(IntegrityError, match="user_id"):
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO conversations (title, model) VALUES ('t', 'm')"))


async def test_deleting_a_user_cascades_conversations_but_protects_files(client, act_as):
    """T-USER-06: deleting a user erases their conversations and messages
    (ON DELETE CASCADE), but is refused while they still own files
    (ON DELETE RESTRICT): their bytes on disk must be deleted first."""
    leaver = await create_user("leaver")
    act_as(leaver)
    await client.post("/api/chat", json={"message": "bye"})

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": leaver})
        assert await conn.scalar(text("SELECT count(*) FROM conversations")) == 0
        assert await conn.scalar(text("SELECT count(*) FROM messages")) == 0

    hoarder = await create_user("hoarder")
    act_as(hoarder)
    await upload(client)
    with pytest.raises(IntegrityError, match="fk_files_user_id_users"):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": hoarder})


async def test_usernames_are_unique():
    """T-USER-07: two accounts can never share a username (UNIQUE constraint)."""
    with pytest.raises(IntegrityError, match="uq_users_username"):
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO users (id, username) VALUES (:id, 'ghost')"), {"id": uuid.uuid4()}
            )
