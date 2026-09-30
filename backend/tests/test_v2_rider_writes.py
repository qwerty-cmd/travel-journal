"""
The v2 rider writes, ``/api/v2/trips/{tripId}/...`` (task ``t-am-v2-rider-writes``).

Written from ``docs/api-contract.md`` and the task's acceptance criteria, before
the implementation was read:

- "Where the session check sits": v2 is **session first**. Anonymous is ``401``
  for every trip id, existing or not, byte-identical.
- The identity x trip matrix, write columns. A non-member (or pending caller)
  gets ``403`` on a public trip and, on a private trip, the ``404`` a
  nonexistent id gets, byte-identical and costing the same statements. Revoked
  gets ``403`` on either visibility. Rider and leader succeed. Every refusal
  writes nothing, **neither to Postgres nor to S3**.
- "Idempotency" plus "Idempotency: additions": an unseen id is ``201``, the same
  parent is a ``200`` replay of the **stored** record (even if the body now
  differs), and a different parent is ``409``. The gate runs before the replay
  lookup.
- The v2 photo form is ``id``, ``takenAt`` and ``file``. The JPEG section applies
  identically: stripping, non-JPEG ``422``, the 15 MiB file cap, and the 16 MiB
  ``Content-Length`` refused before the body is read.
- Added AC: a NUL byte in any client id on any write path, v2 or legacy, is a
  clean 4xx and never a ``500``.
- "Rate limits": every v2 write spends from the per-user ``writes`` bucket, and
  so does every legacy write.
- "CSRF": a cross-site unsafe request is ``403``.
- "Sessions": a stale session (``last_used_at`` over 24 h) is re-issued on a
  successful write.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import pytest
from conftest import (
    SeededTrip,
    SignedInAccount,
    create_signed_in_account,
    delete_accounts,
    grant_membership,
    make_async_client,
)
from httpx import AsyncClient, Response
from jpeg_fixtures import minimal_jpeg
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from test_access_matrix import session_cookies, set_session_times, snapshot
from test_photo_jpeg_http import (
    AFTER_APP0,
    APP0_JFIF,
    EXIF_GPS,
    MAX_PHOTO,
    MAX_REQUEST,
    MIB,
    PNG,
    SOI,
    RawAsgiCall,
    assert_no_metadata_markers,
    jpeg_of_size,
)
from test_photo_upload_storage import keys_under, photo_rows, stored_bytes

from app.core import ratelimit
from app.data import tables
from app.data.db import get_session
from app.storage.s3_client import BUCKET_NAME, get_s3_client

# Contract literals, not the implementation's constants.
NOT_A_RIDER = "You're not a rider on this trip."
NO_LONGER_A_RIDER = "You're no longer a rider on this trip."
CSRF_MESSAGE = "This request came from another site and was blocked."
WWW_AUTHENTICATE = 'Cookie realm="bike-trip-journal"'
COOKIE_MAX_AGE = "7776000"
WRITES_RETRY_AFTER = 6  # 600/hour -> one token every 6 s
WRITES_LIMIT = 600

WRITES = ("create_stop", "upload_photo", "create_bike", "patch_bike")
CREATES = ("create_stop", "upload_photo", "create_bike")
SUCCESS = {
    "create_stop": HTTPStatus.CREATED,
    "upload_photo": HTTPStatus.CREATED,
    "create_bike": HTTPStatus.CREATED,
    "patch_bike": HTTPStatus.OK,
}
IDENTITIES = ("anonymous", "non_member", "pending", "rider", "revoked", "leader")

# The contract matrix, write columns: None = the write's success status.
EXPECTED: dict[tuple[str, str], tuple[int, str, str | None] | None] = {
    ("anonymous", "public"): (401, "UNAUTHENTICATED", None),
    ("anonymous", "private"): (401, "UNAUTHENTICATED", None),
    ("non_member", "public"): (403, "FORBIDDEN", NOT_A_RIDER),
    ("non_member", "private"): (404, "NOT_FOUND", None),
    ("pending", "public"): (403, "FORBIDDEN", NOT_A_RIDER),
    ("pending", "private"): (404, "NOT_FOUND", None),
    ("rider", "public"): None,
    ("rider", "private"): None,
    ("revoked", "public"): (403, "FORBIDDEN", NO_LONGER_A_RIDER),
    ("revoked", "private"): (403, "FORBIDDEN", NO_LONGER_A_RIDER),
    ("leader", "public"): None,
    ("leader", "private"): None,
}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Place:
    """One trip with the stops and bike the writes aim at."""

    trip: SeededTrip
    stop_id: str
    second_stop_id: str
    bike_id: str


@dataclass(frozen=True, slots=True)
class World:
    """A public trip and a private trip; every identity stands in the same relation to both."""

    public: Place
    private: Place
    accounts: dict[str, SignedInAccount]

    def place(self, visibility: str) -> Place:
        return self.public if visibility == "public" else self.private

    def headers(self, identity: str) -> dict[str, str]:
        return {} if identity == "anonymous" else self.accounts[identity].headers


@pytest.fixture
async def world(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[World]:
    accounts = {
        identity: await create_signed_in_account(migrated_engine, display_name=f"V2 {identity}")
        for identity in ("non_member", "pending", "rider", "revoked", "leader")
    }
    places = []
    for trip, visibility in zip(seeded_trips, ("public", "private"), strict=True):
        await grant_membership(migrated_engine, trip.id, accounts["rider"].user_id)
        await grant_membership(migrated_engine, trip.id, accounts["leader"].user_id, role="leader")
        await grant_membership(migrated_engine, trip.id, accounts["revoked"].user_id, revoked=True)
        stop_id, second_stop_id = f"v2w-stop-{uuid4()}", f"v2w-stop-{uuid4()}"
        bike_id = f"v2w-bike-{uuid4()}"
        async with migrated_engine.begin() as conn:
            await conn.execute(
                update(tables.trips)
                .where(tables.trips.c.id == trip.id)
                .values(visibility=visibility)
            )
            await conn.execute(
                tables.join_requests.insert().values(
                    id=str(uuid4()), trip_id=trip.id, user_id=accounts["pending"].user_id
                )
            )
            for sid in (stop_id, second_stop_id):
                await conn.execute(
                    tables.stops.insert().values(
                        id=sid,
                        trip_id=trip.id,
                        name="V2 write stop",
                        lat=-14.46,
                        lng=132.26,
                        location_source="gps",
                        arrived_at=datetime(2026, 6, 3, 9, 0, tzinfo=UTC),
                    )
                )
            await conn.execute(
                tables.bikes.insert().values(
                    id=bike_id,
                    trip_id=trip.id,
                    rider_name="V2",
                    make="Honda",
                    model="XR650L",
                    year=2020,
                    specs="",
                )
            )
        places.append(Place(trip, stop_id, second_stop_id, bike_id))
    try:
        yield World(public=places[0], private=places[1], accounts=accounts)
    finally:
        await delete_accounts(migrated_engine, [a.user_id for a in accounts.values()])


@pytest.fixture
def swept(s3_bucket: None, seeded_trips: list[SeededTrip]) -> Iterator[None]:
    yield
    s3 = get_s3_client()
    for trip in seeded_trips:
        for key in keys_under(f"{trip.id}/"):
            s3.delete_object(Bucket=BUCKET_NAME, Key=key)


@pytest.fixture
def application(migrated_engine: AsyncEngine) -> Iterator[Any]:
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
async def client(application: Any) -> AsyncIterator[AsyncClient]:
    """No session by default; a 500 comes back as a response, never re-raised."""
    async with make_async_client(application, raise_app_exceptions=False) as http_client:
        yield http_client


class FrozenClock:
    def __init__(self) -> None:
        self.now = 50_000.0

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def stop_body(record_id: str, name: str = "V2 stop") -> dict[str, Any]:
    return {
        "id": record_id,
        "name": name,
        "lat": -14.5,
        "lng": 132.3,
        "locationSource": "gps",
        "arrivedAt": "2026-06-14T15:15:00+09:30",
    }


def bike_body(record_id: str, make: str = "BMW") -> dict[str, Any]:
    return {"id": record_id, "riderName": "Kim", "make": make, "model": "R80", "year": 1985}


async def v2(
    client: AsyncClient,
    write: str,
    trip_id: str,
    place: Place,
    headers: dict[str, str],
    *,
    record_id: str | None = None,
    stop_id: str | None = None,
    variant: int = 1,
    content: bytes | None = None,
    extra_form: dict[str, str] | None = None,
) -> Response:
    """One v2 write. ``trip_id`` is separate from ``place`` so a random id can be aimed at."""
    record_id = record_id or str(uuid4())
    base = f"/api/v2/trips/{trip_id}"
    if write == "create_stop":
        return await client.post(
            f"{base}/stops", json=stop_body(record_id, f"V2 stop {variant}"), headers=headers
        )
    if write == "upload_photo":
        return await client.post(
            f"{base}/stops/{stop_id or place.stop_id}/photos",
            data={"id": record_id, "takenAt": "2026-06-14T10:00:00+09:30", **(extra_form or {})},
            files={
                "file": (
                    "p.jpg",
                    minimal_jpeg(variant) if content is None else content,
                    "image/jpeg",
                )
            },
            headers=headers,
        )
    if write == "create_bike":
        return await client.post(
            f"{base}/bikes", json=bike_body(record_id, f"Make {variant}"), headers=headers
        )
    if write == "patch_bike":
        return await client.patch(
            f"{base}/bikes/{place.bike_id}", json={"specs": f"p {record_id}"}, headers=headers
        )
    raise AssertionError(write)


def s3_state(trip: SeededTrip) -> dict[str, bytes]:
    """Every object under the trip's prefix, key -> bytes."""
    return {key: stored_bytes(key) for key in keys_under(f"{trip.id}/")}


