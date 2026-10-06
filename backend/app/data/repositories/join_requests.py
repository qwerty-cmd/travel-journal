"""
Join requests -- the ``join_requests`` table from migration 0003 (decision-log Entry 29).

**Context.** A signed-in user asks to ride on a trip; a leader decides. This
module holds the read the trip view needs (``has_pending``, which makes
``TripOut.viewer.role`` ``pending``) and the requester's side of the lifecycle
(``t-am-join-requester``): create, cancel and the caller's own list
(``docs/api-contract.md``, "Join request create", "Cancel", "Me /
join-requests"); and the leader's side (``t-am-join-leader``): the trip's
list, decide and unblock ("Trip join-requests (leader)", "Decision",
"Unblock").

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
- **Race safety of decide and unblock.** Both lock the trip row first, then
  re-read the caller's leadership, then lock the request row, so they
  serialise with ``create`` and every membership change on the trip, and an
  ``approve``'s rider membership is inserted in the same transaction as the
  state change. A cancel racing a decision is decided by the request row lock:
  whichever commits second sees the other's state.
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

from app.data.repositories import memberships
from app.data.tables import join_requests, trip_members, trips, users
from app.models.member import MemberRole

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


# --------------------------------------------------------------------------
# The leader's side: list, decide, unblock (t-am-join-leader)
# --------------------------------------------------------------------------

# The state each decision moves a pending request to (contract, "Decision").
DECISION_STATES = {
    "approve": "approved",
    "reject": "rejected",
    "reject_and_block": "blocked",
}


@dataclass(frozen=True, slots=True)
class TripJoinRequestRecord:
    """One join request as the trip's leaders see it: the requester's id and display name only."""

    id: str
    user_id: str
    display_name: str
    state: str
    via: str
    message: str | None
    created_at: datetime


class LeaderOutcome(StrEnum):
    """``decide``'s and ``unblock``'s answer. Every outcome but DONE wrote nothing."""

    DONE = "done"  # changed now, or (decide) already in the state this decision produces
    NOT_FOUND = "not_found"  # no such request on this trip
    NOT_A_MEMBER = "not_a_member"  # the caller's own active row went before the lock
    NOT_A_LEADER = "not_a_leader"  # the caller is no longer a leader
    CONFLICT = "conflict"  # decide: decided otherwise; unblock: not blocked


@dataclass(frozen=True, slots=True)
class LeaderResult:
    outcome: LeaderOutcome
    request: TripJoinRequestRecord | None = None


def _trip_record_query():
    return select(
        join_requests.c.id,
        join_requests.c.user_id,
        users.c.display_name,
        join_requests.c.state,
        join_requests.c.via,
        join_requests.c.message,
        join_requests.c.created_at,
    ).join(users, users.c.id == join_requests.c.user_id)


def _trip_record(row) -> TripJoinRequestRecord:
    return TripJoinRequestRecord(
        id=row.id,
        user_id=row.user_id,
        display_name=row.display_name,
        state=row.state,
        via=row.via,
        message=row.message,
        created_at=row.created_at,
    )


async def list_for_trip(
    session: AsyncSession, trip_id: str, state: str
) -> list[TripJoinRequestRecord]:
    """The trip's requests in ``state`` (``pending`` or ``blocked``), oldest first (then by id)."""
    rows = await session.execute(
        _trip_record_query()
        .where(join_requests.c.trip_id == trip_id, join_requests.c.state == state)
        .order_by(join_requests.c.created_at, join_requests.c.id)
    )
    return [_trip_record(row) for row in rows]


async def _leader_check(
    session: AsyncSession, trip_id: str, caller_id: str
) -> LeaderOutcome | None:
    """Under the trip lock: why the caller is no longer a leader, or ``None`` if they still are."""
    caller = await memberships.get_for_user(session, trip_id, caller_id)
    if caller is None or not caller.active:
        return LeaderOutcome.NOT_A_MEMBER
    if caller.role is not MemberRole.LEADER:
        return LeaderOutcome.NOT_A_LEADER
    return None


