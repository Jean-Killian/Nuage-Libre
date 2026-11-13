"""add folders

Private cloud (v0.5.1): folders to organise the files.

- New table ``folders``: an *adjacency list* (each folder points to its
  parent, NULL = at the root of the cloud). Folders are purely logical:
  they exist only in the database, the bytes of the files stay at
  "<user_id>/<file_id>" on disk, so nothing changes in the storage folder.
- ``uq_folders_user_id_parent_id_name`` uses NULLS NOT DISTINCT
  (PostgreSQL 15+): without it, two folders with the same name could
  coexist at the ROOT, because NULL parent ids are "all different".
- Composite foreign keys ``(parent_id, user_id)`` and, on ``files``,
  ``(folder_id, user_id)`` reference ``folders (id, user_id)``: PostgreSQL
  itself guarantees that a folder or a file can only be placed in a folder
  of the SAME user. ``uq_folders_id_user_id`` exists to be their target.
- ``files.folder_id`` is nullable and has no default: every existing file
  simply stays at the root (NULL), so no data migration is needed.
- The index ``(user_id, created_at)`` of ``files`` is replaced by
  ``(user_id, folder_id, created_at)``, which matches the new listing query
  ("files of user X in folder Y, newest first"). The new index is created
  before the old one is dropped (it all runs in one transaction anyway).

Generated with ``alembic revision --autogenerate`` and reviewed (statements
reordered and commented; nothing else needed changing).

Revision ID: 6bb4819d7897
Revises: d8f2b6e1a5c4
Create Date: 2026-11-12 14:43:12.065257
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6bb4819d7897"
down_revision: str | None = "d8f2b6e1a5c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "folders",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("name <> ''", name="ck_folders_name_not_empty"),
        sa.CheckConstraint("parent_id <> id", name="ck_folders_not_own_parent"),
        sa.ForeignKeyConstraint(
            ["parent_id", "user_id"],
            ["folders.id", "folders.user_id"],
            name="fk_folders_parent_id_folders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_folders_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "user_id", name="uq_folders_id_user_id"),
        sa.UniqueConstraint(
            "user_id",
            "parent_id",
            "name",
            name="uq_folders_user_id_parent_id_name",
            postgresql_nulls_not_distinct=True,
        ),
    )

    op.add_column("files", sa.Column("folder_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_files_folder_id_folders",
        "files",
        "folders",
        ["folder_id", "user_id"],
        ["id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_files_user_id_folder_id_created_at", "files", ["user_id", "folder_id", "created_at"], unique=False
    )
    op.drop_index("ix_files_user_id_created_at", table_name="files")


def downgrade() -> None:
    # Files placed in folders go back to the root: dropping the column only
    # forgets the folder they were shown in, never the files themselves.
    op.create_index("ix_files_user_id_created_at", "files", ["user_id", "created_at"], unique=False)
    op.drop_index("ix_files_user_id_folder_id_created_at", table_name="files")
    op.drop_constraint("fk_files_folder_id_folders", "files", type_="foreignkey")
    op.drop_column("files", "folder_id")
    op.drop_table("folders")
