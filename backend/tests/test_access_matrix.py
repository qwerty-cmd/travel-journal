"""
The identity x trip access matrix, legacy slug rows (decision-log Entry 29 §15.1).

``docs/api-contract.md``, "Access control: 401, 403 and 404 are three different
answers" carries the required matrix. For legacy slug writes "the rows are the
same with the trip always located: an unknown slug is ``404``, then ``401`` /
``403``". So on every one of the four legacy writes:

| Identity             | Write (public or private trip) |
|----------------------|--------------------------------|
| anonymous            | 401                            |
| signed-in non-member | 403 "You're not a rider on this trip."       |
| pending              | 403 "You're not a rider on this trip."       |
| rider                | 2xx                            |
| revoked              | 403 "You're no longer a rider on this trip." |
| leader               | 2xx                            |

and an unknown slug is ``404`` for every identity, anonymous included, because
the legacy gate checks the slug first. Either slug locates the trip (contract
default 21), so every row is asserted through the rider slug *and* the viewer
slug. Legacy reads stay full for everyone, and ``TripOut.access`` is ``rider``
iff the caller is an active member.

This file is where later tasks add the v2 rows (``t-am-v2-rider-writes``) and
the leader column (``t-am-trip-create``). ``t-am-write-gate-legacy`` owns the
legacy rows below; ``t-am-v2-trip-reads`` added the v2 read columns (section
12): public read ``200`` for everyone, delayed for non-members and full for
members; private read ``200`` full for members and, for everyone else, the
``404`` a nonexistent trip id gets.

Every rejection is also checked against the tables: a ``401`` or ``403`` that
still wrote the row is the failure that matters, and the status alone can't show
it.
"""

from __future__ import annotations

import asyncio
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
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.security import (
    NO_LONGER_A_RIDER_MESSAGE,
    NOT_A_RIDER_MESSAGE,
    SIGN_IN_REQUIRED_MESSAGE,
)
from app.core.sessions import SESSION_COOKIE_NAME, hash_token
from app.data import tables
from app.data.db import get_session
from app.models.common import ErrorCode, ErrorEnvelope
from app.storage.s3_client import BUCKET_NAME, get_s3_client

UNKNOWN_SLUG = "definitely-not-a-real-slug-0000"

# The two 403 messages, spelled as the task's AC spells them. Asserted as
# literals, not only through the imported constants: a test that compares the
# response against the implementation's own constant passes whatever that
# constant says.
AC_NO_LONGER_A_RIDER = "You're no longer a rider on this trip."
AC_NOT_A_RIDER = "You're not a rider on this trip."
# Contract, "Sessions": every 401 carries this, and the cookie's Max-Age is 90 days.
WWW_AUTHENTICATE = 'Cookie realm="bike-trip-journal"'
COOKIE_MAX_AGE = 7776000


def test_the_403_messages_are_the_ones_the_ac_names() -> None:
    """The constants every assertion below leans on say exactly what the AC says."""
    assert NO_LONGER_A_RIDER_MESSAGE == AC_NO_LONGER_A_RIDER
    assert NOT_A_RIDER_MESSAGE == AC_NOT_A_RIDER


WRITES = ("create_stop", "upload_photo", "create_bike", "patch_bike")
SUCCESS = {
    "create_stop": HTTPStatus.CREATED,
    "upload_photo": HTTPStatus.CREATED,
    "create_bike": HTTPStatus.CREATED,
    "patch_bike": HTTPStatus.OK,
}
SLUG_KINDS = ("rider_slug", "viewer_slug")

# Identity -> the expected answer to a legacy write on a located trip.
# (status, code, message); message None = not asserted (a success).
EXPECTED_WRITE = {
    "anonymous": (HTTPStatus.UNAUTHORIZED, ErrorCode.UNAUTHENTICATED, SIGN_IN_REQUIRED_MESSAGE),
    "non_member": (HTTPStatus.FORBIDDEN, ErrorCode.FORBIDDEN, NOT_A_RIDER_MESSAGE),
    "pending": (HTTPStatus.FORBIDDEN, ErrorCode.FORBIDDEN, NOT_A_RIDER_MESSAGE),
    "rider": (None, None, None),
    "revoked": (HTTPStatus.FORBIDDEN, ErrorCode.FORBIDDEN, NO_LONGER_A_RIDER_MESSAGE),
    "leader": (None, None, None),
}
IDENTITIES = tuple(EXPECTED_WRITE)
MEMBERS = {"rider", "leader"}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cast:
    """One account per signed-in identity, all about ``trip`` (seeded_trips[0])."""

    trip: SeededTrip
    other_trip: SeededTrip
    accounts: dict[str, SignedInAccount]
    stop_id: str
    bike_id: str

    def headers(self, identity: str) -> dict[str, str]:
        """The request headers that act as ``identity``: its cookie, or none."""
        return {} if identity == "anonymous" else self.accounts[identity].headers


@pytest.fixture(params=["private", "public"])
def visibility(request: pytest.FixtureRequest) -> str:
    """Both trip visibilities: the legacy write rows are the same for each."""
    return request.param


@pytest.fixture
async def cast(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip], visibility: str
) -> AsyncIterator[Cast]:
    """
    The trip, set to ``visibility``, with one account per identity, a stop and a bike.

    - ``non_member``: signed in, no row on this trip.
    - ``pending``: signed in, a pending join request and no membership.
    - ``rider`` / ``leader``: an active membership with that role.
    - ``revoked``: a revoked ``rider`` row only.

    Every account is also given no rights on the *other* seeded trip, so the
    cross-trip case can be asserted with the same cast.
    """
    trip, other = seeded_trips
    accounts: dict[str, SignedInAccount] = {}
    for identity in ("non_member", "pending", "rider", "revoked", "leader"):
        accounts[identity] = await create_signed_in_account(
            migrated_engine, display_name=f"Matrix {identity}"
        )

    await grant_membership(migrated_engine, trip.id, accounts["rider"].user_id)
    await grant_membership(migrated_engine, trip.id, accounts["leader"].user_id, role="leader")
    await grant_membership(migrated_engine, trip.id, accounts["revoked"].user_id, revoked=True)

    stop_id = f"matrix-stop-{uuid4()}"
    bike_id = f"matrix-bike-{uuid4()}"
    async with migrated_engine.begin() as conn:
        await conn.execute(
            update(tables.trips).where(tables.trips.c.id == trip.id).values(visibility=visibility)
        )
        await conn.execute(
            tables.join_requests.insert().values(
                id=str(uuid4()), trip_id=trip.id, user_id=accounts["pending"].user_id
            )
        )
        await conn.execute(
            tables.stops.insert().values(
                id=stop_id,
                trip_id=trip.id,
                name="Matrix stop",
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
                rider_name="Matrix",
                make="Honda",
                model="XR650L",
                year=2020,
                specs="",
            )
        )

    try:
        yield Cast(
            trip=trip,
            other_trip=other,
            accounts=accounts,
            stop_id=stop_id,
            bike_id=bike_id,
        )
    finally:
        # Stops, photos and bikes go with the trip (ON DELETE CASCADE) in the
        # seeded_trips teardown; accounts must go first (RESTRICT on members).
        await delete_accounts(migrated_engine, [a.user_id for a in accounts.values()])