async def _locked_request(session: AsyncSession, trip_id: str, request_id: str):
    """``SELECT … FOR UPDATE`` on ``request_id`` if it is on ``trip_id``; else ``None``."""
    if "\x00" in request_id:  # Postgres `text` can't hold it; no request id contains one
        return None
    return (
        await session.execute(
            select(join_requests.c.state, join_requests.c.user_id)
            .where(join_requests.c.id == request_id, join_requests.c.trip_id == trip_id)
            .with_for_update()
        )
    ).first()


async def _reread(session: AsyncSession, request_id: str) -> TripJoinRequestRecord:
    row = (
        await session.execute(_trip_record_query().where(join_requests.c.id == request_id))
    ).one()
    return _trip_record(row)


async def decide(
    session: AsyncSession,
    *,
    trip_id: str,
    request_id: str,
    caller_id: str,
    action: str,
    now: datetime,
) -> LeaderResult:
    """
    Apply a leader's ``action`` to request ``request_id`` on ``trip_id``. Commits.

    **Order.** The trip row is locked first (``memberships._lock_trip``, the
    lock ``create`` and every membership change take), then the caller's
    leadership is re-read, then the request row is locked. A request on another
    trip, or no such request, is ``NOT_FOUND``.

    - Pending → the action's state, with ``decided_at`` = ``now`` and
      ``decided_by`` = the caller. ``approve`` also inserts an active ``rider``
      membership in the same transaction, unless the requester already has an
      active one.
    - Already in the action's state → ``DONE`` with nothing changed.
    - Any other state (decided otherwise, or cancelled) → ``CONFLICT``.
    """
    target = DECISION_STATES[action]
    await memberships._lock_trip(session, trip_id)
    refusal = await _leader_check(session, trip_id, caller_id)
    if refusal is not None:
        await session.commit()
        return LeaderResult(refusal)
    row = await _locked_request(session, trip_id, request_id)
    if row is None:
        await session.commit()
        return LeaderResult(LeaderOutcome.NOT_FOUND)
    if row.state != "pending" and row.state != target:
        await session.commit()
        return LeaderResult(LeaderOutcome.CONFLICT)

    if row.state == "pending":
        await session.execute(
            update(join_requests)
            .where(join_requests.c.id == request_id)
            .values(state=target, decided_at=now, decided_by=caller_id)
        )
        if target == "approved":
            member = await memberships.get_for_user(session, trip_id, row.user_id)
            if member is None or not member.active:
                await memberships.add(session, trip_id, row.user_id, MemberRole.RIDER)
    record = await _reread(session, request_id)
    await session.commit()
    return LeaderResult(LeaderOutcome.DONE, record)


async def unblock(
    session: AsyncSession, *, trip_id: str, request_id: str, caller_id: str
) -> LeaderResult:
    """
    Turn blocked request ``request_id`` on ``trip_id`` into a rejected one. Commits.

    Same locking order as ``decide``. ``decided_at`` (and ``decided_by``) are
    kept, so the 7-day cooldown ``create`` applies still runs from the original
    decision. A request that isn't blocked is ``CONFLICT``, with nothing changed.
    """
    await memberships._lock_trip(session, trip_id)
    refusal = await _leader_check(session, trip_id, caller_id)
    if refusal is not None:
        await session.commit()
        return LeaderResult(refusal)
    row = await _locked_request(session, trip_id, request_id)
    if row is None:
        await session.commit()
        return LeaderResult(LeaderOutcome.NOT_FOUND)
    if row.state != "blocked":
        await session.commit()
        return LeaderResult(LeaderOutcome.CONFLICT)

    await session.execute(
        update(join_requests).where(join_requests.c.id == request_id).values(state="rejected")
    )
    record = await _reread(session, request_id)
    await session.commit()
    return LeaderResult(LeaderOutcome.DONE, record)
