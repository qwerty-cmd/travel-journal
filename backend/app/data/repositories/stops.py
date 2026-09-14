"""
Stop reads — the stops belonging to one trip.

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
  recorded yet. Nothing in ``data/`` decides a status.

SQLAlchemy Core against ``app/data/tables.py``, per that module's "Core, not
ORM" note.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.tables import stops
from app.models.stop import StopOut


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
