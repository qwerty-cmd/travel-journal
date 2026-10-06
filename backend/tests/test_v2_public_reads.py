"""
The v2 trip-scoped reads: private-means-invisible, the public delay, ``viewer.role`` (t-am-v2-trip-reads).

Written from ``docs/api-contract.md``, "Access: public trips, members and leaders":

- **Obligation 2.** "A private trip answers a non-member with a ``404`` that is
  **byte-identical** to the one for a trip id that doesn't exist. Same body,
  same message constant, same headers." Asserted for GET and HEAD of every v2
  trip-scoped read, for every caller who is not an active member, against the
  same request (same method, same cookie) on a random UUID. Only ``date`` is
  dropped from the comparison.
- **Obligation 11.** "Public delay": a non-member sees a stop only when
  ``arrivedAt <= now() - public_delay_hours``; photos follow their stop; the map
  builds pins and trail from visible stops only, and draws the trail only for
  2+; ``lastPublicStopAt`` is the same for every caller; members see everything;
  a hidden stop is the same ``404`` as a missing one; bikes are not delayed.
- ``TripOut.viewer.role`` for every role, and ``access`` derived from it
  ("``rider`` when ``viewer.role`` is ``rider`` or ``leader``, ``viewer``
  otherwise") on the v2 GET and the legacy GET.
- The ``public-read`` limiter (120/min per IP) on a v2 read.

``test_access_matrix.py`` section 12 carries the matrix's read cells (status
and body only) and ``test_no_slug_in_response_bodies.py`` section 4 the identity
leak check; neither is repeated here.
"""

from __future__ import annotations

import itertools
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import pytest
from conftest import (
    SignedInAccount,
    create_signed_in_account,
    delete_accounts,
    grant_membership,
    make_async_client,
)
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.main
from app.core import ratelimit
from app.core.sessions import SESSION_COOKIE_NAME, hash_token
from app.data import tables
from app.data.db import get_session

# The five v2 trip-scoped reads. "{stop}" is filled with a real stop of the trip.
READS = ("", "/bikes", "/stops", "/stops/{stop}/photos", "/map")
READ_IDS = ("trip", "bikes", "stops", "photos", "map")
METHODS = ("GET", "HEAD")

# Contract, "Sessions": the cookie's Max-Age.
COOKIE_MAX_AGE = "7776000"


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """
    The real app on the test database, no session by default.

    ``raise_app_exceptions=False``: a crash must show up as the ``500`` a real
    caller would get, so it can be compared (and fail) like any other answer.
    """
    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application = app.main.app
    application.dependency_overrides[get_session] = session_override
    try:
        async with make_async_client(application, raise_app_exceptions=False) as http_client:
            yield http_client
    finally:
        application.dependency_overrides.pop(get_session, None)


async def send(
    client: AsyncClient, method: str, path: str, headers: dict[str, str] | None = None
) -> Response:
    """One request whose only cookie is the one in ``headers``: the jar is emptied around it."""
    client.cookies.clear()
    response = await client.request(method, path, headers=headers or {})
    client.cookies.clear()
    return response


def wire(response: Response) -> tuple[int, list[tuple[str, str]], bytes]:
    """Everything a caller can observe of a response, except ``date``."""
    headers = [(k.lower(), v) for k, v in response.headers.multi_items() if k.lower() != "date"]
    return response.status_code, headers, response.content


def session_cookies(response: Response) -> list[str]:
    return [
        h
        for h in response.headers.get_list("set-cookie")
        if h.split("=", 1)[0].strip() == SESSION_COOKIE_NAME
    ]


def cookie_header(token: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE_NAME}={token}"}


