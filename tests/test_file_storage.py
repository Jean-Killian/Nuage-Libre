"""Unit tests of FileStorage: path containment and atomic writes.

Each test uses pytest's ``tmp_path`` (a fresh, empty folder), so it runs
without the database and without the application.
"""

import os
import stat
import uuid

import pytest

from app.repositories.file_storage import FileStorage, UnsafePathError


async def chunks_of(*parts: bytes):
    for part in parts:
        yield part


@pytest.mark.parametrize(
    "evil",
    [
        "/etc/passwd",  # absolute path: would replace the root when joined
        "../../../etc/passwd",  # climbs out of the root
        "user/../../outside",  # climbs out after a legitimate-looking part
        "",  # the root itself is not a file
        ".",
    ],
)
def test_resolve_refuses_paths_outside_the_root(tmp_path, evil):
    """T-STO-01 / T-SEC-08: no relative path can designate a file outside the storage."""
    storage = FileStorage(tmp_path / "root")

    with pytest.raises(UnsafePathError):
        storage.resolve(evil)


def test_resolve_refuses_a_symlink_escaping_the_root(tmp_path):
    """T-STO-02 / T-SEC-08: a symbolic link planted in the storage cannot leak a system file."""
    root = tmp_path / "root"
    (root / "user").mkdir(parents=True)
    (root / "user" / "trap").symlink_to("/etc/passwd")
    storage = FileStorage(root)

    with pytest.raises(UnsafePathError):
        storage.resolve("user/trap")


def test_resolve_accepts_the_normal_layout(tmp_path):
    """T-STO-03: "<user_id>/<file_id>" resolves inside the root."""
    storage = FileStorage(tmp_path)
    relative = storage.relative_path_for(uuid.uuid4(), uuid.uuid4())

    assert storage.resolve(relative).parent.parent == tmp_path.resolve()


async def test_save_is_atomic_and_private(tmp_path):
    """T-STO-04: bytes are re-assembled exactly, with owner-only permissions,
    and no temporary ".part" file remains."""
    storage = FileStorage(tmp_path)
    relative = storage.relative_path_for(uuid.uuid4(), uuid.uuid4())
    # 3 MiB in 3000 small chunks: exercises the 1 MiB write buffer.
    parts = [os.urandom(1024 + i % 7) for i in range(3000)]

    written = await storage.save(relative, chunks_of(*parts))

    final = storage.resolve(relative)
    assert written == sum(len(p) for p in parts)
    assert final.read_bytes() == b"".join(parts)
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert stat.S_IMODE(final.parent.stat().st_mode) == 0o700
    assert [p.name for p in final.parent.iterdir()] == [final.name]


async def test_failed_save_leaves_nothing_on_disk(tmp_path):
    """T-STO-05: if the stream fails half-way (client gone), no partial file remains."""
    storage = FileStorage(tmp_path)
    relative = storage.relative_path_for(uuid.uuid4(), uuid.uuid4())

    async def broken_stream():
        yield b"first half of the file"
        raise ConnectionResetError("client disconnected")

    with pytest.raises(ConnectionResetError):
        await storage.save(relative, broken_stream())

    folder = storage.resolve(relative).parent
    assert list(folder.iterdir()) == []


async def test_free_bytes_works_before_the_root_exists(tmp_path):
    """T-STO-06: disk space can be measured before the first upload creates the root."""
    storage = FileStorage(tmp_path / "not" / "created" / "yet")

    assert await storage.free_bytes() > 0
    assert not (tmp_path / "not").exists()  # measuring has no side effect
