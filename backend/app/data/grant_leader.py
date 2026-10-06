"""
Operator CLI: make an account an active leader of a trip.

    cd backend && uv run python -m app.data.grant_leader --trip-id <id> --username <name>

**Context.** The only way to give a legacy (pre-0003) trip its first leader,
or to restore leadership when no leader is left to promote anyone
(decision-log Entry 29). Owner-only in production; agents never run it there.

**How it works.** One transaction: lock the trip row (``memberships._lock_trip``,
the lock every membership change takes), then

- the user already holds an active leader row → nothing is written (idempotent);
- the user holds an active rider row → that row's role becomes ``leader``;
- otherwise → a new active leader row is inserted (revoked rows stay history).

An unknown trip or username exits 1 with a one-line message, no traceback.
No secret is involved, so nothing here needs print-after-commit care beyond
reporting only once the transaction has committed.

**Related.** ``app/data/revoke_member.py``, ``app/data/reset_account.py``,
``app/data/seed_trip.py`` (``--leader-username``).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from contextlib import suppress
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.data.db import normalize_database_url
from app.data.repositories import memberships
from app.data.tables import trip_members, trips, users
from app.models.member import MemberRole


class OperatorError(Exception):
    """A refusal this CLI reports as one line and exit code 1."""


class GrantOutcome(StrEnum):
    """What ``grant_leader`` did."""

    INSERTED = "inserted"
    UPGRADED = "upgraded"
    ALREADY_LEADER = "already_leader"


async def find_user_id(session: AsyncSession, username: str) -> str | None:
    """The id of the account stored under ``username`` (lowercased), or ``None``."""
    # Stored usernames are ASCII; lowercasing non-ASCII could map it onto an
    # ASCII name, so such input names no account (as signin treats it).
    if not username.isascii():
        return None
    return await session.scalar(select(users.c.id).where(users.c.username == username.lower()))


async def lock_existing_trip(session: AsyncSession, trip_id: str) -> None:
    """Take the trip-row lock; raise ``OperatorError`` if there is no such trip."""
    await memberships._lock_trip(session, trip_id)
    if await session.scalar(select(trips.c.id).where(trips.c.id == trip_id)) is None:
        raise OperatorError(f"no trip with id {trip_id!r}. Nothing was changed.")


async def grant_leader(session: AsyncSession, *, trip_id: str, username: str) -> GrantOutcome:
    """Make ``username`` an active leader of ``trip_id``. Does not commit."""
    user_id = await find_user_id(session, username)
    if user_id is None:
        raise OperatorError(f"no account with username {username!r}. Nothing was changed.")
    await lock_existing_trip(session, trip_id)

    role = await session.scalar(
        select(trip_members.c.role).where(
            trip_members.c.trip_id == trip_id,
            trip_members.c.user_id == user_id,
            trip_members.c.revoked_at.is_(None),
        )
    )
    if role == MemberRole.LEADER.value:
        return GrantOutcome.ALREADY_LEADER
    if role is not None:
        await session.execute(
            update(trip_members)
            .where(
                trip_members.c.trip_id == trip_id,
                trip_members.c.user_id == user_id,
                trip_members.c.revoked_at.is_(None),
            )
            .values(role=MemberRole.LEADER.value)
        )
        return GrantOutcome.UPGRADED
    await memberships.add(session, trip_id, user_id, MemberRole.LEADER)
    return GrantOutcome.INSERTED


def describe_db_error(exc: SQLAlchemyError) -> str:
    """Class name and SQLSTATE only — never ``str(exc)``, which renders bound parameters."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return f"database error {type(exc).__name__} (SQLSTATE {sqlstate or 'unknown'})"


async def _run(trip_id: str, username: str) -> GrantOutcome:
    engine = create_async_engine(normalize_database_url(get_settings().database_url))
    try:
        async with AsyncSession(engine) as session, session.begin():
            return await grant_leader(session, trip_id=trip_id, username=username)
    finally:
        with suppress(Exception):
            await engine.dispose()


_MESSAGES = {
    GrantOutcome.INSERTED: "is now an active leader of",
    GrantOutcome.UPGRADED: "was a rider and is now a leader of",
    GrantOutcome.ALREADY_LEADER: "was already an active leader of",
}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: 0 on success (including already-leader), 1 on a refusal or failure."""
    parser = argparse.ArgumentParser(
        prog="python -m app.data.grant_leader",
        description="Make an account an active leader of a trip (inserting or upgrading).",
    )
    parser.add_argument("--trip-id", required=True, help="The trip's id (trips.id).")
    parser.add_argument("--username", required=True, help="The account's username.")
    args = parser.parse_args(argv)

    try:
        outcome = asyncio.run(_run(args.trip_id, args.username))
    except OperatorError as exc:
        print(f"grant_leader refused: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(
            f"grant_leader failed: {describe_db_error(exc)}. Nothing was changed.", file=sys.stderr
        )
        return 1
    print(f"{args.username} {_MESSAGES[outcome]} trip {args.trip_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
