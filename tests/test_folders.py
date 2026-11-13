"""Integration tests of the folders of the private cloud: /api/folders,
the folder_id parameter of /api/files, and the database constraints that
keep the tree consistent.

IDs (T-FOLD-xx, T-SEC-xx) match docs/TEST_PLAN.md.
"""

import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth import GHOST_USER_ID
from app.database import engine
from app.services.file_policy import MAX_FOLDER_DEPTH
from tests.conftest import TEST_STORAGE_DIR, create_folder, create_user, upload


async def make_tree(client, *names, parent_id=None) -> list[dict]:
    """Create nested folders names[0] / names[1] / ... and return them."""
    created = []
    for name in names:
        response = await create_folder(client, name, parent_id)
        assert response.status_code == 201, response.text
        created.append(response.json())
        parent_id = created[-1]["id"]
    return created


# --- Create and read -------------------------------------------------------
async def test_create_folder_at_the_root(client):
    """T-FOLD-01: 201 Created, a Location header, and no user_id exposed."""
    response = await create_folder(client, "Courses")

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Courses"
    assert body["parent_id"] is None
    assert "user_id" not in body
    assert response.headers["location"] == f"/api/folders/{body['id']}"
    # Folders are logical only: creating one writes nothing on disk.
    assert list(TEST_STORAGE_DIR.iterdir()) == []


