"""add users and conversation owner

Multi-user foundation ("Ghost User" strategy, see app/auth.py):

- New table ``users`` (id, username UNIQUE, created_at).
- Data: the Ghost User row is inserted. It is the owner of every piece of
  data until authentication exists (v0.6).
- ``conversations.user_id``: NOT NULL foreign key to ``users.id`` with
  ON DELETE CASCADE (deleting an account erases its conversations and, by
  the existing cascade, their messages).
- The index on ``updated_at`` is replaced by ``(user_id, updated_at)``,
  which matches the new sidebar query (WHERE user_id = ? ORDER BY
  updated_at DESC).

Why the generated script was rewritten by hand
-----------------------------------------------
``alembic revision --autogenerate`` produced
``add_column('conversations', Column('user_id', nullable=False))``. That
works on an empty test database but FAILS on the real one, which already
contains conversations: PostgreSQL cannot add a NOT NULL column without a
value for the existing rows. The safe, standard sequence is used instead:

1. add the column as NULLABLE;
2. backfill: give every existing conversation to the Ghost User;
3. switch the column to NOT NULL, then add the foreign key.

All of it runs in one transaction (PostgreSQL has transactional DDL): if any
step fails, the database is left exactly as it was.

No ``server_default`` is kept on ``user_id``: new rows must always state
their owner explicitly ("fail closed").

Revision ID: c3e9a1d4b7f2
Revises: 902d09722b60
Create Date: 2026-11-12 01:16:49.921652
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3e9a1d4b7f2"
down_revision: str | None = "902d09722b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of app.auth.GHOST_USER_ID. A migration never imports
# application code: the code keeps evolving, but this script must replay
# identically forever (on a fresh database, on a backup restore...).
# Test T-USER-01 checks that both values are still equal.
GHOST_USER_ID = uuid.UUID("a644871e-8417-4565-91f6-0813f368ff3c")
GHOST_USERNAME = "ghost"


def upgrade() -> None:
    # --- 1. Users table + the Ghost User -----------------------------------
    users = op.create_table(
        "users",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("username", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    # bulk_insert sends a parameterised INSERT (and renders proper literals
    # in offline "--sql" mode); created_at takes its server default.
    op.bulk_insert(users, [{"id": GHOST_USER_ID, "username": GHOST_USERNAME}])

    # --- 2. Owner column on conversations (nullable -> backfill -> NOT NULL)
    op.add_column("conversations", sa.Column("user_id", sa.UUID(), nullable=True))
    # Lightweight table description: only the column we update, so this
    # data migration does not depend on the current ORM models.
    conversations = sa.table("conversations", sa.column("user_id", sa.UUID()))
    op.execute(conversations.update().values(user_id=GHOST_USER_ID))
    op.alter_column("conversations", "user_id", existing_type=sa.UUID(), nullable=False)
    op.create_foreign_key(
        "fk_conversations_user_id_users",
        "conversations",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # --- 3. Index matching the new "conversations of one user" query --------
    op.drop_index("ix_conversations_updated_at", table_name="conversations")
    op.create_index(
        "ix_conversations_user_id_updated_at", "conversations", ["user_id", "updated_at"], unique=False
    )


def downgrade() -> None:
    # Reverse order of upgrade(). Information lost on downgrade: who owns
    # which conversation (harmless while the Ghost User is the only user).
    op.drop_index("ix_conversations_user_id_updated_at", table_name="conversations")
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"], unique=False)
    op.drop_constraint("fk_conversations_user_id_users", "conversations", type_="foreignkey")
    op.drop_column("conversations", "user_id")
    op.drop_table("users")