@pytest.fixture
def swept_bucket(s3_bucket: None, seeded_trips: list[SeededTrip]) -> Iterator[None]:
    """Delete every object the uploads here wrote under the seeded trips' prefixes."""
    yield
    s3 = get_s3_client()
    for trip in seeded_trips:
        listing = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix=f"{trip.id}/")
        for item in listing.get("Contents", []):
            s3.delete_object(Bucket=BUCKET_NAME, Key=item["Key"])


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """The real app with **no** session by default; each request says who it is."""
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


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def send(
    client: AsyncClient,
    write: str,
    slug: str,
    cast: Cast,
    headers: dict[str, str],
    *,
    record_id: str | None = None,
) -> Response:
    """One legacy write, with a fresh client-generated id unless one is given."""
    record_id = record_id or str(uuid4())
    base = f"/api/trips/{slug}"
    if write == "create_stop":
        return await client.post(
            f"{base}/stops",
            json={
                "id": record_id,
                "name": "Matrix write",
                "lat": -14.5,
                "lng": 132.3,
                "locationSource": "gps",
                "arrivedAt": "2026-06-14T15:15:00+09:30",
                "notes": None,
            },
            headers=headers,
        )
    if write == "upload_photo":
        return await client.post(
            f"{base}/stops/{cast.stop_id}/photos",
            data={
                "id": record_id,
                "takenAt": "2026-06-14T10:00:00+09:30",
                "uploadedBy": "Forged Name",
            },
            files={"file": ("photo.jpg", b"fake-jpeg-bytes", "image/jpeg")},
            headers=headers,
        )
    if write == "create_bike":
        return await client.post(
            f"{base}/bikes",
            json={"id": record_id, "riderName": "Kim", "make": "BMW", "model": "R80", "year": 1985},
            headers=headers,
        )
    if write == "patch_bike":
        return await client.patch(
            f"{base}/bikes/{cast.bike_id}", json={"specs": f"patched {record_id}"}, headers=headers
        )
    raise AssertionError(write)


async def snapshot(engine: AsyncEngine, trip_id: str) -> dict[str, Any]:
    """Everything a legacy write can change on a trip, read straight from the tables."""
    async with engine.connect() as conn:
        stops = {
            r.id
            for r in await conn.execute(
                select(tables.stops.c.id).where(tables.stops.c.trip_id == trip_id)
            )
        }
        bikes = {
            (r.id, r.specs)
            for r in await conn.execute(
                select(tables.bikes.c.id, tables.bikes.c.specs).where(
                    tables.bikes.c.trip_id == trip_id
                )
            )
        }
        photos = {
            r.id
            for r in await conn.execute(
                select(tables.photos.c.id)
                .join(tables.stops, tables.stops.c.id == tables.photos.c.stop_id)
                .where(tables.stops.c.trip_id == trip_id)
            )
        }
    return {"stops": stops, "bikes": bikes, "photos": photos}


def envelope(response: Response) -> Any:
    assert response.headers["content-type"] == "application/json", response.text
    return ErrorEnvelope.model_validate(response.json()).error


def session_cookies(response: Response) -> list[dict[str, str]]:
    """
    Every ``Set-Cookie`` for the session cookie, parsed into lower-cased attributes.

    ``value`` is the cookie value; flag attributes (``secure``, ``httponly``)
    map to ``""``.
    """
    parsed = []
    for header in response.headers.get_list("set-cookie"):
        name_value, *attributes = (part.strip() for part in header.split(";"))
        name, _, value = name_value.partition("=")
        if name != SESSION_COOKIE_NAME:
            continue
        cookie = {"value": value}
        for attribute in attributes:
            key, _, attr_value = attribute.partition("=")
            cookie[key.lower()] = attr_value
        parsed.append(cookie)
    return parsed


def assert_cookie_cleared(response: Response) -> None:
    """
    The response clears the session cookie in a form a browser will honour.

    A ``__Host-`` cookie is only accepted, deletion included, with ``Secure``
    and ``Path=/`` and no ``Domain``; a clearing header without them is ignored
    by the browser and the dead cookie stays.
    """
    cookies = session_cookies(response)
    assert len(cookies) == 1, response.headers.get_list("set-cookie")
    cookie = cookies[0]
    assert cookie.get("max-age") == "0", cookie
    assert "secure" in cookie, cookie
    assert cookie.get("path") == "/", cookie
    assert "domain" not in cookie, cookie


def assert_unauthenticated(response: Response) -> None:
    """A contract ``401``: UNAUTHENTICATED envelope plus ``WWW-Authenticate``."""
    assert response.status_code == HTTPStatus.UNAUTHORIZED, response.text
    assert envelope(response).code is ErrorCode.UNAUTHENTICATED
    assert response.headers.get("www-authenticate") == WWW_AUTHENTICATE


async def set_session_times(
    engine: AsyncEngine, account: SignedInAccount, **values: datetime
) -> None:
    """Rewrite ``last_used_at`` / ``absolute_expires_at`` on ``account``'s session row."""
    async with engine.begin() as conn:
        result = await conn.execute(
            update(tables.sessions)
            .where(tables.sessions.c.token_hash == hash_token(account.token))
            .values(**values)
        )
        assert result.rowcount == 1


def bucket_listing() -> dict[str, str]:
    """Every object in the photo bucket, key -> ETag, read straight from S3."""
    s3 = get_s3_client()
    listing: dict[str, str] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET_NAME):
        for item in page.get("Contents", []):
            listing[item["Key"]] = item["ETag"]
    return listing


# --------------------------------------------------------------------------
# 1. The legacy write rows: every identity x every write x either slug
# --------------------------------------------------------------------------


@pytest.mark.parametrize("slug_kind", SLUG_KINDS)
@pytest.mark.parametrize("write", WRITES)
@pytest.mark.parametrize("identity", IDENTITIES)
async def test_legacy_write_row(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    cast: Cast,
    swept_bucket: None,
    identity: str,
    write: str,
    slug_kind: str,
) -> None:
    """
    The matrix cell: status, code and message — and on a refusal, no change in the tables.

    Parametrised over both trip visibilities by the ``cast`` fixture: a trip
    found by slug is never hidden, so private and public answer alike.
    """
    before = await snapshot(migrated_engine, cast.trip.id)
    record_id = str(uuid4())

    response = await send(
        client,
        write,
        getattr(cast.trip, slug_kind),
        cast,
        cast.headers(identity),
        record_id=record_id,
    )

    status, code, message = EXPECTED_WRITE[identity]
    after = await snapshot(migrated_engine, cast.trip.id)
    if identity in MEMBERS:
        # A leader or rider through the viewer slug lands here too (default 21).
        assert response.status_code == SUCCESS[write], response.text
        # The success must be a real write, not a 2xx over an unchanged table.
        if write == "create_stop":
            assert after["stops"] == before["stops"] | {record_id}
        elif write == "upload_photo":
            assert after["photos"] == before["photos"] | {record_id}
        elif write == "create_bike":
            assert {b for b, _ in after["bikes"]} == {b for b, _ in before["bikes"]} | {record_id}
        else:
            assert (cast.bike_id, f"patched {record_id}") in after["bikes"]
        return

    assert response.status_code == status, response.text
    # The whole body is the envelope and nothing else: no trip or member data.
    assert response.json() == {"error": {"code": code.value, "message": message}}
    if identity == "anonymous":
        assert response.headers.get("www-authenticate") == WWW_AUTHENTICATE
        assert session_cookies(response) == []  # no cookie was sent, so none to clear
    else:
        # A 403 is "we know who you are": the valid session must survive it.
        assert session_cookies(response) == []
    assert after == before


