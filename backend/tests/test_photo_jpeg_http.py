"""
``POST /api/trips/{slug}/stops/{stop_id}/photos`` -- JPEG only, metadata stripped, 15 MiB cap.

Task ``t-am-photo-exif-strip``, obligation 12, at the HTTP level. Written from
``docs/api-contract.md`` ("Photo upload: JPEG only, metadata stripped, 15 MiB
cap") and the task's acceptance criteria. The walker itself is unit-tested in
``test_jpeg_strip.py``; this file checks what reaches S3 and the database
through the real app, the local Postgres and the local S3 endpoint.

1. **Stripping.** APP1-APP15 and COM are gone from the stored object; everything
   else, including fill bytes on kept markers and RST markers in the scan, is
   byte-identical.
2. **Rejects.** Anything that is not a JPEG walking cleanly to SOS is a ``422
   VALIDATION_ERROR`` that leaves no object and no row. The bytes decide; the
   part's content type and filename do not.
3. **Size.** Exactly 15 MiB is accepted, one byte more is not. A declared
   ``Content-Length`` over 16 MiB is refused before any body message is
   received, even on an unknown slug; a chunked body is cut off as it crosses
   16 MiB.
4. **Ordering.** The legacy gate (slug 404, session 401, membership 403) and
   the replay branch run before the JPEG check; the JPEG check runs before the
   cross-stop 409.
5. **The 422 body echoes none of the uploaded bytes.**
"""

from __future__ import annotations

import io
import secrets
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import httpx
import pytest
from conftest import TEST_ORIGIN, SeededTrip, SignedInAccount, make_async_client
from httpx import AsyncClient
from jpeg_fixtures import (
    APP0_JFIF,
    SOI,
    SOS_HEADER,
    header_segments,
    jpeg_with_scan,
    minimal_jpeg,
    segment,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from test_photo_upload_storage import keys_under, photo_rows, stored_bytes

import app.api.routes.photos as photos_route
from app.api.routes.photos import PHOTO_NOT_JPEG_MESSAGE, PHOTO_TOO_LARGE_MESSAGE
from app.data import tables
from app.data.db import get_session
from app.models.common import ErrorCode, ErrorEnvelope
from app.storage.s3_client import BUCKET_NAME, get_s3_client

PHOTOS_PATH = "/api/trips/{slug}/stops/{stop_id}/photos"

MIB = 1024 * 1024
MAX_PHOTO = 15 * MIB  # 15,728,640 -- the contract's number, not the module constant
MAX_REQUEST = 16 * MIB

# The kept segments between APP0 and SOS, as minimal_jpeg() lays them out.
AFTER_APP0 = minimal_jpeg()[len(SOI) + len(APP0_JFIF) :]

EXIF_GPS = segment(
    0xE1, b"Exif\x00\x00MM\x00\x2a\x00\x00\x00\x08GPSLatitude=-14.3;GPSLongitude=132.4"
)
ICC = segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + b"\x00" * 64)
IRB = segment(0xED, b"Photoshop 3.0\x008BIM\x04\x04\x00\x00")
COM = segment(0xFE, b"shot by alice on the stuart highway")
APP15 = segment(0xEF, b"vendor-private-app15")
DRI = segment(0xDD, b"\x00\x04")

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x08\x00\x00\x00\x08\x08\x02\x00\x00\x00"
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 32
GIF = b"GIF89a\x08\x00\x08\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00,"


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
async def app_under_test(migrated_engine: AsyncEngine) -> AsyncIterator[Any]:
    """The real application with its database session pointed at the test database."""
    import app.main

    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application = app.main.app
    application.dependency_overrides[get_session] = session_override
    try:
        yield application
    finally:
        application.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def client(app_under_test: Any, rider_session: SignedInAccount) -> AsyncIterator[AsyncClient]:
    """Acts as the ``rider_session`` account: an active rider on both seeded trips."""
    async with make_async_client(app_under_test, headers=rider_session.headers) as http_client:
        yield http_client


@pytest.fixture
async def anon_client(app_under_test: Any) -> AsyncIterator[AsyncClient]:
    """Same-origin, but no session cookie at all."""
    async with make_async_client(app_under_test) as http_client:
        yield http_client


