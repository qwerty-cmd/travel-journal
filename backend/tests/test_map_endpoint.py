"""
``GET /api/trips/{slug}/map`` -- the GeoJSON map, asserted against the contract.

The contract (``docs/api-contract.md``, "Map response") promises:

1. Either slug accepted (read endpoint).
2. Response is a GeoJSON FeatureCollection (``MapFeatureCollection``).
3. One Point feature per stop, with ``Feature.id`` = stop id, properties =
   ``{name, arrivedAt}`` only.
4. One LineString trail feature when 2+ stops, coordinates chronological.
5. NO trail when 0 or 1 stops.
6. Empty ``features: []`` for a trip with no stops -- valid 200.
7. GeoJSON coordinates are ``[longitude, latitude]`` -- reversed from Stop
   ``lat``/``lng``.
8. 404 on unknown slug.
9. HEAD returns same headers without body.

Setup matches ``test_stops_list_endpoint.py``: real app, real database,
``get_session`` overridden onto this test's engine.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from typing import Any

import pytest
from conftest import SeededTrip
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.data import tables
from app.data.db import get_session
from app.models.common import ErrorCode, ErrorEnvelope

MAP_PATH = "/api/trips/{slug}/map"
UNKNOWN_SLUG = "definitely-not-a-real-slug-0000"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeededStop:
    """A stop row in the database's own spelling."""

    id: str
    trip_id: str
    name: str
    lat: float
    lng: float
    location_source: str
    arrived_at: datetime
    notes: str | None


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """The real application, talking to the test database on this test's event loop."""
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


@pytest.fixture
async def seeded_stops(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[list[SeededStop]]:
    """
    Stops on **both** seeded trips, removed again on teardown.

    Three stops on the first trip with distinct coordinates and different
    ``arrived_at`` values, inserted out of chronological order so a handler
    returning insertion order would not accidentally match the expected trail.
    One stop on the second trip to guard cross-trip isolation.
    """
    first, second = seeded_trips
    prefix = f"test-stop-{secrets.token_urlsafe(8)}"
    base = datetime(2026, 6, 2, 8, 30, tzinfo=UTC)

    seeded = [
        # Inserted second chronologically, but first in the list -- tests that
        # the trail is sorted by arrivedAt, not insertion order.
        SeededStop(
            id=f"{prefix}-02",
            trip_id=first.id,
            name="Daly Waters Pub",
            lat=-16.2503,
            lng=133.3703,
            location_source="gps",
            arrived_at=base + timedelta(hours=6),
            notes="Schnitzel.",
        ),
        SeededStop(
            id=f"{prefix}-01",
            trip_id=first.id,
            name="Katherine Hot Springs",
            lat=-14.4652,
            lng=132.2635,
            location_source="manual",
            arrived_at=base,
            notes=None,
        ),
        SeededStop(
            id=f"{prefix}-03",
            trip_id=first.id,
            name="Threeways Roadhouse",
            lat=-19.3097,
            lng=134.2098,
            location_source="gps",
            arrived_at=base + timedelta(hours=12),
            notes="Fuel and a pie.",
        ),
        # Other trip entirely.
        SeededStop(
            id=f"{prefix}-04",
            trip_id=second.id,
            name="Perth CBD",
            lat=-31.9523,
            lng=115.8613,
            location_source="manual",
            arrived_at=base + timedelta(days=30),
            notes="Other trip.",
        ),
    ]

    async with migrated_engine.begin() as conn:
        for stop in seeded:
            await conn.execute(
                tables.stops.insert().values(
                    id=stop.id,
                    trip_id=stop.trip_id,
                    name=stop.name,
                    lat=stop.lat,
                    lng=stop.lng,
                    location_source=stop.location_source,
                    arrived_at=stop.arrived_at,
                    notes=stop.notes,
                )
            )

    try:
        yield seeded
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                tables.stops.delete().where(tables.stops.c.id.in_([s.id for s in seeded]))
            )


