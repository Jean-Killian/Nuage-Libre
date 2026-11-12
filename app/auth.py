"""Identity of the caller: "on behalf of which user is this request made?"

Current mode (v0.5): one single user, the "Ghost User"
------------------------------------------------------
Nuage Libre has no login screen yet (authentication is planned for v0.6).
Instead of writing single-user code now and rewriting it later, every
user-owned row in the database already carries a ``user_id`` column, and
every query already filters on it. While there is no authentication, that
``user_id`` is always the same hard-coded value: the *Ghost User*, a real row
of the ``users`` table created by the Alembic migration ``c3e9a1d4b7f2``.

Why this design (the "seam" pattern)
------------------------------------
Routes never read the constant directly. They declare a dependency::

    user_id: uuid.UUID = Depends(get_current_user_id)

``get_current_user_id`` is therefore the *only* place that decides who the
caller is. When JWT authentication arrives, only the body of this function
changes (read the ``Authorization: Bearer <token>`` header, verify the
signature and expiry, return the user id stored in the token). The routers,
services, repositories and the database schema stay exactly as they are,
because they already work with "a user id" rather than "the only user".

Tests use the same seam: ``app.dependency_overrides[get_current_user_id]``
lets a test act as another user and prove that users cannot see each
other's data (tests/test_users.py).
"""

import uuid
from typing import Final

# Hard-coded on purpose (see module docstring). Generated once with
# uuid.uuid4(). The SAME literal value is frozen in the migration that seeds
# the users table (migrations/versions/..._c3e9a1d4b7f2_...): a migration
# must never import application code, because the code keeps changing while
# a migration must replay identically forever. Test T-USER-01 checks that
# both copies stay equal.
GHOST_USER_ID: Final[uuid.UUID] = uuid.UUID("a644871e-8417-4565-91f6-0813f368ff3c")
GHOST_USERNAME: Final[str] = "ghost"


async def get_current_user_id() -> uuid.UUID:
    """FastAPI dependency: id of the user making the current request.

    v0.5: always the Ghost User. v0.6: decode and verify a JWT instead.

    It is declared ``async`` even though it does not await anything yet:
    FastAPI runs plain ``def`` dependencies in a thread pool, which would
    add a thread hop to every request for no benefit.
    """
    return GHOST_USER_ID