async def _make_stop(engine: AsyncEngine, trip: SeededTrip, label: str) -> str:
    stop_id = f"test-jpeg-http-{label}-{secrets.token_urlsafe(8)}"
    async with engine.begin() as conn:
        await conn.execute(
            tables.stops.insert().values(
                id=stop_id,
                trip_id=trip.id,
                name="JPEG stop",
                lat=-14.3,
                lng=132.4,
                location_source="gps",
                arrived_at=datetime(2026, 6, 2, 8, 30, tzinfo=UTC),
                notes=None,
            )
        )
    return stop_id


async def _drop_stop(engine: AsyncEngine, stop_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(tables.photos.delete().where(tables.photos.c.stop_id == stop_id))
        await conn.execute(tables.stops.delete().where(tables.stops.c.id == stop_id))


@pytest.fixture
async def stop(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[tuple[SeededTrip, str]]:
    """One stop on the first seeded trip."""
    trip = seeded_trips[0]
    stop_id = await _make_stop(migrated_engine, trip, "a")
    try:
        yield trip, stop_id
    finally:
        await _drop_stop(migrated_engine, stop_id)


@pytest.fixture
async def other_stop(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[tuple[SeededTrip, str]]:
    """One stop on the second seeded trip -- the other side of a cross-stop conflict."""
    trip = seeded_trips[1]
    stop_id = await _make_stop(migrated_engine, trip, "b")
    try:
        yield trip, stop_id
    finally:
        await _drop_stop(migrated_engine, stop_id)


@pytest.fixture
def bucket_sweep(s3_bucket: None) -> Iterator[list[str]]:
    """Prefixes to clear after the test, whatever it left behind."""
    prefixes: list[str] = []
    try:
        yield prefixes
    finally:
        s3 = get_s3_client()
        for prefix in prefixes:
            for key in keys_under(prefix):
                s3.delete_object(Bucket=BUCKET_NAME, Key=key)


def form(photo_id: str) -> dict[str, str]:
    return {"id": photo_id, "takenAt": "2026-06-14T10:00:00+09:30"}


def upload(
    content: bytes, filename: str = "photo.jpg", content_type: str = "image/jpeg"
) -> dict[str, tuple[str, io.BytesIO, str]]:
    return {"file": (filename, io.BytesIO(content), content_type)}


BOUNDARY = "jpeg-http-test-boundary"


def multipart_body(photo_id: str, content: bytes) -> bytes:
    """A hand-built multipart body, so its exact length is known."""
    parts = []
    for name, value in form(photo_id).items():
        parts.append(
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    parts.append(
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="photo.jpg"'
        f"\r\nContent-Type: image/jpeg\r\n\r\n".encode()
        + content
        + b"\r\n"
    )
    parts.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(parts)


def assert_validation_error(response: httpx.Response, message: str) -> None:
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code == ErrorCode.VALIDATION_ERROR
    assert envelope.error.message == message


def markers_before_sos(data: bytes) -> list[int]:
    """Every marker byte in front of the first SOS, skipping fill bytes (test-side walk)."""
    markers = []
    pos = 2  # past SOI
    while pos < len(data):
        assert data[pos] == 0xFF, f"no marker at offset {pos}"
        while data[pos] == 0xFF:
            pos += 1
        marker = data[pos]
        markers.append(marker)
        if marker == 0xDA:
            return markers
        length = int.from_bytes(data[pos + 1 : pos + 3], "big")
        pos += 1 + length
    raise AssertionError("no SOS in the stored object")


def assert_no_metadata_markers(data: bytes) -> None:
    for marker in markers_before_sos(data):
        assert not (0xE1 <= marker <= 0xEF), f"FF{marker:02X} survived before SOS"
        assert marker != 0xFE, "COM survived before SOS"


async def post_photo(
    client: AsyncClient, slug: str, stop_id: str, photo_id: str, content: bytes, **kwargs: Any
) -> httpx.Response:
    return await client.post(
        PHOTOS_PATH.format(slug=slug, stop_id=stop_id),
        data=form(photo_id),
        files=upload(content, **kwargs.pop("file_kwargs", {})),
        **kwargs,
    )


async def assert_nothing_stored(
    engine: AsyncEngine, trip: SeededTrip, stop_id: str, photo_id: str
) -> None:
    assert keys_under(f"{trip.id}/{stop_id}/") == []
    assert await photo_rows(engine, photo_id) == []


# --------------------------------------------------------------------------
# 1. Stripping
# --------------------------------------------------------------------------


async def test_exif_gps_icc_irb_and_com_are_stripped_and_the_rest_is_byte_identical(
    client: AsyncClient, stop: tuple[SeededTrip, str], bucket_sweep: list[str]
) -> None:
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())
    body = SOI + APP0_JFIF + EXIF_GPS + ICC + IRB + COM + AFTER_APP0
    assert b"GPS" in body

    response = await post_photo(client, trip.rider_slug, stop_id, photo_id, body)

    assert response.status_code == HTTPStatus.CREATED, response.text
    stored = stored_bytes(f"{trip.id}/{stop_id}/{photo_id}")
    # minimal_jpeg() is a complete, decodable baseline JPEG, so identity implies it decodes.
    assert stored == minimal_jpeg()
    assert_no_metadata_markers(stored)
    for leaked in (b"Exif", b"GPS", b"ICC_PROFILE", b"Photoshop", b"stuart highway"):
        assert leaked not in stored


async def test_app15_is_stripped(
    client: AsyncClient, stop: tuple[SeededTrip, str], bucket_sweep: list[str]
) -> None:
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())
    body = SOI + APP15 + APP0_JFIF + AFTER_APP0

    response = await post_photo(client, trip.rider_slug, stop_id, photo_id, body)

    assert response.status_code == HTTPStatus.CREATED, response.text
    stored = stored_bytes(f"{trip.id}/{stop_id}/{photo_id}")
    assert stored == minimal_jpeg()
    assert_no_metadata_markers(stored)


async def test_fill_bytes_before_a_dropped_segment_go_with_it(
    client: AsyncClient, stop: tuple[SeededTrip, str], bucket_sweep: list[str]
) -> None:
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())
    body = SOI + APP0_JFIF + b"\xff\xff\xff" + EXIF_GPS + b"\xff" + COM + AFTER_APP0

    response = await post_photo(client, trip.rider_slug, stop_id, photo_id, body)

    assert response.status_code == HTTPStatus.CREATED, response.text
    stored = stored_bytes(f"{trip.id}/{stop_id}/{photo_id}")
    assert stored == minimal_jpeg()


async def test_fill_bytes_before_kept_markers_are_accepted_and_kept(
    client: AsyncClient, stop: tuple[SeededTrip, str], bucket_sweep: list[str]
) -> None:
    """Nothing to strip -> the stored object is the upload, fill bytes and all."""
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())
    app0, dqt, sof, dht_dc, dht_ac = header_segments()
    body = (
        SOI
        + b"\xff"
        + app0
        + b"\xff\xff"
        + dqt
        + sof
        + dht_dc
        + dht_ac
        + b"\xff\xff\xff"
        + SOS_HEADER
        + b"\x3f\xff\xd9"
    )

    response = await post_photo(client, trip.rider_slug, stop_id, photo_id, body)

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert stored_bytes(f"{trip.id}/{stop_id}/{photo_id}") == body