async def test_folder_detail_gives_its_path(client):
    """T-FOLD-02: the path (breadcrumb) goes from the root to the folder itself."""
    courses, year, maths = await make_tree(client, "Courses", "2026", "Maths")

    response = await client.get(f"/api/folders/{maths['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["parent_id"] == year["id"]
    assert [step["name"] for step in body["path"]] == ["Courses", "2026", "Maths"]
    assert [step["id"] for step in body["path"]] == [courses["id"], year["id"], maths["id"]]


async def test_list_sub_folders_by_name(client):
    """T-FOLD-03: the root lists only its own children, sorted by name as a
    person expects (the en_US collation ignores case)."""
    for name in ("banana", "Apple", "cherry"):
        await create_folder(client, name)
    (apple,) = [f for f in (await client.get("/api/folders")).json() if f["name"] == "Apple"]
    await create_folder(client, "Inside apple", apple["id"])

    root = (await client.get("/api/folders")).json()
    inside = (await client.get("/api/folders", params={"parent_id": apple["id"]})).json()

    assert [f["name"] for f in root] == ["Apple", "banana", "cherry"]
    assert [f["name"] for f in inside] == ["Inside apple"]


# --- Names -------------------------------------------------------------------
async def test_duplicate_sibling_names_are_refused(client):
    """T-FOLD-04: two folders side by side cannot share a name, AT THE ROOT
    too (UNIQUE ... NULLS NOT DISTINCT); the same name elsewhere is fine."""
    (photos,) = await make_tree(client, "Photos")

    duplicate_at_root = await create_folder(client, "Photos")
    same_name_inside = await create_folder(client, "Photos", photos["id"])

    assert duplicate_at_root.status_code == 409
    assert duplicate_at_root.json()["detail"] == "A folder with this name already exists here."
    assert same_name_inside.status_code == 201


async def test_rename_on_conflict_keeps_both(client):
    """T-FOLD-05: on_conflict = "rename" (used for uploaded folders) picks the
    next free name instead of merging into the existing folder."""
    await create_folder(client, "Photos")

    second = await create_folder(client, "Photos", on_conflict="rename")
    third = await create_folder(client, "Photos", on_conflict="rename")

    assert (second.status_code, second.json()["name"]) == (201, "Photos (2)")
    assert (third.status_code, third.json()["name"]) == (201, "Photos (3)")


@pytest.mark.parametrize(
    ("name", "expected_status", "expected_name"),
    [
        ("  Projects  ", 201, "Projects"),  # spaces around are stripped
        ("Courś", 201, "Courś"),  # NFC: s + combining acute = one character
        ("Invoice‮exe", 201, "Invoiceexe"),  # bidi override removed
        ("   ", 422, None),
        ("..", 422, None),
        ("Courses/2026", 422, None),  # refused, not silently cut
        ("Courses\\2026", 422, None),
        ("x" * 256, 422, None),
    ],
)
async def test_folder_names_are_cleaned_or_refused(client, name, expected_status, expected_name):
    """T-FOLD-06: the same hygiene as file names; separators are refused."""
    response = await create_folder(client, name)

    assert response.status_code == expected_status, response.text
    if expected_name is not None:
        assert response.json()["name"] == expected_name


async def test_depth_is_limited(client):
    """T-FOLD-07: folders cannot be nested deeper than MAX_FOLDER_DEPTH."""
    tree = await make_tree(client, *[f"level {n}" for n in range(1, MAX_FOLDER_DEPTH + 1)])

    too_deep = await create_folder(client, "one more", tree[-1]["id"])

    assert len(tree) == MAX_FOLDER_DEPTH
    assert too_deep.status_code == 422
    assert str(MAX_FOLDER_DEPTH) in too_deep.json()["detail"]


# --- Unknown and foreign folders (IDOR) --------------------------------------
async def test_unknown_parent_is_not_found(client):
    """T-FOLD-08: a folder can only be created in an existing parent."""
    response = await create_folder(client, "Lost", uuid.uuid4())

    assert response.status_code == 404
    assert (await client.get("/api/folders")).json() == []


async def test_users_cannot_reach_each_others_folders(client, act_as):
    """T-FOLD-09 / T-SEC-10 (IDOR): another user's folder cannot be read, listed,
    used as a parent, listed for files, or receive an upload: always 404, and
    nothing is created."""
    (private,) = await make_tree(client, "Private")
    other = await create_user("mallory")
    act_as(other)

    assert (await client.get(f"/api/folders/{private['id']}")).status_code == 404
    assert (await client.get("/api/folders", params={"parent_id": private["id"]})).status_code == 404
    assert (await create_folder(client, "Intruder", private["id"])).status_code == 404
    assert (await client.get("/api/files", params={"folder_id": private["id"]})).status_code == 404
    assert (await upload(client, b"x", "planted.txt", folder_id=private["id"])).status_code == 404
    # The other user has their own, empty, root: the same name is free there.
    assert (await client.get("/api/folders")).json() == []
    assert (await create_folder(client, "Private")).status_code == 201

    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM files")) == 0
    assert list(TEST_STORAGE_DIR.rglob("*.txt")) == []


# --- Files in folders ----------------------------------------------------------
async def test_upload_into_a_folder(client):
    """T-FOLD-10: a file uploaded into a folder is listed there and not at the
    root; on disk it is still stored at <user_id>/<file_id> (folders are
    logical only, the folder name never reaches the disk)."""
    (courses,) = await make_tree(client, "Courses")

    response = await upload(client, b"notes", "notes.md", folder_id=courses["id"])
    body = response.json()

    assert response.status_code == 201
    assert body["folder_id"] == courses["id"]
    in_folder = (await client.get("/api/files", params={"folder_id": courses["id"]})).json()
    at_root = (await client.get("/api/files")).json()
    assert [f["id"] for f in in_folder] == [body["id"]]
    assert at_root == []
    assert (TEST_STORAGE_DIR / str(GHOST_USER_ID) / body["id"]).read_bytes() == b"notes"


async def test_upload_into_an_unknown_folder_reads_nothing(client):
    """T-FOLD-11: the destination is checked BEFORE the body is read: 404, no
    file on disk, no row."""
    response = await upload(client, b"x" * 1000, "data.bin", folder_id=uuid.uuid4())

    assert response.status_code == 404
    assert response.json()["detail"] == "Destination folder not found."
    assert [p for p in TEST_STORAGE_DIR.rglob("*") if p.is_file()] == []


async def test_summary_counts_the_whole_cloud(client):
    """T-FOLD-12: the summary covers every folder, while a listing shows one."""
    (courses, year) = await make_tree(client, "Courses", "2026")
    await upload(client, b"12345", "a.txt")
    await upload(client, b"123", "b.txt", folder_id=courses["id"])
    await upload(client, b"1", "c.txt", folder_id=year["id"])

    summary = (await client.get("/api/files/summary")).json()

    assert summary == {"files": 3, "folders": 2, "total_size": 9, "in_ai_memory": 0}


async def test_summary_of_an_empty_cloud(client):
    """T-FOLD-13: an empty cloud has zero everything (no NULL from SUM)."""
    assert (await client.get("/api/files/summary")).json() == {
        "files": 0,
        "folders": 0,
        "total_size": 0,
        "in_ai_memory": 0,
    }


# --- Database guarantees ---------------------------------------------------------
async def test_database_keeps_trees_within_one_user(client):
    """T-SEC-11 (composite foreign keys): even with direct SQL access, a file or
    a folder cannot be placed in a folder that belongs to another user."""
    (private,) = await make_tree(client, "Private")
    other = await create_user("mallory")

    with pytest.raises(IntegrityError, match="fk_folders_parent_id_folders"):
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO folders (user_id, parent_id, name) VALUES (:user, :parent, 'x')"),
                {"user": other, "parent": private["id"]},
            )
    with pytest.raises(IntegrityError, match="fk_files_folder_id_folders"):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO files (user_id, folder_id, filename, file_path, mime_type, size)"
                    " VALUES (:user, :folder, 'x', :path, 'text/plain', 1)"
                ),
                {"user": other, "folder": private["id"], "path": f"{other}/{uuid.uuid4()}"},
            )