def at(hours: float) -> datetime:
    """A time ``hours`` from now (negative = past), whole seconds, UTC."""
    return (datetime.now(UTC) + timedelta(hours=hours)).replace(microsecond=0)


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class World:
    """Trips, stops and accounts this module made; ``engine`` removes them."""

    engine: AsyncEngine
    trip_ids: list[str]
    user_ids: list[str]

    async def trip(
        self, *, visibility: str, delay: int = 24, slugs: bool = False
    ) -> tuple[str, str | None]:
        """A new trip; returns ``(id, viewer_slug)``."""
        trip_id = str(uuid4())
        viewer_slug = secrets.token_urlsafe(16) if slugs else None
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.trips.insert().values(
                    id=trip_id,
                    name="Public reads trip",
                    start_date=date(2026, 6, 1),
                    visibility=visibility,
                    public_delay_hours=delay,
                    rider_slug=secrets.token_urlsafe(16) if slugs else None,
                    viewer_slug=viewer_slug,
                )
            )
        self.trip_ids.append(trip_id)
        return trip_id, viewer_slug

    async def stop(self, trip_id: str, arrived_at: datetime, lat: float, lng: float) -> str:
        """A stop with one photo on it; returns the stop id."""
        stop_id = f"reads-stop-{uuid4()}"
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.stops.insert().values(
                    id=stop_id,
                    trip_id=trip_id,
                    name=f"Stop at {arrived_at.isoformat()}",
                    lat=lat,
                    lng=lng,
                    location_source="gps",
                    arrived_at=arrived_at,
                )
            )
            await conn.execute(
                tables.photos.insert().values(
                    id=f"photo-of-{stop_id}",
                    stop_id=stop_id,
                    object_key=f"{trip_id}/photo-of-{stop_id}.jpg",
                    uploaded_by="Reads Rider",
                    taken_at=arrived_at,
                )
            )
        return stop_id

    async def bike(self, trip_id: str) -> str:
        bike_id = f"reads-bike-{uuid4()}"
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.bikes.insert().values(
                    id=bike_id,
                    trip_id=trip_id,
                    rider_name="Reads Rider",
                    make="Honda",
                    model="XR650L",
                    year=2020,
                    specs="",
                )
            )
        return bike_id

    async def account(self, name: str = "Reads Caller") -> SignedInAccount:
        account = await create_signed_in_account(self.engine, display_name=name)
        self.user_ids.append(account.user_id)
        return account

    async def join_request(self, trip_id: str, user_id: str, state: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.join_requests.insert().values(
                    id=str(uuid4()),
                    trip_id=trip_id,
                    user_id=user_id,
                    state=state,
                    decided_at=None if state == "pending" else datetime.now(UTC),
                )
            )

    async def set_last_used(self, account: SignedInAccount, value: datetime) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(tables.sessions)
                .where(tables.sessions.c.token_hash == hash_token(account.token))
                .values(last_used_at=value)
            )

    async def last_used(self, account: SignedInAccount) -> datetime:
        async with self.engine.connect() as conn:
            value = await conn.scalar(
                select(tables.sessions.c.last_used_at).where(
                    tables.sessions.c.token_hash == hash_token(account.token)
                )
            )
        assert value is not None
        return value


@pytest.fixture
async def world(migrated_engine: AsyncEngine) -> AsyncIterator[World]:
    made = World(engine=migrated_engine, trip_ids=[], user_ids=[])
    try:
        yield made
    finally:
        # Accounts first: trip_members / join_requests are RESTRICT on the user.
        if made.user_ids:
            await delete_accounts(migrated_engine, made.user_ids)
        if made.trip_ids:
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    tables.trips.delete().where(tables.trips.c.id.in_(made.trip_ids))
                )


@dataclass(frozen=True)
class PrivateTrip:
    """A private trip with content, a member of each role, and each non-member identity."""

    trip_id: str
    stop_id: str
    headers: dict[str, dict[str, str]]
    accounts: dict[str, SignedInAccount]


@pytest.fixture
async def private_trip(world: World) -> PrivateTrip:
    trip_id, _ = await world.trip(visibility="private", delay=0)
    stop_id = await world.stop(trip_id, at(-48), -14.46, 132.26)
    await world.bike(trip_id)

    accounts = {
        name: await world.account(f"Private {name}")
        for name in ("non_member", "pending", "revoked", "rider", "leader")
    }
    await world.join_request(trip_id, accounts["pending"].user_id, "pending")
    await grant_membership(world.engine, trip_id, accounts["revoked"].user_id, revoked=True)
    await grant_membership(world.engine, trip_id, accounts["rider"].user_id)
    await grant_membership(world.engine, trip_id, accounts["leader"].user_id, role="leader")

    headers: dict[str, dict[str, str]] = {"anonymous": {}}
    headers.update({name: account.headers for name, account in accounts.items()})
    return PrivateTrip(trip_id=trip_id, stop_id=stop_id, headers=headers, accounts=accounts)


def path(trip_id: str, read: str, stop_id: str) -> str:
    return f"/api/v2/trips/{trip_id}{read.format(stop=stop_id)}"