@pytest.mark.parametrize("write", WRITES)
@pytest.mark.parametrize("identity", IDENTITIES)
async def test_unknown_slug_is_404_for_every_identity(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    cast: Cast,
    swept_bucket: None,
    identity: str,
    write: str,
) -> None:
    """
    Slug first: an unknown slug is ``404`` before the session is looked at.

    So anonymous gets ``404`` here, not ``401``, and a member gets ``404``, not
    a success on some other trip. The body is byte-identical whoever asks, so
    the ``404`` says nothing about the caller's memberships.
    """
    before = await snapshot(migrated_engine, cast.trip.id)

    response = await send(client, write, UNKNOWN_SLUG, cast, cast.headers(identity))
    anonymous = await send(client, write, UNKNOWN_SLUG, cast, {})

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    assert envelope(response).code is ErrorCode.NOT_FOUND
    assert response.content == anonymous.content
    assert session_cookies(response) == []
    assert await snapshot(migrated_engine, cast.trip.id) == before


# --------------------------------------------------------------------------
# 2. Obligation 3: one rider slug, three sessions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("write", WRITES)
async def test_rider_slug_alone_is_not_a_credential(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    cast: Cast,
    swept_bucket: None,
    write: str,
) -> None:
    """
    The same valid rider slug: no session -> 401, non-member -> 403, active rider -> success.

    The rider slug used to be the whole write authorisation. It now grants
    nothing on its own (Entry 29, obligation 3).
    """
    slug = cast.trip.rider_slug

    anonymous = await send(client, write, slug, cast, {})
    stranger = await send(client, write, slug, cast, cast.headers("non_member"))
    rider = await send(client, write, slug, cast, cast.headers("rider"))

    assert anonymous.status_code == HTTPStatus.UNAUTHORIZED, anonymous.text
    assert stranger.status_code == HTTPStatus.FORBIDDEN, stranger.text
    assert rider.status_code == SUCCESS[write], rider.text


# --------------------------------------------------------------------------
# 3. What a successful write records
# --------------------------------------------------------------------------


@pytest.mark.parametrize("identity", sorted(MEMBERS))
async def test_creates_record_the_writing_account(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    cast: Cast,
    swept_bucket: None,
    identity: str,
) -> None:
    """Stops, photos and bikes store ``created_by`` = the session's account."""
    account = cast.accounts[identity]
    ids = {write: str(uuid4()) for write in ("create_stop", "upload_photo", "create_bike")}
    for write, record_id in ids.items():
        response = await send(
            client, write, cast.trip.viewer_slug, cast, account.headers, record_id=record_id
        )
        assert response.status_code == HTTPStatus.CREATED, response.text

    async with migrated_engine.connect() as conn:
        stop = await conn.scalar(
            select(tables.stops.c.created_by).where(tables.stops.c.id == ids["create_stop"])
        )
        photo = await conn.scalar(
            select(tables.photos.c.created_by).where(tables.photos.c.id == ids["upload_photo"])
        )
        bike = await conn.scalar(
            select(tables.bikes.c.created_by).where(tables.bikes.c.id == ids["create_bike"])
        )

    assert (stop, photo, bike) == (account.user_id,) * 3


async def test_photo_uploaded_by_is_the_account_display_name_not_the_form(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast, swept_bucket: None
) -> None:
    """The form says "Forged Name"; the row and the response say the account's display name."""
    account = cast.accounts["rider"]
    photo_id = str(uuid4())

    response = await send(
        client, "upload_photo", cast.trip.rider_slug, cast, account.headers, record_id=photo_id
    )

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert response.json()["uploadedBy"] == account.display_name
    async with migrated_engine.connect() as conn:
        stored = await conn.scalar(
            select(tables.photos.c.uploaded_by).where(tables.photos.c.id == photo_id)
        )
    assert stored == account.display_name


async def test_photo_upload_without_uploaded_by_is_accepted(
    client: AsyncClient, cast: Cast, swept_bucket: None
) -> None:
    """``uploadedBy`` is optional on the legacy form now."""
    response = await client.post(
        f"/api/trips/{cast.trip.rider_slug}/stops/{cast.stop_id}/photos",
        data={"id": str(uuid4()), "takenAt": "2026-06-14T10:00:00+09:30"},
        files={"file": ("photo.jpg", b"fake-jpeg-bytes", "image/jpeg")},
        headers=cast.headers("rider"),
    )

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert response.json()["uploadedBy"] == cast.accounts["rider"].display_name


# --------------------------------------------------------------------------
# 4. Legacy reads: full for everyone, `access` from membership
# --------------------------------------------------------------------------


@pytest.mark.parametrize("slug_kind", SLUG_KINDS)
@pytest.mark.parametrize("identity", IDENTITIES)
async def test_legacy_trip_read_access_follows_membership(
    client: AsyncClient, cast: Cast, identity: str, slug_kind: str
) -> None:
    """
    ``GET /api/trips/{slug}`` is ``200`` for everyone; ``access`` is ``rider`` iff an active member.

    Which slug was followed plays no part: a member reads ``rider`` through the
    viewer slug, and an anonymous caller reads ``viewer`` through the rider slug.
    """
    response = await client.get(
        f"/api/trips/{getattr(cast.trip, slug_kind)}", headers=cast.headers(identity)
    )

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json()["access"] == ("rider" if identity in MEMBERS else "viewer")


@pytest.mark.parametrize(
    "path",
    ["/api/trips/{slug}/stops", "/api/trips/{slug}/map", "/api/trips/{slug}/stops/{stop}/photos"],
)
@pytest.mark.parametrize("identity", ["anonymous", "non_member", "revoked"])
async def test_legacy_reads_stay_open_to_non_members(
    client: AsyncClient, cast: Cast, identity: str, path: str
) -> None:
    """Legacy GETs stay full and undelayed for either slug until the removal task (default 22)."""
    response = await client.get(
        path.format(slug=cast.trip.viewer_slug, stop=cast.stop_id), headers=cast.headers(identity)
    )

    assert response.status_code == HTTPStatus.OK, response.text


# --------------------------------------------------------------------------
# 5. Adversarial cases around the gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("write", WRITES)
async def test_a_member_of_one_trip_cannot_write_to_another(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast, write: str
) -> None:
    """
    An active rider of trip A, with trip B's slug: ``403``, and trip B untouched.

    The membership lookup must be keyed on the trip the slug located, not on
    "is this user a rider anywhere".
    """
    before = await snapshot(migrated_engine, cast.other_trip.id)

    response = await send(client, write, cast.other_trip.rider_slug, cast, cast.headers("rider"))

    # For the photo and the bike patch, the stop and bike ids are trip A's; the
    # gate answers before either is looked up, so this is still the 403.
    assert response.status_code == HTTPStatus.FORBIDDEN, response.text
    assert envelope(response).message == NOT_A_RIDER_MESSAGE
    assert await snapshot(migrated_engine, cast.other_trip.id) == before