async def state(engine: AsyncEngine, trip: SeededTrip) -> tuple[Any, Any]:
    return await snapshot(engine, trip.id), s3_state(trip)


def as_instants(body: dict[str, Any]) -> dict[str, Any]:
    """The body with its timestamps parsed: the contract promises an instant, not a spelling."""
    return {
        k: datetime.fromisoformat(v) if k in {"arrivedAt", "takenAt"} else v
        for k, v in body.items()
    }


def without_date(response: Response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k != "date"}


def assert_error(response: Response, status: int, code: str, message: str | None = None) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code, response.text
    if message is not None:
        assert error["message"] == message


# --------------------------------------------------------------------------
# 1. Obligation 1: the v2 write rows
# --------------------------------------------------------------------------


@pytest.mark.parametrize("visibility", ["public", "private"])
@pytest.mark.parametrize("write", WRITES)
@pytest.mark.parametrize("identity", IDENTITIES)
async def test_v2_write_matrix_cell_and_nothing_written_on_refusal(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    world: World,
    swept: None,
    identity: str,
    write: str,
    visibility: str,
) -> None:
    place = world.place(visibility)
    before = await state(migrated_engine, place.trip)
    record_id = str(uuid4())

    response = await v2(
        client, write, place.trip.id, place, world.headers(identity), record_id=record_id
    )

    expected = EXPECTED[(identity, visibility)]
    if expected is None:
        assert response.status_code == SUCCESS[write], response.text
        if write == "upload_photo":
            assert keys_under(f"{place.trip.id}/{place.stop_id}/{record_id}")
        return
    status, code, message = expected
    assert_error(response, status, code, message)
    assert await state(migrated_engine, place.trip) == before, "a refusal wrote something"


