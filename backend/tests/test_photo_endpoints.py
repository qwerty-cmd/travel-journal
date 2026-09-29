"""
Photo endpoints — ``GET`` and ``POST /api/trips/{slug}/stops/{stop_id}/photos``.

**Written from the contract, not from the handler.** These endpoints sit in two
of spec Section 12's three priority categories: access control (POST is rider
only, GET accepts either slug) and data integrity (the three-way idempotency
branch is what makes the offline queue safe to retry photo uploads).

What the contract promises:

1. **GET — either slug accepted.** A viewer slug is an ordinary 200. An unknown
   slug is 404 / ``NOT_FOUND``, never 403.
2. **GET — stop must belong to trip.** A stop id that is not on this trip is 404.
3. **GET — empty list for stop with no photos.** ``200`` with ``[]``.
4. **GET — photos carry presigned URLs.** ``PhotoOut[]`` with ``url`` populated.
5. **POST — rider slug only.** Viewer slug is 403 / ``FORBIDDEN``.
6. **POST — three-way idempotency on the client-generated id:**
   unseen -> 201; same stop -> 200 replay; different stop -> 409 conflict.
7. **POST — stop must belong to trip.** 404 if not.
8. **POST — the 409 discloses nothing** from the conflicting record.
9. **POST — `takenAt` is an offset-aware instant.** A naive value (no UTC
   offset) is 422 / ``VALIDATION_ERROR`` and writes nothing; an offset-carrying
   one is stored as the instant the device sent, whatever zone the API runs in.
"""

from __future__ import annotations

import io
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import pytest
from conftest import SeededTrip
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.data import tables
from app.data.db import get_session
from app.models.common import ErrorCode, ErrorEnvelope

PHOTOS_PATH = "/api/trips/{slug}/stops/{stop_id}/photos"

UNKNOWN_SLUG = "definitely-not-a-real-slug-0000"

