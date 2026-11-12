"""create files table

Private cloud (v0.5): one row per uploaded file. The bytes themselves are
stored on disk (NUAGE_STORAGE_DIR); this table is their index.

- ``user_id``: owner, NOT NULL foreign key with ON DELETE **RESTRICT**. A
  row is a pointer to a physical file that PostgreSQL cannot delete, so the
  database refuses to drop a user who still owns files instead of silently
  forgetting them (the application deletes the bytes first).
- ``file_path``: path RELATIVE to the storage folder ("<user_id>/<file_id>"),
  UNIQUE (two rows never share the same bytes) and protected by a CHECK
  constraint: no absolute path, no "..". This is a database-level layer of
  the path traversal defence (docs/CLOUD_STORAGE.md §4).
- ``size``: BIGINT (an INTEGER would overflow at 2 GiB), CHECK size >= 0.
- ``is_vectorized``: RAG preparation, false by default (stored only).
- Index ``(user_id, created_at)`` for "the files of user X, newest first".

Generated with ``alembic revision --autogenerate`` and reviewed: a new,
empty table needs no data migration. ``downgrade()`` drops the table but
NOT the physical files, which remain in the storage folder.

Revision ID: d8f2b6e1a5c4
Revises: c3e9a1d4b7f2
Create Date: 2026-11-12 01:17:47.629880
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8f2b6e1a5c4"
down_revision: str | None = "c3e9a1d4b7f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("is_vectorized", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("filename <> ''", name="ck_files_filename_not_empty"),
        sa.CheckConstraint(
            "left(file_path, 1) <> '/' AND strpos(file_path, '..') = 0", name="ck_files_path_relative"
        ),
        sa.CheckConstraint("size >= 0", name="ck_files_size_not_negative"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_files_user_id_users", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_path", name="uq_files_file_path"),
    )
    op.create_index("ix_files_user_id_created_at", "files", ["user_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_files_user_id_created_at", table_name="files")
    op.drop_table("files")