@pytest.fixture
async def single_stop_trip(
    migrated_engine: AsyncEngine,
) -> AsyncIterator[tuple[SeededTrip, SeededStop]]:
    """A trip with exactly one stop -- for testing the no-trail case."""
    trip = SeededTrip(
        id=f"test-trip-{secrets.token_urlsafe(8)}",
        name="Single stop trip",
        start_date=date(2026, 7, 1),
        rider_slug=secrets.token_urlsafe(16),
        viewer_slug=secrets.token_urlsafe(16),
    )
    stop = SeededStop(
        id=f"test-stop-{secrets.token_urlsafe(8)}-solo",
        trip_id=trip.id,
        name="Lone Pine",
        lat=-23.7000,
        lng=133.8700,
        location_source="gps",
        arrived_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        notes=None,
    )

    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.trips.insert().values(
                id=trip.id,
                name=trip.name,
                rider_slug=trip.rider_slug,
                viewer_slug=trip.viewer_slug,
                start_date=trip.start_date,
            )
        )
        await conn.execute(
            tables.stops.insert().values(
                id=stop.id,
                trip_id=stop.trip_id,
                name=stop.name,
                lat=stop.lat,
                lng=stop.lng,
                location_source=stop.location_source,
                arrived_at=stop.arrived_at,
                notes=stop.notes,
            )
        )

    try:
        yield trip, stop
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.stops.delete().where(tables.stops.c.id == stop.id))
            await conn.execute(tables.trips.delete().where(tables.trips.c.id == trip.id))