async def test_revocation_takes_effect_on_the_next_request(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast
) -> None:
    """
    No cache: a write succeeds, the membership is revoked, the very next write is ``403``.

    The same session, the same client, back to back.
    """
    account = cast.accounts["rider"]
    first = await send(client, "create_bike", cast.trip.rider_slug, cast, account.headers)
    assert first.status_code == HTTPStatus.CREATED, first.text

    async with migrated_engine.begin() as conn:
        await conn.execute(
            update(tables.trip_members)
            .where(
                tables.trip_members.c.trip_id == cast.trip.id,
                tables.trip_members.c.user_id == account.user_id,
            )
            .values(revoked_at=datetime.now(UTC))
        )

    second = await send(client, "create_bike", cast.trip.rider_slug, cast, account.headers)
    assert second.status_code == HTTPStatus.FORBIDDEN, second.text
    assert envelope(second).message == NO_LONGER_A_RIDER_MESSAGE


async def test_a_revoked_riders_replay_of_a_stored_id_is_refused(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast
) -> None:
    """
    A replay is not an exception to authorisation (contract, "Idempotency: additions").

    The id is stored while the rider is active; after revocation the identical
    request is a ``403``, not the ``200`` replay.
    """
    account = cast.accounts["rider"]
    stop_id = str(uuid4())
    created = await send(
        client, "create_stop", cast.trip.rider_slug, cast, account.headers, record_id=stop_id
    )
    assert created.status_code == HTTPStatus.CREATED, created.text

    async with migrated_engine.begin() as conn:
        await conn.execute(
            update(tables.trip_members)
            .where(tables.trip_members.c.user_id == account.user_id)
            .values(revoked_at=datetime.now(UTC))
        )

    replay = await send(
        client, "create_stop", cast.trip.rider_slug, cast, account.headers, record_id=stop_id
    )
    assert replay.status_code == HTTPStatus.FORBIDDEN, replay.text
    assert envelope(replay).message == NO_LONGER_A_RIDER_MESSAGE


async def test_a_re_admitted_rider_writes_again(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast
) -> None:
    """A revoked row plus a newer active row: the active row wins, the write succeeds."""
    account = cast.accounts["revoked"]
    await grant_membership(migrated_engine, cast.trip.id, account.user_id)

    response = await send(client, "create_bike", cast.trip.viewer_slug, cast, account.headers)

    assert response.status_code == HTTPStatus.CREATED, response.text


@pytest.mark.parametrize("cookie", ["", "garbage", "x" * 43])
async def test_an_invalid_session_cookie_is_401_and_cleared(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast, cookie: str
) -> None:
    """A cookie that resolves to no session is a ``401`` that clears it, and writes nothing."""
    before = await snapshot(migrated_engine, cast.trip.id)

    response = await send(
        client,
        "create_stop",
        cast.trip.rider_slug,
        cast,
        {"Cookie": f"{SESSION_COOKIE_NAME}={cookie}"},
    )

    assert_unauthenticated(response)
    assert_cookie_cleared(response)
    assert await snapshot(migrated_engine, cast.trip.id) == before


async def test_a_disabled_members_session_is_401(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast
) -> None:
    """An active membership does not outlive the account: a disabled rider gets ``401``."""
    account = cast.accounts["rider"]
    async with migrated_engine.begin() as conn:
        await conn.execute(
            update(tables.users)
            .where(tables.users.c.id == account.user_id)
            .values(disabled_at=datetime.now(UTC))
        )

    before = await snapshot(migrated_engine, cast.trip.id)

    response = await send(client, "create_bike", cast.trip.rider_slug, cast, account.headers)

    assert_unauthenticated(response)
    assert_cookie_cleared(response)
    assert await snapshot(migrated_engine, cast.trip.id) == before


async def test_no_403_or_401_body_carries_trip_or_member_data(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast
) -> None:
    """
    The gate's refusals name nothing beyond the contract message.

    No slug, trip id or name, stop or bike id, user id, username or display
    name, in the body or in any header. The bodies are also asserted to be the
    bare envelope, which is the stronger statement; the substring scan guards
    headers and the 401, whose message the contract does not spell.
    """
    async with migrated_engine.connect() as conn:
        usernames = [
            r.username
            for r in await conn.execute(
                select(tables.users.c.username).where(
                    tables.users.c.id.in_([a.user_id for a in cast.accounts.values()])
                )
            )
        ]
    cases = [
        (await send(client, "create_stop", cast.trip.rider_slug, cast, {}), None),
        (
            await send(client, "create_stop", cast.trip.rider_slug, cast, cast.headers("pending")),
            AC_NOT_A_RIDER,
        ),
        (
            await send(
                client, "upload_photo", cast.trip.rider_slug, cast, cast.headers("non_member")
            ),
            AC_NOT_A_RIDER,
        ),
        (
            await send(client, "patch_bike", cast.trip.viewer_slug, cast, cast.headers("revoked")),
            AC_NO_LONGER_A_RIDER,
        ),
        (
            await send(
                client, "create_bike", cast.other_trip.rider_slug, cast, cast.headers("rider")
            ),
            AC_NOT_A_RIDER,
        ),
    ]
    forbidden = [
        cast.trip.rider_slug,
        cast.trip.viewer_slug,
        cast.trip.id,
        cast.trip.name,
        cast.other_trip.rider_slug,
        cast.other_trip.viewer_slug,
        cast.other_trip.id,
        cast.other_trip.name,
        cast.stop_id,
        cast.bike_id,
        *(a.user_id for a in cast.accounts.values()),
        *(a.display_name for a in cast.accounts.values()),
        *usernames,
    ]
    for response, message in cases:
        if message is None:
            assert response.status_code == HTTPStatus.UNAUTHORIZED, response.text
        else:
            assert response.status_code == HTTPStatus.FORBIDDEN, response.text
            assert response.json() == {"error": {"code": "FORBIDDEN", "message": message}}
        exposed = response.text + "\n".join(f"{k}: {v}" for k, v in response.headers.items())
        for value in forbidden:
            assert value not in exposed


# --------------------------------------------------------------------------
# 6. Storage: a refused photo writes nothing to S3 either
# --------------------------------------------------------------------------