async def test_dri_and_rst_markers_in_the_scan_survive_untouched(
    client: AsyncClient, stop: tuple[SeededTrip, str], bucket_sweep: list[str]
) -> None:
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())
    # RST0..RST7, FF00 stuffing and a fill-padded RST, all after SOS.
    scan = (
        b"\x12\x34"
        + b"".join(bytes([0xFF, 0xD0 + n, n]) for n in range(8))
        + b"\xff\x00\x56\xff\xff\xd3\x78"
    )
    kept_header = b"".join(header_segments()) + DRI
    body = SOI + kept_header[: len(APP0_JFIF)] + EXIF_GPS + kept_header[len(APP0_JFIF) :]
    body += SOS_HEADER + scan + b"\xff\xd9"
    expected = SOI + kept_header + SOS_HEADER + scan + b"\xff\xd9"

    response = await post_photo(client, trip.rider_slug, stop_id, photo_id, body)

    assert response.status_code == HTTPStatus.CREATED, response.text
    stored = stored_bytes(f"{trip.id}/{stop_id}/{photo_id}")
    assert stored == expected
    assert 0xDD in markers_before_sos(stored)


# --------------------------------------------------------------------------
# 2. Rejects
# --------------------------------------------------------------------------

NOT_JPEG_CASES = {
    "png": PNG,
    "heic": HEIC,
    "gif": GIF,
    "empty": b"",
    "truncated": minimal_jpeg()[:40],
    "truncated-inside-exif": SOI + APP0_JFIF + EXIF_GPS[:12],
    "no-sos": SOI + b"".join(header_segments()) + b"\xff\xd9",
    "soi-only": SOI,
}