# --------------------------------------------------------------------------
# 1. Obligation 2: a private trip is byte-identical to a nonexistent one
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("read", READS, ids=READ_IDS)
@pytest.mark.parametrize("identity", ["anonymous", "non_member", "pending", "revoked"])
async def test_a_private_trip_is_indistinguishable_from_a_missing_one(
    client: AsyncClient, private_trip: PrivateTrip, identity: str, read: str, method: str
) -> None:
    headers = private_trip.headers[identity]
    hidden = await send(
        client, method, path(private_trip.trip_id, read, private_trip.stop_id), headers
    )
    missing = await send(client, method, path(str(uuid4()), read, private_trip.stop_id), headers)

    assert hidden.status_code == HTTPStatus.NOT_FOUND, hidden.text
    if method == "GET":
        assert hidden.json()["error"]["code"] == "NOT_FOUND"
    assert wire(hidden) == wire(missing)


@pytest.mark.parametrize("read", READS, ids=READ_IDS)
async def test_the_members_of_that_private_trip_do_read_it(
    client: AsyncClient, private_trip: PrivateTrip, read: str
) -> None:
    """The other half of obligation 2: the 404s above are about the caller, not the trip."""
    for identity in ("rider", "leader"):
        for method in METHODS:
            response = await send(
                client,
                method,
                path(private_trip.trip_id, read, private_trip.stop_id),
                private_trip.headers[identity],
            )
            assert response.status_code == HTTPStatus.OK, (identity, method, response.text)


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("read", READS, ids=READ_IDS)
async def test_a_garbage_cookie_is_indistinguishable_too(
    client: AsyncClient, private_trip: PrivateTrip, read: str, method: str
) -> None:
    headers = cookie_header(secrets.token_urlsafe(32))
    hidden = await send(
        client, method, path(private_trip.trip_id, read, private_trip.stop_id), headers
    )
    missing = await send(client, method, path(str(uuid4()), read, private_trip.stop_id), headers)

    assert hidden.status_code == HTTPStatus.NOT_FOUND, hidden.text
    assert wire(hidden) == wire(missing)


@pytest.mark.parametrize("read", READS, ids=READ_IDS)
async def test_a_members_expired_session_reads_as_anonymous(
    client: AsyncClient, world: World, private_trip: PrivateTrip, read: str
) -> None:
    """
    A rider whose session idled out is no one: the reader gate is "never 401".

    So the private trip is the missing-trip 404, identical to a random id with the
    same dead cookie.
    """
    rider = private_trip.accounts["rider"]
    await world.set_last_used(rider, datetime.now(UTC) - timedelta(days=91))

    hidden = await send(
        client, "GET", path(private_trip.trip_id, read, private_trip.stop_id), rider.headers
    )
    missing = await send(
        client, "GET", path(str(uuid4()), read, private_trip.stop_id), rider.headers
    )

    assert hidden.status_code == HTTPStatus.NOT_FOUND, hidden.text
    assert wire(hidden) == wire(missing)


@pytest.mark.parametrize("read", READS, ids=READ_IDS)
@pytest.mark.parametrize("identity", ["non_member", "pending", "revoked"])
async def test_a_stale_non_member_session_leaks_nothing_now_or_on_the_next_request(
    client: AsyncClient, world: World, private_trip: PrivateTrip, identity: str, read: str
) -> None:
    """
    ``last_used_at`` over 24 h old, on the private trip vs a random id.

    The session is valid either way. Whatever the server does about the daily
    refresh (bump, re-issue, neither), it must do the same for both: no
    ``Set-Cookie`` on the 404, the row bumped in both cases or in neither, and
    so the *next* request answers identically too.
    """
    account = private_trip.accounts[identity]
    stale = datetime.now(UTC) - timedelta(hours=25)
    follow_up = path(str(uuid4()), read, private_trip.stop_id)

    async def probe(trip_id: str) -> tuple[Any, bool, Any]:
        await world.set_last_used(account, stale)
        first = await send(
            client, "GET", path(trip_id, read, private_trip.stop_id), account.headers
        )
        bumped = await world.last_used(account) != stale
        second = await send(client, "GET", follow_up, account.headers)
        assert first.status_code == HTTPStatus.NOT_FOUND, first.text
        assert session_cookies(first) == []
        return wire(first), bumped, wire(second)

    assert await probe(private_trip.trip_id) == await probe(str(uuid4()))