async def test_folder_holding_files_cannot_be_deleted(client):
    """T-FOLD-14: deleting an empty tree removes its sub-folders (CASCADE), but a
    folder that contains a file, even deep down, is protected (RESTRICT): rows
    pointing to bytes on disk are never silently forgotten."""
    empty_top, _ = await make_tree(client, "Empty", "Empty child")
    full_top, full_child = await make_tree(client, "Full", "Full child")
    await upload(client, b"x", "kept.txt", folder_id=full_child["id"])

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM folders WHERE id = :id"), {"id": empty_top["id"]})
    with pytest.raises(IntegrityError, match="fk_files_folder_id_folders"):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM folders WHERE id = :id"), {"id": full_top["id"]})

    names = [f["name"] for f in (await client.get("/api/folders")).json()]
    assert names == ["Full"]


async def test_cycle_made_by_hand_cannot_hang_the_server(client):
    """T-SEC-12: the path query is bounded. A cycle cannot be created through
    the API, but if someone made one directly in the database (A inside B
    inside A), reading the folder still answers at once."""
    a, b = await make_tree(client, "A", "B")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE folders SET parent_id = :b WHERE id = :a"), {"a": a["id"], "b": b["id"]}
        )

    started = time.perf_counter()
    response = await client.get(f"/api/folders/{b['id']}")

    assert response.status_code == 200
    assert len(response.json()["path"]) == MAX_FOLDER_DEPTH
    assert time.perf_counter() - started < 2


async def test_folder_cannot_be_its_own_parent(client):
    """T-FOLD-15: the smallest cycle is refused by a CHECK constraint."""
    (a,) = await make_tree(client, "A")

    with pytest.raises(IntegrityError, match="ck_folders_not_own_parent"):
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE folders SET parent_id = id WHERE id = :a"), {"a": a["id"]})


async def test_deleting_a_user_deletes_their_folders(client, act_as):
    """T-FOLD-16: folders are metadata only, so they follow their owner
    (ON DELETE CASCADE), unlike files."""
    other = await create_user("leaving")
    act_as(other)
    await make_tree(client, "Mine", "Sub")

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": other})
        remaining = await conn.scalar(text("SELECT count(*) FROM folders"))

    assert remaining == 0


async def test_database_refuses_duplicate_names_at_the_root(client):
    """T-FOLD-17 (UNIQUE NULLS NOT DISTINCT): the database itself refuses two
    folders with the same name at the root, where parent_id is NULL. With a
    plain UNIQUE, NULLs are "all different" and the duplicate would pass."""
    await create_folder(client, "Photos")

    with pytest.raises(IntegrityError, match="uq_folders_user_id_parent_id_name"):
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO folders (user_id, parent_id, name) VALUES (:user, NULL, 'Photos')"),
                {"user": GHOST_USER_ID},
            )


async def test_concurrent_creation_is_settled_by_the_database(client, monkeypatch):
    """T-FOLD-18: two requests racing for the same name both pass the service's
    check; the UNIQUE constraint stops the second INSERT, reported as 409 (not
    500, and never a duplicate)."""
    from app.repositories.folder_repository import FolderRepository

    await create_folder(client, "Photos")

    async def nothing_taken(self, user_id, parent_id, prefix):
        return set()  # what a request that checked just before the other one sees

    monkeypatch.setattr(FolderRepository, "names_starting_with", nothing_taken)
    response = await create_folder(client, "Photos")

    assert response.status_code == 409
    assert [f["name"] for f in (await client.get("/api/folders")).json()] == ["Photos"]


async def test_folder_creation_needs_a_json_request(client):
    """T-SEC-13 (CSRF): another website can only send "simple" requests without
    the browser asking for permission first (CORS preflight), e.g. a hidden
    form posting text/plain. Such a body is not read as JSON: 422, nothing
    created. Only a real application/json request (impossible cross-site
    without a preflight) creates a folder."""
    response = await client.post(
        "/api/folders",
        content=b'{"name": "Planted by another site"}',
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 422
    assert (await client.get("/api/folders")).json() == []
