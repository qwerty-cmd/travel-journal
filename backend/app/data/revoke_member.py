"""
Operator CLI: end an account's active membership of a trip — a leader included.

    cd backend && uv run python -m app.data.revoke_member --trip-id <id> --username <name>

**Context.** Peer leaders can't remove each other (decision-log Entry 29), so a
rogue leader can only be removed here. Owner-only in production; agents never
run it there.

**How it works.** One transaction, under the trip-row lock
(``memberships._lock_trip``):

- no active row → nothing written; exit 0 if the user has a revoked row
  (already revoked, so a retry is safe), exit 1 if they were never a member;
- an active leader with no other active leader (the last-leader rule,
  ``memberships._other_active_leaders``) → refused, exit 1, nothing written;
- otherwise → ``revoked_at`` is set and ``revoked_by`` stays NULL, the
  contract's marker for an operator revocation. The row is kept as history,
  and the gate refuses the user's very next write.

The user's sessions are left alone: membership, not the account, is revoked.

**Related.** ``app/data/grant_leader.py``, ``app/data/reset_account.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from contextlib import suppress
from enum import StrEnum

from sqlalchemy import func, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.data.db import normalize_database_url
from app.data.grant_leader import OperatorError, describe_db_error, find_user_id, lock_existing_trip
from app.data.repositories import memberships
from app.data.tables import trip_members
from app.models.member import MemberRole


class RevokeOutcome(StrEnum):
    """What ``revoke_member`` did."""

    REVOKED = "revoked"
    ALREADY_REVOKED = "already_revoked"


async def revoke_member(session: AsyncSession, *, trip_id: str, username: str) -> RevokeOutcome:
    """
    Revoke ``username``'s active membership of ``trip_id``. Does not commit.

    Raises ``OperatorError`` for an unknown user or trip, a user who was never
    a member, and the trip's last active leader.
    """
    user_id = await find_user_id(session, username)
    if user_id is None:
        raise OperatorError(f"no account with username {username!r}. Nothing was changed.")
    await lock_existing_trip(session, trip_id)

    record = await memberships.get_for_user(session, trip_id, user_id)
    if record is None:
        raise OperatorError(f"{username!r} is not a member of trip {trip_id}. Nothing was changed.")
    if not record.active:
        return RevokeOutcome.ALREADY_REVOKED
    if (
        record.role is MemberRole.LEADER
        and await memberships._other_active_leaders(session, trip_id, user_id) == 0
    ):
        raise OperatorError(
            f"{username!r} is the last active leader of trip {trip_id}; removing them would "
            "leave it with no leader. Grant another leader first (app.data.grant_leader). "
            "Nothing was changed."
        )

    await session.execute(
        update(trip_members)
        .where(
            trip_members.c.trip_id == trip_id,
            trip_members.c.user_id == user_id,
            trip_members.c.revoked_at.is_(None),
        )
        .values(revoked_at=func.now(), revoked_by=None)
    )
    return RevokeOutcome.REVOKED


async def _run(trip_id: str, username: str) -> RevokeOutcome:
    engine = create_async_engine(normalize_database_url(get_settings().database_url))
    try:
        async with AsyncSession(engine) as session, session.begin():
            return await revoke_member(session, trip_id=trip_id, username=username)
    finally:
        with suppress(Exception):
            await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: 0 on revoked or already revoked, 1 on a refusal or failure."""
    parser = argparse.ArgumentParser(
        prog="python -m app.data.revoke_member",
        description=(
            "Revoke an account's active membership of a trip, a leader included. "
            "Refuses to remove the trip's last active leader."
        ),
    )
    parser.add_argument("--trip-id", required=True, help="The trip's id (trips.id).")
    parser.add_argument("--username", required=True, help="The account's username.")
    args = parser.parse_args(argv)

    try:
        outcome = asyncio.run(_run(args.trip_id, args.username))
    except OperatorError as exc:
        print(f"revoke_member refused: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(
            f"revoke_member failed: {describe_db_error(exc)}. Nothing was changed.", file=sys.stderr
        )
        return 1
    if outcome is RevokeOutcome.REVOKED:
        print(f"{args.username}'s membership of trip {args.trip_id} is revoked.")
    else:
        print(f"{args.username}'s membership of trip {args.trip_id} was already revoked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
