"""
``StopCreate.lat`` / ``lng`` are real WGS 84 coordinates or a 422.

Latitude is bounded to [-90, 90], longitude to [-180, 180], both inclusive, and
NaN / Infinity are rejected. Python's ``json`` module accepts the non-standard
literals ``NaN``, ``Infinity`` and ``-Infinity``, so a raw body can carry them
even though ``JSON.stringify`` can never produce one (it writes ``null``). The
raw-body tests send the literal bytes via ``content=`` for exactly that reason.
Every rejection writes no row.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import pytest
from conftest import SeededTrip, make_async_client
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.data import tables
from app.data.db import get_session
from app.models.common import ErrorCode, ErrorEnvelope

STOPS_PATH = "/api/trips/{slug}/stops"


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
        async with make_async_client(application) as http_client:
            yield http_client
    finally:
        application.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def created_ids(migrated_engine: AsyncEngine) -> AsyncIterator[list[str]]:
    ids: list[str] = []
    try:
        yield ids
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.stops.delete().where(tables.stops.c.id.in_(ids)))


def payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(uuid4()),
        "name": "Daly Waters Pub",
        "lat": -16.2503,
        "lng": 133.3703,
        "locationSource": "gps",
        "arrivedAt": "2026-06-14T15:15:00+09:30",
    }
    body.update(overrides)
    return body


async def row_exists(engine: AsyncEngine, stop_id: str) -> bool:
    async with engine.connect() as conn:
        return (
            await conn.execute(select(tables.stops.c.id).where(tables.stops.c.id == stop_id))
        ).first() is not None


def assert_validation_error(response: Any, field: str) -> None:
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code == ErrorCode.VALIDATION_ERROR
    assert field in envelope.error.message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lat", 90.0001),
        ("lat", -90.0001),
        ("lat", 1000),
        ("lng", 180.0001),
        ("lng", -180.0001),
        ("lng", 1000),
    ],
)
async def test_out_of_range_coordinate_is_422_and_writes_nothing(
    field: str,
    value: float,
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
    migrated_engine: AsyncEngine,
) -> None:
    body = payload(**{field: value})
    created_ids.append(body["id"])

    response = await client.post(STOPS_PATH.format(slug=seeded_trips[0].rider_slug), json=body)

    assert_validation_error(response, field)
    assert not await row_exists(migrated_engine, body["id"])


@pytest.mark.parametrize(("lat", "lng"), [(90, 180), (-90, -180), (0, 0)])
async def test_boundary_coordinates_are_accepted(
    lat: float,
    lng: float,
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    body = payload(lat=lat, lng=lng)
    created_ids.append(body["id"])

    response = await client.post(STOPS_PATH.format(slug=seeded_trips[0].rider_slug), json=body)

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert (response.json()["lat"], response.json()["lng"]) == (lat, lng)


@pytest.mark.parametrize("field", ["lat", "lng"])
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_non_finite_literal_in_raw_json_is_422_and_writes_nothing(
    field: str,
    literal: str,
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
    migrated_engine: AsyncEngine,
) -> None:
    body = payload(**{field: "__PLACEHOLDER__"})
    created_ids.append(body["id"])
    raw = json.dumps(body).replace('"__PLACEHOLDER__"', literal)
    assert f'"{field}": {literal}' in raw

    response = await client.post(
        STOPS_PATH.format(slug=seeded_trips[0].rider_slug),
        content=raw.encode(),
        headers={"Content-Type": "application/json"},
    )

    assert_validation_error(response, field)
    assert not await row_exists(migrated_engine, body["id"])


def test_openapi_declares_the_coordinate_bounds() -> None:
    from app.main import app

    props = app.openapi()["components"]["schemas"]["StopCreate"]["properties"]
    assert (props["lat"]["minimum"], props["lat"]["maximum"]) == (-90, 90)
    assert (props["lng"]["minimum"], props["lng"]["maximum"]) == (-180, 180)
