"""
Account rows — the ``users`` table from migration 0003 (decision-log Entry 29).

**Context.** An account is a private ``username`` (the login handle, stored
lowercase), a public ``display_name``, an argon2id ``password_hash`` and the
SHA-256 of the current one-time recovery code (``docs/api-contract.md``,
"Sessions" → Identity). This module is the only layer that names those columns.

**How it works.** SQLAlchemy Core against ``app/data/tables.py``, like the other
repositories. What leaves here is a ``UserRecord``, never a ``Row``.

- **No secret comes back.** ``UserRecord`` carries no ``password_hash`` and no
  ``recovery_code_hash``, so a record can be logged, returned or put into a
  response model without dragging a hash along. A read that needs the hash
  (signin) is its own function when it lands.
- **The caller owns the transaction.** ``create`` does not commit. Signup inserts
  the user and its first session together, and one commit keeps them together.
- **It never raises ``ApiError``.** A taken username surfaces as the database's
  ``IntegrityError`` on ``users_username_key``. Turning that into a ``409`` is the
  route's decision, not this module's.

**Related.** ``app/data/repositories/sessions.py`` (rows that point here, deleted
with their user), ``app/core/passwords.py`` (what makes ``password_hash``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.data.tables import users


@dataclass(frozen=True, slots=True)
class UserRecord:
    """
    One account, without its password or recovery-code hash.

    ``username`` is private: it goes only to its owner (``MeOut``), never into a
    response a non-member of a trip can receive.
    """

    id: str
    username: str
    display_name: str
    created_at: datetime


async def create(
    session: AsyncSession,
    *,
    username: str,
    display_name: str,
    password_hash: str,
    recovery_code_hash: str | None,
) -> UserRecord:
    """
    Insert one account with a server-generated UUID4 id. Does not commit.

    ``username`` must already be lowercased and valid (``AccountCreate`` does
    both); the table's CHECK is the backstop. ``password_hash`` is the argon2id
    PHC string from ``app.core.passwords.hash_password``.
    """
    row = (
        await session.execute(
            users.insert()
            .values(
                id=str(uuid.uuid4()),
                username=username,
                display_name=display_name,
                password_hash=password_hash,
                recovery_code_hash=recovery_code_hash,
            )
            .returning(users.c.id, users.c.username, users.c.display_name, users.c.created_at)
        )
    ).one()

    return UserRecord(
        id=row.id,
        username=row.username,
        display_name=row.display_name,
        created_at=row.created_at,
    )