@pytest.mark.parametrize("write", WRITES)
async def test_anonymous_401_is_byte_identical_for_public_private_and_random_ids(
    client: AsyncClient, migrated_engine: AsyncEngine, world: World, swept: None, write: str
) -> None:
    random_place = Place(world.public.trip, f"s-{uuid4()}", f"s-{uuid4()}", f"b-{uuid4()}")
    responses = [
        await v2(client, write, world.public.trip.id, world.public, {}),
        await v2(client, write, world.private.trip.id, world.private, {}),
        await v2(client, write, str(uuid4()), random_place, {}),
    ]
    for response in responses:
        assert_error(response, 401, "UNAUTHENTICATED")
        assert response.headers.get("www-authenticate") == WWW_AUTHENTICATE
    assert len({r.content for r in responses}) == 1
    assert without_date(responses[0]) == without_date(responses[1]) == without_date(responses[2])


async def test_anonymous_with_an_invalid_body_is_still_401(
    client: AsyncClient, world: World
) -> None:
    """Session first means before body validation too: no 422 for a caller we don't know."""
    for trip_id in (world.private.trip.id, str(uuid4())):
        response = await client.post(f"/api/v2/trips/{trip_id}/stops", json={})
        assert_error(response, 401, "UNAUTHENTICATED")


