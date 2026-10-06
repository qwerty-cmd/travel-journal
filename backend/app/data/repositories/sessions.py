"""
Session rows — the ``sessions`` table from migration 0003 (decision-log Entry 29).

**Context.** A signed-in device holds a random token in the
``__Host-btj_session`` cookie. The database holds only the token's SHA-256, as
``sessions.token_hash`` (32 bytes, enforced by
``sessions_token_hash_length_check``). This module reads and writes those rows.
It never sees a token, only its hash.

**How it works.** SQLAlchemy Core against ``app/data/tables.py``.

- **Rows, not policy.** Whether a session is still valid (90 days idle, 365 days
  absolute) and when ``last_used_at`` is worth writing are decided in
  ``app/core/sessions.py``. This module stores the timestamps it's given and
  returns the ones it finds.
- **One query per request.** ``get_with_user`` joins ``users`` so the session
  gate learns, in the same round trip, who the session belongs to and whether
  that account is disabled.
- **Who commits.** ``insert``, the deletes and the confirmation counter
  (``reserve_confirmation``, ``reset_confirmations``) do not commit: signup,
  signin, signout, recovery, password change and rotation change sessions
  inside a larger transaction. ``touch`` commits, because
  it is the only write in the session gate and nothing else in the request
  should be held open behind it.
- **It never raises ``ApiError``.** A missing row is ``None``.

**Related.** ``app/core/sessions.py`` (tokens, lifetime rules, the cookie),
``app/core/security.py`` (``require_session``), ``app/data/repositories/users.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.tables import sessions, users


@dataclass(frozen=True, slots=True)
class SessionWithUser:
    """
    One session row joined to its account.

    Carries no token and no token hash, since the caller already has the hash it
    looked up by. Carries no password hash either.
    """

    user_id: str
    last_used_at: datetime
    absolute_expires_at: datetime
    username: str
    display_name: str
    user_created_at: datetime
    user_disabled_at: datetime | None


async def insert(
    session: AsyncSession,
    *,
    token_hash: bytes,
    user_id: str,
    created_at: datetime,
    absolute_expires_at: datetime,
) -> None:
    """
    Store one session. ``last_used_at`` starts at ``created_at``. Does not commit.

    All three timestamps come from the caller, from one clock, rather than
    mixing the database's ``now()`` with the app's.
    """
    await session.execute(
        sessions.insert().values(
            token_hash=token_hash,
            user_id=user_id,
            created_at=created_at,
            last_used_at=created_at,
            absolute_expires_at=absolute_expires_at,
        )
    )


async def get_with_user(session: AsyncSession, token_hash: bytes) -> SessionWithUser | None:
    """The session stored under ``token_hash``, with its account, or ``None``."""
    row = (
        await session.execute(
            select(
                sessions.c.user_id,
                sessions.c.last_used_at,
                sessions.c.absolute_expires_at,
                users.c.username,
                users.c.display_name,
                users.c.created_at.label("user_created_at"),
                users.c.disabled_at.label("user_disabled_at"),
            )
            .join(users, users.c.id == sessions.c.user_id)
            .where(sessions.c.token_hash == token_hash)
        )
    ).first()

    if row is None:
        return None

    return SessionWithUser(
        user_id=row.user_id,
        last_used_at=row.last_used_at,
        absolute_expires_at=row.absolute_expires_at,
        username=row.username,
        display_name=row.display_name,
        user_created_at=row.user_created_at,
        user_disabled_at=row.user_disabled_at,
    )


async def touch(session: AsyncSession, token_hash: bytes, now: datetime) -> None:
    """Set ``last_used_at = now`` on one session, and commit."""
    await session.execute(
        update(sessions).where(sessions.c.token_hash == token_hash).values(last_used_at=now)
    )
    await session.commit()


async def reserve_confirmation(
    session: AsyncSession, token_hash: bytes, *, threshold: int
) -> int | None:
    """
    Claim one password confirmation on this session, before it is checked. Does not commit.

    One conditional ``UPDATE``: the counter goes up by one only while it is
    under ``threshold``, so racing requests can claim at most ``threshold``
    checks between them. Returns the new count, or ``None`` when the session is
    already at ``threshold`` or its row is gone (decision-log Entry 33).
    """
    claimed = (
        await session.execute(
            update(sessions)
            .where(
                sessions.c.token_hash == token_hash,
                sessions.c.failed_confirmations < threshold,
            )
            .values(failed_confirmations=sessions.c.failed_confirmations + 1)
            .returning(sessions.c.failed_confirmations)
        )
    ).scalar_one_or_none()
    return claimed


async def reset_confirmations(session: AsyncSession, token_hash: bytes) -> bool:
    """
    Set this session's confirmation counter back to 0. Does not commit.

    ``False`` when the row is gone, for example deleted by a concurrent 10th
    wrong confirmation.
    """
    reset = (
        await session.execute(
            update(sessions)
            .where(sessions.c.token_hash == token_hash)
            .values(failed_confirmations=0)
            .returning(sessions.c.token_hash)
        )
    ).first()
    return reset is not None


async def delete(session: AsyncSession, token_hash: bytes) -> None:
    """Delete the session stored under ``token_hash``, if there is one. Does not commit."""
    await session.execute(sessions.delete().where(sessions.c.token_hash == token_hash))


async def delete_all_for_user(session: AsyncSession, user_id: str) -> None:
    """
    Delete every session of ``user_id``, the caller's own included. Does not commit.

    Signout-all, a password change and a recovery (contract, "Sessions" →
    Revocation).
    """
    await session.execute(sessions.delete().where(sessions.c.user_id == user_id))


async def delete_expired_for_user(
    session: AsyncSession, user_id: str, *, idle_cutoff: datetime, now: datetime
) -> None:
    """
    Delete ``user_id``'s sessions that can no longer be used. Does not commit.

    Expired means idle since ``idle_cutoff`` or earlier (``last_used_at <=
    now - 90 days``) or past ``absolute_expires_at``: exactly the rows the
    validity rule in ``app/core/sessions.py`` already refuses.
    """
    await session.execute(
        sessions.delete().where(
            sessions.c.user_id == user_id,
            or_(sessions.c.last_used_at <= idle_cutoff, sessions.c.absolute_expires_at <= now),
        )
    )