async def test_a_revoked_riders_photo_upload_leaves_no_object_and_no_row(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast, swept_bucket: None
) -> None:
    """
    "A replay from a revoked rider ... gets ``403`` with nothing written to the database or S3."

    Two refused uploads after the revocation:
    - a replay of a photo id stored while the rider was active, with
      *different* bytes, so an overwrite of the stored object would change its
      ETag;
    - a fresh id, which must leave no new object anywhere in the bucket.

    The bucket is compared as a whole (key -> ETag) rather than at a guessed
    key, so the assertion doesn't depend on how the key is built.
    """
    account = cast.accounts["rider"]
    stored_id = str(uuid4())
    slug = cast.trip.rider_slug
    url = f"/api/trips/{slug}/stops/{cast.stop_id}/photos"

    empty = bucket_listing()
    created = await send(client, "upload_photo", slug, cast, account.headers, record_id=stored_id)
    assert created.status_code == HTTPStatus.CREATED, created.text
    with_photo = bucket_listing()
    # The listing can see an upload at all, so an unchanged listing below means something.
    assert len(with_photo) > len(empty)

    async with migrated_engine.begin() as conn:
        await conn.execute(
            update(tables.trip_members)
            .where(
                tables.trip_members.c.trip_id == cast.trip.id,
                tables.trip_members.c.user_id == account.user_id,
            )
            .values(revoked_at=datetime.now(UTC))
        )
        stored_row = (
            await conn.execute(select(tables.photos).where(tables.photos.c.id == stored_id))
        ).one()
    before = await snapshot(migrated_engine, cast.trip.id)

    fresh_id = str(uuid4())
    replay = await client.post(
        url,
        data={"id": stored_id, "takenAt": "2026-06-14T11:00:00+09:30"},
        files={"file": ("photo.jpg", b"different-bytes-entirely", "image/jpeg")},
        headers=account.headers,
    )
    fresh = await client.post(
        url,
        data={"id": fresh_id, "takenAt": "2026-06-14T11:00:00+09:30"},
        files={"file": ("photo.jpg", b"fresh-bytes", "image/jpeg")},
        headers=account.headers,
    )

    for response in (replay, fresh):
        assert response.status_code == HTTPStatus.FORBIDDEN, response.text
        assert response.json() == {"error": {"code": "FORBIDDEN", "message": AC_NO_LONGER_A_RIDER}}
    assert bucket_listing() == with_photo
    assert await snapshot(migrated_engine, cast.trip.id) == before
    async with migrated_engine.connect() as conn:
        assert (
            await conn.execute(select(tables.photos).where(tables.photos.c.id == stored_id))
        ).one() == stored_row
        assert (
            await conn.scalar(select(tables.photos.c.id).where(tables.photos.c.id == fresh_id))
            is None
        )


# --------------------------------------------------------------------------
# 7. Session lifetime on the legacy routes
# --------------------------------------------------------------------------


def assert_cookie_refreshed(response: Response, token: str) -> None:
    """
    The session cookie is re-issued: same token, fresh 90-day ``Max-Age``.

    Contract, "Sessions": ``__Host-btj_session=<token>; Path=/; Secure;
    HttpOnly; SameSite=Lax; Max-Age=7776000``.
    """
    cookies = session_cookies(response)
    assert len(cookies) == 1, response.headers.get_list("set-cookie")
    cookie = cookies[0]
    assert cookie["value"] == token
    assert cookie.get("max-age") == str(COOKIE_MAX_AGE), cookie
    assert cookie.get("path") == "/", cookie
    assert "secure" in cookie, cookie
    assert "httponly" in cookie, cookie
    assert cookie.get("samesite", "").lower() == "lax", cookie
    assert "domain" not in cookie, cookie


async def last_used_at(engine: AsyncEngine, account: SignedInAccount) -> datetime:
    async with engine.connect() as conn:
        value = await conn.scalar(
            select(tables.sessions.c.last_used_at).where(
                tables.sessions.c.token_hash == hash_token(account.token)
            )
        )
    assert value is not None
    return value


@pytest.mark.parametrize("write", WRITES)
async def test_a_stale_session_is_refreshed_on_a_legacy_write(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    cast: Cast,
    swept_bucket: None,
    write: str,
) -> None:
    """
    ``last_used_at`` over 24 h old: the write succeeds, the row is bumped, the cookie re-issued.

    Contract, "Sessions" / default 12: ``last_used_at`` is written at most once
    every 24 h, and each such write re-issues the cookie with a fresh Max-Age.
    """
    account = cast.accounts["rider"]
    stale = datetime.now(UTC) - timedelta(hours=25)
    await set_session_times(migrated_engine, account, last_used_at=stale)

    response = await send(client, write, cast.trip.viewer_slug, cast, account.headers)

    assert response.status_code == SUCCESS[write], response.text
    assert_cookie_refreshed(response, account.token)
    assert await last_used_at(migrated_engine, account) > stale + timedelta(hours=24)


@pytest.mark.parametrize("identity", ["rider", "non_member"])
async def test_a_stale_session_is_refreshed_on_a_legacy_get(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast, identity: str
) -> None:
    """
    ``GET /api/trips/{slug}`` resolves the optional session to fill ``access``, so it refreshes too.

    Refreshing is about the session, not the trip: a signed-in non-member who
    only ever reads must not have their session expire underneath them.
    """
    account = cast.accounts[identity]
    stale = datetime.now(UTC) - timedelta(hours=25)
    await set_session_times(migrated_engine, account, last_used_at=stale)

    response = await client.get(f"/api/trips/{cast.trip.rider_slug}", headers=account.headers)

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json()["access"] == ("rider" if identity == "rider" else "viewer")
    assert_cookie_refreshed(response, account.token)
    assert await last_used_at(migrated_engine, account) > stale + timedelta(hours=24)


async def test_a_fresh_session_is_not_reissued_on_every_request(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast
) -> None:
    """At most once every 24 h: a session used an hour ago gets no ``Set-Cookie`` and no bump."""
    account = cast.accounts["rider"]
    recent = datetime.now(UTC) - timedelta(hours=1)
    await set_session_times(migrated_engine, account, last_used_at=recent)

    write = await send(client, "create_bike", cast.trip.rider_slug, cast, account.headers)
    read = await client.get(f"/api/trips/{cast.trip.rider_slug}", headers=account.headers)

    assert write.status_code == HTTPStatus.CREATED, write.text
    assert read.status_code == HTTPStatus.OK, read.text
    assert session_cookies(write) == []
    assert session_cookies(read) == []
    assert await last_used_at(migrated_engine, account) == recent


@pytest.mark.parametrize("write", WRITES)
@pytest.mark.parametrize("expiry", ["idle_over_90_days", "over_365_day_cap"])
async def test_an_expired_session_on_a_legacy_write_is_401_and_cleared(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    cast: Cast,
    swept_bucket: None,
    expiry: str,
    write: str,
) -> None:
    """
    An active rider whose session has expired: ``401``, cookie cleared, nothing written.

    Valid means ``now < last_used_at + 90 days`` **and** ``now <
    absolute_expires_at``. Each clause is broken on its own, with the other
    holding, so a gate that checked only one of them fails one of the two cases.
    """
    account = cast.accounts["rider"]
    now = datetime.now(UTC)
    if expiry == "idle_over_90_days":
        await set_session_times(
            migrated_engine,
            account,
            last_used_at=now - timedelta(days=90, minutes=1),
            absolute_expires_at=now + timedelta(days=100),
        )
    else:
        await set_session_times(
            migrated_engine,
            account,
            # sessions_expiry_check: absolute_expires_at > created_at, so the
            # session is backdated to a sign-in a year and a day ago.
            created_at=now - timedelta(days=366),
            last_used_at=now - timedelta(minutes=5),
            absolute_expires_at=now - timedelta(minutes=1),
        )
    before = await snapshot(migrated_engine, cast.trip.id)

    response = await send(client, write, cast.trip.rider_slug, cast, account.headers)

    assert_unauthenticated(response)
    assert_cookie_cleared(response)
    assert await snapshot(migrated_engine, cast.trip.id) == before


