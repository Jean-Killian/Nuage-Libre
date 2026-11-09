"""add model to messages

Since v0.3 the user can switch models in the middle of a conversation, so the
model is now recorded on each assistant message, not only on the conversation.

- Schema change: new nullable column ``messages.model`` (NULL for user messages).
- Data migration: existing assistant answers were all produced by the model
  stored on their conversation, so that value is copied over. Without this
  backfill, old answers would show no model at all.

Revision ID: 902d09722b60
Revises: f7179d8256ea
Create Date: 2026-11-09 11:45:56.385028
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "902d09722b60"
down_revision: str | None = "f7179d8256ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("model", sa.String(length=255), nullable=True))
    # Backfill: UPDATE ... FROM is PostgreSQL's join syntax for updates.
    op.execute(
        """
        UPDATE messages AS m
        SET model = c.model
        FROM conversations AS c
        WHERE m.conversation_id = c.id AND m.role = 'assistant'
        """
    )


def downgrade() -> None:
    op.drop_column("messages", "model")
