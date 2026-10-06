"""
Trip membership reads — the ``trip_members`` table from migration 0003 (decision-log Entry 29).

**Context.** Under Entry 29 a slug only *locates* a trip. Whether the caller may
write to it is answered by their row in ``trip_members``, read here on every
request (``docs/api-contract.md``, "Access: public trips, members and
leaders" → The gates). This module is mostly the read side; its one write so
far is ``add``, the first leader inserted with a new trip. The other writes
(claim approval, promote, revoke, leave) arrive with the tasks that build those
endpoints.

**How it works.** SQLAlchemy Core against ``app/data/tables.py``.

- **No cache, ever.** Every call is one query. A revocation therefore takes
  effect on the caller's very next request, which is the contract's rule
  ("Membership is read from Postgres on every request, with no cache").
- **Revoked is not "never a member".** A revoked row is kept as history
  (``revoked_at`` set, ``ux_trip_members_active`` only covers active rows), and
  the gate answers a revoked member with a different ``403`` message from a
  stranger. So ``get_for_user`` returns the active row when there is one, and
  otherwise the most recently revoked row, rather than ``None``.
- **It never raises ``ApiError``.** No row is ``None``; what that means for the
  request is ``app/core/security.py``'s decision. The writes return an
  outcome, and the route maps it to a status.
- **Every role change, self-leave and revoke locks the trip row first**
  (``SELECT … FOR UPDATE``, contract default 26), then re-reads the rows it
  decides on. Two step-downs racing on a two-leader trip are serialised: the
  second counts the first's committed change and gets ``LAST_LEADER``, so a
  trip can't be left with no leader. Each write commits (or, refused, ends the
  transaction with nothing written), releasing the lock.

**Related.** ``app/core/security.py`` (``require_trip_writer``,
``require_trip_access``), ``app/data/repositories/sessions.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.tables import trip_members, trips, users
from app.models.member import MemberOut, MemberRole


@dataclass(frozen=True, slots=True)
class MembershipRecord:
    """
    One user's standing on one trip.

    ``active`` is False for a revoked (or self-departed) membership. ``role`` is
    the role that row carried, which for a revoked row is history only and grants
    nothing.
    """

    role: MemberRole
    active: bool


async def get_for_user(
    session: AsyncSession, trip_id: str, user_id: str
) -> MembershipRecord | None:
    """
    ``user_id``'s membership on ``trip_id``: the active row, else the latest revoked one, else None.

    One query. Active rows sort first (``revoked_at IS NULL``), then revoked
    rows newest first, and only the first row is read. There is at most one
    active row per (trip, user) (``ux_trip_members_active``), so the answer is
    never ambiguous.

    ``trip_id`` may be any string a caller sent, including one no trip has (the
    v2 reader looks membership up before knowing whether the trip exists). A NUL
    byte, which Postgres ``text`` can't hold, is ``None`` without a query, like
    ``trips.get_by_id``; no trip id can contain one.
    """
    if "\x00" in trip_id:
        return None
    row = (
        await session.execute(
            select(trip_members.c.role, trip_members.c.revoked_at)
            .where(trip_members.c.trip_id == trip_id, trip_members.c.user_id == user_id)
            .order_by(
                trip_members.c.revoked_at.is_(None).desc(),
                trip_members.c.revoked_at.desc(),
            )
            .limit(1)
        )
    ).first()

    if row is None:
        return None

    return MembershipRecord(role=MemberRole(row.role), active=row.revoked_at is None)


async def add(session: AsyncSession, trip_id: str, user_id: str, role: MemberRole) -> None:
    """
    Insert an active ``role`` membership for ``user_id`` on ``trip_id``. Does not commit.

    The caller owns the transaction: a trip's creation inserts the trip and its
    first leader here together (``trips.create_for_user``), so neither can exist
    without the other. The row id is a server-generated UUID4.
    """
    await session.execute(
        trip_members.insert().values(
            id=str(uuid4()), trip_id=trip_id, user_id=user_id, role=role.value
        )
    )


# --------------------------------------------------------------------------
# Member list and peer leadership (t-am-trip-leadership)
# --------------------------------------------------------------------------

_MEMBER_COLUMNS = (
    trip_members.c.user_id,
    users.c.display_name,
    trip_members.c.role,
    trip_members.c.joined_at,
)


def _member_out(row) -> MemberOut:
    return MemberOut(
        userId=row.user_id,
        displayName=row.display_name,
        role=MemberRole(row.role),
        joinedAt=row.joined_at,
    )


def _active_member(trip_id: str, user_id: str):
    """The select for ``user_id``'s active row on ``trip_id``, as ``MemberOut`` columns."""
    return (
        select(*_MEMBER_COLUMNS)
        .join(users, users.c.id == trip_members.c.user_id)
        .where(
            trip_members.c.trip_id == trip_id,
            trip_members.c.user_id == user_id,
            trip_members.c.revoked_at.is_(None),
        )
    )