@pytest.mark.parametrize("slug_kind", SLUG_KINDS)
@pytest.mark.parametrize(
    "path",
    [
        "/api/trips/{slug}",
        "/api/trips/{slug}/stops",
        "/api/trips/{slug}/map",
        "/api/trips/{slug}/stops/{stop}/photos",
    ],
)
async def test_head_on_legacy_reads_with_a_member_cookie(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    cast: Cast,
    path: str,
    slug_kind: str,
) -> None:
    """
    ``HEAD`` is a read: ``200`` with no body for a member, and it never runs the write gate.

    The unknown-slug ``HEAD`` is ``404``, so the ``200`` is the trip being found
    and not a blanket answer.
    """
    account = cast.accounts["rider"]
    before = await snapshot(migrated_engine, cast.trip.id)
    url = path.format(slug=getattr(cast.trip, slug_kind), stop=cast.stop_id)

    head = await client.head(url, headers=account.headers)
    get = await client.get(url, headers=account.headers)
    missing = await client.head(
        path.format(slug=UNKNOWN_SLUG, stop=cast.stop_id), headers=account.headers
    )

    assert head.status_code == HTTPStatus.OK, head.text
    assert head.content == b""
    assert head.headers["content-type"] == get.headers["content-type"]
    assert missing.status_code == HTTPStatus.NOT_FOUND
    assert await snapshot(migrated_engine, cast.trip.id) == before


async def test_an_invalid_cookie_on_a_legacy_get_reads_as_anonymous(
    client: AsyncClient, cast: Cast
) -> None:
    """``require_trip_access``: the session is optional and only fills ``viewer``, so no ``401``."""
    response = await client.get(
        f"/api/trips/{cast.trip.rider_slug}", headers={"Cookie": f"{SESSION_COOKIE_NAME}=garbage"}
    )

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json()["access"] == "viewer"


# --------------------------------------------------------------------------
# 8. Which 403 message: the shape of the caller's rows on this trip
# --------------------------------------------------------------------------


async def add_member_row(
    engine: AsyncEngine,
    trip_id: str,
    user_id: str,
    *,
    role: str = "rider",
    revoked_by: str | None = None,
    revoked_ago: timedelta | None = None,
) -> None:
    """One ``trip_members`` row; revoked when ``revoked_ago`` is given."""
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            tables.trip_members.insert().values(
                id=str(uuid4()),
                trip_id=trip_id,
                user_id=user_id,
                role=role,
                joined_at=now - (revoked_ago or timedelta()) - timedelta(days=1),
                revoked_at=now - revoked_ago if revoked_ago is not None else None,
                revoked_by=revoked_by,
            )
        )


async def add_join_request(engine: AsyncEngine, trip_id: str, user_id: str, state: str) -> None:
    """One ``join_requests`` row in ``state`` (decided states carry ``decided_at``)."""
    async with engine.begin() as conn:
        await conn.execute(
            tables.join_requests.insert().values(
                id=str(uuid4()),
                trip_id=trip_id,
                user_id=user_id,
                state=state,
                decided_at=None if state == "pending" else datetime.now(UTC),
            )
        )


# scenario -> the message the AC requires. Revoked = has at least one revoked
# membership row and no active one; anyone else without an active row is "not".
ROW_SHAPES = {
    "self_departed": AC_NO_LONGER_A_RIDER,
    "revoked_by_a_leader": AC_NO_LONGER_A_RIDER,
    "revoked_leader": AC_NO_LONGER_A_RIDER,
    "several_revoked_rows": AC_NO_LONGER_A_RIDER,
    "pending_request_and_a_revoked_row": AC_NO_LONGER_A_RIDER,
    "pending_request_only": AC_NOT_A_RIDER,
    "rejected_request_only": AC_NOT_A_RIDER,
    "blocked_request_only": AC_NOT_A_RIDER,
    "cancelled_request_only": AC_NOT_A_RIDER,
    "revoked_on_the_other_trip_only": AC_NOT_A_RIDER,
}


@pytest.mark.parametrize("write", WRITES)
@pytest.mark.parametrize("shape", list(ROW_SHAPES))
async def test_the_403_message_follows_the_callers_rows(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    cast: Cast,
    swept_bucket: None,
    shape: str,
    write: str,
) -> None:
    """
    "You're no longer a rider" iff the caller has a revoked row and no active one on *this* trip.

    A pending request does not mask a revocation, several revoked rows are
    still "no longer", a self-departure (``revoked_by`` = self) is still a
    revocation, and a revocation on a *different* trip says nothing about this
    one.
    """
    account = await create_signed_in_account(migrated_engine, display_name=f"Shape {shape}")
    trip_id = cast.trip.id
    leader_id = cast.accounts["leader"].user_id
    try:
        if shape == "self_departed":
            await add_member_row(
                migrated_engine,
                trip_id,
                account.user_id,
                revoked_by=account.user_id,
                revoked_ago=timedelta(hours=1),
            )
        elif shape == "revoked_by_a_leader":
            await add_member_row(
                migrated_engine,
                trip_id,
                account.user_id,
                revoked_by=leader_id,
                revoked_ago=timedelta(hours=1),
            )
        elif shape == "revoked_leader":
            await add_member_row(
                migrated_engine,
                trip_id,
                account.user_id,
                role="leader",
                revoked_by=account.user_id,
                revoked_ago=timedelta(hours=1),
            )
        elif shape == "several_revoked_rows":
            for days, role in ((30, "rider"), (20, "leader"), (10, "rider")):
                await add_member_row(
                    migrated_engine,
                    trip_id,
                    account.user_id,
                    role=role,
                    revoked_by=leader_id,
                    revoked_ago=timedelta(days=days),
                )
        elif shape == "pending_request_and_a_revoked_row":
            await add_member_row(
                migrated_engine,
                trip_id,
                account.user_id,
                revoked_by=leader_id,
                revoked_ago=timedelta(days=10),
            )
            await add_join_request(migrated_engine, trip_id, account.user_id, "pending")
        elif shape == "revoked_on_the_other_trip_only":
            await add_member_row(
                migrated_engine,
                cast.other_trip.id,
                account.user_id,
                revoked_ago=timedelta(hours=1),
            )
        else:
            state = shape.removesuffix("_request_only")
            await add_join_request(migrated_engine, trip_id, account.user_id, state)

        before = await snapshot(migrated_engine, trip_id)
        response = await send(client, write, cast.trip.rider_slug, cast, account.headers)

        assert response.status_code == HTTPStatus.FORBIDDEN, response.text
        assert response.json() == {"error": {"code": "FORBIDDEN", "message": ROW_SHAPES[shape]}}
        assert await snapshot(migrated_engine, trip_id) == before
    finally:
        await delete_accounts(migrated_engine, [account.user_id])


# --------------------------------------------------------------------------
# 9. Gate order against an invalid body
# --------------------------------------------------------------------------

# Every body below parses, then fails field validation. An *unparseable* body is
# a different case: FastAPI parses before it solves any dependency, so it is a
# 422 before the gate. Decision-log Entry 23 closed that as won't-fix; the test
# after these checks the property that ruling rests on.


async def send_invalid(
    client: AsyncClient, write: str, slug: str, cast: Cast, headers: dict[str, str]
) -> Response:
    """One legacy write whose body parses but can never validate."""
    base = f"/api/trips/{slug}"
    if write == "upload_photo":
        # Multipart, but no id, no takenAt and no file part.
        return await client.post(
            f"{base}/stops/{cast.stop_id}/photos", data={"note": "no id"}, headers=headers
        )
    body = {"year": "not a number", "lat": 999, "specs": None}
    if write == "patch_bike":
        return await client.patch(f"{base}/bikes/{cast.bike_id}", json=body, headers=headers)
    path = "stops" if write == "create_stop" else "bikes"
    return await client.post(f"{base}/{path}", json=body, headers=headers)