@pytest.mark.parametrize("content", NOT_JPEG_CASES.values(), ids=NOT_JPEG_CASES.keys())
async def test_non_jpeg_is_422_and_stores_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    stop: tuple[SeededTrip, str],
    bucket_sweep: list[str],
    content: bytes,
) -> None:
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())

    response = await post_photo(client, trip.rider_slug, stop_id, photo_id, content)

    assert_validation_error(response, PHOTO_NOT_JPEG_MESSAGE)
    await assert_nothing_stored(migrated_engine, trip, stop_id, photo_id)


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [("notes.txt", "text/plain"), ("blob", "application/octet-stream"), ("x.png", "image/png")],
)
async def test_the_bytes_decide_not_the_part_content_type_or_filename(
    client: AsyncClient,
    stop: tuple[SeededTrip, str],
    bucket_sweep: list[str],
    filename: str,
    content_type: str,
) -> None:
    """Contract: 'The file must start FF D8 FF and walk cleanly to SOS' -- nothing about labels."""
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())
    body = SOI + APP0_JFIF + EXIF_GPS + AFTER_APP0

    response = await post_photo(
        client,
        trip.rider_slug,
        stop_id,
        photo_id,
        body,
        file_kwargs={"filename": filename, "content_type": content_type},
    )

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert stored_bytes(f"{trip.id}/{stop_id}/{photo_id}") == minimal_jpeg()


async def test_a_png_labelled_image_jpeg_is_still_422(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    stop: tuple[SeededTrip, str],
    bucket_sweep: list[str],
) -> None:
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())

    response = await post_photo(
        client,
        trip.rider_slug,
        stop_id,
        photo_id,
        PNG,
        file_kwargs={"filename": "photo.jpg", "content_type": "image/jpeg"},
    )

    assert_validation_error(response, PHOTO_NOT_JPEG_MESSAGE)
    await assert_nothing_stored(migrated_engine, trip, stop_id, photo_id)


# --------------------------------------------------------------------------
# 3. Size
# --------------------------------------------------------------------------


def jpeg_of_size(size: int) -> bytes:
    overhead = len(jpeg_with_scan(b""))
    content = jpeg_with_scan(b"\x5a" * (size - overhead))
    assert len(content) == size
    return content


async def test_exactly_15_mib_is_accepted_and_stored_whole(
    client: AsyncClient, stop: tuple[SeededTrip, str], bucket_sweep: list[str]
) -> None:
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())
    content = jpeg_of_size(MAX_PHOTO)

    response = await post_photo(client, trip.rider_slug, stop_id, photo_id, content)

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert stored_bytes(f"{trip.id}/{stop_id}/{photo_id}") == content


async def test_15_mib_plus_one_byte_is_422_and_stores_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    stop: tuple[SeededTrip, str],
    bucket_sweep: list[str],
) -> None:
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())

    response = await post_photo(
        client, trip.rider_slug, stop_id, photo_id, jpeg_of_size(MAX_PHOTO + 1)
    )

    assert_validation_error(response, PHOTO_TOO_LARGE_MESSAGE)
    await assert_nothing_stored(migrated_engine, trip, stop_id, photo_id)


async def test_a_request_under_16_mib_carrying_a_file_over_15_mib_is_422(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    stop: tuple[SeededTrip, str],
    bucket_sweep: list[str],
) -> None:
    """The request cap alone would let this through; the file-part cap must catch it."""
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())
    framing = len(multipart_body(photo_id, b""))
    body = multipart_body(photo_id, jpeg_of_size(MAX_REQUEST - framing - 1))
    assert MAX_PHOTO < len(body) < MAX_REQUEST

    response = await client.post(
        PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop_id),
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
    )

    assert_validation_error(response, PHOTO_TOO_LARGE_MESSAGE)
    await assert_nothing_stored(migrated_engine, trip, stop_id, photo_id)