async def list_active(session: AsyncSession, trip_id: str) -> list[MemberOut]:
    """
    The trip's active members, oldest ``joined_at`` first (ties by user id).

    Only the ``MemberOut`` fields are selected: never a username, a revoked row,
    or anything about sessions.
    """
    rows = await session.execute(
        select(*_MEMBER_COLUMNS)
        .join(users, users.c.id == trip_members.c.user_id)
        .where(trip_members.c.trip_id == trip_id, trip_members.c.revoked_at.is_(None))
        .order_by(trip_members.c.joined_at, trip_members.c.user_id)
    )
    return [_member_out(row) for row in rows]


async def _lock_trip(session: AsyncSession, trip_id: str) -> None:
    """``SELECT … FOR UPDATE`` on the trip row: the lock every membership change takes first."""
    await session.execute(select(trips.c.id).where(trips.c.id == trip_id).with_for_update())


async def _other_active_leaders(session: AsyncSession, trip_id: str, user_id: str) -> int:
    """How many active leaders the trip has besides ``user_id``. Run under the trip lock."""
    return await session.scalar(
        select(func.count())
        .select_from(trip_members)
        .where(
            trip_members.c.trip_id == trip_id,
            trip_members.c.user_id != user_id,
            trip_members.c.role == MemberRole.LEADER.value,
            trip_members.c.revoked_at.is_(None),
        )
    )


class ChangeOutcome(StrEnum):
    """Why a role change, leave or revoke did, or did not, happen."""

    DONE = "done"  # committed (or, for promote, the target was already a leader)
    NOT_FOUND = "not_found"  # promote: no active target row; revoke: no row at all
    NOT_A_MEMBER = "not_a_member"  # the caller's own active row went before the lock
    NOT_A_LEADER = "not_a_leader"  # promote/step-down: the caller is no longer a leader
    LAST_LEADER = "last_leader"  # step-down/leave: no other active leader would remain
    LEADER_TARGET = "leader_target"  # revoke: the target is a leader (the caller included)


@dataclass(frozen=True, slots=True)
class ChangeResult:
    """A write's outcome, and for ``DONE`` on promote / step-down the member afterwards."""

    outcome: ChangeOutcome
    member: MemberOut | None = None


async def promote(
    session: AsyncSession, trip_id: str, caller_id: str, user_id: str
) -> ChangeResult:
    """
    Make ``user_id``'s active membership a leader one, under the trip lock.

    The caller's own row is re-read under the lock first, so a leader revoked,
    demoted or departed since the gate gets the answer the gate would now give:
    ``NOT_A_MEMBER`` or ``NOT_A_LEADER``, with nothing written. Then
    ``NOT_FOUND`` when the target has no active row (never a member, revoked,
    left, or only a pending requester). An existing leader is ``DONE`` with
    nothing written: promote is idempotent. A NUL byte, which Postgres ``text``
    can't hold and no user id contains, is ``NOT_FOUND`` without a target query.
    """
    await _lock_trip(session, trip_id)
    caller = (await session.execute(_active_member(trip_id, caller_id))).first()
    if caller is None or caller.role != MemberRole.LEADER.value:
        await session.commit()
        return ChangeResult(
            ChangeOutcome.NOT_A_MEMBER if caller is None else ChangeOutcome.NOT_A_LEADER
        )
    row = (
        None
        if "\x00" in user_id
        else (await session.execute(_active_member(trip_id, user_id))).first()
    )
    if row is None:
        await session.commit()
        return ChangeResult(ChangeOutcome.NOT_FOUND)
    if row.role != MemberRole.LEADER.value:
        await session.execute(
            update(trip_members)
            .where(
                trip_members.c.trip_id == trip_id,
                trip_members.c.user_id == user_id,
                trip_members.c.revoked_at.is_(None),
            )
            .values(role=MemberRole.LEADER.value)
        )
        row = (await session.execute(_active_member(trip_id, user_id))).one()
    await session.commit()
    return ChangeResult(ChangeOutcome.DONE, _member_out(row))


