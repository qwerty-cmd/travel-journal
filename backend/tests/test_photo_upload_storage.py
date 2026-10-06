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
4. **A storage or database failure mid-upload is a retryable ``500``**, never
   a silent drop: ``INTERNAL_ERROR`` (the code the offline queue retries on),
   no row left behind, and a retry under the same id lands the retry's bytes.
5. **A stop on another trip writes nothing anywhere** -- not under either
   trip's prefix, not in the table.
6. **``PhotoOut.url`` is a SigV4 presigned GET** for the deterministic key,
   valid for an hour, and it actually serves the uploaded bytes.

**Since decision-log Entry 29 (``t-am-write-gate-legacy``).** A slug only
locates the trip; the write gate needs a signed-in, active member. Every request
here acts as the ``rider_session`` fixture (an active rider on both seeded
trips), and the "viewer slug" tests send a ``non_member_session`` instead, so
their ``403`` now comes from the membership gate rather than from the slug. The
assertions are unchanged. A viewer slug with a member's session is a successful
write (contract default 21); ``test_access_matrix.py`` covers that and the rest
of the identity matrix.
"""

from __future__ import annotations

import io
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import ClientError
from conftest import SeededTrip, SignedInAccount, make_async_client
from httpx import AsyncClient
from jpeg_fixtures import jpeg_with_scan, minimal_jpeg
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.api.routes.photos as photos_route
from app.core.errors import INTERNAL_ERROR_MESSAGE
from app.data import tables
from app.data.db import get_session
from app.models.common import ErrorCode, ErrorEnvelope
from app.storage.s3_client import BUCKET_NAME, get_s3_client

PHOTOS_PATH = "/api/trips/{slug}/stops/{stop_id}/photos"

# boto3's default multipart_threshold is 8 MiB; one byte over forces the
# multipart branch of upload_fileobj.
ABOVE_MULTIPART_THRESHOLD = 8 * 1024 * 1024 + 1


async def _app_client(
    engine: AsyncEngine, account: SignedInAccount, *, raise_app_exceptions: bool
) -> AsyncIterator[AsyncClient]:
    import app.main

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application = app.main.app
    application.dependency_overrides[get_session] = session_override
    try:
        # Every request acts as `account` (the `rider_session`, an active member of
        # both seeded trips): since decision-log Entry 29 the slug only locates the
        # trip and the write gate needs a member's session (t-am-write-gate-legacy).
        async with make_async_client(
            application, headers=account.headers, raise_app_exceptions=raise_app_exceptions
        ) as http_client:
            yield http_client
    finally:
        application.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def client(
    migrated_engine: AsyncEngine, rider_session: SignedInAccount
) -> AsyncIterator[AsyncClient]:
    """The real application, on the test database."""
    async for http_client in _app_client(migrated_engine, rider_session, raise_app_exceptions=True):
        yield http_client


@pytest.fixture
async def quiet_client(
    migrated_engine: AsyncEngine, rider_session: SignedInAccount
) -> AsyncIterator[AsyncClient]:
    """
    The same app, but an unhandled exception comes back as the ``500`` a phone sees.

    Starlette's ``ServerErrorMiddleware`` sends the envelope *and* re-raises, and
    ``ASGITransport`` would surface the re-raise instead of the response -- which
    is the thing under test.
    """
    async for http_client in _app_client(
        migrated_engine, rider_session, raise_app_exceptions=False
    ):
        yield http_client


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
    content = jpeg_with_scan(os.urandom(size))

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

    first = await client.post(url, data=form(photo_id), files=upload(minimal_jpeg(1)))
    replay = await client.post(url, data=form(photo_id), files=upload(minimal_jpeg(2)))

    assert first.status_code == HTTPStatus.CREATED, first.text
    assert replay.status_code == HTTPStatus.OK, replay.text
    assert stored_bytes(key) == minimal_jpeg(1)


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

    created = await client.post(url, data=form(photo_id), files=upload(minimal_jpeg()))
    replay = await client.post(url, data=form(photo_id), files=upload(minimal_jpeg()))

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
        files=upload(minimal_jpeg(1)),
    )
    conflict = await client.post(
        PHOTOS_PATH.format(slug=other_trip.rider_slug, stop_id=other_stop_id),
        data=form(photo_id),
        files=upload(minimal_jpeg(2)),
    )

    assert first.status_code == HTTPStatus.CREATED, first.text
    assert conflict.status_code == HTTPStatus.CONFLICT, conflict.text
    assert keys_under(conflicting_prefix) == []
    # And the original object is still the original.
    assert stored_bytes(f"{trip.id}/{stop_id}/{photo_id}") == minimal_jpeg(1)


async def test_viewer_slug_writes_nothing_to_storage(
    client: AsyncClient,
    stop: tuple[SeededTrip, str],
    swept_prefixes: list[str],
    non_member_session: SignedInAccount,
) -> None:
    """A ``403`` on the viewer slug leaves no object under the stop's key."""
    trip, stop_id = stop
    photo_id = str(uuid4())
    prefix = f"{trip.id}/{stop_id}/"
    swept_prefixes.append(prefix)

    response = await client.post(
        PHOTOS_PATH.format(slug=trip.viewer_slug, stop_id=stop_id),
        data=form(photo_id),
        files=upload(minimal_jpeg()),
        headers=non_member_session.headers,
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
        files=upload(minimal_jpeg()),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    assert keys_under(prefix) == []
    assert [key for key in keys_under("") if key.endswith(f"/{photo_id}")] == []


# --------------------------------------------------------------------------
# A failure mid-upload is a retryable 500, never a silent drop
# --------------------------------------------------------------------------
#
# Contract "Idempotency" and "Error envelope", decision-log Entry 20: the offline
# queue retries INTERNAL_ERROR and never-retries everything else, so a storage or
# database fault has to come back as exactly that code -- and must leave nothing
# half-written that would turn the retry into a 200 replay of a photo whose bytes
# never landed, or into a 409.


async def photo_rows(engine: AsyncEngine, photo_id: str) -> list[Any]:
    """Every photos row with this id, straight from the table."""
    async with engine.connect() as conn:
        result = await conn.execute(tables.photos.select().where(tables.photos.c.id == photo_id))
        return list(result.mappings())


def assert_internal_error_leaks_nothing(response: httpx.Response, *secrets_: str) -> None:
    """500 / INTERNAL_ERROR with the fixed message, and none of ``secrets_`` in the body."""
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR, response.text
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code == ErrorCode.INTERNAL_ERROR
    assert envelope.error.message == INTERNAL_ERROR_MESSAGE
    for value in secrets_:
        assert value not in response.text, f"{value!r} leaked into the 500 body"


async def test_a_storage_failure_is_a_500_that_writes_no_row_and_leaks_no_slug(
    quiet_client: AsyncClient,
    migrated_engine: AsyncEngine,
    stop: tuple[SeededTrip, str],
    swept_prefixes: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``PutObject`` fails: the queue must see ``INTERNAL_ERROR`` and the table must
    hold nothing. A row without its object would answer every retry with a ``200``
    replay pointing at bytes that never landed -- the photo lost, silently.

    The simulated S3 error text carries the slug, the object key and a host, the
    kind of thing a real botocore message can carry; none may reach the body.
    """
    trip, stop_id = stop
    photo_id = str(uuid4())
    key = f"{trip.id}/{stop_id}/{photo_id}"
    swept_prefixes.append(f"{trip.id}/{stop_id}/")
    leaky = f"minio.internal:9000 {BUCKET_NAME}/{key} via {trip.rider_slug}"

    real_get_s3_client = photos_route.get_s3_client

    def failing_upload_client():
        s3 = real_get_s3_client()

        def upload_fileobj(*args: Any, **kwargs: Any) -> None:
            raise ClientError({"Error": {"Code": "InternalError", "Message": leaky}}, "PutObject")

        s3.upload_fileobj = upload_fileobj
        return s3

    monkeypatch.setattr(photos_route, "get_s3_client", failing_upload_client)

    response = await quiet_client.post(
        PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop_id),
        data=form(photo_id),
        files=upload(minimal_jpeg()),
    )

    assert_internal_error_leaks_nothing(
        response, trip.rider_slug, trip.viewer_slug, key, "minio.internal", BUCKET_NAME
    )
    assert await photo_rows(migrated_engine, photo_id) == []
    assert keys_under(f"{trip.id}/{stop_id}/") == []


async def test_an_insert_failure_after_upload_is_a_500_and_a_retry_lands_the_retry_bytes(
    quiet_client: AsyncClient,
    migrated_engine: AsyncEngine,
    stop: tuple[SeededTrip, str],
    created_keys: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The object lands, then the ``INSERT`` fails once. Entry 20's safety argument:
    the key is deterministic, so the queue's retry under the same id overwrites
    rather than duplicates, and it is a fresh ``201`` -- not a ``409`` from the
    orphaned object, not a ``200`` replay of a row that never committed.
    """
    trip, stop_id = stop
    photo_id = str(uuid4())
    key = f"{trip.id}/{stop_id}/{photo_id}"
    created_keys.append(key)
    url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop_id)

    real_insert = photos_route.insert
    calls = 0

    async def insert_failing_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError(
                "INSERT INTO photos", {}, Exception(f"connection to db lost ({trip.rider_slug})")
            )
        return await real_insert(*args, **kwargs)

    monkeypatch.setattr(photos_route, "insert", insert_failing_once)

    failed = await quiet_client.post(url, data=form(photo_id), files=upload(minimal_jpeg(1)))

    assert_internal_error_leaks_nothing(failed, trip.rider_slug, trip.viewer_slug, key)
    assert await photo_rows(migrated_engine, photo_id) == []

    retry = await quiet_client.post(url, data=form(photo_id), files=upload(minimal_jpeg(2)))

    assert retry.status_code == HTTPStatus.CREATED, retry.text
    assert retry.json()["id"] == photo_id
    assert calls == 2
    rows = await photo_rows(migrated_engine, photo_id)
    assert len(rows) == 1
    assert rows[0]["stop_id"] == stop_id
    assert rows[0]["object_key"] == key
    assert stored_bytes(key) == minimal_jpeg(2)


# --------------------------------------------------------------------------
# A stop on another trip writes nothing anywhere
# --------------------------------------------------------------------------


async def test_a_stop_on_another_trip_is_404_and_writes_nothing_under_either_trip(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    other_trip_stop: tuple[SeededTrip, str],
    swept_prefixes: list[str],
) -> None:
    """
    Trip 1's rider slug, trip 2's stop id. The trips are fresh per test, so both
    whole trip prefixes must be empty -- an upload keyed on the slug's trip and
    one keyed on the stop's real trip are both caught.
    """
    trip = seeded_trips[0]
    other_trip, other_stop_id = other_trip_stop
    photo_id = str(uuid4())
    swept_prefixes.extend([f"{trip.id}/", f"{other_trip.id}/"])

    response = await client.post(
        PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=other_stop_id),
        data=form(photo_id),
        files=upload(minimal_jpeg()),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    assert ErrorEnvelope.model_validate(response.json()).error.code == ErrorCode.NOT_FOUND
    assert keys_under(f"{trip.id}/") == []
    assert keys_under(f"{other_trip.id}/") == []
    assert await photo_rows(migrated_engine, photo_id) == []


# --------------------------------------------------------------------------
# PhotoOut.url is a SigV4 presigned GET that serves the uploaded bytes
# --------------------------------------------------------------------------

SIGV4_PARAMS = {
    "X-Amz-Algorithm",
    "X-Amz-Credential",
    "X-Amz-Date",
    "X-Amz-Expires",
    "X-Amz-SignedHeaders",
    "X-Amz-Signature",
}


async def assert_presigned_get(url: str, key: str, content: bytes) -> None:
    """
    Shape and behaviour of one presigned URL. The host is deliberately not
    asserted: the public endpoint may be configured separately from the one the
    API talks to, and path-style and virtual-host style both end in the key.
    """
    parts = urlsplit(url)
    assert parts.scheme in {"http", "https"}, url
    assert unquote(parts.path).endswith(f"/{key}"), url

    query = parse_qs(parts.query)
    assert SIGV4_PARAMS <= query.keys(), f"missing SigV4 params: {SIGV4_PARAMS - query.keys()}"
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["3600"]

    # Straight to the object store, not through the agent proxy. Not our app, so
    # not the shared app-client factory.
    async with httpx.AsyncClient(trust_env=False) as s3_http:
        fetched = await s3_http.get(url)
    assert fetched.status_code == HTTPStatus.OK, fetched.text
    assert fetched.content == content


async def test_photo_url_is_a_sigv4_presigned_get_that_serves_the_uploaded_bytes(
    client: AsyncClient, stop: tuple[SeededTrip, str], created_keys: list[str]
) -> None:
    """Both the ``201`` body and the list carry a working signed GET for the key."""
    trip, stop_id = stop
    photo_id = str(uuid4())
    key = f"{trip.id}/{stop_id}/{photo_id}"
    created_keys.append(key)
    content = jpeg_with_scan(os.urandom(4096))
    url = PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop_id)

    created = await client.post(url, data=form(photo_id), files=upload(content))
    assert created.status_code == HTTPStatus.CREATED, created.text
    await assert_presigned_get(created.json()["url"], key, content)

    listed = await client.get(PHOTOS_PATH.format(slug=trip.viewer_slug, stop_id=stop_id))
    assert listed.status_code == HTTPStatus.OK, listed.text
    photo = next(p for p in listed.json() if p["id"] == photo_id)
    await assert_presigned_get(photo["url"], key, content)
