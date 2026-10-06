"""
Rate limits over HTTP: the buckets, their keys and their order (task ``t-am-rate-limits``).

**Written from the contract.** ``docs/api-contract.md``, "Rate limits and
lockout", is the source of every number here. The ``Retry-After`` values below
are the contract's own arithmetic ("whole seconds until one token is
available", at least 1) applied to its table, with a frozen clock so no bucket
refills mid-test:

- ``public-read`` 120/min: one token every 0.5 s, so ``Retry-After: 1``;
- ``signup-ip`` 5/hour: 720 s; ``signup-global`` 50/day: 1728 s;
- ``signin`` 10 per 15 min: 90 s;
- ``writes`` 600/hour: 6 s.

What the contract does **not** define, and where these tests follow the
implementation instead (each is labelled at its test):

- the limiter's 429 message text (the contract only fixes the code and header);
- the ``X-Forwarded-For`` key for ``TRUSTED_PROXY_HOPS`` > 1, and for a header
  with fewer entries than the hop count;
- the key ``writes`` uses for a caller with no session (the contract says "user";
  the limiter runs before the gate, so an anonymous caller needs *some* key).

Not repeated here: the bucket arithmetic and eviction
(``tests/unit/test_ratelimit_buckets.py``) and "every route declares the right
limiter, first" (``tests/test_ratelimit_audit.py``). The lockout's own
behaviour is ``tests/test_auth_endpoints.py``; here it is only told apart from
the limiter.

Time is the registry's injectable clock, frozen and advanced by hand: no sleeps.
Every in-process client shares one socket address (``127.0.0.1``), so tests
that need several clients use distinct ``X-Forwarded-For`` values.
"""

from __future__ import annotations

import io
import itertools
import re
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from botocore.exceptions import ClientError
from conftest import (
    SeededBike,
    SeededTrip,
    SignedInAccount,
    create_signed_in_account,
    delete_accounts,
    grant_membership,
    make_async_client,
)
from httpx import AsyncClient
from jpeg_fixtures import minimal_jpeg
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.main
from app.api.routes.v2 import auth as auth_routes
from app.core import ratelimit
from app.core.config import get_settings
from app.data import tables
from app.data.db import get_session
from app.storage.s3_client import BUCKET_NAME, get_s3_client

# Implementation-defined (the contract names no text): the limiter's message.
LIMITER_MESSAGE = "Too many requests. Please wait a moment and try again."

HEALTH = "/api/health"
SIGNUP = "/api/v2/auth/signup"
SIGNIN = "/api/v2/auth/signin"
SIGNOUT = "/api/v2/auth/signout"
SIGNOUT_ALL = "/api/v2/auth/signout-all"
RECOVER = "/api/v2/auth/recover"
PASSWORD = "/api/v2/auth/password"
RECOVERY_CODE = "/api/v2/auth/recovery-code"
ME = "/api/v2/auth/me"
UNKNOWN_SLUG = "definitely-not-a-real-slug-0000"

# The contract's table, as the Retry-After a fresh-then-drained bucket must carry.
PUBLIC_READ = (120, 1)
SIGNUP_IP = (5, 720)
SIGNUP_GLOBAL = (50, 1728)
SIGNIN_BUCKET = (10, 90)
WRITES = (600, 6)

Request = tuple[str, str, dict[str, Any]]


