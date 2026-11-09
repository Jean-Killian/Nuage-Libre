"""Database connection layer.

We use SQLAlchemy 2 in *async* mode with the ``asyncpg`` driver. FastAPI is
an asynchronous framework (one event loop serves many requests at the same
time): a blocking database call would freeze every other request while it
waits for PostgreSQL. With ``async``/``await`` the event loop keeps serving
other clients while a query is in flight.

Key objects:
- ``engine``: owns the *connection pool* (a set of already-open TCP
  connections to PostgreSQL that are reused, because opening a connection
  is expensive).
- ``SessionLocal``: a factory producing ``AsyncSession`` objects. A session
  is a short-lived "unit of work": it tracks the objects you load/create and
  sends them to the database inside a transaction.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Parent class of every ORM model (see ``app/models.py``).

    SQLAlchemy collects all subclasses in ``Base.metadata``; Alembic reads
    this metadata to compare the Python models with the real database.
    """


_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    echo=_settings.sql_echo,
    # Check that a pooled connection is still alive before handing it out
    # (PostgreSQL may have been restarted in the meantime).
    pool_pre_ping=True,
)

SessionLocal = async_sessionmaker(
    engine,
    # Keep attribute values readable after commit(); otherwise SQLAlchemy
    # would try to reload them lazily, which is not allowed in async code.
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that provides one session per HTTP request.

    The ``async with`` block guarantees the session (and its connection) is
    returned to the pool when the request ends, even if an exception occurs.
    Routes receive it with ``session: AsyncSession = Depends(get_session)``.
    """
    async with SessionLocal() as session:
        yield session