@pytest.mark.parametrize("read", READS, ids=READ_IDS)
async def test_a_stale_member_session_is_refreshed_on_its_200(
    client: AsyncClient, world: World, private_trip: PrivateTrip, read: str
) -> None:
    rider = private_trip.accounts["rider"]
    stale = datetime.now(UTC) - timedelta(hours=25)
    await world.set_last_used(rider, stale)

    response = await send(
        client, "GET", path(private_trip.trip_id, read, private_trip.stop_id), rider.headers
    )

    assert response.status_code == HTTPStatus.OK, response.text
    cookies = session_cookies(response)
    assert len(cookies) == 1, cookies
    attributes = [part.strip().lower() for part in cookies[0].split(";")]
    assert cookies[0].split(";")[0] == f"{SESSION_COOKIE_NAME}={rider.token}"
    assert f"max-age={COOKIE_MAX_AGE}" in attributes
    assert {"secure", "httponly", "path=/", "samesite=lax"} <= set(attributes)
    assert await world.last_used(rider) > stale + timedelta(hours=24)


# Ids that name no trip, in shapes a uuid-minded handler might trip over. None may
# answer anything but the missing-trip 404.
MALFORMED_IDS = {
    "not_a_uuid": "not-a-uuid",
    "long": "a" * 1024,
    "nul": "%00",
    "nul_inside": f"{uuid4()}%00",
    "non_ascii": "%C3%A9",
    "invalid_utf8": "%FF",
}


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("read", READS, ids=READ_IDS)
@pytest.mark.parametrize("malformed", list(MALFORMED_IDS.values()), ids=list(MALFORMED_IDS))
async def test_a_malformed_trip_id_is_the_same_404(
    client: AsyncClient, private_trip: PrivateTrip, malformed: str, read: str, method: str
) -> None:
    bad = await send(client, method, path(malformed, read, private_trip.stop_id))
    missing = await send(client, method, path(str(uuid4()), read, private_trip.stop_id))

    assert bad.status_code == HTTPStatus.NOT_FOUND, bad.text
    assert wire(bad) == wire(missing)


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("malformed", list(MALFORMED_IDS.values()), ids=list(MALFORMED_IDS))
async def test_a_malformed_stop_id_on_a_readable_trip_is_the_unknown_stop_404(
    client: AsyncClient, world: World, malformed: str, method: str
) -> None:
    trip_id, _ = await world.trip(visibility="public")
    await world.stop(trip_id, at(-48), -14.46, 132.26)

    bad = await send(client, method, f"/api/v2/trips/{trip_id}/stops/{malformed}/photos")
    missing = await send(client, method, f"/api/v2/trips/{trip_id}/stops/{uuid4()}/photos")

    assert bad.status_code == HTTPStatus.NOT_FOUND, bad.text
    assert wire(bad) == wire(missing)


# --------------------------------------------------------------------------
# 2. Obligation 11: the public delay
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DelayedTrip:
    trip_id: str
    old: str  # now - 30 h
    recent: str  # now - 1 h
    future: str  # now + 1 h
    times: dict[str, datetime]
    bike_id: str
    headers: dict[str, dict[str, str]]


@pytest.fixture
async def delayed_trip(world: World) -> DelayedTrip:
    trip_id, _ = await world.trip(visibility="public", delay=24)
    times = {"old": at(-30), "recent": at(-1), "future": at(1)}
    old = await world.stop(trip_id, times["old"], -14.46, 132.26)
    recent = await world.stop(trip_id, times["recent"], -15.10, 133.10)
    future = await world.stop(trip_id, times["future"], -16.25, 133.37)
    # A bike added just now: bikes are not delayed.
    bike_id = await world.bike(trip_id)

    accounts = {
        name: await world.account(f"Delay {name}")
        for name in ("non_member", "pending", "revoked", "rider", "leader")
    }
    await world.join_request(trip_id, accounts["pending"].user_id, "pending")
    await grant_membership(world.engine, trip_id, accounts["revoked"].user_id, revoked=True)
    await grant_membership(world.engine, trip_id, accounts["rider"].user_id)
    await grant_membership(world.engine, trip_id, accounts["leader"].user_id, role="leader")
    headers: dict[str, dict[str, str]] = {"anonymous": {}}
    headers.update({name: account.headers for name, account in accounts.items()})
    return DelayedTrip(
        trip_id=trip_id,
        old=old,
        recent=recent,
        future=future,
        times=times,
        bike_id=bike_id,
        headers=headers,
    )