@pytest.mark.parametrize("write", WRITES)
@pytest.mark.parametrize("identity", ["non_member", "pending"])
async def test_private_trip_404_is_byte_identical_to_a_random_id_and_costs_the_same(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    world: World,
    swept: None,
    identity: str,
    write: str,
) -> None:
    statements: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    async def run(trip_id: str) -> tuple[int, Response]:
        statements.clear()
        response = await v2(client, write, trip_id, world.private, world.headers(identity))
        return len(statements), response

    event.listen(migrated_engine.sync_engine, "before_cursor_execute", count)
    try:
        private_count, private = await run(world.private.trip.id)
        missing_count, missing = await run(str(uuid4()))
    finally:
        event.remove(migrated_engine.sync_engine, "before_cursor_execute", count)

    assert_error(private, 404, "NOT_FOUND")
    assert private.content == missing.content
    assert without_date(private) == without_date(missing)
    assert private_count > 0
    assert private_count == missing_count


async def test_a_photo_to_a_stop_on_another_trip_is_404_and_writes_nothing(
    client: AsyncClient, migrated_engine: AsyncEngine, world: World, swept: None
) -> None:
    """The rider is a member of both trips: the stop must still belong to the trip in the path."""
    before = await state(migrated_engine, world.private.trip)
    response = await v2(
        client,
        "upload_photo",
        world.public.trip.id,
        world.public,
        world.headers("rider"),
        stop_id=world.private.stop_id,
    )
    assert_error(response, 404, "NOT_FOUND")
    assert await state(migrated_engine, world.private.trip) == before


# --------------------------------------------------------------------------
# 2. Idempotency three-way branch
# --------------------------------------------------------------------------


@pytest.mark.parametrize("write", CREATES)
async def test_three_way_branch(
    client: AsyncClient, migrated_engine: AsyncEngine, world: World, swept: None, write: str
) -> None:
    rider = world.headers("rider")
    place = world.public
    record_id = str(uuid4())

    first = await v2(client, write, place.trip.id, place, rider, record_id=record_id)
    assert first.status_code == 201, first.text

    same = await v2(client, write, place.trip.id, place, rider, record_id=record_id)
    assert same.status_code == 200, same.text
    assert as_instants(same.json()) == as_instants(first.json())

    # Different content, same parent: still a replay of the STORED record.
    differs = await v2(client, write, place.trip.id, place, rider, record_id=record_id, variant=2)
    assert differs.status_code == 200, differs.text
    assert as_instants(differs.json()) == as_instants(first.json())
    if write == "upload_photo":
        key = f"{place.trip.id}/{place.stop_id}/{record_id}"
        assert stored_bytes(key) == minimal_jpeg(1), "a replay overwrote the stored object"

    # Different parent: another trip for stops/bikes, another stop for photos.
    if write == "upload_photo":
        other_s3 = s3_state(place.trip)
        conflict = await v2(
            client,
            write,
            place.trip.id,
            place,
            rider,
            record_id=record_id,
            stop_id=place.second_stop_id,
        )
        assert s3_state(place.trip) == other_s3
        assert len(await photo_rows(migrated_engine, record_id)) == 1
    else:
        before = await snapshot(migrated_engine, world.private.trip.id)
        conflict = await v2(
            client, write, world.private.trip.id, world.private, rider, record_id=record_id
        )
        assert await snapshot(migrated_engine, world.private.trip.id) == before
    assert_error(conflict, 409, "CONFLICT")
    assert place.trip.id not in conflict.text

    fresh = await v2(client, write, place.trip.id, place, rider)
    assert fresh.status_code == 201, fresh.text


