"""
Join requests -- the ``join_requests`` table from migration 0003 (decision-log Entry 29).

**Context.** A signed-in user asks to ride on a trip; a leader decides. This
module holds the read the trip view needs (``has_pending``, which makes
``TripOut.viewer.role`` ``pending``) and the requester's side of the lifecycle
(``t-am-join-requester``): create, cancel and the caller's own list
(``docs/api-contract.md``, "Join request create", "Cancel", "Me /
join-requests"). The leader side (decide, unblock, list) arrives with
``t-am-join-leader``.

**How it works.** SQLAlchemy Core against ``app/data/tables.py``, no cache. It
never raises ``ApiError``: the writes return an outcome and the route maps it to
a status.

- **Race safety of create.** ``create`` first locks the trip row, then the
  requester's ``users`` row (``SELECT … FOR UPDATE``), always in that order.
  The trip lock is the one every membership change takes
  (``memberships._lock_trip``), so the trip's 100-pending cap and the "already
  an active member" check are decided against committed state; the user lock is
  the one ``trips.create_for_user`` takes, so the caller's 20-pending cap and
  the one-pending-per-(trip, user) check can't be overshot by parallel sends.
  ``ux_join_requests_one_pending`` backs the duplicate check: the insert is
  ``ON CONFLICT DO NOTHING`` on it, and a row that didn't go in is the existing
  pending request, never a unique-violation ``500``.
- **Time comes from the caller** (``now``), from the route's one clock, so the
  7-day cooldown can be tested without sleeping and every timestamp one
  request writes agrees.

**Related.** ``app/data/repositories/memberships.py`` (the ``trip_members``
reads and writes), ``app/api/routes/v2/join_requests.py`` (the routes).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.tables import join_requests, trip_members, trips, users

# Contract, "Join request create": the limits a create is refused (409) beyond.
JOIN_COOLDOWN = timedelta(days=7)
USER_PENDING_CAP = 20
TRIP_PENDING_CAP = 100

# How many of the caller's own requests `list_for_user` returns, newest first.
MY_REQUESTS_LIMIT = 100


async def has_pending(session: AsyncSession, trip_id: str, user_id: str) -> bool:
    """
    True when ``user_id`` has a pending join request on ``trip_id``.

    At most one can exist (``ux_join_requests_one_pending``). Decided,
    cancelled and blocked requests are not pending, so they answer ``False``.
    """
    return bool(
        await session.scalar(
            select(
                exists().where(
                    join_requests.c.trip_id == trip_id,
                    join_requests.c.user_id == user_id,
                    join_requests.c.state == "pending",
                )
            )
        )
    )


@dataclass(frozen=True, slots=True)
class JoinRequestRecord:
    """One join request as its requester sees it. ``state`` is the stored one, ``blocked`` included."""

    id: str
    trip_id: str
    trip_name: str
    state: str
    message: str | None
    created_at: datetime


class CreateOutcome(StrEnum):
    """``create``'s answer. Every outcome but CREATED and EXISTING wrote nothing."""

    CREATED = "created"
    EXISTING = "existing"  # a pending request was already there; returned unchanged
    ACTIVE_MEMBER = "active_member"
    BLOCKED = "blocked"
    REJECTED_RECENTLY = "rejected_recently"
    REVOKED_RECENTLY = "revoked_recently"  # by someone else; a self-leave doesn't count
    USER_CAP = "user_cap"
    TRIP_CAP = "trip_cap"


@dataclass(frozen=True, slots=True)
class CreateResult:
    outcome: CreateOutcome
    request: JoinRequestRecord | None = None


class CancelOutcome(StrEnum):
    """``cancel``'s answer."""

    CANCELLED = "cancelled"  # cancelled now, or already cancelled (idempotent)
    NOT_FOUND = "not_found"  # no such request, or not the caller's
    DECIDED = "decided"  # approved, rejected or blocked: nothing changed


@dataclass(frozen=True, slots=True)
class CancelResult:
    outcome: CancelOutcome
    request: JoinRequestRecord | None = None


_RECORD_COLUMNS = (
    join_requests.c.id,
    join_requests.c.trip_id,
    trips.c.name.label("trip_name"),
    join_requests.c.state,
    join_requests.c.message,
    join_requests.c.created_at,
)


def _record_query():
    return select(*_RECORD_COLUMNS).join(trips, trips.c.id == join_requests.c.trip_id)


def _record(row) -> JoinRequestRecord:
    return JoinRequestRecord(
        id=row.id,
        trip_id=row.trip_id,
        trip_name=row.trip_name,
        state=row.state,
        message=row.message,
        created_at=row.created_at,
    )


async def _pending(session: AsyncSession, trip_id: str, user_id: str) -> JoinRequestRecord | None:
    row = (
        await session.execute(
            _record_query().where(
                join_requests.c.trip_id == trip_id,
                join_requests.c.user_id == user_id,
                join_requests.c.state == "pending",
            )
        )
    ).first()
    return _record(row) if row is not None else None