# Exactly the keys PhotoOut promises.
CONTRACT_KEYS = {"id", "stopId", "url", "uploadedBy", "takenAt", "archived"}


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeededStop:
    """A stop row this fixture put in the database."""

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
    One stop on each seeded trip — needed as parents for photos.

    Two stops on different trips so the cross-stop conflict test can use a stop
    on the other trip.
    """
    first, second = seeded_trips
    prefix = f"test-stop-{secrets.token_urlsafe(8)}"
    base = datetime(2026, 6, 2, 8, 30, tzinfo=UTC)

    seeded = [
        SeededStop(
            id=f"{prefix}-trip1",
            trip_id=first.id,
            name="Katherine Gorge",
            lat=-14.3155,
            lng=132.4187,
            location_source="gps",
            arrived_at=base,
            notes="Gorge walk",
        ),
        SeededStop(
            id=f"{prefix}-trip2",
            trip_id=second.id,
            name="Wave Rock",
            lat=-32.4414,
            lng=118.8975,
            location_source="manual",
            arrived_at=base + timedelta(days=10),
            notes="Other trip entirely",
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
async def created_photo_ids(migrated_engine: AsyncEngine) -> AsyncIterator[list[str]]:
    """
    Ids this module's *requests* put in the photos table, deleted on teardown.

    Tests append every photo id they send via POST to ensure cleanup.
    """
    ids: list[str] = []
    try:
        yield ids
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.photos.delete().where(tables.photos.c.id.in_(ids)))


def upload_form(
    photo_id: str | None = None,
    uploaded_by: str = "Alice",
    taken_at: str = "2026-06-14T10:00:00+09:30",
) -> dict[str, str]:
    """Form fields for the photo upload multipart request."""
    return {
        "id": photo_id or str(uuid4()),
        "uploadedBy": uploaded_by,
        "takenAt": taken_at,
    }


def fake_file(content: bytes = b"fake-jpeg-bytes", filename: str = "photo.jpg"):
    """A minimal file-like object for multipart upload."""
    return {"file": (filename, io.BytesIO(content), "image/jpeg")}


async def photo_rows_for_id(engine: AsyncEngine, photo_id: str) -> list[Any]:
    """Every photos row with this id, read straight from the table."""
    async with engine.connect() as conn:
        result = await conn.execute(tables.photos.select().where(tables.photos.c.id == photo_id))
        return list(result.mappings())


def parse_envelope(response: Any) -> Any:
    """Validate an error body against ErrorEnvelope and return its detail."""
    assert response.headers["content-type"] == "application/json", response.headers
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.message, "message must be non-empty"
    return envelope.error


# --------------------------------------------------------------------------
# GET /api/trips/{slug}/stops/{stop_id}/photos
# --------------------------------------------------------------------------


class TestListPhotos:
    """GET — photo list for a stop."""

    async def test_empty_list_for_stop_with_no_photos(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
    ) -> None:
        """A stop with no photos returns 200 with []."""
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop.id)

        response = await client.get(url)

        assert response.status_code == HTTPStatus.OK
        assert response.json() == []

    async def test_photos_returned_with_presigned_urls(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
        created_photo_ids: list[str],
        s3_bucket: None,
    ) -> None:
        """After uploading a photo, GET returns it with a presigned URL."""
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        post_url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop.id)

        # Upload a photo first
        form = upload_form()
        created_photo_ids.append(form["id"])
        await client.post(post_url, data=form, files=fake_file())

        # Now list
        get_url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop.id)
        response = await client.get(get_url)

        assert response.status_code == HTTPStatus.OK
        photos = response.json()
        assert len(photos) >= 1

        photo = next(p for p in photos if p["id"] == form["id"])
        assert set(photo.keys()) == CONTRACT_KEYS
        assert photo["stopId"] == stop.id
        assert photo["uploadedBy"] == "Alice"
        assert photo["archived"] is False
        # URL is a presigned S3 URL
        assert photo["url"].startswith("http")

    async def test_viewer_slug_can_list_photos(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
    ) -> None:
        """Either slug accepted on GET — viewer slug returns 200."""
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.viewer_slug, stop_id=stop.id)

        response = await client.get(url)

        assert response.status_code == HTTPStatus.OK

    async def test_rider_slug_can_list_photos(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
    ) -> None:
        """Either slug accepted on GET — rider slug returns 200."""
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop.id)

        response = await client.get(url)

        assert response.status_code == HTTPStatus.OK

    async def test_unknown_slug_returns_404(
        self,
        client: AsyncClient,
        seeded_stops: list[SeededStop],
    ) -> None:
        """Unknown slug is 404 / NOT_FOUND, never 403."""
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=UNKNOWN_SLUG, stop_id=stop.id)

        response = await client.get(url)

        assert response.status_code == HTTPStatus.NOT_FOUND
        error = parse_envelope(response)
        assert error.code == ErrorCode.NOT_FOUND

    async def test_stop_not_on_trip_returns_404(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
    ) -> None:
        """A stop id that belongs to a different trip is 404."""
        trip = seeded_trips[0]
        # Use the stop from trip 2
        other_stop = seeded_stops[1]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=other_stop.id)

        response = await client.get(url)

        assert response.status_code == HTTPStatus.NOT_FOUND
        error = parse_envelope(response)
        assert error.code == ErrorCode.NOT_FOUND

    async def test_nonexistent_stop_returns_404(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
    ) -> None:
        """A stop id that does not exist at all is 404."""
        trip = seeded_trips[0]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id="no-such-stop")

        response = await client.get(url)

        assert response.status_code == HTTPStatus.NOT_FOUND

    async def test_head_on_get_route_works(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
    ) -> None:
        """HEAD returns 200 with no body — the schema-excluded sibling."""
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop.id)

        response = await client.head(url)

        assert response.status_code == HTTPStatus.OK
        assert response.content == b""


# --------------------------------------------------------------------------
# POST /api/trips/{slug}/stops/{stop_id}/photos
# --------------------------------------------------------------------------


class TestUploadPhoto:
    """POST — upload a photo to a stop."""

    async def test_rider_slug_creates_photo_returns_201(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
        created_photo_ids: list[str],
        s3_bucket: None,
    ) -> None:
        """Happy path: rider slug, unseen id -> 201 with PhotoOut."""
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop.id)

        form = upload_form()
        created_photo_ids.append(form["id"])

        response = await client.post(url, data=form, files=fake_file())

        assert response.status_code == HTTPStatus.CREATED, response.text
        body = response.json()
        assert set(body.keys()) == CONTRACT_KEYS
        assert body["id"] == form["id"]
        assert body["stopId"] == stop.id
        assert body["uploadedBy"] == "Alice"
        assert body["archived"] is False
        assert body["url"].startswith("http")

    async def test_replay_same_stop_returns_200(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
        created_photo_ids: list[str],
        s3_bucket: None,
    ) -> None:
        """Same id + same stop -> 200 replay with stored photo."""
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop.id)

        form = upload_form()
        created_photo_ids.append(form["id"])

        # First upload -> 201
        first = await client.post(url, data=form, files=fake_file())
        assert first.status_code == HTTPStatus.CREATED

        # Replay -> 200
        second = await client.post(url, data=form, files=fake_file(b"different-bytes"))
        assert second.status_code == HTTPStatus.OK

        # The stored photo is returned, not a new one
        assert second.json()["id"] == form["id"]
        assert second.json()["stopId"] == stop.id

    async def test_cross_stop_conflict_returns_409(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
        created_photo_ids: list[str],
        s3_bucket: None,
    ) -> None:
        """Same id but different stop -> 409 CONFLICT."""
        trip = seeded_trips[0]
        stop_own = seeded_stops[0]
        # The second stop is on trip 2, but we need a stop on trip 1 for the
        # conflict test. We'll upload to stop_own first, then try to upload
        # the same id to a different stop on the same trip. However, we only
        # have one stop per trip. Instead, use the second trip's rider slug
        # and its stop.
        second_trip = seeded_trips[1]
        stop_other = seeded_stops[1]

        photo_id = str(uuid4())
        created_photo_ids.append(photo_id)

        # Upload to stop_own on trip 1
        form = upload_form(photo_id=photo_id)
        url1 = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop_own.id)
        first = await client.post(url1, data=form, files=fake_file())
        assert first.status_code == HTTPStatus.CREATED

        # Same id to stop_other on trip 2 -> 409
        url2 = PHOTOS_PATH.format(slug=second_trip.rider_slug, stop_id=stop_other.id)
        conflict = await client.post(url2, data=form, files=fake_file())
        assert conflict.status_code == HTTPStatus.CONFLICT

        error = parse_envelope(conflict)
        assert error.code == ErrorCode.CONFLICT

    async def test_conflict_discloses_nothing(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
        created_photo_ids: list[str],
        s3_bucket: None,
    ) -> None:
        """The 409 message must not contain any value from the conflicting record."""
        trip = seeded_trips[0]
        stop_own = seeded_stops[0]
        second_trip = seeded_trips[1]
        stop_other = seeded_stops[1]

        photo_id = str(uuid4())
        created_photo_ids.append(photo_id)

        form = upload_form(photo_id=photo_id)
        url1 = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop_own.id)
        await client.post(url1, data=form, files=fake_file())

        url2 = PHOTOS_PATH.format(slug=second_trip.rider_slug, stop_id=stop_other.id)
        conflict = await client.post(url2, data=form, files=fake_file())

        raw = conflict.text
        # None of the first stop's identifying values should appear
        assert stop_own.id not in raw
        assert stop_own.name not in raw
        assert trip.rider_slug not in raw
        assert trip.viewer_slug not in raw
        assert trip.id not in raw

    async def test_viewer_slug_returns_403(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
    ) -> None:
        """Viewer slug on POST -> 403 FORBIDDEN."""
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.viewer_slug, stop_id=stop.id)

        form = upload_form()
        response = await client.post(url, data=form, files=fake_file())

        assert response.status_code == HTTPStatus.FORBIDDEN
        error = parse_envelope(response)
        assert error.code == ErrorCode.FORBIDDEN

    async def test_viewer_slug_writes_nothing(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
        migrated_engine: AsyncEngine,
    ) -> None:
        """A rejected 403 must not write a row."""
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.viewer_slug, stop_id=stop.id)

        form = upload_form()
        await client.post(url, data=form, files=fake_file())

        rows = await photo_rows_for_id(migrated_engine, form["id"])
        assert rows == []

    async def test_unknown_slug_returns_404(
        self,
        client: AsyncClient,
        seeded_stops: list[SeededStop],
    ) -> None:
        """Unknown slug on POST -> 404 NOT_FOUND, never 403."""
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=UNKNOWN_SLUG, stop_id=stop.id)

        form = upload_form()
        response = await client.post(url, data=form, files=fake_file())

        assert response.status_code == HTTPStatus.NOT_FOUND
        error = parse_envelope(response)
        assert error.code == ErrorCode.NOT_FOUND

    async def test_stop_not_on_trip_returns_404(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
        migrated_engine: AsyncEngine,
    ) -> None:
        """A stop id that belongs to a different trip is 404, and nothing is written."""
        trip = seeded_trips[0]
        other_stop = seeded_stops[1]  # belongs to trip 2
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=other_stop.id)

        form = upload_form()
        response = await client.post(url, data=form, files=fake_file())

        assert response.status_code == HTTPStatus.NOT_FOUND
        error = parse_envelope(response)
        assert error.code == ErrorCode.NOT_FOUND

        # Nothing written
        rows = await photo_rows_for_id(migrated_engine, form["id"])
        assert rows == []

    async def test_nonexistent_stop_returns_404(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
    ) -> None:
        """A stop id that does not exist at all is 404."""
        trip = seeded_trips[0]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id="no-such-stop")

        form = upload_form()
        response = await client.post(url, data=form, files=fake_file())

        assert response.status_code == HTTPStatus.NOT_FOUND

    async def test_conflict_writes_nothing_on_other_stop(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
        created_photo_ids: list[str],
        migrated_engine: AsyncEngine,
        s3_bucket: None,
    ) -> None:
        """A 409 conflict must not create a second row."""
        trip = seeded_trips[0]
        stop_own = seeded_stops[0]
        second_trip = seeded_trips[1]
        stop_other = seeded_stops[1]

        photo_id = str(uuid4())
        created_photo_ids.append(photo_id)

        form = upload_form(photo_id=photo_id)
        url1 = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop_own.id)
        await client.post(url1, data=form, files=fake_file())

        url2 = PHOTOS_PATH.format(slug=second_trip.rider_slug, stop_id=stop_other.id)
        await client.post(url2, data=form, files=fake_file())

        # Only one row should exist
        rows = await photo_rows_for_id(migrated_engine, photo_id)
        assert len(rows) == 1
        assert rows[0]["stop_id"] == stop_own.id


# --------------------------------------------------------------------------
# takenAt is an offset-aware instant (t-takenat-tz-question)
# --------------------------------------------------------------------------

# Every one of these parses fine as a datetime and carries no UTC offset. The
# server is the only thing that rejects them: JSON Schema cannot express
# timezone-awareness, so the OpenAPI document and the generated client both see
# a plain `{"type": "string", "format": "date-time"}` and wave them through.
NAIVE_TAKEN_AT = [
    "2026-06-14T10:00:00",
    "2026-06-14T10:00",
    "2026-06-14 10:00:00",
    "2026-06-14T10:00:00.250000",
]

# The offsets a real device sends: a half-hour zone, Zulu, explicit UTC, and a
# negative offset. All four are accepted; none is preferred over another.
AWARE_TAKEN_AT = [
    "2026-06-14T10:00:00+09:30",
    "2026-06-14T10:00:00Z",
    "2026-06-14T10:00:00+00:00",
    "2026-06-14T10:00:00-05:00",
]


class TestTakenAtIsOffsetAware:
    """
    POST -- ``takenAt`` must carry a UTC offset.

    The ruling (``t-takenat-tz-question``): ``takenAt`` is an offset-aware ISO
    8601 instant, and a naive value is rejected ``422`` / ``VALIDATION_ERROR``.
    It is not defaulted, not assumed UTC, and not assumed server-local.

    Why a naive value cannot simply be accepted: nothing between the wire and the
    ``timestamptz`` column resolves it deterministically. SQLAlchemy's asyncpg
    dialect has no bind processor, so a naive datetime reaches asyncpg untouched
    and is resolved in the *host process's* local zone -- the same request writes
    ``02:00Z`` from a UTC+8 host and ``10:00Z`` from a UTC one. The stored instant
    would depend on where the API happens to run, and every symptom of that is
    silent: the upload returns ``201``, the photo appears, and it is simply filed
    at the wrong hour in the timeline.

    Written from the ruling and the acceptance criteria, not from the handler.
    """

    @pytest.mark.parametrize("taken_at", NAIVE_TAKEN_AT)
    async def test_naive_taken_at_returns_422_validation_error(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
        taken_at: str,
    ) -> None:
        """A ``takenAt`` with no offset is refused, in the standard error envelope."""
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop.id)

        response = await client.post(url, data=upload_form(taken_at=taken_at), files=fake_file())

        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
        assert parse_envelope(response).code == ErrorCode.VALIDATION_ERROR

    @pytest.mark.parametrize("taken_at", NAIVE_TAKEN_AT)
    async def test_naive_taken_at_writes_nothing(
        self,
        client: AsyncClient,
        migrated_engine: AsyncEngine,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
        taken_at: str,
    ) -> None:
        """
        A rejected upload leaves no row behind -- the rejection is total, not partial.

        The 422 is the half of the contract a caller sees; this is the half that
        matters to the data. A row written from a value the server then called
        invalid would be exactly the silently-wrong-hour record the ruling exists
        to prevent.
        """
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop.id)
        form = upload_form(taken_at=taken_at)

        await client.post(url, data=form, files=fake_file())

        assert await photo_rows_for_id(migrated_engine, form["id"]) == []

    @pytest.mark.parametrize("taken_at", AWARE_TAKEN_AT)
    async def test_offset_aware_taken_at_returns_201(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
        created_photo_ids: list[str],
        s3_bucket: None,
        taken_at: str,
    ) -> None:
        """
        Any offset is accepted -- the rule is "carries one", not "is UTC".

        The rider crosses timezones mid-trip, so demanding UTC on the wire would
        push the same conversion onto the client that the server is refusing to
        guess at.
        """
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop.id)

        form = upload_form(taken_at=taken_at)
        created_photo_ids.append(form["id"])

        response = await client.post(url, data=form, files=fake_file())

        assert response.status_code == HTTPStatus.CREATED, response.text

    async def test_submitted_instant_is_what_gets_stored(
        self,
        client: AsyncClient,
        migrated_engine: AsyncEngine,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
        created_photo_ids: list[str],
        s3_bucket: None,
    ) -> None:
        """
        The instant in the column is the instant the device sent -- not a re-reading
        of its wall clock in the host process's zone.

        The point of the whole ruling, asserted end to end. ``+09:30`` is chosen
        because it is a half-hour offset matching no plausible CI or container
        zone, so a host-local re-resolution cannot coincidentally land on the right
        answer.

        Compared as datetimes on purpose. The ``201`` body comes from the stored
        row (``t-photo-insert-echoes-argument``), so it is UTC like the column and
        every read-back, while ``submitted`` carries ``+09:30``. The instants match
        and the spellings don't, so a string comparison would fail on a difference
        that is not a defect.
        """
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop.id)

        submitted = datetime(2026, 6, 14, 10, 0, tzinfo=timezone(timedelta(hours=9, minutes=30)))
        form = upload_form(taken_at=submitted.isoformat())
        created_photo_ids.append(form["id"])

        response = await client.post(url, data=form, files=fake_file())
        assert response.status_code == HTTPStatus.CREATED, response.text

        rows = await photo_rows_for_id(migrated_engine, form["id"])
        assert len(rows) == 1
        stored = rows[0]["taken_at"]
        assert stored.tzinfo is not None, "photos.taken_at came back naive"
        assert stored == submitted, f"stored {stored!r}, submitted {submitted!r}"

        assert datetime.fromisoformat(response.json()["takenAt"]) == submitted

        listed = (await client.get(url)).json()
        photo = next(p for p in listed if p["id"] == form["id"])
        assert datetime.fromisoformat(photo["takenAt"]) == submitted

    @pytest.mark.parametrize("missing", ["id", "uploadedBy", "takenAt"])
    async def test_missing_form_field_still_returns_422_validation_error(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
        missing: str,
    ) -> None:
        """
        The ordinary missing-field ``422`` is unchanged by the aware-datetime rule.

        Tightening ``takenAt``'s type moves a value from "accepted" to "rejected";
        it must not move anything from "rejected" to "accepted", and it must not
        change the envelope the other two fields already produce.
        """
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop.id)

        form = upload_form()
        del form[missing]

        response = await client.post(url, data=form, files=fake_file())

        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
        assert parse_envelope(response).code == ErrorCode.VALIDATION_ERROR

    async def test_unparseable_taken_at_returns_422_validation_error(
        self,
        client: AsyncClient,
        seeded_trips: list[SeededTrip],
        seeded_stops: list[SeededStop],
    ) -> None:
        """A ``takenAt`` that is not a datetime at all is the same 422, not a 500."""
        trip = seeded_trips[0]
        stop = seeded_stops[0]
        url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop.id)

        response = await client.post(
            url, data=upload_form(taken_at="yesterday afternoon"), files=fake_file()
        )

        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
        assert parse_envelope(response).code == ErrorCode.VALIDATION_ERROR