@pytest.mark.parametrize("write", CREATES)
async def test_a_legacy_create_replayed_on_v2_is_a_200_replay(
    client: AsyncClient, world: World, swept: None, write: str
) -> None:
    """Same parent, whichever surface: the id created via the slug is a replay via the id."""
    rider = world.headers("rider")
    place = world.public
    record_id = str(uuid4())
    base = f"/api/trips/{place.trip.rider_slug}"
    if write == "create_stop":
        legacy = await client.post(f"{base}/stops", json=stop_body(record_id), headers=rider)
    elif write == "create_bike":
        legacy = await client.post(f"{base}/bikes", json=bike_body(record_id), headers=rider)
    else:
        legacy = await client.post(
            f"{base}/stops/{place.stop_id}/photos",
            data={"id": record_id, "takenAt": "2026-06-14T10:00:00+09:30"},
            files={"file": ("p.jpg", minimal_jpeg(), "image/jpeg")},
            headers=rider,
        )
    assert legacy.status_code == 201, legacy.text

    replay = await v2(client, write, place.trip.id, place, rider, record_id=record_id, variant=3)
    assert replay.status_code == 200, replay.text
    assert as_instants(replay.json()) == as_instants(legacy.json())


@pytest.mark.parametrize("visibility", ["public", "private"])
@pytest.mark.parametrize("write", CREATES)
async def test_a_revoked_riders_replay_is_403_not_the_stored_body(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    world: World,
    swept: None,
    write: str,
    visibility: str,
) -> None:
    rider = world.accounts["rider"]
    place = world.place(visibility)
    record_id = str(uuid4())
    first = await v2(client, write, place.trip.id, place, rider.headers, record_id=record_id)
    assert first.status_code == 201, first.text

    async with migrated_engine.begin() as conn:
        await conn.execute(
            update(tables.trip_members)
            .where(
                tables.trip_members.c.trip_id == place.trip.id,
                tables.trip_members.c.user_id == rider.user_id,
            )
            .values(revoked_at=datetime.now(UTC))
        )
    before = await state(migrated_engine, place.trip)

    # Different bytes for the photo, so an overwrite in S3 would show.
    replay = await v2(
        client, write, place.trip.id, place, rider.headers, record_id=record_id, variant=7
    )

    assert_error(replay, 403, "FORBIDDEN", NO_LONGER_A_RIDER)
    assert record_id not in replay.text
    assert await state(migrated_engine, place.trip) == before


# --------------------------------------------------------------------------
# 3. The v2 photo form and the JPEG rules
# --------------------------------------------------------------------------


