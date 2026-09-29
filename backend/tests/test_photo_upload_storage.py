"""
``POST /api/trips/{slug}/stops/{stop_id}/photos`` -- what lands in object storage.

Tasks ``t-photo-upload-stream-to-s3`` and ``t-photo-insert-echoes-argument``.
Against the real app, the local Postgres and the local MinIO; nothing mocked.

1. **The stored object is byte-identical to the upload**, under the
   deterministic key ``{trip_id}/{stop_id}/{photo_id}`` -- for a small photo
   (one ``PutObject``) and for a body above boto3's multipart threshold, the
   path streaming opened up.
2. **A replay writes nothing to storage.** The replay check still runs before
   the upload, so a second request with different bytes leaves the first
   object untouched (decision-log Entry 20).
3. **A ``201`` and its own ``200`` replay spell ``takenAt`` identically**,
   because ``insert`` returns the row as stored rather than its arguments.
"""

from __future__ import annotations

import io
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from http import HTTPStatus
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from conftest import SeededTrip
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.data import tables
from app.data.db import get_session
from app.storage.s3_client import BUCKET_NAME, get_s3_client

PHOTOS_PATH = "/api/trips/{slug}/stops/{stop_id}/photos"

# boto3's default multipart_threshold is 8 MiB; one byte over forces the
# multipart branch of upload_fileobj.
ABOVE_MULTIPART_THRESHOLD = 8 * 1024 * 1024 + 1


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """The real application, on the test database."""
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
async def stop(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[tuple[SeededTrip, str]]:
    """One stop on the first seeded trip; its photos and itself removed on teardown."""
    trip = seeded_trips[0]
    stop_id = f"test-upload-storage-stop-{secrets.token_urlsafe(8)}"
    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.stops.insert().values(
                id=stop_id,
                trip_id=trip.id,
                name="Storage stop",
                lat=-14.3,
                lng=132.4,
                location_source="gps",
                arrived_at=datetime(2026, 6, 2, 8, 30, tzinfo=UTC),
                notes=None,
            )
        )
    try:
        yield trip, stop_id
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.photos.delete().where(tables.photos.c.stop_id == stop_id))
            await conn.execute(tables.stops.delete().where(tables.stops.c.id == stop_id))


@pytest.fixture
def created_keys() -> Iterator[list[str]]:
    """Object keys a test's uploads wrote -- the bucket ensured first, the keys deleted after."""
    s3 = get_s3_client()
    try:
        s3.head_bucket(Bucket=BUCKET_NAME)
    except ClientError:
        s3.create_bucket(Bucket=BUCKET_NAME)
    keys: list[str] = []
    try:
        yield keys
    finally:
        for key in keys:
            s3.delete_object(Bucket=BUCKET_NAME, Key=key)


def form(photo_id: str, taken_at: str = "2026-06-14T10:00:00+09:30") -> dict[str, str]:
    return {"id": photo_id, "uploadedBy": "Alice", "takenAt": taken_at}


def upload(content: bytes) -> dict[str, tuple[str, io.BytesIO, str]]:
    return {"file": ("photo.jpg", io.BytesIO(content), "image/jpeg")}


def stored_bytes(key: str) -> bytes:
    return get_s3_client().get_object(Bucket=BUCKET_NAME, Key=key)["Body"].read()


@pytest.mark.parametrize("size", [64 * 1024, ABOVE_MULTIPART_THRESHOLD])
async def test_stored_object_is_the_uploaded_bytes_under_the_deterministic_key(
    client: AsyncClient, stop: tuple[SeededTrip, str], created_keys: list[str], size: int
) -> None:
    """Streaming must not truncate, reorder or re-key the body, below or above multipart."""
    trip, stop_id = stop
    photo_id = str(uuid4())
    key = f"{trip.id}/{stop_id}/{photo_id}"
    created_keys.append(key)
    content = os.urandom(size)

    response = await client.post(
        PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop_id),
        data=form(photo_id),
        files=upload(content),
    )

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert stored_bytes(key) == content


async def test_replay_does_not_touch_the_stored_object(
    client: AsyncClient, stop: tuple[SeededTrip, str], created_keys: list[str]
) -> None:
    """The replay branch still answers before any upload: different bytes, same object."""
    trip, stop_id = stop
    photo_id = str(uuid4())
    key = f"{trip.id}/{stop_id}/{photo_id}"
    created_keys.append(key)
    url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop_id)

    first = await client.post(url, data=form(photo_id), files=upload(b"original-bytes"))
    replay = await client.post(url, data=form(photo_id), files=upload(b"different-bytes"))

    assert first.status_code == HTTPStatus.CREATED, first.text
    assert replay.status_code == HTTPStatus.OK, replay.text
    assert stored_bytes(key) == b"original-bytes"


async def test_created_and_replayed_bodies_are_identical(
    client: AsyncClient, stop: tuple[SeededTrip, str], created_keys: list[str]
) -> None:
    """
    A ``+09:30`` ``takenAt``: the ``201`` and its ``200`` replay return the same
    string, not two spellings of one instant. Everything but the presigned URL
    (freshly signed per request) is compared.
    """
    trip, stop_id = stop
    photo_id = str(uuid4())
    created_keys.append(f"{trip.id}/{stop_id}/{photo_id}")
    url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop_id)

    created = await client.post(url, data=form(photo_id), files=upload(b"bytes"))
    replay = await client.post(url, data=form(photo_id), files=upload(b"bytes"))

    assert created.status_code == HTTPStatus.CREATED, created.text
    assert replay.status_code == HTTPStatus.OK, replay.text
    created_body = {k: v for k, v in created.json().items() if k != "url"}
    replay_body = {k: v for k, v in replay.json().items() if k != "url"}
    assert created_body == replay_body
    assert datetime.fromisoformat(created_body["takenAt"]) == datetime(
        2026, 6, 14, 0, 30, tzinfo=UTC
    )


