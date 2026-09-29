"""
Stop reads and the stop create — the stops belonging to one trip.

``GET /trips/{slug}/stops`` is the full stop list, and the contract names it the
one place ``notes`` and ``locationSource`` come from: the map endpoint
deliberately does not duplicate them (``docs/api-contract.md``, "Why ``notes``
and ``locationSource`` are absent from map properties"). This module is where
that list comes from.

The same two boundaries ``bikes.py`` and ``trips.py`` hold:

- **It is the only layer that names database columns.** What leaves here is a
  list of ``StopOut`` — the contract model — never a ``Row`` the caller indexes
  by string. The snake_case -> camelCase mapping (``tables.py``, "snake_case
  here, camelCase in the API") happens by explicit keyword at the SELECT site,
  one line below the column it comes from: ``arrivedAt=row.arrived_at``,
  ``locationSource=row.location_source``. No ``alias_generator``, no
  ``model_validate(row)`` — the contract models already spell the wire names
  literally, so an alias layer would be a second encoding of something the model
  already states.
- **It never raises ``ApiError``.** A trip with no stops is an empty list, which
  is an ordinary answer and never a 404 — the trip exists, it just has no stops
  recorded yet. Nothing in ``data/`` decides a status. ``create`` does raise
  ``StopIdOnAnotherTrip``, which is a fact about the *rows* ("this id is taken
  elsewhere"), not a transport decision: the route is what turns it into a 409,
  the same way ``core/security.py`` turns a ``None`` from ``trips.get_by_slug``
  into a 404.

SQLAlchemy Core against ``app/data/tables.py``, per that module's "Core, not
ORM" note.
"""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.tables import stops
from app.models.map import (
    LineStringGeometry,
    MapFeatureCollection,
    PointGeometry,
    StopFeature,
    StopFeatureProperties,
    TrailFeature,
)
from app.models.stop import StopCreate, StopOut


class StopIdOnAnotherTrip(Exception):
    """
    The client-generated id is already a stop on a *different* trip.

    **Carries nothing, deliberately.** The caller has no link to the trip that
    owns the id, so no value from that row — its trip's slug, id or name, its own
    name, notes, coordinates or timestamp — may reach the response
    (``docs/api-contract.md``, "Idempotency"; decision-log Entry 14). The cheapest
    way to guarantee that is to never load the row: the check that raises this is
    an ``EXISTS``, which answers yes/no and selects no columns. An exception with
    no attributes leaves nothing for a handler to disclose even by accident.
    """


async def list_by_trip(session: AsyncSession, trip_id: str) -> list[StopOut]:
    """
    Every stop belonging to this trip, and — the part that matters — no other trip's.

    ``trip_id`` comes from the ``TripContext`` the slug dependency already
    resolved, not from anything the caller typed, so this filter is what keeps
    one trip's stops out of another trip's response — the same isolation the
    slug lookup provides for the trip itself. A trip with no stops returns
    ``[]``.

    **Ordering is for response stability, not a contract guarantee.**
    ``ORDER BY arrived_at, id`` exists so that two identical requests return the
    same JSON: without an ``ORDER BY`` Postgres may return rows in any order it
    likes, which would make the response body churn between calls and turn any
    cached or diffed copy of it into noise. ``id`` breaks the tie so the order
    is total even when two stops share an ``arrived_at``. The API contract does
    **not** promise the stop list in any particular order, and nothing in the
    frontend or in tests may depend on a stop's position in the list — look
    stops up by ``id``.

    That holds even though the order happens to be chronological.
    ``GET /trips/{slug}/map`` *does* require chronological coordinates, because
    a trail drawn out of order is a different line — but that is the map
    handler's own responsibility to establish (``docs/api-contract.md``,
    "Outstanding", item 3), not something it may inherit by calling this
    function. This endpoint is not the map's source of truth for order, and
    changing the ``ORDER BY`` here must not be able to bend a trail.

    ``notes`` is nullable in the schema, unlike ``bikes.specs`` — a stop with no
    notes written comes back as ``null``, not ``""``, and that round-trips
    through ``StopOut.notes``.
    """
    statement = (
        select(
            stops.c.id,
            stops.c.name,
            stops.c.lat,
            stops.c.lng,
            stops.c.location_source,
            stops.c.arrived_at,
            stops.c.notes,
        )
        .where(stops.c.trip_id == trip_id)
        .order_by(stops.c.arrived_at, stops.c.id)
    )

    rows = (await session.execute(statement)).all()

    return [
        StopOut(
            id=row.id,
            name=row.name,
            lat=row.lat,
            lng=row.lng,
            locationSource=row.location_source,
            arrivedAt=row.arrived_at,
            notes=row.notes,
        )
        for row in rows
    ]


