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

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.tables import stops, trip_members, trips


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