async def step_down(session: AsyncSession, trip_id: str, user_id: str) -> ChangeResult:
    """
    Turn ``user_id``'s active leader membership into a rider one, under the trip lock.

    The caller's row is re-read under the lock, so the answer is the one a
    serial order would give: ``NOT_A_MEMBER`` if it is no longer active,
    ``NOT_A_LEADER`` if it is a rider now, ``LAST_LEADER`` if no other active
    leader exists. A refusal writes nothing.
    """
    await _lock_trip(session, trip_id)
    row = (await session.execute(_active_member(trip_id, user_id))).first()
    outcome = None
    if row is None:
        outcome = ChangeOutcome.NOT_A_MEMBER
    elif row.role != MemberRole.LEADER.value:
        outcome = ChangeOutcome.NOT_A_LEADER
    elif await _other_active_leaders(session, trip_id, user_id) == 0:
        outcome = ChangeOutcome.LAST_LEADER
    if outcome is not None:
        await session.commit()
        return ChangeResult(outcome)

    await session.execute(
        update(trip_members)
        .where(
            trip_members.c.trip_id == trip_id,
            trip_members.c.user_id == user_id,
            trip_members.c.revoked_at.is_(None),
        )
        .values(role=MemberRole.RIDER.value)
    )
    row = (await session.execute(_active_member(trip_id, user_id))).one()
    await session.commit()
    return ChangeResult(ChangeOutcome.DONE, _member_out(row))


async def leave(session: AsyncSession, trip_id: str, user_id: str) -> ChangeResult:
    """
    End ``user_id``'s own active membership, under the trip lock.

    Sets ``revoked_at`` with ``revoked_by`` = the caller; the row is kept as
    history. ``join_requests`` is not touched, so leaving starts no cooldown.
    ``NOT_A_MEMBER`` if the caller's row is no longer active, ``LAST_LEADER``
    if the caller is a leader and no other active leader exists. A refusal
    writes nothing.
    """
    await _lock_trip(session, trip_id)
    row = (await session.execute(_active_member(trip_id, user_id))).first()
    outcome = None
    if row is None:
        outcome = ChangeOutcome.NOT_A_MEMBER
    elif (
        row.role == MemberRole.LEADER.value
        and await _other_active_leaders(session, trip_id, user_id) == 0
    ):
        outcome = ChangeOutcome.LAST_LEADER
    if outcome is not None:
        await session.commit()
        return ChangeResult(outcome)

    await session.execute(
        update(trip_members)
        .where(
            trip_members.c.trip_id == trip_id,
            trip_members.c.user_id == user_id,
            trip_members.c.revoked_at.is_(None),
        )
        .values(revoked_at=func.now(), revoked_by=user_id)
    )
    await session.commit()
    return ChangeResult(ChangeOutcome.DONE)


async def revoke(session: AsyncSession, trip_id: str, caller_id: str, user_id: str) -> ChangeResult:
    """
    End ``user_id``'s active rider membership on ``trip_id``, under the trip lock.

    The caller's own row is re-read under the lock first, as in ``promote``:
    ``NOT_A_MEMBER`` or ``NOT_A_LEADER`` with nothing written. Then the target,
    read the way the gate reads a caller (``get_for_user``: the active row, else
    the latest revoked one):

    - no row at all, or a NUL byte no user id contains → ``NOT_FOUND``;
    - an active leader, the caller included → ``LEADER_TARGET``: peer leaders
      can't remove each other (decision-log Entry 29);
    - only revoked rows (revoked earlier, or departed) → ``DONE`` with nothing
      written, so a retry is safe;
    - an active rider → ``revoked_at`` and ``revoked_by`` = the caller are set,
      and the row is kept as history.

    The gate reads this row on every request with no cache, so the rider's very
    next write is refused.
    """
    await _lock_trip(session, trip_id)
    caller = (await session.execute(_active_member(trip_id, caller_id))).first()
    if caller is None or caller.role != MemberRole.LEADER.value:
        await session.commit()
        return ChangeResult(
            ChangeOutcome.NOT_A_MEMBER if caller is None else ChangeOutcome.NOT_A_LEADER
        )
    target = None if "\x00" in user_id else await get_for_user(session, trip_id, user_id)
    outcome = None
    if target is None:
        outcome = ChangeOutcome.NOT_FOUND
    elif not target.active:
        outcome = ChangeOutcome.DONE
    elif target.role is MemberRole.LEADER:
        outcome = ChangeOutcome.LEADER_TARGET
    if outcome is not None:
        await session.commit()
        return ChangeResult(outcome)

    await session.execute(
        update(trip_members)
        .where(
            trip_members.c.trip_id == trip_id,
            trip_members.c.user_id == user_id,
            trip_members.c.revoked_at.is_(None),
        )
        .values(revoked_at=func.now(), revoked_by=caller_id)
    )
    await session.commit()
    return ChangeResult(ChangeOutcome.DONE)