async def create(session: AsyncSession, trip_id: str, stop: StopCreate) -> tuple[StopOut, bool]:
    """
    Store one stop under this trip — or recognise that it is already stored.

    Returns ``(stop, created)``. ``created`` is ``True`` when the row was
    inserted and ``False`` when the id was already on this trip and the
    **stored** row is being handed back; the route turns that flag into 201 vs
    200. Raises ``StopIdOnAnotherTrip`` when the id belongs to a stop on some
    other trip, which the route turns into a 409.

    ``trip_id`` is the id the slug dependency resolved, never anything the caller
    typed — the same rule ``list_by_trip`` holds, and here it is also what the
    replay lookup is keyed on.

    **Why this is two statements and not one insert.** The whole of decision-log
    Entry 14 lives in the shape below, and this is the file where someone
    collapses it back. ``stops.id`` is a **global** primary key while the trip it
    belongs to is a *separate* column (``tables.py``), so "this id exists" and
    "this id exists *here*" are two different questions and only the second one
    means replay:

    - **The replay lookup is on ``(trip_id, id)``, never on ``id`` alone.** A
      lookup by ``id`` alone returns another trip's stop through this trip's
      slug — a cross-trip leak, spec Section 12's top-priority failure class. The
      slug is the entire access model, so a query that ignores the parent hands
      out a row this caller's link does not authorise.
    - **The cross-trip case is found by the check below, never by a failed
      ``INSERT``.** Catching the driver's ``IntegrityError`` (or reading zero
      rows back from ``ON CONFLICT ... DO NOTHING``) cannot tell "replay, return
      the stored row" from "different trip, 409" — and an uncaught one renders
      ``500`` / ``INTERNAL_ERROR``, which the offline queue reads as "the server
      broke, retry later" and so retries forever a request that can never
      succeed. The primary key stays what it always was, a backstop; it is not
      the mechanism. (A concurrent insert of the same id between the check and
      the insert here is exactly what that backstop is for.)

    **A replay does not update anything.** The stored row is returned as it is,
    even if this request's body differs from the one that created it — the queue
    is re-sending a write it could not confirm, not editing a stop, and the last
    retry to arrive must not be able to rewrite what the first one stored.
    """
    stored = (
        await session.execute(
            select(
                stops.c.id,
                stops.c.name,
                stops.c.lat,
                stops.c.lng,
                stops.c.location_source,
                stops.c.arrived_at,
                stops.c.notes,
            ).where(stops.c.trip_id == trip_id, stops.c.id == stop.id)
        )
    ).first()

    if stored is not None:
        return (
            StopOut(
                id=stored.id,
                name=stored.name,
                lat=stored.lat,
                lng=stored.lng,
                locationSource=stored.location_source,
                arrivedAt=stored.arrived_at,
                notes=stored.notes,
            ),
            False,
        )

    # Not on this trip — so any row still holding this id is on another one.
    # `EXISTS` rather than selecting the row: the answer needed is yes/no, and
    # not loading the row is what makes it impossible for one of its values to
    # end up in the 409 message.
    if await session.scalar(select(exists().where(stops.c.id == stop.id))):
        raise StopIdOnAnotherTrip

    await session.execute(
        stops.insert().values(
            id=stop.id,
            trip_id=trip_id,
            name=stop.name,
            lat=stop.lat,
            lng=stop.lng,
            # snake_case here, camelCase on the wire (``tables.py``), mapped by
            # explicit keyword one line from the value it comes from.
            location_source=stop.locationSource,
            arrived_at=stop.arrivedAt,
            notes=stop.notes,
        )
    )
    await session.commit()

    return (
        StopOut(
            id=stop.id,
            name=stop.name,
            lat=stop.lat,
            lng=stop.lng,
            locationSource=stop.locationSource,
            arrivedAt=stop.arrivedAt,
            notes=stop.notes,
        ),
        True,
    )


async def map_features(session: AsyncSession, trip_id: str) -> MapFeatureCollection:
    """
    The GeoJSON FeatureCollection for ``GET /trips/{slug}/map``.

    Fetches only the columns the map needs (no ``notes``, no
    ``location_source``), ordered by ``arrived_at`` — that ordering is
    **load-bearing** here, unlike ``list_by_trip``, because the trail's
    coordinate list must be chronological.

    Building the response models here rather than in the route keeps the
    handler a one-liner and keeps every column name in ``data/``.
    """
    statement = (
        select(
            stops.c.id,
            stops.c.name,
            stops.c.lat,
            stops.c.lng,
            stops.c.arrived_at,
        )
        .where(stops.c.trip_id == trip_id)
        .order_by(stops.c.arrived_at, stops.c.id)
    )

    rows = (await session.execute(statement)).all()

    features: list[StopFeature | TrailFeature] = [
        StopFeature(
            id=row.id,
            geometry=PointGeometry(coordinates=[row.lng, row.lat]),
            properties=StopFeatureProperties(name=row.name, arrivedAt=row.arrived_at),
        )
        for row in rows
    ]

    if len(rows) >= 2:
        features.append(
            TrailFeature(
                geometry=LineStringGeometry(coordinates=[[row.lng, row.lat] for row in rows]),
            )
        )

    return MapFeatureCollection(features=features)