NON_MEMBERS = ("anonymous", "non_member", "pending", "revoked")
MEMBERS = ("rider", "leader")


def points_and_lines(feature_collection: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    features = feature_collection["features"]
    points = [f for f in features if f["geometry"]["type"] == "Point"]
    lines = [f for f in features if f["geometry"]["type"] == "LineString"]
    assert len(points) + len(lines) == len(features)
    return points, lines


@pytest.mark.parametrize("identity", NON_MEMBERS)
async def test_a_non_member_sees_only_stops_older_than_the_delay(
    client: AsyncClient, delayed_trip: DelayedTrip, identity: str
) -> None:
    trip, headers = delayed_trip, delayed_trip.headers[identity]
    base = f"/api/v2/trips/{trip.trip_id}"

    stops = await send(client, "GET", f"{base}/stops", headers)
    assert stops.status_code == HTTPStatus.OK, stops.text
    assert [s["id"] for s in stops.json()] == [trip.old]

    map_ = await send(client, "GET", f"{base}/map", headers)
    assert map_.status_code == HTTPStatus.OK, map_.text
    points, lines = points_and_lines(map_.json())
    assert [p["id"] for p in points] == [trip.old]
    assert points[0]["geometry"]["coordinates"] == [132.26, -14.46]  # [lng, lat]
    assert lines == []

    visible = await send(client, "GET", f"{base}/stops/{trip.old}/photos", headers)
    assert visible.status_code == HTTPStatus.OK, visible.text
    assert [p["id"] for p in visible.json()] == [f"photo-of-{trip.old}"]

    bikes = await send(client, "GET", f"{base}/bikes", headers)
    assert bikes.status_code == HTTPStatus.OK, bikes.text
    assert [b["id"] for b in bikes.json()] == [trip.bike_id]


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("hidden", ["recent", "future"])
@pytest.mark.parametrize("identity", NON_MEMBERS)
async def test_a_stop_hidden_by_the_delay_is_the_unknown_stop_404(
    client: AsyncClient, delayed_trip: DelayedTrip, identity: str, hidden: str, method: str
) -> None:
    trip, headers = delayed_trip, delayed_trip.headers[identity]
    hidden_id = getattr(trip, hidden)
    base = f"/api/v2/trips/{trip.trip_id}"

    hidden_response = await send(client, method, f"{base}/stops/{hidden_id}/photos", headers)
    unknown = await send(client, method, f"{base}/stops/reads-stop-{uuid4()}/photos", headers)

    assert hidden_response.status_code == HTTPStatus.NOT_FOUND, hidden_response.text
    assert wire(hidden_response) == wire(unknown)


@pytest.mark.parametrize("identity", MEMBERS)
async def test_a_member_sees_every_stop_photo_and_the_trail(
    client: AsyncClient, delayed_trip: DelayedTrip, identity: str
) -> None:
    trip, headers = delayed_trip, delayed_trip.headers[identity]
    base = f"/api/v2/trips/{trip.trip_id}"
    every = {trip.old, trip.recent, trip.future}

    stops = await send(client, "GET", f"{base}/stops", headers)
    assert stops.status_code == HTTPStatus.OK, stops.text
    assert {s["id"] for s in stops.json()} == every

    points, lines = points_and_lines((await send(client, "GET", f"{base}/map", headers)).json())
    assert {p["id"] for p in points} == every
    assert len(lines) == 1
    # Chronological: old, recent, future.
    assert lines[0]["geometry"]["coordinates"] == [
        [132.26, -14.46],
        [133.10, -15.10],
        [133.37, -16.25],
    ]

    for stop_id in every:
        photos = await send(client, "GET", f"{base}/stops/{stop_id}/photos", headers)
        assert photos.status_code == HTTPStatus.OK, photos.text
        assert [p["id"] for p in photos.json()] == [f"photo-of-{stop_id}"]


@pytest.mark.parametrize("identity", NON_MEMBERS + MEMBERS)
async def test_last_public_stop_at_is_the_same_for_everyone(
    client: AsyncClient, delayed_trip: DelayedTrip, identity: str
) -> None:
    """The latest *visible* stop, even for members, who can see later ones."""
    response = await send(
        client, "GET", f"/api/v2/trips/{delayed_trip.trip_id}", delayed_trip.headers[identity]
    )
    assert response.status_code == HTTPStatus.OK, response.text
    assert parse(response.json()["lastPublicStopAt"]) == delayed_trip.times["old"]


async def test_with_no_delay_a_non_member_sees_every_past_stop_and_no_future_one(
    client: AsyncClient, world: World, delayed_trip: DelayedTrip
) -> None:
    async with world.engine.begin() as conn:
        await conn.execute(
            update(tables.trips)
            .where(tables.trips.c.id == delayed_trip.trip_id)
            .values(public_delay_hours=0)
        )
    trip, base = delayed_trip, f"/api/v2/trips/{delayed_trip.trip_id}"

    for identity in NON_MEMBERS:
        headers = trip.headers[identity]
        stops = await send(client, "GET", f"{base}/stops", headers)
        assert {s["id"] for s in stops.json()} == {trip.old, trip.recent}, identity

        points, lines = points_and_lines((await send(client, "GET", f"{base}/map", headers)).json())
        assert {p["id"] for p in points} == {trip.old, trip.recent}, identity
        assert len(lines) == 1, identity  # two visible stops: a trail
        assert len(lines[0]["geometry"]["coordinates"]) == 2

        future = await send(client, "GET", f"{base}/stops/{trip.future}/photos", headers)
        assert future.status_code == HTTPStatus.NOT_FOUND, identity

    for identity in NON_MEMBERS + MEMBERS:
        body = (await send(client, "GET", base, trip.headers[identity])).json()
        assert parse(body["lastPublicStopAt"]) == trip.times["recent"], identity


async def test_a_trip_with_no_visible_stop_has_a_null_last_public_stop_at(
    client: AsyncClient, world: World
) -> None:
    trip_id, _ = await world.trip(visibility="public", delay=24)
    await world.stop(trip_id, at(-1), -14.46, 132.26)

    body = (await send(client, "GET", f"/api/v2/trips/{trip_id}")).json()
    assert body["lastPublicStopAt"] is None
    assert body["publicDelayHours"] == 24
    assert body["visibility"] == "public"


# --------------------------------------------------------------------------
# 3. viewer.role and access
# --------------------------------------------------------------------------

# Scenario -> the role the contract's table gives it. Each is set up on the trip
# under test by `arrange` below.
ROLE_SCENARIOS = {
    "anonymous": "anonymous",
    "no_rows": "none",
    "pending": "pending",
    "rider": "rider",
    "leader": "leader",
    "revoked": "none",
    "rejected": "none",
    "blocked": "none",
    "cancelled": "none",
    "revoked_and_pending": "pending",
    "rejected_then_pending": "pending",
    # Rights on a *different* trip say nothing about this one.
    "rider_elsewhere": "none",
    "pending_elsewhere": "none",
}


async def arrange(world: World, scenario: str, trip_id: str, other_id: str) -> dict[str, str]:
    if scenario == "anonymous":
        return {}
    account = await world.account(f"Role {scenario}"[:40])
    user = account.user_id
    if scenario in ("rider", "leader"):
        await grant_membership(world.engine, trip_id, user, role=scenario)
    elif scenario in ("pending", "rejected", "blocked", "cancelled"):
        await world.join_request(trip_id, user, scenario)
    elif scenario == "revoked":
        await grant_membership(world.engine, trip_id, user, revoked=True)
    elif scenario == "revoked_and_pending":
        await grant_membership(world.engine, trip_id, user, revoked=True)
        await world.join_request(trip_id, user, "pending")
    elif scenario == "rejected_then_pending":
        await world.join_request(trip_id, user, "rejected")
        await world.join_request(trip_id, user, "pending")
    elif scenario == "rider_elsewhere":
        await grant_membership(world.engine, other_id, user)
    elif scenario == "pending_elsewhere":
        await world.join_request(other_id, user, "pending")
    return account.headers


@pytest.mark.parametrize("scenario", list(ROLE_SCENARIOS))
async def test_viewer_role_and_access_on_the_v2_and_legacy_get(
    client: AsyncClient, world: World, scenario: str
) -> None:
    trip_id, viewer_slug = await world.trip(visibility="public", slugs=True)
    other_id, _ = await world.trip(visibility="public")
    headers = await arrange(world, scenario, trip_id, other_id)
    role = ROLE_SCENARIOS[scenario]

    v2 = await send(client, "GET", f"/api/v2/trips/{trip_id}", headers)
    legacy = await send(client, "GET", f"/api/trips/{viewer_slug}", headers)

    for response in (v2, legacy):
        assert response.status_code == HTTPStatus.OK, response.text
        body = response.json()
        assert body["viewer"] == {"role": role}
        assert body["access"] == ("rider" if role in ("rider", "leader") else "viewer")


# --------------------------------------------------------------------------
# 4. A public trip, anonymously
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("read", READS, ids=READ_IDS)
async def test_an_anonymous_caller_reads_every_part_of_a_public_trip(
    client: AsyncClient, world: World, read: str, method: str
) -> None:
    trip_id, _ = await world.trip(visibility="public")
    stop_id = await world.stop(trip_id, at(-48), -14.46, 132.26)
    await world.bike(trip_id)

    response = await send(client, method, path(trip_id, read, stop_id))

    assert response.status_code == HTTPStatus.OK, response.text
    assert session_cookies(response) == []
    if method == "HEAD":
        assert response.content == b""
        get = await send(client, "GET", path(trip_id, read, stop_id))
        assert response.headers["content-length"] == get.headers["content-length"]


# --------------------------------------------------------------------------
# 5. public-read on a v2 read
# --------------------------------------------------------------------------


class FrozenClock:
    """The registry's clock (the pattern in ``test_ratelimit.py``): still until advanced."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_the_public_read_limit_applies_to_v2_reads(
    client: AsyncClient, world: World, private_trip: PrivateTrip
) -> None:
    """
    120 a minute per IP, shared by GET and HEAD; the 121st is ``429`` + ``Retry-After: 1``.

    The refusal comes before the trip is looked up, so it is the same for a
    private trip and a random id.
    """
    clock = FrozenClock()
    ratelimit.registry.clock = clock  # conftest's autouse fixture puts it back
    public_id, _ = await world.trip(visibility="public")
    stops = f"/api/v2/trips/{public_id}/stops"

    methods = itertools.cycle(METHODS)
    for i in range(120):
        response = await send(client, next(methods), stops)
        assert response.status_code == HTTPStatus.OK, (i, response.status_code)

    # One shared bucket: every v2 read, GET and HEAD, is now refused.
    stop_id = private_trip.stop_id
    for read in READS:
        for method in METHODS:
            refused = await send(client, method, path(public_id, read, stop_id))
            assert refused.status_code == HTTPStatus.TOO_MANY_REQUESTS, (read, method)
            assert refused.headers["retry-after"] == "1"
    refused = await send(client, "GET", stops)
    assert refused.json()["error"]["code"] == "RATE_LIMITED"

    hidden = await send(client, "GET", f"/api/v2/trips/{private_trip.trip_id}")
    missing = await send(client, "GET", f"/api/v2/trips/{uuid4()}")
    assert hidden.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert wire(hidden) == wire(missing)

    clock.advance(1)
    assert (await send(client, "GET", stops)).status_code == HTTPStatus.OK


V2_READ_PATHS = {
    "/api/v2/trips/{tripId}",
    "/api/v2/trips/{tripId}/bikes",
    "/api/v2/trips/{tripId}/stops",
    "/api/v2/trips/{tripId}/stops/{stopId}/photos",
    "/api/v2/trips/{tripId}/map",
}


def test_openapi_declares_404_and_429_on_every_v2_read_and_never_401() -> None:
    """
    Contract v2 table: each read is ``200, 404, 429``; the reader gate is never ``401``.

    ``422`` is tolerated, not required: the project declares it on every operation
    with path parameters so FastAPI's ``HTTPValidationError`` never enters the
    document (``test_openapi_error_responses.py``), as on the legacy reads.
    """
    application = app.main.app
    cached = application.openapi_schema
    try:
        application.openapi_schema = None
        spec = application.openapi()
    finally:
        application.openapi_schema = cached

    for read_path in V2_READ_PATHS:
        operations = spec["paths"][read_path]
        # HEAD is schema-excluded. Other verbs may share the path (the v2 rider
        # writes, `t-am-v2-rider-writes`); only the GET is checked here.
        assert "get" in operations and "head" not in operations, (read_path, set(operations))
        errors = {code for code in operations["get"]["responses"] if not code.startswith("2")}
        assert {"404", "429"} <= errors <= {"404", "422", "429"}, (read_path, errors)