class FrozenClock:
    """The registry's clock: stands still until advanced."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FrozenClock:
    # The autouse conftest fixture has already reset the registry and will put
    # time.monotonic back before the next test.
    frozen = FrozenClock()
    ratelimit.registry.clock = frozen
    return frozen


@pytest.fixture
async def client(migrated_engine: AsyncEngine, clock: FrozenClock) -> AsyncIterator[AsyncClient]:
    """The real app on the test database, with no session and the frozen clock."""
    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.main.app.dependency_overrides[get_session] = session_override
    try:
        async with make_async_client(app.main.app) as http_client:
            yield http_client
    finally:
        app.main.app.dependency_overrides.pop(get_session, None)


_addresses = itertools.count(1)


def fresh_ip() -> str:
    """An address no other request in this run has used."""
    n = next(_addresses)
    return f"198.18.{(n >> 8) & 255}.{n & 255}"


def xff(value: str) -> dict[str, str]:
    return {"X-Forwarded-For": value}


def garbage_cookie() -> dict[str, str]:
    """A session cookie that names no stored session, different every call."""
    return {"Cookie": f"__Host-btj_session={secrets.token_urlsafe(32)}"}


async def send(client: AsyncClient, method: str, path: str, **kwargs: Any) -> httpx.Response:
    """One request with an empty cookie jar: the only cookie sent is one passed in headers."""
    client.cookies.clear()
    response = await client.request(method, path, **kwargs)
    client.cookies.clear()
    return response


async def drain(client: AsyncClient, requests: list[Request], count: int) -> None:
    """Send ``count`` requests, cycling through ``requests``; none may be refused."""
    cycle = itertools.cycle(requests)
    for i in range(count):
        method, path, kwargs = next(cycle)
        kwargs = {k: (v() if callable(v) else v) for k, v in kwargs.items()}
        response = await send(client, method, path, **kwargs)
        assert response.status_code != 429, f"request {i + 1} of {count} ({method} {path}) was 429"


def assert_rate_limited(response: httpx.Response, retry_after: int | None = None) -> None:
    """The limiter's 429: the contract envelope, an integer Retry-After >= 1."""
    assert response.status_code == 429, (response.status_code, response.text)
    header = response.headers.get("retry-after")
    assert header is not None and re.fullmatch(r"[0-9]+", header), header
    assert int(header) >= 1
    if retry_after is not None:
        assert int(header) == retry_after
    if response.request.method != "HEAD":
        assert response.json() == {"error": {"code": "RATE_LIMITED", "message": LIMITER_MESSAGE}}


def stop_payload() -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "name": "Daly Waters Pub",
        "lat": -16.2503,
        "lng": 133.3703,
        "locationSource": "gps",
        "arrivedAt": "2026-06-14T15:15:00+09:30",
        "notes": None,
    }


def bike_payload() -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "riderName": "Zoe",
        "make": "Yamaha",
        "model": "Tenere 700",
        "year": 2022,
        "specs": "Rally tower",
    }


def photo_form(photo_id: str) -> dict[str, Any]:
    return {
        "data": {"id": photo_id, "takenAt": "2026-06-14T15:15:00+09:30"},
        "files": {"file": ("photo.jpg", io.BytesIO(minimal_jpeg()), "image/jpeg")},
    }


# --------------------------------------------------------------------------
# public-read, and the IP key
# --------------------------------------------------------------------------


async def test_public_read_is_120_a_minute_across_legacy_and_v2_get_and_head(
    client: AsyncClient, seeded_trips: list[SeededTrip], clock: FrozenClock
) -> None:
    trip = seeded_trips[0]
    stop = f"/api/trips/{trip.viewer_slug}/stops/no-such-stop/photos"
    reads: list[Request] = [
        (method, path, {})
        for path in (
            f"/api/trips/{trip.viewer_slug}",
            f"/api/trips/{trip.rider_slug}/stops",
            f"/api/trips/{trip.viewer_slug}/map",
            stop,
            f"/api/trips/{UNKNOWN_SLUG}",
            ME,
        )
        for method in ("GET", "HEAD")
    ]
    count, retry_after = PUBLIC_READ
    await drain(client, reads, count)

    # Request N+1 is refused on every one of them: one shared bucket. This
    # includes the unknown slug (would be 404) and `me` with no session (401).
    for method, path, _ in reads:
        assert_rate_limited(await send(client, method, path), retry_after)

    # Refill: after Retry-After, the next read goes through.
    clock.advance(retry_after)
    assert (await send(client, "GET", f"/api/trips/{trip.viewer_slug}")).status_code == 200