@pytest.fixture
async def trip_without_stops(migrated_engine: AsyncEngine) -> AsyncIterator[SeededTrip]:
    """A trip that has no stops."""
    trip = SeededTrip(
        id=f"test-trip-{secrets.token_urlsafe(8)}",
        name="Trip with no stops",
        start_date=date(2026, 8, 9),
        rider_slug=secrets.token_urlsafe(16),
        viewer_slug=secrets.token_urlsafe(16),
    )

    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.trips.insert().values(
                id=trip.id,
                name=trip.name,
                rider_slug=trip.rider_slug,
                viewer_slug=trip.viewer_slug,
                start_date=trip.start_date,
            )
        )

    try:
        yield trip
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.trips.delete().where(tables.trips.c.id == trip.id))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _features_by_type(body: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Group features by geometry type."""
    result: dict[str, list[dict[str, Any]]] = {}
    for feature in body["features"]:
        gtype = feature["geometry"]["type"]
        result.setdefault(gtype, []).append(feature)
    return result


# --------------------------------------------------------------------------
# 1. Either slug reads -- 200 on both
# --------------------------------------------------------------------------


async def test_rider_slug_returns_200(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """The rider's own link returns the map."""
    trip = seeded_trips[0]
    response = await client.get(MAP_PATH.format(slug=trip.rider_slug))
    assert response.status_code == HTTPStatus.OK


async def test_viewer_slug_returns_200(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """A viewer link returns the map too -- this is a read."""
    trip = seeded_trips[0]
    response = await client.get(MAP_PATH.format(slug=trip.viewer_slug))
    assert response.status_code == HTTPStatus.OK


async def test_both_slugs_return_identical_body(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """Nothing in the map response may differ by slug kind."""
    trip = seeded_trips[0]
    rider = await client.get(MAP_PATH.format(slug=trip.rider_slug))
    viewer = await client.get(MAP_PATH.format(slug=trip.viewer_slug))
    assert rider.json() == viewer.json()


# --------------------------------------------------------------------------
# 2. Unknown slug -- 404
# --------------------------------------------------------------------------


async def test_unknown_slug_is_not_found(client: AsyncClient) -> None:
    """A slug no trip has is 404."""
    response = await client.get(MAP_PATH.format(slug=UNKNOWN_SLUG))
    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_unknown_slug_is_never_forbidden(client: AsyncClient) -> None:
    """Not 403 -- that would leak slug validity."""
    response = await client.get(MAP_PATH.format(slug=UNKNOWN_SLUG))
    assert response.status_code != HTTPStatus.FORBIDDEN


async def test_unknown_slug_returns_not_found_envelope(client: AsyncClient) -> None:
    """The body is the standard ErrorEnvelope with NOT_FOUND code."""
    response = await client.get(MAP_PATH.format(slug=UNKNOWN_SLUG))
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code is ErrorCode.NOT_FOUND


# --------------------------------------------------------------------------
# 3. Trip with no stops -- empty features, valid 200
# --------------------------------------------------------------------------


async def test_no_stops_returns_empty_feature_collection(
    client: AsyncClient,
    trip_without_stops: SeededTrip,
    seeded_stops: list[SeededStop],
) -> None:
    """
    A trip with no stops is 200 with ``features: []`` -- not a 404.

    ``seeded_stops`` is active so the table is not empty: ``[]`` is a real
    answer about this trip.
    """
    response = await client.get(MAP_PATH.format(slug=trip_without_stops.rider_slug))
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["type"] == "FeatureCollection"
    assert body["features"] == []


# --------------------------------------------------------------------------
# 4. Trip with 1 stop -- one Point, no trail
# --------------------------------------------------------------------------


async def test_single_stop_has_one_point_and_no_trail(
    client: AsyncClient,
    single_stop_trip: tuple[SeededTrip, SeededStop],
) -> None:
    """One stop produces one Point feature and no LineString."""
    trip, stop = single_stop_trip
    response = await client.get(MAP_PATH.format(slug=trip.rider_slug))
    assert response.status_code == HTTPStatus.OK
    body = response.json()

    by_type = _features_by_type(body)
    assert len(by_type.get("Point", [])) == 1
    assert "LineString" not in by_type

    point = by_type["Point"][0]
    assert point["id"] == stop.id


# --------------------------------------------------------------------------
# 5. Trip with 2+ stops -- Point features + LineString trail
# --------------------------------------------------------------------------


async def test_multi_stop_trip_has_point_per_stop_plus_trail(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """Three stops on trip 1 produce three Points and one LineString."""
    trip = seeded_trips[0]
    trip_stops = [s for s in seeded_stops if s.trip_id == trip.id]
    assert len(trip_stops) >= 2

    response = await client.get(MAP_PATH.format(slug=trip.rider_slug))
    body = response.json()
    by_type = _features_by_type(body)

    assert len(by_type["Point"]) == len(trip_stops)
    assert len(by_type["LineString"]) == 1


# --------------------------------------------------------------------------
# 6. Coordinate order -- [longitude, latitude], NOT [lat, lng]
# --------------------------------------------------------------------------


async def test_point_coordinates_are_lng_lat(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    GeoJSON positions are ``[longitude, latitude]`` -- the reverse of Stop's
    ``lat``/``lng`` field order.

    Both are plain floats; swapping them validates fine and returns 200. The
    only symptom is a pin plotted somewhere else on Earth. This assertion is
    what catches it.
    """
    trip = seeded_trips[0]
    trip_stops = {s.id: s for s in seeded_stops if s.trip_id == trip.id}

    response = await client.get(MAP_PATH.format(slug=trip.rider_slug))
    by_type = _features_by_type(response.json())

    for point in by_type["Point"]:
        stop = trip_stops[point["id"]]
        coords = point["geometry"]["coordinates"]
        assert coords == [stop.lng, stop.lat], (
            f"Expected [lng={stop.lng}, lat={stop.lat}], got {coords}"
        )


async def test_trail_coordinates_are_lng_lat(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """The trail's coordinate pairs are also [lng, lat]."""
    trip = seeded_trips[0]
    trip_stops = {s.id: s for s in seeded_stops if s.trip_id == trip.id}

    response = await client.get(MAP_PATH.format(slug=trip.rider_slug))
    by_type = _features_by_type(response.json())
    trail_coords = by_type["LineString"][0]["geometry"]["coordinates"]

    # Every coordinate pair in the trail must be [lng, lat] of some stop.
    known_pairs = {(s.lng, s.lat) for s in trip_stops.values()}
    for coord in trail_coords:
        assert tuple(coord) in known_pairs, f"{coord} is not a known [lng, lat] pair"


# --------------------------------------------------------------------------
# 7. Stop Feature shape
# --------------------------------------------------------------------------


async def test_stop_feature_id_is_stop_id(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """``Feature.id`` carries the stop's id -- not inside properties."""
    trip = seeded_trips[0]
    trip_stop_ids = {s.id for s in seeded_stops if s.trip_id == trip.id}

    response = await client.get(MAP_PATH.format(slug=trip.rider_slug))
    by_type = _features_by_type(response.json())

    returned_ids = {f["id"] for f in by_type["Point"]}
    assert returned_ids == trip_stop_ids


async def test_stop_properties_has_only_name_and_arrived_at(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """Properties is exactly ``{name, arrivedAt}`` -- no notes, no locationSource."""
    trip = seeded_trips[0]

    response = await client.get(MAP_PATH.format(slug=trip.rider_slug))
    by_type = _features_by_type(response.json())

    for point in by_type["Point"]:
        assert set(point["properties"].keys()) == {"name", "arrivedAt"}


async def test_stop_properties_values_match_database(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """The name and arrivedAt on each Point match the seeded row."""
    trip = seeded_trips[0]
    trip_stops = {s.id: s for s in seeded_stops if s.trip_id == trip.id}

    response = await client.get(MAP_PATH.format(slug=trip.rider_slug))
    by_type = _features_by_type(response.json())

    for point in by_type["Point"]:
        stop = trip_stops[point["id"]]
        assert point["properties"]["name"] == stop.name
        returned_dt = datetime.fromisoformat(point["properties"]["arrivedAt"])
        assert returned_dt == stop.arrived_at


# --------------------------------------------------------------------------
# 8. Trail feature shape
# --------------------------------------------------------------------------


async def test_trail_properties_is_empty(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """The trail's properties is an empty object -- no metadata of its own."""
    trip = seeded_trips[0]

    response = await client.get(MAP_PATH.format(slug=trip.rider_slug))
    by_type = _features_by_type(response.json())

    trail = by_type["LineString"][0]
    assert trail["properties"] == {}


async def test_trail_coordinates_are_chronological(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """Trail coordinates are sorted by arrivedAt, earliest first."""
    trip = seeded_trips[0]
    trip_stops = sorted(
        [s for s in seeded_stops if s.trip_id == trip.id],
        key=lambda s: (s.arrived_at, s.id),
    )

    response = await client.get(MAP_PATH.format(slug=trip.rider_slug))
    by_type = _features_by_type(response.json())
    trail_coords = by_type["LineString"][0]["geometry"]["coordinates"]

    expected_coords = [[s.lng, s.lat] for s in trip_stops]
    assert trail_coords == expected_coords


# --------------------------------------------------------------------------
# 9. Cross-trip isolation
# --------------------------------------------------------------------------


async def test_map_contains_only_this_trips_stops(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """The Point feature ids are exactly this trip's stop ids -- no leaks."""
    first, second = seeded_trips
    expected_ids = {s.id for s in seeded_stops if s.trip_id == first.id}
    foreign_ids = {s.id for s in seeded_stops if s.trip_id == second.id}
    assert expected_ids and foreign_ids

    response = await client.get(MAP_PATH.format(slug=first.rider_slug))
    by_type = _features_by_type(response.json())
    returned_ids = {f["id"] for f in by_type["Point"]}

    assert returned_ids == expected_ids


# --------------------------------------------------------------------------
# 10. HEAD
# --------------------------------------------------------------------------


async def test_head_returns_200_with_no_body(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """HEAD is GET without a body."""
    trip = seeded_trips[0]
    response = await client.head(MAP_PATH.format(slug=trip.rider_slug))
    assert response.status_code == HTTPStatus.OK
    assert not response.content


async def test_head_on_unknown_slug_is_404(client: AsyncClient) -> None:
    """HEAD runs the same access dependency."""
    response = await client.head(MAP_PATH.format(slug=UNKNOWN_SLUG))
    assert response.status_code == HTTPStatus.NOT_FOUND


# --------------------------------------------------------------------------
# 11. FeatureCollection envelope
# --------------------------------------------------------------------------


async def test_response_is_feature_collection_type(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """Top-level type is 'FeatureCollection'."""
    trip = seeded_trips[0]
    body = (await client.get(MAP_PATH.format(slug=trip.rider_slug))).json()
    assert body["type"] == "FeatureCollection"


async def test_every_feature_has_type_feature(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """Every member of features[] has type 'Feature'."""
    trip = seeded_trips[0]
    body = (await client.get(MAP_PATH.format(slug=trip.rider_slug))).json()
    for feature in body["features"]:
        assert feature["type"] == "Feature"


# --------------------------------------------------------------------------
# 12. No slug leaks
# --------------------------------------------------------------------------


async def test_no_slug_appears_in_the_response(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """Neither slug of either trip appears in the response body."""
    first, second = seeded_trips
    response = await client.get(MAP_PATH.format(slug=first.rider_slug))
    assert response.status_code == HTTPStatus.OK

    for candidate in (first.rider_slug, first.viewer_slug, second.rider_slug, second.viewer_slug):
        assert candidate not in response.text, f"{candidate!r} leaked into the body"


# --------------------------------------------------------------------------
# 13. OpenAPI
# --------------------------------------------------------------------------


def _openapi() -> dict[str, Any]:
    import app.main

    return app.main.app.openapi()


def test_openapi_200_response_is_map_feature_collection() -> None:
    """The endpoint is documented as returning MapFeatureCollection."""
    paths = _openapi()["paths"]
    path = "/api/trips/{slug}/map"
    assert path in paths
    schema = paths[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].rsplit("/", 1)[-1] == "MapFeatureCollection"


def test_openapi_404_response_is_error_envelope() -> None:
    """A 404 is declared and typed as ErrorEnvelope."""
    operation = _openapi()["paths"]["/api/trips/{slug}/map"]["get"]
    assert "404" in operation["responses"]
    schema = operation["responses"]["404"]["content"]["application/json"]["schema"]
    assert schema["$ref"].rsplit("/", 1)[-1] == "ErrorEnvelope"
