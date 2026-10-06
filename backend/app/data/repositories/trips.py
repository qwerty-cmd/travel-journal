"""
Trip reads — the query behind every request in the API.

Every one of the contract's eight endpoints is scoped by ``{slug}``, so
"which trip does this slug belong to" runs before anything else happens
(``docs/api-contract.md``, "Access: two slugs, no accounts"). This module owns
that query; ``app/core/security.py`` owns what the answer *means*.

Two boundaries this module holds deliberately:

- **It does not return an ``Access``.** Which of the two slugs was presented is
  a property of the *request*, not of the stored row — the row carries both. A
  repository that returned "rider" or "viewer" would be reporting a fact about
  an HTTP request it never saw, and would have to be called differently from a
  background job or a seed script that legitimately has no requester at all.
  Callers derive access by comparing the slug they were given against
  ``rider_slug`` / ``viewer_slug`` on the record.
- **It never raises ``ApiError``.** "No row matched" is a plain ``None``.
  Turning that into a 404 is a transport decision, and the 403-vs-404
  distinction it belongs to is enforced in one place (``core/security.py``)
  rather than in every module that can fail to find something. Anything in
  ``data/`` raising an HTTP-shaped error would also make this layer unusable
  outside a request.

SQLAlchemy Core against ``app/data/tables.py``, per that module's "Core, not
ORM" note. This is also the only layer allowed to name database columns: what
leaves here is a typed ``TripRecord``, never a ``Row`` the caller indexes by
string.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.repositories import memberships
from app.data.tables import stops, trip_members, trips, users
from app.models.member import MemberRole


@dataclass(frozen=True, slots=True)
class TripRecord:
    """
    One row of ``trips``, as a typed value rather than a database row.

    Frozen because it is read-only by construction: nothing in the request path
    that resolves a slug also edits the trip, and a mutable copy invites a
    caller to "fix" a slug in memory and be surprised the database disagrees.

    Both slugs are carried, not just the one that matched, because the caller
    needs both to work out which kind of link it was handed. That is also why
    this is not a Pydantic response model — ``TripOut`` deliberately has no slug
    fields, and these values must never be serialised into a response.

    Both slugs are ``None`` on a trip created in the app (migration 0003 made
    them nullable, paired). ``visibility`` (``'public'`` or ``'private'``) and
    ``public_delay_hours`` are what the v2 read gate and the delay filters read.
    """

    id: str
    name: str
    start_date: date
    rider_slug: str | None
    viewer_slug: str | None
    visibility: str
    public_delay_hours: int


_TRIP_COLUMNS = (
    trips.c.id,
    trips.c.name,
    trips.c.start_date,
    trips.c.rider_slug,
    trips.c.viewer_slug,
    trips.c.visibility,
    trips.c.public_delay_hours,
)


def _record(row) -> TripRecord:
    return TripRecord(
        id=row.id,
        name=row.name,
        start_date=row.start_date,
        rider_slug=row.rider_slug,
        viewer_slug=row.viewer_slug,
        visibility=row.visibility,
        public_delay_hours=row.public_delay_hours,
    )


async def get_by_slug(session: AsyncSession, slug: str) -> TripRecord | None:
    """
    The trip whose rider **or** viewer slug is ``slug``; ``None`` if there is none.

    One query over both columns rather than two round trips, and the caller is
    told nothing about *which* column matched — see the module docstring. Both
    slug columns are UNIQUE (``migrations/0001_initial_schema.sql``), so a slug
    identifies at most one trip through either column.

    ``None`` is a legitimate answer here, not an error: a mistyped link and a
    revoked one both land on it, and only the HTTP layer knows what to do about
    that. A slug Postgres cannot even represent is one more way of being no
    trip's slug — see the NUL guard below.
    """
    # A NUL byte cannot be a value of a Postgres `text` column at all: the
    # parameter is rejected by the server before any row is examined
    # (`invalid byte sequence for encoding "UTF8": 0x00`). This is reachable
    # from outside — uvicorn percent-decodes `%00` straight into the path
    # parameter, so `GET /api/trips/abc%00def` arrives here as `"abc\x00def"`.
    #
    # Guarded rather than allowed to raise, for two reasons:
    #
    # - **The layering.** A `DBAPIError` escaping this function would break the
    #   module docstring's promise that nothing in `data/` raises a transport
    #   error, and would move the 404 decision out of `core/security.py`, which
    #   is meant to be the single place "nothing resolved" becomes a status.
    # - **The error code, which is the load-bearing part.** An escaping driver
    #   error renders as `500 INTERNAL_ERROR`, and `code` is what the offline
    #   queue branches on to decide retry-vs-never-retry (decision-log entry 6).
    #   `INTERNAL_ERROR` means "the server broke, try again later", so a queued
    #   write against such a URL would retry forever on a request that can never
    #   succeed. `404 NOT_FOUND` is both true and terminal.
    #
    # No slug can contain a NUL, so returning `None` is not a shortcut around a
    # lookup that might have matched: it is the answer the lookup would give if
    # the database could be asked.
    if "\x00" in slug:
        return None

    statement = select(*_TRIP_COLUMNS).where(
        or_(trips.c.rider_slug == slug, trips.c.viewer_slug == slug)
    )

    row = (await session.execute(statement)).first()
    return None if row is None else _record(row)


async def get_by_id(session: AsyncSession, trip_id: str) -> TripRecord | None:
    """
    The trip with this id, whatever its visibility; ``None`` if there is none.

    Visibility is deliberately not filtered here: whether the caller may see a
    private trip is a membership question, answered in ``core/security.py``
    (``require_trip_reader``). The id is taken as given, with no format check:
    trips from before migration 0003 need not have UUID ids, and a string that
    is no trip's id is simply ``None``, like any other unknown id. A NUL byte is
    ``None`` without a query, for the reason ``get_by_slug`` gives.
    """
    if "\x00" in trip_id:
        return None
    row = (await session.execute(select(*_TRIP_COLUMNS).where(trips.c.id == trip_id))).first()
    return None if row is None else _record(row)


def _last_public_stop_at():
    """
    ``max(arrived_at)`` over the trip's stops visible to the public, correlated on ``trips``.

    Visible means ``arrived_at <= now() - public_delay_hours``, on the database
    clock at statement time: the same rule ``stops.public_visibility`` applies to
    the stop list and the map, so ``lastPublicStopAt`` always names a stop a
    non-member can see.
    """
    delay = func.make_interval(0, 0, 0, 0, trips.c.public_delay_hours)
    return (
        select(func.max(stops.c.arrived_at))
        .where(stops.c.trip_id == trips.c.id, stops.c.arrived_at <= func.now() - delay)
        .scalar_subquery()
    )


def _rider_count():
    """Active ``trip_members`` rows (leaders included), correlated on ``trips``."""
    return (
        select(func.count())
        .select_from(trip_members)
        .where(trip_members.c.trip_id == trips.c.id, trip_members.c.revoked_at.is_(None))
        .scalar_subquery()
    )


@dataclass(frozen=True, slots=True)
class TripStats:
    """The two derived ``TripOut`` values: the same for every caller, members included."""

    rider_count: int
    last_public_stop_at: datetime | None


async def get_stats(session: AsyncSession, trip_id: str) -> TripStats:
    """
    ``riderCount`` and ``lastPublicStopAt`` for one trip, computed as the public list computes them.

    One statement, built from the same two expressions as ``list_public``, so
    a trip's own page and its Discover entry can't disagree.
    """
    row = (
        await session.execute(
            select(
                _rider_count().label("rider_count"),
                _last_public_stop_at().label("last_public_stop_at"),
            ).where(trips.c.id == trip_id)
        )
    ).one()
    return TripStats(rider_count=row.rider_count, last_public_stop_at=row.last_public_stop_at)


@dataclass(frozen=True, slots=True)
class PublicTripSummary:
    """
    One public trip as the anonymous Discover list shows it (``GET /api/v2/trips``).

    Carries no slug and no member identity: only what ``TripSummaryOut`` exposes.
    """

    id: str
    name: str
    start_date: date
    rider_count: int
    last_public_stop_at: datetime | None


async def list_public(
    session: AsyncSession,
    *,
    after: tuple[datetime | None, str] | None,
    limit: int,
) -> list[PublicTripSummary]:
    """
    Up to ``limit`` public trips, in list order, strictly after the keyset ``after``.

    **Which trips.** ``visibility = 'public'`` only, and that filter is applied
    before the keyset: no cursor value can reach a private trip.

    **Order.** ``last_public_stop_at`` descending with nulls last, then ``id``
    ascending (``docs/api-contract.md``, "Notes per endpoint (v2)"). ``id`` is the
    primary key, so the pair is a total order and ties never reorder.

    **``last_public_stop_at``** is ``max(arrived_at)`` over the trip's stops with
    ``arrived_at <= now() - public_delay_hours`` — the public delay, on the
    database clock at statement time. The same value for every caller.

    **``rider_count``** counts the trip's active ``trip_members`` rows
    (``revoked_at IS NULL``), leaders included.

    ``after`` is the ``(last_public_stop_at, id)`` of the previous page's last
    row, or ``None`` for the first page.
    """
    public = (
        select(
            trips.c.id,
            trips.c.name,
            trips.c.start_date,
            _rider_count().label("rider_count"),
            _last_public_stop_at().label("last_public_stop_at"),
        )
        .where(trips.c.visibility == "public")
        .subquery()
    )

    statement = select(public).order_by(
        public.c.last_public_stop_at.desc().nulls_last(), public.c.id.asc()
    )
    if after is not None:
        after_at, after_id = after
        if after_at is None:
            # Already in the null tail: only later ids remain.
            statement = statement.where(
                public.c.last_public_stop_at.is_(None), public.c.id > after_id
            )
        else:
            statement = statement.where(
                or_(
                    public.c.last_public_stop_at < after_at,
                    and_(public.c.last_public_stop_at == after_at, public.c.id > after_id),
                    public.c.last_public_stop_at.is_(None),
                )
            )

    rows = (await session.execute(statement.limit(limit))).all()
    return [
        PublicTripSummary(
            id=row.id,
            name=row.name,
            start_date=row.start_date,
            rider_count=row.rider_count,
            last_public_stop_at=row.last_public_stop_at,
        )
        for row in rows
    ]


# --------------------------------------------------------------------------
# Creating a trip (`POST /api/v2/trips`), editing it, and a member's trips
# --------------------------------------------------------------------------


async def creator_replay_role(
    session: AsyncSession, trip_id: str, user_id: str
) -> MemberRole | None:
    """
    The caller's active role on ``trip_id`` if they created it, else ``None``.

    This is the whole replay test for ``POST /api/v2/trips`` (contract,
    "Idempotency: additions"): the id is a replay only when ``trips.created_by``
    is the caller **and** the caller is still an active member. One statement.
    ``None`` covers every other case alike: no such trip, someone else's trip,
    a legacy trip (``created_by`` NULL), and a creator who has since left or
    been revoked. A NUL byte is ``None`` without a query (see ``get_by_slug``).
    """
    if "\x00" in trip_id:
        return None
    role = await session.scalar(
        select(trip_members.c.role)
        .join(trips, trips.c.id == trip_members.c.trip_id)
        .where(
            trips.c.id == trip_id,
            trips.c.created_by == user_id,
            trip_members.c.user_id == user_id,
            trip_members.c.revoked_at.is_(None),
        )
    )
    return None if role is None else MemberRole(role)


class CreateOutcome(StrEnum):
    """Which branch ``create_for_user`` took."""

    CREATED = "created"  # new trip and leader membership, committed
    REPLAY = "replay"  # the caller's own trip, still an active member: nothing written
    ID_TAKEN = "id_taken"  # the id is some other trip's (another user's, or a legacy one)
    CAP_REACHED = "cap_reached"  # the caller has already created `cap` trips


@dataclass(frozen=True, slots=True)
class CreateResult:
    """``create_for_user``'s answer: the branch, and for CREATED/REPLAY the trip and role."""

    outcome: CreateOutcome
    trip: TripRecord | None = None
    role: MemberRole | None = None


