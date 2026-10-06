"""
``POST /api/v2/trips/{trip_id}/stops/{stop_id}/photos`` -- the full-file strip over HTTP.

Task ``t-am-jpeg-after-sos`` (decision-log Entry 31). Written from
``docs/api-contract.md`` ("Photo upload: JPEG only, metadata stripped, 15 MiB
cap") and the task's AC, not from the implementation. The walker is unit-tested
in ``test_jpeg_strip.py``; this file checks what reaches S3 and the database
through the real app, acting as an active rider of the trip.

1. **Stored bytes.** Metadata between progressive scans, MPF and everything
   after the first EOI never reaches S3: the object is exactly the clean
   primary image, and a canary planted in every droppable location is absent.
2. **Rejects (AC6).** Every truncation of a complete progressive JPEG, and each
   malformed-after-a-scan case, is ``422 VALIDATION_ERROR`` leaving no S3
   object and no ``photos`` row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from conftest import SeededTrip, SignedInAccount, make_async_client
from httpx import AsyncClient
from jpeg_fixtures import APP0_JFIF, EOI, SOI, dqt, minimal_jpeg, progressive_jpeg, segment
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from test_photo_upload_storage import keys_under, photo_rows, stored_bytes

from app.core import ratelimit
from app.data import tables
from app.data.db import get_session
from app.models.common import ErrorCode, ErrorEnvelope
from app.storage.s3_client import BUCKET_NAME, get_s3_client

CANARY = b"CANARY-v2-5d81e0"


def canary_app1(where: str) -> bytes:
    return segment(0xE1, b"Exif\x00\x00MM\x00\x2a" + CANARY + where.encode())


def canary_com(where: str) -> bytes:
    return segment(0xFE, CANARY + where.encode())


MPF_APP2 = segment(0xE2, b"MPF\x00MM\x00\x2a\x00\x00\x00\x08" + CANARY)
SECONDARY = minimal_jpeg(2)[:2] + canary_app1("secondary") + minimal_jpeg(2)[2:]
MP4_TRAILER = b"\x00\x00\x00\x1cftypmp42\x00\x00\x00\x00isommp42" + CANARY


@pytest.fixture
async def application(migrated_engine: AsyncEngine) -> AsyncIterator[Any]:
    import app.main

    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.main.app.dependency_overrides[get_session] = session_override
    try:
        yield app.main.app
    finally:
        app.main.app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def client(application: Any, rider_session: SignedInAccount) -> AsyncIterator[AsyncClient]:
    """Acts as ``rider_session``: an active rider on both seeded trips."""
    async with make_async_client(
        application, headers=rider_session.headers, raise_app_exceptions=False
    ) as http_client:
        yield http_client


@pytest.fixture
async def stop(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[tuple[SeededTrip, str]]:
    trip = seeded_trips[0]
    stop_id = f"test-jpeg-v2-after-sos-{uuid4()}"
    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.stops.insert().values(
                id=stop_id,
                trip_id=trip.id,
                name="After-SOS stop",
                lat=-14.3,
                lng=132.4,
                location_source="gps",
                arrived_at=datetime(2026, 6, 2, 8, 30, tzinfo=UTC),
            )
        )
    try:
        yield trip, stop_id
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.photos.delete().where(tables.photos.c.stop_id == stop_id))
            await conn.execute(tables.stops.delete().where(tables.stops.c.id == stop_id))


@pytest.fixture
def swept(s3_bucket: None, stop: tuple[SeededTrip, str]) -> Iterator[None]:
    yield
    trip, stop_id = stop
    s3 = get_s3_client()
    for key in keys_under(f"{trip.id}/{stop_id}/"):
        s3.delete_object(Bucket=BUCKET_NAME, Key=key)


async def upload(client: AsyncClient, trip_id: str, stop_id: str, photo_id: str, content: bytes):
    return await client.post(
        f"/api/v2/trips/{trip_id}/stops/{stop_id}/photos",
        data={"id": photo_id, "takenAt": "2026-06-14T10:00:00+09:30"},
        files={"file": ("photo.jpg", content, "image/jpeg")},
    )


def assert_validation_error(response: httpx.Response) -> None:
    assert response.status_code == 422, response.text
    assert ErrorEnvelope.model_validate(response.json()).error.code == ErrorCode.VALIDATION_ERROR


# --------------------------------------------------------------------------
# 1. Stored bytes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dirty",
    [
        pytest.param(
            progressive_jpeg(
                pre=canary_app1("pre") + canary_com("pre"),
                between=(canary_app1("b1") + canary_com("b1"), canary_com("b2")),
            ),
            id="between-scans",
        ),
        pytest.param(progressive_jpeg(pre=MPF_APP2, trailer=SECONDARY), id="mpf-secondary"),
        pytest.param(progressive_jpeg(trailer=MP4_TRAILER), id="motion-photo"),
        pytest.param(
            progressive_jpeg(
                pre=canary_app1("pre") + MPF_APP2 + segment(0xE0, b"JFXX\x00\x10" + CANARY),
                between=(canary_app1("b1"), canary_com("b2") + segment(0xEF, CANARY)),
                trailer=SECONDARY + MP4_TRAILER,
            ),
            id="canary-everywhere",
        ),
    ],
)
async def test_stored_object_is_exactly_the_clean_primary(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    stop: tuple[SeededTrip, str],
    swept: None,
    dirty: bytes,
) -> None:
    trip, stop_id = stop
    photo_id = str(uuid4())
    response = await upload(client, trip.id, stop_id, photo_id, dirty)
    assert response.status_code == 201, response.text
    stored = stored_bytes(f"{trip.id}/{stop_id}/{photo_id}")
    assert stored == progressive_jpeg()
    assert CANARY not in stored
    assert len(await photo_rows(migrated_engine, photo_id)) == 1


async def test_a_clean_progressive_upload_is_stored_byte_identical(
    client: AsyncClient, stop: tuple[SeededTrip, str], swept: None
) -> None:
    trip, stop_id = stop
    photo_id = str(uuid4())
    response = await upload(client, trip.id, stop_id, photo_id, progressive_jpeg())
    assert response.status_code == 201, response.text
    assert stored_bytes(f"{trip.id}/{stop_id}/{photo_id}") == progressive_jpeg()


# --------------------------------------------------------------------------
# 2. Rejects: 422 VALIDATION_ERROR, nothing stored
# --------------------------------------------------------------------------


async def test_every_truncation_of_a_progressive_jpeg_is_422_and_stores_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    stop: tuple[SeededTrip, str],
    swept: None,
) -> None:
    """AC6 over HTTP, at every offset including inside and between scans."""
    trip, stop_id = stop
    full = progressive_jpeg()
    photo_ids = []
    for cut in range(len(full)):
        ratelimit.registry.reset()  # one request per offset; the writes bucket is not under test
        photo_id = str(uuid4())
        photo_ids.append(photo_id)
        response = await upload(client, trip.id, stop_id, photo_id, full[:cut])
        assert response.status_code == 422, f"cut={cut}: {response.status_code} {response.text}"
        assert_validation_error(response)
    assert keys_under(f"{trip.id}/{stop_id}/") == []
    for photo_id in photo_ids:
        assert await photo_rows(migrated_engine, photo_id) == []


_HEAD = SOI + APP0_JFIF + dqt()


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(progressive_jpeg()[:-2], id="no-eoi"),
        pytest.param(progressive_jpeg(between=(SOI, b"")), id="soi-after-scan"),
        pytest.param(progressive_jpeg(between=(b"\xff\x01junk", b"")), id="tem-then-junk"),
        pytest.param(progressive_jpeg(between=(b"\xff\xe1\xff\xff", b"")), id="length-past-end"),
        pytest.param(_HEAD + EOI + progressive_jpeg()[len(_HEAD) :], id="eoi-before-sos"),
    ],
)
async def test_malformed_after_a_scan_is_422_and_stores_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    stop: tuple[SeededTrip, str],
    swept: None,
    data: bytes,
) -> None:
    trip, stop_id = stop
    photo_id = str(uuid4())
    response = await upload(client, trip.id, stop_id, photo_id, data)
    assert_validation_error(response)
    assert keys_under(f"{trip.id}/{stop_id}/") == []
    assert await photo_rows(migrated_engine, photo_id) == []