async def test_the_429_is_the_same_for_an_existing_and_an_unknown_trip(
    client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    await drain(client, [("GET", f"/api/trips/{UNKNOWN_SLUG}", {})], PUBLIC_READ[0])
    known = await send(client, "GET", f"/api/trips/{seeded_trips[0].viewer_slug}")
    unknown = await send(client, "GET", f"/api/trips/{UNKNOWN_SLUG}")
    assert_rate_limited(known)
    assert_rate_limited(unknown)
    assert known.content == unknown.content
    assert known.headers["retry-after"] == unknown.headers["retry-after"]


async def test_the_key_is_the_right_most_x_forwarded_for_entry(client: AsyncClient) -> None:
    await drain(client, [("GET", ME, {"headers": xff("1.1.1.1, 203.0.113.9")})], PUBLIC_READ[0])
    # Same right-most entry, whatever the rest says: same bucket.
    assert_rate_limited(await send(client, "GET", ME, headers=xff("203.0.113.9")))
    assert_rate_limited(await send(client, "GET", ME, headers=xff("2.2.2.2, 203.0.113.9")))
    # Same left-most entry, different right-most: a different bucket.
    assert (await send(client, "GET", ME, headers=xff("1.1.1.1, 203.0.113.10"))).status_code == 401


async def test_varying_only_the_left_most_entry_shares_one_bucket(client: AsyncClient) -> None:
    spoofed = [
        ("GET", ME, {"headers": xff(f"10.0.0.{i}, 203.0.113.20")}) for i in range(PUBLIC_READ[0])
    ]
    await drain(client, spoofed, PUBLIC_READ[0])
    assert_rate_limited(await send(client, "GET", ME, headers=xff("10.9.9.9, 203.0.113.20")))


async def test_several_x_forwarded_for_headers_are_one_list(client: AsyncClient) -> None:
    await drain(client, [("GET", ME, {"headers": xff("203.0.113.30")})], PUBLIC_READ[0])
    # Two headers read as "5.5.5.5, 203.0.113.30": the right-most is the drained key.
    two = [("X-Forwarded-For", "5.5.5.5"), ("X-Forwarded-For", "203.0.113.30")]
    assert_rate_limited(await send(client, "GET", ME, headers=two))
    # Reversed, the right-most is 5.5.5.5: a fresh bucket.
    reversed_two = [("X-Forwarded-For", "203.0.113.30"), ("X-Forwarded-For", "5.5.5.5")]
    assert (await send(client, "GET", ME, headers=reversed_two)).status_code == 401


async def test_with_no_x_forwarded_for_the_socket_address_is_the_key(client: AsyncClient) -> None:
    # httpx's ASGITransport reports the socket as 127.0.0.1.
    await drain(client, [("GET", ME, {})], PUBLIC_READ[0])
    assert_rate_limited(await send(client, "GET", ME))
    # The same address arriving through the proxy lands in the same bucket;
    # any other address does not.
    assert_rate_limited(await send(client, "GET", ME, headers=xff("127.0.0.1")))
    assert (await send(client, "GET", ME, headers=xff("203.0.113.40"))).status_code == 401


async def test_trusted_proxy_hops_zero_ignores_x_forwarded_for(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 0)
    varied = [("GET", ME, {"headers": xff(f"203.0.113.{i}")}) for i in range(PUBLIC_READ[0])]
    await drain(client, varied, PUBLIC_READ[0])
    assert_rate_limited(await send(client, "GET", ME, headers=xff("198.51.100.1")))
    assert_rate_limited(await send(client, "GET", ME))


async def test_two_trusted_hops_key_on_the_second_entry_from_the_right(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Implementation-defined (the contract fixes only the default of one hop)."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 2)
    await drain(client, [("GET", ME, {"headers": xff("9.9.9.9, 203.0.113.50, 10.0.0.1")})], 120)
    assert_rate_limited(await send(client, "GET", ME, headers=xff("203.0.113.50, 10.0.0.2")))
    assert (await send(client, "GET", ME, headers=xff("9.9.9.9, 10.0.0.1"))).status_code == 401


async def test_fewer_entries_than_trusted_hops_falls_back_to_the_socket_address(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Implementation-defined: the contract does not cover a header shorter than the hop count."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 2)
    await drain(client, [("GET", ME, {})], PUBLIC_READ[0])
    # One entry, two hops trusted: the request didn't come through both, so the
    # socket address (already drained) is the key, not 203.0.113.60.
    assert_rate_limited(await send(client, "GET", ME, headers=xff("203.0.113.60")))


# --------------------------------------------------------------------------
# signup-ip + signup-global
# --------------------------------------------------------------------------


async def test_signup_ip_is_5_an_hour(client: AsyncClient, clock: FrozenClock) -> None:
    # `{}` fails validation (422) after the limiter has spent its token, which
    # keeps argon2 out of the loop; the limiter never sees the body.
    ip = fresh_ip()
    count, retry_after = SIGNUP_IP
    await drain(client, [("POST", SIGNUP, {"json": {}, "headers": xff(ip)})], count)
    assert_rate_limited(await send(client, "POST", SIGNUP, json={}, headers=xff(ip)), retry_after)
    # Another address is untouched.
    assert (await send(client, "POST", SIGNUP, json={}, headers=xff(fresh_ip()))).status_code == 422
    clock.advance(retry_after)
    assert (await send(client, "POST", SIGNUP, json={}, headers=xff(ip))).status_code == 422


async def test_signup_global_is_50_a_day_and_its_refusal_spends_no_per_ip_token(
    client: AsyncClient, clock: FrozenClock
) -> None:
    count, retry_after = SIGNUP_GLOBAL
    many = [("POST", SIGNUP, {"json": {}, "headers": lambda: xff(fresh_ip())})]
    await drain(client, many, count)

    # A fresh address, with its own five untouched, is refused by the global bucket.
    ip = fresh_ip()
    for _ in range(SIGNUP_IP[0] + 2):
        response = await send(client, "POST", SIGNUP, json={}, headers=xff(ip))
        assert_rate_limited(response, retry_after)

    # White-box: drop the global bucket (as a new day would refill it without
    # also refilling the per-IP one), then show the address still has all five.
    del ratelimit.registry._states[(ratelimit.SIGNUP_GLOBAL.name, "*")]
    await drain(client, [("POST", SIGNUP, {"json": {}, "headers": xff(ip)})], SIGNUP_IP[0])
    assert_rate_limited(await send(client, "POST", SIGNUP, json={}, headers=xff(ip)), SIGNUP_IP[1])


# --------------------------------------------------------------------------
# signin, and the lockout told apart from it
# --------------------------------------------------------------------------


async def test_signin_bucket_is_10_per_15_minutes_shared_by_four_routes(
    client: AsyncClient, clock: FrozenClock
) -> None:
    ip = xff(fresh_ip())
    routes = [SIGNIN, RECOVER, PASSWORD, RECOVERY_CODE]
    count, retry_after = SIGNIN_BUCKET
    await drain(client, [("POST", path, {"json": {}, "headers": ip}) for path in routes], count)

    # Every one of the four is now refused, including password and
    # recovery-code, which without a session would otherwise be 401.
    for path in routes:
        assert_rate_limited(await send(client, "POST", path, json={}, headers=ip), retry_after)
    # public-read and signup are separate buckets.
    assert (await send(client, "GET", ME, headers=ip)).status_code == 401
    assert (await send(client, "POST", SIGNUP, json={}, headers=ip)).status_code == 422

    clock.advance(retry_after)
    assert (await send(client, "POST", SIGNIN, json={}, headers=ip)).status_code == 422


async def test_signin_429_is_the_same_for_an_existing_and_an_unknown_account(
    client: AsyncClient, migrated_engine: AsyncEngine
) -> None:
    account = await create_signed_in_account(migrated_engine)
    try:
        username = await _username(migrated_engine, account.user_id)
        ip = xff(fresh_ip())
        await drain(client, [("POST", SIGNIN, {"json": {}, "headers": ip})], SIGNIN_BUCKET[0])
        body = {"password": "correct horse battery staple"}
        known = await send(client, "POST", SIGNIN, json={**body, "username": username}, headers=ip)
        unknown = await send(
            client, "POST", SIGNIN, json={**body, "username": "t-no-such-user"}, headers=ip
        )
        assert_rate_limited(known)
        assert_rate_limited(unknown)
        assert known.content == unknown.content
        assert known.headers["retry-after"] == unknown.headers["retry-after"]
    finally:
        await delete_accounts(migrated_engine, [account.user_id])


async def test_the_lockout_429_and_the_limiter_429_differ_by_message(
    client: AsyncClient, migrated_engine: AsyncEngine
) -> None:
    account = await create_signed_in_account(migrated_engine)
    try:
        username = await _username(migrated_engine, account.user_id)
        async with migrated_engine.begin() as conn:
            await conn.execute(
                update(tables.users)
                .where(tables.users.c.id == account.user_id)
                .values(locked_until=datetime.now(UTC) + timedelta(minutes=10))
            )
        body = {"username": username, "password": "whatever it is"}
        locked = await send(client, "POST", SIGNIN, json=body, headers=xff(fresh_ip()))
        assert locked.status_code == 429
        assert locked.json()["error"]["code"] == "RATE_LIMITED"
        assert locked.json()["error"]["message"] == auth_routes.LOCKED_MESSAGE

        ip = xff(fresh_ip())
        await drain(client, [("POST", SIGNIN, {"json": {}, "headers": ip})], SIGNIN_BUCKET[0])
        limited = await send(client, "POST", SIGNIN, json=body, headers=ip)
        assert_rate_limited(limited)
        assert limited.json()["error"]["message"] != locked.json()["error"]["message"]
    finally:
        await delete_accounts(migrated_engine, [account.user_id])


async def _username(engine: AsyncEngine, user_id: str) -> str:
    async with engine.connect() as conn:
        return (
            await conn.execute(select(tables.users.c.username).where(tables.users.c.id == user_id))
        ).scalar_one()


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------


def _write_routes(trip: SeededTrip) -> list[tuple[str, str]]:
    return [
        ("POST", f"/api/trips/{UNKNOWN_SLUG}/stops"),
        ("POST", f"/api/trips/{trip.rider_slug}/stops"),
        ("POST", f"/api/trips/{trip.rider_slug}/stops/no-such-stop/photos"),
        ("POST", f"/api/trips/{trip.viewer_slug}/bikes"),
        ("PATCH", f"/api/trips/{trip.rider_slug}/bikes/no-such-bike"),
        ("POST", SIGNOUT_ALL),
    ]


async def test_writes_without_a_session_share_one_bucket_per_address(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    rider_session: SignedInAccount,
    clock: FrozenClock,
) -> None:
    """
    No cookie and a cookie naming no session are both keyed by the address
    (implementation-defined; see the module docstring), and a fresh garbage
    cookie on every request does not buy a fresh bucket.
    """
    trip = seeded_trips[0]
    ip = fresh_ip()
    routes = _write_routes(trip)
    anonymous: list[Request] = [
        (method, path, {"json": {}, "headers": xff(ip)}) for method, path in routes
    ] + [
        (method, path, {"json": {}, "headers": lambda: {**xff(ip), **garbage_cookie()}})
        for method, path in routes
    ]
    count, retry_after = WRITES
    await drain(client, anonymous, count)

    for method, path in routes:
        # 429 before the gate's 401/403/404, for an unknown slug too.
        assert_rate_limited(await send(client, method, path, json={}, headers=xff(ip)), retry_after)
        garbage = {**xff(ip), **garbage_cookie()}
        assert_rate_limited(await send(client, method, path, json={}, headers=garbage), retry_after)

    # A signed-in member behind the same address has their own bucket.
    ok = await send(
        client,
        "POST",
        f"/api/trips/{trip.rider_slug}/stops",
        json=stop_payload(),
        headers={**xff(ip), **rider_session.headers},
    )
    assert ok.status_code == 201, ok.text

    # signout has no limiter.
    for _ in range(5):
        assert (await send(client, "POST", SIGNOUT, headers=xff(ip))).status_code == 204

    clock.advance(retry_after)
    refilled = await send(client, "POST", f"/api/trips/{UNKNOWN_SLUG}/stops", headers=xff(ip))
    assert refilled.status_code != 429


async def test_writes_is_per_account_and_a_refused_write_stores_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    seeded_bikes: list[SeededBike],
    rider_session: SignedInAccount,
    s3_bucket: None,
    clock: FrozenClock,
) -> None:
    trip = seeded_trips[0]
    bike = next(b for b in seeded_bikes if b.trip_id == trip.id)
    stop_id = await _insert_stop(migrated_engine, trip.id)
    other = await create_signed_in_account(migrated_engine, display_name="Other Rider")
    await grant_membership(migrated_engine, trip.id, other.user_id)
    ip = fresh_ip()
    me = {**xff(ip), **rider_session.headers}
    photo_id = str(uuid.uuid4())
    object_key = f"{trip.id}/{stop_id}/{photo_id}"
    s3 = get_s3_client()
    try:
        # Drain the account's bucket on a route its gate refuses (unknown slug, 404):
        # the token is spent before the gate runs.
        count, retry_after = WRITES
        await drain(
            client,
            [("POST", f"/api/trips/{UNKNOWN_SLUG}/stops", {"json": {}, "headers": me})],
            count,
        )

        # Every write this account could otherwise make is refused, from any address.
        stop = stop_payload()
        bike_new = bike_payload()
        refused: list[Callable[[dict[str, str]], Any]] = [
            lambda h: send(
                client, "POST", f"/api/trips/{trip.rider_slug}/stops", json=stop, headers=h
            ),
            lambda h: send(
                client, "POST", f"/api/trips/{trip.rider_slug}/bikes", json=bike_new, headers=h
            ),
            lambda h: send(
                client,
                "PATCH",
                f"/api/trips/{trip.rider_slug}/bikes/{bike.id}",
                json={"specs": "changed by a refused write"},
                headers=h,
            ),
            lambda h: send(
                client,
                "POST",
                f"/api/trips/{trip.rider_slug}/stops/{stop_id}/photos",
                headers=h,
                **photo_form(photo_id),
            ),
            lambda h: send(client, "POST", SIGNOUT_ALL, headers=h),
        ]
        for make in refused:
            assert_rate_limited(await make(me), retry_after)
            assert_rate_limited(
                await make({**xff(fresh_ip()), **rider_session.headers}), retry_after
            )

        # Nothing was stored: no stop, no bike, bike unchanged, no photo row, no object,
        # and signout-all revoked nothing.
        async with migrated_engine.connect() as conn:
            assert (
                await conn.execute(tables.stops.select().where(tables.stops.c.id == stop["id"]))
            ).first() is None
            assert (
                await conn.execute(tables.bikes.select().where(tables.bikes.c.id == bike_new["id"]))
            ).first() is None
            specs = (
                await conn.execute(select(tables.bikes.c.specs).where(tables.bikes.c.id == bike.id))
            ).scalar_one()
            assert specs == bike.specs
            assert (
                await conn.execute(tables.photos.select().where(tables.photos.c.id == photo_id))
            ).first() is None
        with pytest.raises(ClientError):
            s3.head_object(Bucket=BUCKET_NAME, Key=object_key)
        assert (await send(client, "GET", ME, headers=me)).status_code == 200

        # Another account behind the same address has its own bucket, and so does
        # the address itself.
        theirs = {**xff(ip), **other.headers}
        created = await send(
            client,
            "POST",
            f"/api/trips/{trip.rider_slug}/stops",
            json=stop_payload(),
            headers=theirs,
        )
        assert created.status_code == 201, created.text
        anonymous = await send(client, "POST", f"/api/trips/{UNKNOWN_SLUG}/stops", headers=xff(ip))
        assert anonymous.status_code != 429

        # The 429 says nothing about whether the trip exists.
        known = await refused[0](me)
        unknown = await send(
            client, "POST", f"/api/trips/{UNKNOWN_SLUG}/stops", json=stop, headers=me
        )
        assert known.content == unknown.content

        # After Retry-After, the same upload succeeds: the refusal was the limiter's alone.
        clock.advance(retry_after)
        uploaded = await refused[3](me)
        assert uploaded.status_code == 201, uploaded.text
        s3.head_object(Bucket=BUCKET_NAME, Key=object_key)
    finally:
        try:
            s3.delete_object(Bucket=BUCKET_NAME, Key=object_key)
        finally:
            await delete_accounts(migrated_engine, [other.user_id])


async def _insert_stop(engine: AsyncEngine, trip_id: str) -> str:
    stop_id = f"test-stop-{secrets.token_urlsafe(8)}"
    async with engine.begin() as conn:
        await conn.execute(
            tables.stops.insert().values(
                id=stop_id,
                trip_id=trip_id,
                name="Katherine Gorge",
                lat=-14.3155,
                lng=132.4187,
                location_source="gps",
                arrived_at=datetime(2026, 6, 2, 8, 30, tzinfo=UTC),
                notes=None,
            )
        )
    return stop_id


# --------------------------------------------------------------------------
# Never limited
# --------------------------------------------------------------------------


async def test_health_catch_all_and_signout_are_never_limited(client: AsyncClient) -> None:
    await drain(client, [("GET", f"/api/trips/{UNKNOWN_SLUG}", {})], PUBLIC_READ[0])
    await drain(client, [("POST", SIGNIN, {"json": {}})], SIGNIN_BUCKET[0])
    assert_rate_limited(await send(client, "GET", f"/api/trips/{UNKNOWN_SLUG}"))
    assert_rate_limited(await send(client, "POST", SIGNIN, json={}))

    for _ in range(PUBLIC_READ[0] + 10):
        assert (await send(client, "GET", HEALTH)).status_code == 200
        assert (await send(client, "HEAD", HEALTH)).status_code == 200
        assert (await send(client, "GET", "/api/no-such-thing")).status_code == 404
        assert (await send(client, "POST", "/api/no-such-thing")).status_code == 404
        assert (await send(client, "POST", SIGNOUT)).status_code == 204