async def create_for_user(
    session: AsyncSession,
    *,
    trip_id: str,
    name: str,
    start_date: date,
    visibility: str,
    user_id: str,
    cap: int,
) -> CreateResult:
    """
    Create a trip with ``user_id`` as its first leader, in one transaction, or say why not.

    **Order.** Replay first, then "id taken", then the lifetime cap, then the
    insert — so a replay by a user already at the cap is still a replay.

    **Race safety.** The first statement locks the creator's ``users`` row
    (``SELECT … FOR UPDATE``), and every create by that user takes the same lock,
    so two parallel creates are serialised: the second counts the first's trip,
    and at 19 only one of them gets past 20. The same lock turns two parallel
    sends of one id into a create and a replay. Two *different* users racing on
    one id hold different locks, so the insert is ``ON CONFLICT (id) DO NOTHING``
    and a row that didn't go in is ``ID_TAKEN`` — detected, never a primary-key
    violation surfacing as a ``500``.

    **Transaction.** On ``CREATED`` the trip (both slugs ``NULL``, ``created_by``
    the caller, ``public_delay_hours`` from the column default of 24) and the
    leader membership commit together. Every other branch ends the transaction
    with nothing written, releasing the lock.
    """
    await session.execute(select(users.c.id).where(users.c.id == user_id).with_for_update())

    role = await creator_replay_role(session, trip_id, user_id)
    if role is not None:
        trip = await get_by_id(session, trip_id)
        await session.commit()
        return CreateResult(CreateOutcome.REPLAY, trip, role)

    if await session.scalar(select(exists().where(trips.c.id == trip_id))):
        await session.commit()
        return CreateResult(CreateOutcome.ID_TAKEN)

    created = await session.scalar(
        select(func.count()).select_from(trips).where(trips.c.created_by == user_id)
    )
    if created >= cap:
        await session.commit()
        return CreateResult(CreateOutcome.CAP_REACHED)

    inserted = await session.scalar(
        pg_insert(trips)
        .values(
            id=trip_id,
            name=name,
            start_date=start_date,
            visibility=visibility,
            created_by=user_id,
        )
        .on_conflict_do_nothing(index_elements=[trips.c.id])
        .returning(trips.c.id)
    )
    if inserted is None:
        await session.rollback()
        return CreateResult(CreateOutcome.ID_TAKEN)

    await memberships.add(session, trip_id, user_id, MemberRole.LEADER)
    trip = await get_by_id(session, trip_id)
    await session.commit()
    return CreateResult(CreateOutcome.CREATED, trip, MemberRole.LEADER)