class RawAsgiCall:
    """Drive the app with a hand-made receive, counting the body messages it pulls."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.body_messages_received = 0
        self.status: int | None = None
        self.body = b""

    async def receive(self) -> dict[str, Any]:
        if self.body_messages_received < len(self._chunks):
            index = self.body_messages_received
            self.body_messages_received += 1
            return {
                "type": "http.request",
                "body": self._chunks[index],
                "more_body": index < len(self._chunks) - 1,
            }
        return {"type": "http.disconnect"}

    async def send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")

    async def run(self, application: Any, path: str, headers: dict[str, str]) -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [
                (b"host", b"testserver"),
                (b"origin", TEST_ORIGIN.encode()),
                (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode()),
                *((k.lower().encode(), v.encode()) for k, v in headers.items()),
            ],
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 50000),
        }
        await application(scope, self.receive, self.send)

    def envelope(self) -> ErrorEnvelope:
        import json

        return ErrorEnvelope.model_validate(json.loads(self.body))


@pytest.mark.parametrize("slug_kind", ["rider", "unknown"])
async def test_declared_content_length_over_16_mib_is_422_before_any_body_is_read(
    app_under_test: Any,
    rider_session: SignedInAccount,
    stop: tuple[SeededTrip, str],
    bucket_sweep: list[str],
    slug_kind: str,
) -> None:
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    slug = trip.rider_slug if slug_kind == "rider" else f"no-such-{secrets.token_urlsafe(8)}"
    call = RawAsgiCall([b"x" * MIB] * 17)

    await call.run(
        app_under_test,
        PHOTOS_PATH.format(slug=slug, stop_id=stop_id),
        {**rider_session.headers, "content-length": str(MAX_REQUEST + 1)},
    )

    assert call.status == HTTPStatus.UNPROCESSABLE_ENTITY, call.body
    envelope = call.envelope()
    assert envelope.error.code == ErrorCode.VALIDATION_ERROR
    assert envelope.error.message == PHOTO_TOO_LARGE_MESSAGE
    assert call.body_messages_received == 0
    assert keys_under(f"{trip.id}/{stop_id}/") == []


async def test_a_chunked_body_over_16_mib_is_cut_off_with_422(
    app_under_test: Any,
    migrated_engine: AsyncEngine,
    rider_session: SignedInAccount,
    stop: tuple[SeededTrip, str],
    bucket_sweep: list[str],
) -> None:
    """No Content-Length: the stream is counted and the read stops once it crosses 16 MiB."""
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())
    body = multipart_body(photo_id, jpeg_of_size(20 * MIB))
    chunks = [body[i : i + MIB] for i in range(0, len(body), MIB)]
    call = RawAsgiCall(chunks)

    await call.run(
        app_under_test,
        PHOTOS_PATH.format(slug=trip.rider_slug, stop_id=stop_id),
        {**rider_session.headers, "transfer-encoding": "chunked"},
    )

    assert call.status == HTTPStatus.UNPROCESSABLE_ENTITY, call.body
    envelope = call.envelope()
    assert envelope.error.code == ErrorCode.VALIDATION_ERROR
    assert envelope.error.message == PHOTO_TOO_LARGE_MESSAGE
    # 16 full chunks sit exactly at the cap; the 17th crosses it and the read stops.
    assert call.body_messages_received <= MAX_REQUEST // MIB + 1 < len(chunks)
    await assert_nothing_stored(migrated_engine, trip, stop_id, photo_id)


# --------------------------------------------------------------------------
# 4. Ordering
# --------------------------------------------------------------------------


class FailingS3:
    def upload_fileobj(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("a replay must not write to storage")


async def test_a_replay_with_non_jpeg_bytes_is_200_and_writes_nothing(
    client: AsyncClient,
    stop: tuple[SeededTrip, str],
    bucket_sweep: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())
    first = await post_photo(client, trip.rider_slug, stop_id, photo_id, minimal_jpeg(1))
    assert first.status_code == HTTPStatus.CREATED, first.text

    monkeypatch.setattr(photos_route, "get_s3_client", lambda: FailingS3())
    replay = await post_photo(client, trip.rider_slug, stop_id, photo_id, PNG)

    assert replay.status_code == HTTPStatus.OK, replay.text
    assert replay.json()["id"] == photo_id
    monkeypatch.undo()
    assert stored_bytes(f"{trip.id}/{stop_id}/{photo_id}") == minimal_jpeg(1)


async def test_a_non_member_with_a_png_is_403_not_422(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    non_member_session: SignedInAccount,
    stop: tuple[SeededTrip, str],
    bucket_sweep: list[str],
) -> None:
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())

    response = await post_photo(
        client, trip.rider_slug, stop_id, photo_id, PNG, headers=non_member_session.headers
    )

    assert response.status_code == HTTPStatus.FORBIDDEN, response.text
    await assert_nothing_stored(migrated_engine, trip, stop_id, photo_id)


async def test_an_unknown_slug_with_a_png_is_404_not_422(
    client: AsyncClient, stop: tuple[SeededTrip, str]
) -> None:
    _, stop_id = stop
    response = await post_photo(
        client, f"no-such-{secrets.token_urlsafe(8)}", stop_id, str(uuid4()), PNG
    )

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text


async def test_an_anonymous_caller_with_a_png_is_401_not_422(
    anon_client: AsyncClient,
    migrated_engine: AsyncEngine,
    stop: tuple[SeededTrip, str],
    bucket_sweep: list[str],
) -> None:
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")
    photo_id = str(uuid4())

    response = await post_photo(anon_client, trip.rider_slug, stop_id, photo_id, PNG)

    assert response.status_code == HTTPStatus.UNAUTHORIZED, response.text
    await assert_nothing_stored(migrated_engine, trip, stop_id, photo_id)


async def test_a_conflicting_id_with_a_png_is_422_not_409(
    client: AsyncClient,
    stop: tuple[SeededTrip, str],
    other_stop: tuple[SeededTrip, str],
    bucket_sweep: list[str],
) -> None:
    trip, stop_id = stop
    other_trip, other_stop_id = other_stop
    bucket_sweep.extend([f"{trip.id}/{stop_id}/", f"{other_trip.id}/{other_stop_id}/"])
    photo_id = str(uuid4())
    first = await post_photo(client, other_trip.rider_slug, other_stop_id, photo_id, minimal_jpeg())
    assert first.status_code == HTTPStatus.CREATED, first.text

    response = await post_photo(client, trip.rider_slug, stop_id, photo_id, PNG)

    assert_validation_error(response, PHOTO_NOT_JPEG_MESSAGE)
    assert keys_under(f"{trip.id}/{stop_id}/") == []
    assert stored_bytes(f"{other_trip.id}/{other_stop_id}/{photo_id}") == minimal_jpeg()


# --------------------------------------------------------------------------
# 5. The 422 body echoes nothing of the upload
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (GIF + b"CANARY-4f1c9e-UPLOADED-BYTES" * 8, PHOTO_NOT_JPEG_MESSAGE),
        (
            SOI + APP0_JFIF + COM[:2] + b"\x00\x40CANARY-4f1c9e-UPLOADED-BYTES",
            PHOTO_NOT_JPEG_MESSAGE,
        ),
        (
            jpeg_with_scan(b"CANARY-4f1c9e-UPLOADED-BYTES" * (MAX_PHOTO // 28 + 1)),
            PHOTO_TOO_LARGE_MESSAGE,
        ),
    ],
    ids=["gif", "truncated-com", "oversize"],
)
async def test_the_422_body_echoes_none_of_the_file(
    client: AsyncClient,
    stop: tuple[SeededTrip, str],
    bucket_sweep: list[str],
    content: bytes,
    message: str,
) -> None:
    trip, stop_id = stop
    bucket_sweep.append(f"{trip.id}/{stop_id}/")

    response = await post_photo(client, trip.rider_slug, stop_id, str(uuid4()), content)

    assert_validation_error(response, message)
    assert "CANARY-4f1c9e" not in response.text
    assert "GIF89a" not in response.text