async def _refusal(
    session: AsyncSession, trip_id: str, user_id: str, now: datetime
) -> CreateOutcome | None:
    """Why a new request may not be made, checked in the contract's order; ``None`` if it may."""
    if await session.scalar(
        select(
            exists().where(
                trip_members.c.trip_id == trip_id,
                trip_members.c.user_id == user_id,
                trip_members.c.revoked_at.is_(None),
            )
        )
    ):
        return CreateOutcome.ACTIVE_MEMBER

    mine = (join_requests.c.trip_id == trip_id, join_requests.c.user_id == user_id)
    if await session.scalar(select(exists().where(*mine, join_requests.c.state == "blocked"))):
        return CreateOutcome.BLOCKED

    since = now - JOIN_COOLDOWN
    if await session.scalar(
        select(
            exists().where(
                *mine,
                join_requests.c.state == "rejected",
                join_requests.c.decided_at > since,
            )
        )
    ):
        return CreateOutcome.REJECTED_RECENTLY

    # `IS DISTINCT FROM`: a NULL `revoked_by` (an operator revocation, or a
    # revoker whose account is gone) is someone else. Only the member's own
    # leave (`revoked_by` = them) starts no cooldown.
    if await session.scalar(
        select(
            exists().where(
                trip_members.c.trip_id == trip_id,
                trip_members.c.user_id == user_id,
                trip_members.c.revoked_at > since,
                trip_members.c.revoked_by.is_distinct_from(user_id),
            )
        )
    ):
        return CreateOutcome.REVOKED_RECENTLY

    pending = join_requests.c.state == "pending"
    if (
        await session.scalar(
            select(func.count())
            .select_from(join_requests)
            .where(join_requests.c.user_id == user_id, pending)
        )
        >= USER_PENDING_CAP
    ):
        return CreateOutcome.USER_CAP

    if (
        await session.scalar(
            select(func.count())
            .select_from(join_requests)
            .where(join_requests.c.trip_id == trip_id, pending)
        )
        >= TRIP_PENDING_CAP
    ):
        return CreateOutcome.TRIP_CAP

    return None


async def create(
    session: AsyncSession, *, trip_id: str, user_id: str, message: str | None, now: datetime
) -> CreateResult:
    """
    Make a pending ``direct`` request for ``user_id`` on ``trip_id``, or say why not. Commits.

    **Order.** Under the trip lock and then the user lock: an existing pending
    request first (``EXISTING``, its message unchanged), then the refusals in
    the contract's order (active member, blocked, rejected or revoked by someone
    else within 7 days of ``now``, the caller's 20-pending cap, the trip's
    100-pending cap), then the insert. Every branch ends the transaction,
    releasing both locks; only ``CREATED`` writes.

    Whether the trip may be seen at all is the gate's decision, made before this.
    """
    await session.execute(select(trips.c.id).where(trips.c.id == trip_id).with_for_update())
    await session.execute(select(users.c.id).where(users.c.id == user_id).with_for_update())

    existing = await _pending(session, trip_id, user_id)
    if existing is not None:
        await session.commit()
        return CreateResult(CreateOutcome.EXISTING, existing)

    refusal = await _refusal(session, trip_id, user_id, now)
    if refusal is not None:
        await session.commit()
        return CreateResult(refusal)

    inserted = await session.scalar(
        pg_insert(join_requests)
        .values(
            id=str(uuid4()),
            trip_id=trip_id,
            user_id=user_id,
            state="pending",
            via="direct",
            message=message,
            created_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[join_requests.c.trip_id, join_requests.c.user_id],
            # A literal predicate, never a bound parameter: after 5 runs on a pooled
            # connection Postgres switches to a generic plan, which can't match a
            # parameterised predicate to the partial index (InvalidColumnReferenceError).
            index_where=text("state = 'pending'"),
        )
        .returning(join_requests.c.id)
    )
    request = await _pending(session, trip_id, user_id)
    await session.commit()
    outcome = CreateOutcome.CREATED if inserted is not None else CreateOutcome.EXISTING
    return CreateResult(outcome, request)


async def cancel(
    session: AsyncSession, *, request_id: str, user_id: str, now: datetime
) -> CancelResult:
    """
    Cancel ``user_id``'s own request ``request_id``. Commits.

    Pending → ``cancelled`` (``decided_at`` = ``now``); already cancelled →
    ``CANCELLED`` with nothing changed; any other state → ``DECIDED``. Someone
    else's request and an unknown id are the same ``NOT_FOUND``, from the same
    one query. The update is conditional on ``state = 'pending'``, so a cancel
    racing a leader's decision either wins or is re-read as decided.
    """
    if "\x00" in request_id:  # Postgres `text` can't hold it; no request id contains one
        return CancelResult(CancelOutcome.NOT_FOUND)

    await session.execute(
        update(join_requests)
        .where(
            join_requests.c.id == request_id,
            join_requests.c.user_id == user_id,
            join_requests.c.state == "pending",
        )
        .values(state="cancelled", decided_at=now)
    )
    row = (
        await session.execute(
            _record_query().where(
                join_requests.c.id == request_id, join_requests.c.user_id == user_id
            )
        )
    ).first()
    await session.commit()

    if row is None:
        return CancelResult(CancelOutcome.NOT_FOUND)
    record = _record(row)
    if record.state != "cancelled":
        return CancelResult(CancelOutcome.DECIDED, record)
    return CancelResult(CancelOutcome.CANCELLED, record)


async def list_for_user(session: AsyncSession, user_id: str) -> list[JoinRequestRecord]:
    """``user_id``'s own requests, newest first (then by id), at most ``MY_REQUESTS_LIMIT``."""
    rows = await session.execute(
        _record_query()
        .where(join_requests.c.user_id == user_id)
        .order_by(join_requests.c.created_at.desc(), join_requests.c.id.desc())
        .limit(MY_REQUESTS_LIMIT)
    )
    return [_record(row) for row in rows]