# camelCase `TripPatch` field -> snake_case column.
_PATCH_FIELD_TO_COLUMN = {
    "name": "name",
    "visibility": "visibility",
    "publicDelayHours": "public_delay_hours",
}


async def update_settings(
    session: AsyncSession, trip_id: str, fields: dict[str, object]
) -> TripRecord | None:
    """
    Apply the ``TripPatch`` fields present in ``fields`` to ``trip_id``, and return the trip.

    ``fields`` is ``TripPatch.model_dump(exclude_unset=True)``: an omitted
    field is absent and left alone, and an empty dict writes nothing (the
    contract's "empty body → 200 with nothing changed"). Last write wins.
    """
    if fields:
        values = {_PATCH_FIELD_TO_COLUMN[key]: value for key, value in fields.items()}
        await session.execute(update(trips).where(trips.c.id == trip_id).values(**values))
        await session.commit()
    return await get_by_id(session, trip_id)


@dataclass(frozen=True, slots=True)
class MemberTrip:
    """One trip the user is an active member of, with their role on it (``MyTripOut``)."""

    id: str
    name: str
    start_date: date
    role: MemberRole


async def list_for_member(session: AsyncSession, user_id: str) -> list[MemberTrip]:
    """
    Every trip ``user_id`` has an **active** membership on, newest membership first.

    Revoked and self-departed rows are excluded; pending join requests are not
    memberships and never appear. Ordered by ``joined_at`` descending, then trip
    id ascending, so the order is total and stable.
    """
    rows = (
        await session.execute(
            select(trips.c.id, trips.c.name, trips.c.start_date, trip_members.c.role)
            .join(trip_members, trip_members.c.trip_id == trips.c.id)
            .where(trip_members.c.user_id == user_id, trip_members.c.revoked_at.is_(None))
            .order_by(trip_members.c.joined_at.desc(), trips.c.id.asc())
        )
    ).all()
    return [
        MemberTrip(id=row.id, name=row.name, start_date=row.start_date, role=MemberRole(row.role))
        for row in rows
    ]