async def test_an_extra_uploaded_by_field_is_ignored_and_the_account_name_stored(
    client: AsyncClient, migrated_engine: AsyncEngine, world: World, swept: None
) -> None:
    """The form is id, takenAt, file; the name comes from the account ("Rider writes (v2)")."""
    place = world.public
    record_id = str(uuid4())
    response = await v2(
        client,
        "upload_photo",
        place.trip.id,
        place,
        world.headers("rider"),
        record_id=record_id,
        extra_form={"uploadedBy": "Forged Name"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["uploadedBy"] == "V2 rider"
    rows = await photo_rows(migrated_engine, record_id)
    assert [r["uploaded_by"] for r in rows] == ["V2 rider"]
    assert [r["created_by"] for r in rows] == [world.accounts["rider"].user_id]


async def test_v2_upload_strips_exif(client: AsyncClient, world: World, swept: None) -> None:
    place = world.public
    record_id = str(uuid4())
    body = SOI + APP0_JFIF + EXIF_GPS + AFTER_APP0
    response = await v2(
        client,
        "upload_photo",
        place.trip.id,
        place,
        world.headers("rider"),
        record_id=record_id,
        content=body,
    )
    assert response.status_code == 201, response.text
    stored = stored_bytes(f"{place.trip.id}/{place.stop_id}/{record_id}")
    assert stored == minimal_jpeg()
    assert_no_metadata_markers(stored)
    assert b"GPS" not in stored


@pytest.mark.parametrize(
    "content",
    [PNG, b"", minimal_jpeg()[:40], jpeg_of_size(MAX_PHOTO + 1)],
    ids=["png", "empty", "truncated", "15MiB+1"],
)
async def test_v2_non_jpeg_or_oversize_file_is_422_and_stores_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    world: World,
    swept: None,
    content: bytes,
) -> None:
    place = world.public
    record_id = str(uuid4())
    response = await v2(
        client,
        "upload_photo",
        place.trip.id,
        place,
        world.headers("rider"),
        record_id=record_id,
        content=content,
    )
    assert_error(response, 422, "VALIDATION_ERROR")
    assert keys_under(f"{place.trip.id}/") == []
    assert await photo_rows(migrated_engine, record_id) == []


@pytest.mark.parametrize("target", ["member-trip", "random-id"])
async def test_v2_content_length_over_16_mib_is_422_before_any_body_is_read(
    application: Any, world: World, swept: None, target: str
) -> None:
    place = world.public
    trip_id = place.trip.id if target == "member-trip" else str(uuid4())
    call = RawAsgiCall([b"x" * MIB] * 17)

    await call.run(
        application,
        f"/api/v2/trips/{trip_id}/stops/{place.stop_id}/photos",
        {**world.headers("rider"), "content-length": str(MAX_REQUEST + 1)},
    )

    assert call.status == 422, call.body
    assert call.envelope().error.code.value == "VALIDATION_ERROR"
    assert call.body_messages_received == 0
    assert keys_under(f"{place.trip.id}/") == []


# --------------------------------------------------------------------------
# 4. NUL ids on every write path, v2 and legacy
# --------------------------------------------------------------------------

NUL = "bad\x00id"
NUL_PATH = "bad%00id"


def _nul_requests(place: Place) -> dict[str, tuple[str, str, dict[str, Any]]]:
    photo_form = {"id": NUL, "takenAt": "2026-06-14T10:00:00+09:30"}
    photo_file = {"file": ("p.jpg", minimal_jpeg(), "image/jpeg")}
    requests: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for surface, base in (
        ("v2", f"/api/v2/trips/{place.trip.id}"),
        ("legacy", f"/api/trips/{place.trip.rider_slug}"),
    ):
        requests[f"{surface}-stop-body-id"] = ("POST", f"{base}/stops", {"json": stop_body(NUL)})
        requests[f"{surface}-bike-body-id"] = ("POST", f"{base}/bikes", {"json": bike_body(NUL)})
        requests[f"{surface}-photo-form-id"] = (
            "POST",
            f"{base}/stops/{place.stop_id}/photos",
            {"data": photo_form, "files": photo_file},
        )
        requests[f"{surface}-patch-bike-path-id"] = (
            "PATCH",
            f"{base}/bikes/{NUL_PATH}",
            {"json": {"specs": "x"}},
        )
    requests["v2-photo-stop-path-id"] = (
        "POST",
        f"/api/v2/trips/{place.trip.id}/stops/{NUL_PATH}/photos",
        {"data": {**photo_form, "id": str(uuid4())}, "files": photo_file},
    )
    requests["v2-trip-path-id"] = (
        "POST",
        f"/api/v2/trips/{NUL_PATH}/stops",
        {"json": stop_body(str(uuid4()))},
    )
    return requests


NUL_CASES = [
    f"{surface}-{path}"
    for surface in ("v2", "legacy")
    for path in ("stop-body-id", "bike-body-id", "photo-form-id", "patch-bike-path-id")
] + ["v2-photo-stop-path-id", "v2-trip-path-id"]


@pytest.mark.parametrize("case", NUL_CASES)
async def test_a_nul_id_is_a_clean_4xx_never_a_500(
    client: AsyncClient, migrated_engine: AsyncEngine, world: World, swept: None, case: str
) -> None:
    place = world.public
    method, path, kwargs = _nul_requests(place)[case]
    before = await state(migrated_engine, place.trip)

    response = await client.request(method, path, headers=world.headers("rider"), **kwargs)

    assert 400 <= response.status_code < 500, (response.status_code, response.text)
    assert response.json()["error"]["code"] in {"VALIDATION_ERROR", "NOT_FOUND"}
    assert await state(migrated_engine, place.trip) == before


# --------------------------------------------------------------------------
# 5. Rate limit, CSRF, stale session
# --------------------------------------------------------------------------


async def test_v2_writes_spend_the_per_user_writes_bucket_shared_with_legacy(
    client: AsyncClient, migrated_engine: AsyncEngine, world: World, swept: None
) -> None:
    clock = FrozenClock()
    ratelimit.registry.clock = clock
    rider = world.headers("rider")
    place = world.public

    # Drain on v2 alone: a mix of real writes and gate-refused ones (random trip id).
    for n in range(WRITES_LIMIT):
        if n % 100 == 0:
            ok = await v2(client, "create_stop", place.trip.id, place, rider)
            assert ok.status_code == 201, ok.text
        else:
            refused = await client.post(f"/api/v2/trips/{uuid4()}/stops", json={}, headers=rider)
            assert refused.status_code == 404, refused.text
    before = await state(migrated_engine, place.trip)

    for write in WRITES:
        limited = await v2(client, write, place.trip.id, place, rider)
        assert_error(limited, 429, "RATE_LIMITED")
        assert limited.headers["retry-after"] == str(WRITES_RETRY_AFTER)
    legacy = await client.post(
        f"/api/trips/{place.trip.rider_slug}/stops", json=stop_body(str(uuid4())), headers=rider
    )
    assert_error(legacy, 429, "RATE_LIMITED")
    assert await state(migrated_engine, place.trip) == before

    # Per user: another member still writes.
    other = await v2(client, "create_stop", place.trip.id, place, world.headers("leader"))
    assert other.status_code == 201, other.text

    clock.now += WRITES_RETRY_AFTER
    refilled = await v2(client, "create_bike", place.trip.id, place, rider)
    assert refilled.status_code == 201, refilled.text


@pytest.mark.parametrize(
    "cross_site",
    [{"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example"}],
    ids=["sec-fetch-site", "origin"],
)
@pytest.mark.parametrize("write", WRITES)
async def test_a_cross_site_v2_write_is_403_and_writes_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    world: World,
    swept: None,
    write: str,
    cross_site: dict[str, str],
) -> None:
    place = world.private
    before = await state(migrated_engine, place.trip)
    response = await v2(
        client, write, place.trip.id, place, {**world.headers("rider"), **cross_site}
    )
    assert_error(response, 403, "FORBIDDEN", CSRF_MESSAGE)
    assert await state(migrated_engine, place.trip) == before


@pytest.mark.parametrize("write", WRITES)
async def test_a_stale_member_session_is_refreshed_on_a_successful_v2_write(
    client: AsyncClient, migrated_engine: AsyncEngine, world: World, swept: None, write: str
) -> None:
    rider = world.accounts["rider"]
    place = world.private
    stale = datetime.now(UTC) - timedelta(hours=25)
    await set_session_times(migrated_engine, rider, last_used_at=stale)

    response = await v2(client, write, place.trip.id, place, rider.headers)

    assert response.status_code == SUCCESS[write], response.text
    cookies = session_cookies(response)
    assert len(cookies) == 1, response.headers.get_list("set-cookie")
    assert cookies[0]["value"] == rider.token
    assert cookies[0].get("max-age") == COOKIE_MAX_AGE
    assert "secure" in cookies[0] and "httponly" in cookies[0]
    async with migrated_engine.connect() as conn:
        used = (
            await conn.execute(
                select(tables.sessions.c.last_used_at).where(
                    tables.sessions.c.user_id == rider.user_id
                )
            )
        ).scalar_one()
    assert used > stale + timedelta(hours=24)
