"""
``PATCH /api/trips/{slug}/bikes/{id}`` with an explicit ``null`` is a 422.

``docs/api-contract.md``: every ``BikePatch`` field is optional, absent fields
are left alone, and absent "is not the same as a field explicitly set to
null". Every bike column is NOT NULL, so before this fix an explicit null went
straight into the ``UPDATE`` and came back as a 500. It is now rejected at
validation as ``422`` / ``VALIDATION_ERROR`` and the stored bike is untouched.
The OpenAPI schema says the same thing: optional, never nullable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus

import pytest
from conftest import SeededBike, SeededTrip
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.data import tables
from app.data.db import get_session
from app.models.common import ErrorCode, ErrorEnvelope

PATCH_PATH = "/api/trips/{slug}/bikes/{bike_id}"
FIELDS = ["riderName", "make", "model", "year", "specs"]


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    import app.main

    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application = app.main.app
    application.dependency_overrides[get_session] = session_override
    try:
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            yield http_client
    finally:
        application.dependency_overrides.pop(get_session, None)


async def _stored(engine: AsyncEngine, bike_id: str) -> tuple:
    async with engine.connect() as conn:
        return tuple(
            (
                await conn.execute(
                    select(
                        tables.bikes.c.rider_name,
                        tables.bikes.c.make,
                        tables.bikes.c.model,
                        tables.bikes.c.year,
                        tables.bikes.c.specs,
                    ).where(tables.bikes.c.id == bike_id)
                )
            ).one()
        )


@pytest.mark.parametrize("field", FIELDS)
async def test_explicit_null_is_422_and_changes_nothing(
    field: str,
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    seeded_bikes: list[SeededBike],
    migrated_engine: AsyncEngine,
) -> None:
    trip = seeded_trips[0]
    bike = next(b for b in seeded_bikes if b.trip_id == trip.id)
    before = await _stored(migrated_engine, bike.id)

    response = await client.patch(
        PATCH_PATH.format(slug=trip.rider_slug, bike_id=bike.id), json={field: None}
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code == ErrorCode.VALIDATION_ERROR
    assert field in envelope.error.message
    assert await _stored(migrated_engine, bike.id) == before


async def test_null_alongside_a_valid_field_rejects_the_whole_patch(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    seeded_bikes: list[SeededBike],
    migrated_engine: AsyncEngine,
) -> None:
    """No partial apply: the valid field is not written either."""
    trip = seeded_trips[0]
    bike = next(b for b in seeded_bikes if b.trip_id == trip.id)
    before = await _stored(migrated_engine, bike.id)

    response = await client.patch(
        PATCH_PATH.format(slug=trip.rider_slug, bike_id=bike.id),
        json={"make": "Changed make", "year": None},
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert await _stored(migrated_engine, bike.id) == before


async def test_omitted_fields_still_leave_the_bike_alone(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    seeded_bikes: list[SeededBike],
) -> None:
    trip = seeded_trips[0]
    bike = next(b for b in seeded_bikes if b.trip_id == trip.id)

    response = await client.patch(
        PATCH_PATH.format(slug=trip.rider_slug, bike_id=bike.id), json={"make": "New make"}
    )

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["make"] == "New make"
    assert (body["riderName"], body["model"], body["year"], body["specs"]) == (
        bike.rider_name,
        bike.model,
        bike.year,
        bike.specs,
    )


def test_openapi_bikepatch_fields_are_optional_and_not_nullable() -> None:
    from app.main import app

    schema = app.openapi()["components"]["schemas"]["BikePatch"]
    assert not schema.get("required")
    assert set(schema["properties"]) == set(FIELDS)
    for name, prop in schema["properties"].items():
        assert "anyOf" not in prop, name
        assert prop.get("type") in ("string", "integer"), name
        assert "default" not in prop, name