@pytest.mark.parametrize("write", WRITES)
async def test_an_invalid_body_from_a_rider_is_422(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast, write: str
) -> None:
    """The control: each body below really is a ``422`` once the gate lets it through."""
    before = await snapshot(migrated_engine, cast.trip.id)

    response = await send_invalid(client, write, cast.trip.rider_slug, cast, cast.headers("rider"))

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
    assert envelope(response).code is ErrorCode.VALIDATION_ERROR
    assert await snapshot(migrated_engine, cast.trip.id) == before


@pytest.mark.parametrize("write", WRITES)
async def test_anonymous_with_an_invalid_body_is_401_not_422(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast, write: str
) -> None:
    """
    Slug -> session -> membership, then the body: an anonymous caller is ``401``.

    A ``422`` here would be never-retry in the queue, where the ``401`` pauses
    it until sign-in.
    """
    before = await snapshot(migrated_engine, cast.trip.id)

    response = await send_invalid(client, write, cast.trip.rider_slug, cast, {})

    assert_unauthenticated(response)
    assert await snapshot(migrated_engine, cast.trip.id) == before


@pytest.mark.parametrize("identity", ["non_member", "revoked"])
@pytest.mark.parametrize("write", WRITES)
async def test_a_non_member_with_an_invalid_body_is_403_not_422(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast, write: str, identity: str
) -> None:
    """The membership gate also answers before the body: ``403``, and the right message."""
    response = await send_invalid(
        client, write, cast.trip.viewer_slug, cast, cast.headers(identity)
    )

    expected = AC_NO_LONGER_A_RIDER if identity == "revoked" else AC_NOT_A_RIDER
    assert response.status_code == HTTPStatus.FORBIDDEN, response.text
    assert response.json() == {"error": {"code": "FORBIDDEN", "message": expected}}


@pytest.mark.parametrize("identity", ["anonymous", "non_member", "rider"])
@pytest.mark.parametrize("write", WRITES)
async def test_an_unknown_slug_with_an_invalid_body_is_404_first(
    client: AsyncClient, cast: Cast, write: str, identity: str
) -> None:
    """The slug is checked before the session and before the body: ``404`` for everyone."""
    response = await send_invalid(client, write, UNKNOWN_SLUG, cast, cast.headers(identity))

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    assert envelope(response).code is ErrorCode.NOT_FOUND


@pytest.mark.parametrize("write", ["create_stop", "create_bike", "patch_bike"])
async def test_an_unparseable_json_body_answers_the_same_to_everyone(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast, write: str
) -> None:
    """
    Entry 23's won't-fix still holds under accounts: the pre-gate ``422`` is no oracle.

    The ruling rests on the ``422`` being byte-identical whatever the slug. With
    sessions it must also be identical whoever asks: anonymous, a non-member or
    a rider, on a real slug or an unknown one. Otherwise it would reveal either
    the slug or the caller's membership before any gate has run.
    """
    before = await snapshot(migrated_engine, cast.trip.id)
    method, suffix = {
        "create_stop": ("POST", "stops"),
        "create_bike": ("POST", "bikes"),
        "patch_bike": ("PATCH", f"bikes/{cast.bike_id}"),
    }[write]

    responses = [
        await client.request(
            method,
            f"/api/trips/{slug}/{suffix}",
            content=b'{"id": ',
            headers={**cast.headers(identity), "Content-Type": "application/json"},
        )
        for slug in (cast.trip.rider_slug, cast.trip.viewer_slug, UNKNOWN_SLUG)
        for identity in ("anonymous", "non_member", "rider")
    ]

    assert responses[0].status_code == HTTPStatus.UNPROCESSABLE_ENTITY, responses[0].text
    assert {r.status_code for r in responses} == {HTTPStatus.UNPROCESSABLE_ENTITY}
    assert {r.content for r in responses} == {responses[0].content}
    assert await snapshot(migrated_engine, cast.trip.id) == before


# --------------------------------------------------------------------------
# 10. Revocation racing a write
# --------------------------------------------------------------------------


