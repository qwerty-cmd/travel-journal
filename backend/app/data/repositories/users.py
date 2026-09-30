"""
Account rows — the ``users`` table from migration 0003 (decision-log Entry 29).

**Context.** An account is a private ``username`` (the login handle, stored
lowercase), a public ``display_name``, an argon2id ``password_hash`` and the
SHA-256 of the current one-time recovery code (``docs/api-contract.md``,
"Sessions" → Identity). This module is the only layer that names those columns.

**How it works.** SQLAlchemy Core against ``app/data/tables.py``, like the other
repositories. What leaves here is a ``UserRecord``, never a ``Row``.

- **No secret comes back by accident.** ``UserRecord`` carries no
  ``password_hash`` and no ``recovery_code_hash``, so a record can be logged,
  returned or put into a response model without dragging a hash along. The one
  read that needs the password hash (signin) is its own function,
  ``get_credentials_by_username``, returning a ``UserCredentials`` whose hash is
  left out of its ``repr``.
- **The caller owns the transaction.** Nothing here commits. Signup inserts
  the user and its first session together, and signin resets the lockout
  alongside its session work; one commit keeps each together.
- **It never raises ``ApiError``.** A taken username surfaces as
  ``UsernameTakenError``, translated here from the database's unique violation
  on ``users_username_key`` so no route has to import SQLAlchemy to recognise
  it. Turning that into a ``409`` is the route's decision, not this module's.
- **The lockout is a set of conditional single-row ``UPDATE`` statements**, so it holds
  under concurrency without a row lock held across argon2
  (``docs/api-contract.md``, "Rate limits and lockout"):

  - ``reserve_login_attempt`` claims a password check *before* it runs, by
    counting it in ``failed_logins``, only while the account is unlocked and
    under the threshold. At most ``threshold`` checks can be claimed between
    two locks, however many requests race.
  - ``lock_if_saturated`` sets ``locked_until`` and resets the counter once the
    threshold is counted, and never touches a lock already in force.
  - ``reset_lockout`` (a success) clears the counter only while no lock is in
    force, so it can't wipe a lock a concurrent failure set.

  Postgres re-checks a row's ``WHERE`` against the newest committed version
  when a concurrent transaction changed it first, so each statement sees the
  others' effects. Because the state is in Postgres, a lock survives a restart.

**Related.** ``app/data/repositories/sessions.py`` (rows that point here, deleted
with their user), ``app/core/passwords.py`` (what makes ``password_hash``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import ColumnElement, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.tables import users

# The unique constraint behind "that username is taken" (migration 0003).
_USERNAME_UNIQUE_CONSTRAINT = "users_username_key"


class UsernameTakenError(Exception):
    """``create`` was given a username another account already has."""


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


@dataclass(frozen=True, slots=True)
class UserCredentials:
    """
    One account as signin needs it: identity, password hash and lockout state.

    ``password_hash`` is excluded from ``repr``, so logging or printing a record
    doesn't write the hash out. No recovery-code hash: signin never needs it.
    """

    id: str
    username: str
    display_name: str
    created_at: datetime
    password_hash: str = field(repr=False)
    locked_until: datetime | None
    disabled_at: datetime | None


def _is_username_taken(exc: IntegrityError) -> bool:
    """Whether ``exc`` is the unique violation on ``users_username_key``."""
    # asyncpg's exception, wrapped by SQLAlchemy's DBAPI adapter, names the
    # constraint it violated.
    cause = getattr(exc.orig, "__cause__", None)
    return getattr(cause, "constraint_name", None) == _USERNAME_UNIQUE_CONSTRAINT


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

    Raises ``UsernameTakenError`` if the username is already in use. The
    transaction is then aborted, and the caller must roll it back.
    """
    try:
        result = await session.execute(
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
    except IntegrityError as exc:
        if _is_username_taken(exc):
            raise UsernameTakenError from None
        raise
    row = result.one()

    return UserRecord(
        id=row.id,
        username=row.username,
        display_name=row.display_name,
        created_at=row.created_at,
    )


async def get_credentials_by_username(
    session: AsyncSession, username: str
) -> UserCredentials | None:
    """
    The account stored under ``username`` (already lowercased), or ``None``.

    Disabled accounts are returned too: the route decides what a disabled
    account's signin answers.
    """
    row = (
        await session.execute(
            select(
                users.c.id,
                users.c.username,
                users.c.display_name,
                users.c.created_at,
                users.c.password_hash,
                users.c.locked_until,
                users.c.disabled_at,
            ).where(users.c.username == username)
        )
    ).first()

    if row is None:
        return None

    return UserCredentials(
        id=row.id,
        username=row.username,
        display_name=row.display_name,
        created_at=row.created_at,
        password_hash=row.password_hash,
        locked_until=row.locked_until,
        disabled_at=row.disabled_at,
    )


def _not_locked(now: datetime) -> ColumnElement[bool]:
    """The account has no lock in force at ``now`` ("locked" is ``now < locked_until``)."""
    return or_(users.c.locked_until.is_(None), users.c.locked_until <= now)


async def reserve_login_attempt(
    session: AsyncSession, user_id: str, *, threshold: int, now: datetime
) -> bool:
    """
    Claim one password check for ``user_id`` before it runs. Does not commit.

    Counts the attempt as a failure up front: ``failed_logins`` goes up by one,
    but only while the account is unlocked at ``now`` and fewer than
    ``threshold`` attempts are counted. True if the attempt was claimed, False
    if the account is locked or ``threshold`` attempts are already counted (some
    of them may still be in flight). A success hands its claim back through
    ``reset_lockout``; a failure keeps it.

    Because the claim is one conditional ``UPDATE``, concurrent callers can't
    claim more than ``threshold`` checks between two locks.
    """
    claimed = await session.execute(
        update(users)
        .where(users.c.id == user_id, _not_locked(now), users.c.failed_logins < threshold)
        .values(failed_logins=users.c.failed_logins + 1)
        .returning(users.c.id)
    )
    return claimed.first() is not None


async def lock_if_saturated(
    session: AsyncSession, user_id: str, *, threshold: int, now: datetime, lock_until: datetime
) -> None:
    """
    Lock ``user_id`` until ``lock_until`` if ``threshold`` attempts are counted. Does not commit.

    Resets the counter with the lock, in the same ``UPDATE``. A no-op when fewer
    attempts are counted, or when a lock is already in force at ``now``, so a
    late caller never moves someone else's lock.
    """
    await session.execute(
        update(users)
        .where(users.c.id == user_id, _not_locked(now), users.c.failed_logins >= threshold)
        .values(failed_logins=0, locked_until=lock_until)
    )


async def reset_lockout(session: AsyncSession, user_id: str, *, now: datetime) -> bool:
    """
    Clear the failure counter and any expired lock on ``user_id``. Does not commit.

    Only when no lock is in force at ``now``: a success never clears a lock that
    a concurrent failure set. Returns False, changing nothing, if one is.
    """
    cleared = await session.execute(
        update(users)
        .where(users.c.id == user_id, _not_locked(now))
        .values(failed_logins=0, locked_until=None)
        .returning(users.c.id)
    )
    return cleared.first() is not None


async def get_locked_until(session: AsyncSession, user_id: str) -> datetime | None:
    """``locked_until`` for ``user_id`` as stored now (it may be in the past)."""
    return (
        await session.execute(select(users.c.locked_until).where(users.c.id == user_id))
    ).scalar_one_or_none()
