"""Alembic runtime environment.

Alembic runs this file for every command (upgrade, downgrade, revision...).
It connects to PostgreSQL with the *owner* role and applies the migration
scripts found in ``migrations/versions/``. Alembic records the current
revision in a ``alembic_version`` table, so it always knows which
migrations are already applied.
"""

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app import models  # noqa: F401  (import registers the tables in Base.metadata)
from app.config import get_settings
from app.database import Base

# Metadata of our ORM models: used by "--autogenerate" to compute the
# difference between models.py and the real database.
target_metadata = Base.metadata


def get_url() -> str:
    # The test suite points Alembic to the test database through this
    # variable; otherwise use the value from .env.
    return os.environ.get("ALEMBIC_DATABASE_URL") or get_settings().migration_database_url


def run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,  # also detect column type changes
    )
    # Each migration runs inside a transaction: PostgreSQL supports
    # transactional DDL, so a migration that fails halfway is fully undone.
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(get_url())
    async with engine.connect() as connection:
        # Alembic's API is synchronous: run_sync executes it on the async connection.
        await connection.run_sync(run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    # "alembic upgrade head --sql": print the SQL instead of executing it
    # (useful to review the DDL or hand it to a DBA).
    context.configure(url=get_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_async_migrations())