async def revoke(conn: Any, trip_id: str, user_id: str, *, lock_trip: bool) -> None:
    """
    Revoke ``user_id`` on ``trip_id`` inside ``conn``'s open transaction.

    ``lock_trip`` takes ``SELECT ... FOR UPDATE`` on the trip row first, as
    contract default 26 says a membership change does.
    """
    if lock_trip:
        await conn.execute(
            select(tables.trips.c.id).where(tables.trips.c.id == trip_id).with_for_update()
        )
    await conn.execute(
        update(tables.trip_members)
        .where(
            tables.trip_members.c.trip_id == trip_id,
            tables.trip_members.c.user_id == user_id,
            tables.trip_members.c.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


@pytest.mark.parametrize("lock_trip", [False, True], ids=["plain_revoke", "revoke_locking_trip"])
async def test_a_write_in_flight_while_a_revoke_commits_is_all_or_nothing(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast, lock_trip: bool
) -> None:
    """
    A write racing an uncommitted revocation, then one after it commits.

    What is deterministic, and asserted:
    - the in-flight write is either ``201`` with its row stored or ``403`` with
      nothing stored. Never a ``2xx`` without the row, a refusal that wrote it,
      or a ``500``;
    - once the revocation has committed, the next request is ``403`` "no
      longer" and writes nothing ("a revocation takes effect on the next
      request").

    Which of ``201`` / ``403`` the in-flight write gets is not asserted: the
    contract binds only the *next* request, so either is correct. Observed when
    this was written: ``201`` in both variants. With a plain revoke, the gate
    reads the still-committed active row and the write finishes before the
    commit. With the trip row locked, the gate passes, the INSERT then waits on
    the trips-row key-share lock its foreign key needs, and the write lands
    just *after* the revocation commits.
    """
    account = cast.accounts["rider"]
    in_flight_id = str(uuid4())
    next_id = str(uuid4())

    async with migrated_engine.connect() as revoker:
        transaction = await revoker.begin()
        try:
            await revoke(revoker, cast.trip.id, account.user_id, lock_trip=lock_trip)
            task = asyncio.ensure_future(
                send(
                    client,
                    "create_bike",
                    cast.trip.rider_slug,
                    cast,
                    account.headers,
                    record_id=in_flight_id,
                )
            )
            # Let the request get as far as it can while the revoke is open.
            await asyncio.wait({task}, timeout=2)
            await transaction.commit()
        except BaseException:
            await transaction.rollback()
            raise
    in_flight = await asyncio.wait_for(task, timeout=15)
    after = await send(
        client, "create_bike", cast.trip.rider_slug, cast, account.headers, record_id=next_id
    )

    async with migrated_engine.connect() as conn:
        stored = set(
            (
                await conn.scalars(
                    select(tables.bikes.c.id).where(tables.bikes.c.id.in_([in_flight_id, next_id]))
                )
            ).all()
        )

    assert in_flight.status_code in {HTTPStatus.CREATED, HTTPStatus.FORBIDDEN}, in_flight.text
    assert (in_flight_id in stored) == (in_flight.status_code == HTTPStatus.CREATED)
    assert after.status_code == HTTPStatus.FORBIDDEN, after.text
    assert envelope(after).message == AC_NO_LONGER_A_RIDER
    assert next_id not in stored


# --------------------------------------------------------------------------
# 11. Private trips on the legacy surface
# --------------------------------------------------------------------------


@pytest.mark.parametrize("visibility", ["private"])
@pytest.mark.parametrize("identity", ["non_member", "pending", "revoked"])
@pytest.mark.parametrize("write", WRITES)
async def test_a_private_trip_located_by_slug_is_403_not_404(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    cast: Cast,
    identity: str,
    write: str,
) -> None:
    """
    The 404-not-403 rule is v2's; a legacy slug always locates, so a private trip answers ``403``.

    Contract, "Access control", under the matrix: "For legacy slug writes, the
    rows are the same with the trip always located: an unknown slug is ``404``,
    then ``401`` / ``403``". And under "Where the session check sits": "A trip
    found by slug is never hidden, so this order cannot reveal anything." The
    slug holder already knows the trip exists (the legacy GETs answer them in
    full, default 22), so a ``404`` would hide nothing and would make the
    queue fail the item as "trip gone" instead of "no longer a rider".

    So the private-trip write is **not** byte-identical to the unknown-slug
    ``404``: it is a different status with a different body.
    """
    before = await snapshot(migrated_engine, cast.trip.id)

    response = await send(client, write, cast.trip.rider_slug, cast, cast.headers(identity))
    unknown = await send(client, write, UNKNOWN_SLUG, cast, cast.headers(identity))

    expected = AC_NO_LONGER_A_RIDER if identity == "revoked" else AC_NOT_A_RIDER
    assert response.status_code == HTTPStatus.FORBIDDEN, response.text
    assert response.json() == {"error": {"code": "FORBIDDEN", "message": expected}}
    assert unknown.status_code == HTTPStatus.NOT_FOUND
    assert response.content != unknown.content
    assert await snapshot(migrated_engine, cast.trip.id) == before


# --------------------------------------------------------------------------
# 12. The v2 read columns: "Public read" and "Private read" (t-am-v2-trip-reads)
# --------------------------------------------------------------------------

V2_READS = ("", "/bikes", "/stops", "/stops/{stop}/photos", "/map")

# Contract matrix: the read columns. rider / leader: 200 full on both;
# everyone else: 200 delayed on public, 404 on private.
EXPECTED_ROLE = {
    "anonymous": "anonymous",
    "non_member": "none",
    "pending": "pending",
    "rider": "rider",
    "revoked": "none",
    "leader": "leader",
}


@pytest.mark.parametrize("read", V2_READS)
@pytest.mark.parametrize("identity", IDENTITIES)
async def test_v2_read_row(
    client: AsyncClient, cast: Cast, visibility: str, identity: str, read: str
) -> None:
    """
    The matrix cell for every v2 read, on both visibilities.

    A refusal is compared with the same request for a random trip id: the same
    status and the same bytes. (The full header comparison, HEAD included, is
    obligation 2's suite, ``test_v2_public_reads.py``.)
    """
    suffix = read.format(stop=cast.stop_id)
    response = await client.get(
        f"/api/v2/trips/{cast.trip.id}{suffix}", headers=cast.headers(identity)
    )

    if identity in MEMBERS or visibility == "public":
        assert response.status_code == HTTPStatus.OK, response.text
        return

    nonexistent = await client.get(
        f"/api/v2/trips/{uuid4()}{suffix}", headers=cast.headers(identity)
    )
    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    assert envelope(response).code is ErrorCode.NOT_FOUND
    assert response.content == nonexistent.content
    assert session_cookies(response) == []


@pytest.mark.parametrize("identity", IDENTITIES)
async def test_v2_public_read_is_delayed_for_non_members_only(
    client: AsyncClient, migrated_engine: AsyncEngine, cast: Cast, visibility: str, identity: str
) -> None:
    """
    "200, delayed" vs "200, full": a stop an hour old shows to members only.

    The cast's own stop (months old) is visible to everyone who can read the
    trip. The trip keeps the default 24 h delay.
    """
    recent_id = f"matrix-recent-{uuid4()}"
    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.stops.insert().values(
                id=recent_id,
                trip_id=cast.trip.id,
                name="Matrix recent stop",
                lat=-14.47,
                lng=132.27,
                location_source="gps",
                arrived_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )

    response = await client.get(
        f"/api/v2/trips/{cast.trip.id}/stops", headers=cast.headers(identity)
    )

    if identity not in MEMBERS and visibility == "private":
        assert response.status_code == HTTPStatus.NOT_FOUND, response.text
        return
    assert response.status_code == HTTPStatus.OK, response.text
    ids = {stop["id"] for stop in response.json()}
    expected = {cast.stop_id, recent_id} if identity in MEMBERS else {cast.stop_id}
    assert ids == expected


@pytest.mark.parametrize("identity", IDENTITIES)
async def test_v2_and_legacy_trip_report_the_viewer_role(
    client: AsyncClient, cast: Cast, visibility: str, identity: str
) -> None:
    """
    ``viewer.role`` for every identity, and ``access`` derived from it, on both surfaces.

    The legacy GET answers every identity (a slug always locates); the v2 GET
    answers wherever the read column says ``200``.
    """
    legacy = await client.get(f"/api/trips/{cast.trip.viewer_slug}", headers=cast.headers(identity))
    responses = [legacy]
    if identity in MEMBERS or visibility == "public":
        responses.append(
            await client.get(f"/api/v2/trips/{cast.trip.id}", headers=cast.headers(identity))
        )

    for response in responses:
        assert response.status_code == HTTPStatus.OK, response.text
        body = response.json()
        assert body["viewer"] == {"role": EXPECTED_ROLE[identity]}
        assert body["access"] == ("rider" if identity in MEMBERS else "viewer")
        assert body["visibility"] == visibility


@pytest.mark.parametrize("visibility", ["private"])
@pytest.mark.parametrize("read", V2_READS)
@pytest.mark.parametrize("identity", ["non_member", "pending", "revoked"])
async def test_a_private_trip_costs_the_same_statements_as_a_nonexistent_id(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    cast: Cast,
    identity: str,
    read: str,
) -> None:
    """
    No timing oracle: the private-trip 404 runs as many SQL statements as a nonexistent id's.

    Before the fix the membership lookup ran only for a trip that exists, so a
    signed-in caller's private-trip 404 took one more round trip than a
    nonexistent id's -- measurable, and exactly what the byte-identical body is
    meant to hide. Counted with a ``before_cursor_execute`` listener on the
    engine the app is using.
    """
    statements: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    async def run(trip_id: str) -> tuple[int, Response]:
        statements.clear()
        response = await client.get(
            f"/api/v2/trips/{trip_id}{read.format(stop=cast.stop_id)}",
            headers=cast.headers(identity),
        )
        return len(statements), response

    event.listen(migrated_engine.sync_engine, "before_cursor_execute", count)
    try:
        private_count, private = await run(cast.trip.id)
        missing_count, missing = await run(str(uuid4()))
    finally:
        event.remove(migrated_engine.sync_engine, "before_cursor_execute", count)

    assert private.status_code == missing.status_code == HTTPStatus.NOT_FOUND, private.text
    assert private_count > 0, "the listener saw nothing -- this check would be vacuous"
    assert private_count == missing_count