# --------------------------------------------------------------------------
# A rejected upload writes nothing to storage (QA finding F2)
# --------------------------------------------------------------------------
#
# The row-level "writes nothing" tests in test_photo_endpoints.py only look at
# Postgres. An upload moved ahead of the conflict or access check still passes
# them, leaving a stray object in the bucket under a key no row points at.


@pytest.fixture
async def other_trip_stop(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[tuple[SeededTrip, str]]:
    """One stop on the *second* seeded trip -- the target of a cross-stop conflict."""
    trip = seeded_trips[1]
    stop_id = f"test-upload-storage-other-{secrets.token_urlsafe(8)}"
    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.stops.insert().values(
                id=stop_id,
                trip_id=trip.id,
                name="Other trip stop",
                lat=-12.4,
                lng=130.8,
                location_source="gps",
                arrived_at=datetime(2026, 6, 3, 8, 30, tzinfo=UTC),
                notes=None,
            )
        )
    try:
        yield trip, stop_id
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.photos.delete().where(tables.photos.c.stop_id == stop_id))
            await conn.execute(tables.stops.delete().where(tables.stops.c.id == stop_id))


@pytest.fixture
def swept_prefixes(s3_bucket: None) -> Iterator[list[str]]:
    """Prefixes a test asserts empty; anything a failing run left under them is deleted after."""
    prefixes: list[str] = []
    try:
        yield prefixes
    finally:
        s3 = get_s3_client()
        for prefix in prefixes:
            for key in keys_under(prefix):
                s3.delete_object(Bucket=BUCKET_NAME, Key=key)


def keys_under(prefix: str) -> list[str]:
    """Every object key in the bucket starting with ``prefix`` (paginated)."""
    paginator = get_s3_client().get_paginator("list_objects_v2")
    return [
        obj["Key"]
        for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix=prefix)
        for obj in page.get("Contents", [])
    ]


async def test_cross_stop_conflict_writes_nothing_under_the_other_stop(
    client: AsyncClient,
    stop: tuple[SeededTrip, str],
    other_trip_stop: tuple[SeededTrip, str],
    created_keys: list[str],
    swept_prefixes: list[str],
) -> None:
    """A ``409`` answers before any upload: no object under the conflicting stop's key."""
    trip, stop_id = stop
    other_trip, other_stop_id = other_trip_stop
    photo_id = str(uuid4())
    created_keys.append(f"{trip.id}/{stop_id}/{photo_id}")
    conflicting_prefix = f"{other_trip.id}/{other_stop_id}/"
    swept_prefixes.append(conflicting_prefix)

    first = await client.post(
        PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop_id),
        data=form(photo_id),
        files=upload(b"original-bytes"),
    )
    conflict = await client.post(
        PHOTOS_PATH.format(slug=other_trip.rider_slug, stop_id=other_stop_id),
        data=form(photo_id),
        files=upload(b"conflicting-bytes"),
    )

    assert first.status_code == HTTPStatus.CREATED, first.text
    assert conflict.status_code == HTTPStatus.CONFLICT, conflict.text
    assert keys_under(conflicting_prefix) == []
    # And the original object is still the original.
    assert stored_bytes(f"{trip.id}/{stop_id}/{photo_id}") == b"original-bytes"


async def test_viewer_slug_writes_nothing_to_storage(
    client: AsyncClient, stop: tuple[SeededTrip, str], swept_prefixes: list[str]
) -> None:
    """A ``403`` on the viewer slug leaves no object under the stop's key."""
    trip, stop_id = stop
    photo_id = str(uuid4())
    prefix = f"{trip.id}/{stop_id}/"
    swept_prefixes.append(prefix)

    response = await client.post(
        PHOTOS_PATH.format(slug=trip.viewer_slug, stop_id=stop_id),
        data=form(photo_id),
        files=upload(b"viewer-bytes"),
    )

    assert response.status_code == HTTPStatus.FORBIDDEN, response.text
    assert keys_under(prefix) == []


async def test_unknown_slug_writes_nothing_to_storage(
    client: AsyncClient, stop: tuple[SeededTrip, str], swept_prefixes: list[str]
) -> None:
    """
    A ``404`` on an unknown slug leaves no object anywhere for that photo id.

    An unknown slug resolves to no trip, so there is no single trip prefix a
    misplaced upload would land under. The stop's own prefix is checked, and so
    is the whole bucket for any key ending in the (fresh, unique) photo id.
    """
    trip, stop_id = stop
    photo_id = str(uuid4())
    prefix = f"{trip.id}/{stop_id}/"
    swept_prefixes.append(prefix)

    response = await client.post(
        PHOTOS_PATH.format(slug=f"no-such-slug-{secrets.token_urlsafe(8)}", stop_id=stop_id),
        data=form(photo_id),
        files=upload(b"unknown-slug-bytes"),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    assert keys_under(prefix) == []
    assert [key for key in keys_under("") if key.endswith(f"/{photo_id}")] == []
