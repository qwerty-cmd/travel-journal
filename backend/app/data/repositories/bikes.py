"""
Bike reads and the bike create — the bikes belonging to one trip.

``GET /trips/{slug}`` returns trip metadata *plus this trip's bikes* in one
response (``docs/api-contract.md``, "Notes per endpoint"), so the app's first
load is a single request. This module is where that second half comes from.

Two boundaries this module holds, the same ones ``trips.py`` holds:

- **It is the only layer that names database columns.** What leaves here is a
  list of ``BikeOut`` — the contract model — never a ``Row`` the caller indexes
  by string. That is also where the snake_case -> camelCase mapping happens
  (``tables.py``, "snake_case here, camelCase in the API"): ``rider_name``
  becomes ``riderName`` by explicit keyword at the SELECT site, one line below
  the column it comes from, so the two are read together. There is deliberately
  no ``alias_generator`` and no ``model_validate(row)`` spread doing it
  implicitly — the contract models already spell the wire names literally, and
  a mapping layer would make the wire name a second encoding of something the
  model already states.
- **It never raises ``ApiError``.** A trip with no bikes is an empty list, which
  is a perfectly ordinary answer and never a 404 — the trip exists, it just has
  no bikes recorded yet. Nothing in ``data/`` decides a status. ``create`` does
  raise ``BikeIdOnAnotherTrip``, which is a fact about the *rows* ("this id is
  taken elsewhere"), not a transport decision: the route turns it into a 409.

SQLAlchemy Core against ``app/data/tables.py``, per that module's "Core, not
ORM" note: no relationship loading, no lazy attribute that would fire a second
query from inside a response serialiser.
"""

from __future__ import annotations

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.tables import bikes
from app.models.bike import BikeCreate, BikeOut, BikePatch


class BikeIdOnAnotherTrip(Exception):
    """
    The client-generated id is already a bike on a *different* trip.

    Carries nothing — same reasoning as ``StopIdOnAnotherTrip``: the check is
    an ``EXISTS``, no columns are loaded, so there is nothing to disclose.
    """


async def list_by_trip(session: AsyncSession, trip_id: str) -> list[BikeOut]:
    """
    Every bike belonging to this trip, and — the part that matters — no other trip's.

    ``trip_id`` comes from the ``TripContext`` the slug dependency already
    resolved, not from anything the caller typed, so this filter is what keeps
    one trip's bikes out of another trip's response — the same isolation the
    slug lookup provides for the trip itself. A trip with no bikes returns
    ``[]``.

    **Ordering is for response stability, not a contract guarantee.**
    ``ORDER BY rider_name, id`` exists so that two identical requests return the
    same JSON: without an ``ORDER BY`` Postgres may return rows in any order it
    likes, which would make the response body churn between calls and turn any
    cached or diffed copy of it into noise. ``id`` breaks the tie so the order
    is total even when two bikes share a rider name. The API contract does
    **not** promise ``bikes`` in any particular order, and nothing in the
    frontend or in tests may depend on a bike's position in the list — look
    bikes up by ``id``.
    """
    statement = (
        select(
            bikes.c.id,
            bikes.c.rider_name,
            bikes.c.make,
            bikes.c.model,
            bikes.c.year,
            bikes.c.specs,
        )
        .where(bikes.c.trip_id == trip_id)
        .order_by(bikes.c.rider_name, bikes.c.id)
    )

    rows = (await session.execute(statement)).all()

    return [
        BikeOut(
            id=row.id,
            riderName=row.rider_name,
            make=row.make,
            model=row.model,
            year=row.year,
            specs=row.specs,
        )
        for row in rows
    ]


async def create(session: AsyncSession, trip_id: str, bike: BikeCreate) -> tuple[BikeOut, bool]:
    """
    Store one bike under this trip — or recognise that it is already stored.

    Returns ``(bike, created)``: ``True`` when inserted, ``False`` when the id
    was already on this trip and the **stored** row is handed back unchanged.
    Raises ``BikeIdOnAnotherTrip`` when the id belongs to a bike on another
    trip. Same three-way branch as ``stops.create`` (decision-log Entry 14).
    """
    stored = (
        await session.execute(
            select(
                bikes.c.id,
                bikes.c.rider_name,
                bikes.c.make,
                bikes.c.model,
                bikes.c.year,
                bikes.c.specs,
            ).where(bikes.c.trip_id == trip_id, bikes.c.id == bike.id)
        )
    ).first()

    if stored is not None:
        return (
            BikeOut(
                id=stored.id,
                riderName=stored.rider_name,
                make=stored.make,
                model=stored.model,
                year=stored.year,
                specs=stored.specs,
            ),
            False,
        )

    if await session.scalar(select(exists().where(bikes.c.id == bike.id))):
        raise BikeIdOnAnotherTrip

    await session.execute(
        bikes.insert().values(
            id=bike.id,
            trip_id=trip_id,
            rider_name=bike.riderName,
            make=bike.make,
            model=bike.model,
            year=bike.year,
            specs=bike.specs,
        )
    )
    await session.commit()

    return (
        BikeOut(
            id=bike.id,
            riderName=bike.riderName,
            make=bike.make,
            model=bike.model,
            year=bike.year,
            specs=bike.specs,
        ),
        True,
    )


# camelCase field name -> snake_case column name
_FIELD_TO_COLUMN = {
    "riderName": "rider_name",
    "make": "make",
    "model": "model",
    "year": "year",
    "specs": "specs",
}


async def patch(
    session: AsyncSession, trip_id: str, bike_id: str, body: BikePatch
) -> BikeOut | None:
    """
    Partial update of a bike on this trip. Returns the updated bike, or
    ``None`` when no bike with this id exists on this trip (the handler turns
    that into a 404). Last-write-wins — no conflict detection.
    """
    fields = body.model_dump(exclude_unset=True)

    if fields:
        values = {_FIELD_TO_COLUMN[k]: v for k, v in fields.items()}
        await session.execute(
            update(bikes).where(bikes.c.trip_id == trip_id, bikes.c.id == bike_id).values(**values)
        )
        await session.commit()

    row = (
        await session.execute(
            select(
                bikes.c.id,
                bikes.c.rider_name,
                bikes.c.make,
                bikes.c.model,
                bikes.c.year,
                bikes.c.specs,
            ).where(bikes.c.trip_id == trip_id, bikes.c.id == bike_id)
        )
    ).first()

    if row is None:
        return None

    return BikeOut(
        id=row.id,
        riderName=row.rider_name,
        make=row.make,
        model=row.model,
        year=row.year,
        specs=row.specs,
    )
