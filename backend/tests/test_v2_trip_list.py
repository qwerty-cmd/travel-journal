"""
``GET /api/v2/trips``: the anonymous public trip list (task ``t-am-v2-trip-list``).

**Written from the contract**, not the implementation: ``docs/api-contract.md``
-- the ``GET /api/v2/trips`` row (anonymous; ``cursor?``, ``limit?`` 1-50,
default 20; ``TripPageOut``; 200, 422, 429; ``public-read``), its v2 note
(public trips only, the same for every caller, a session is ignored; sorted by
``lastPublicStopAt`` desc with nulls last, then ``id`` asc; ``nextCursor`` is
opaque and null on the last page; a malformed cursor is a 422), "Public delay"
(``lastPublicStopAt`` is the latest visible ``arrivedAt``, the same value for
every caller, members included) and "Rate limits" (``public-read`` 120/min per
IP, GET and HEAD).

This is a privacy boundary: a private trip in this list is a leak of a trip
whose riders never agreed to publish it. The private-trip tests therefore try
the three ways one could slip in -- sorting first, a hand-built cursor sitting
right on its sort key, and a caller who is a member of it.

**Isolation.** Other rows may be in ``trips``. Nothing here assumes the table is
empty: every listing is walked to the end and filtered to this test's own trip
ids, and "3 pages" / "default 20" are asserted against the total count of public
trips read from the database at the same moment. Every test deletes its trips.

**Implementation-following**, labelled where used: the cursor's inner encoding
(the contract only says "base64url of the last row's sort key"), needed to build
a cursor that points near a private trip and one that is validly encoded but
edited.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from conftest import (
    SignedInAccount,
    create_signed_in_account,
    delete_accounts,
    grant_membership,
    make_async_client,
)
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.main
from app.core import ratelimit
from app.data import tables
from app.data.db import get_session
from app.models.trip import TripPageOut

LIST = "/api/v2/trips"
SUMMARY_KEYS = {"id", "name", "startDate", "riderCount", "lastPublicStopAt"}
PAGE_KEYS = {"items", "nextCursor"}
LIMITER_MESSAGE = "Too many requests. Please wait a moment and try again."


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


class FrozenClock:
    """The rate-limit registry's clock: stands still until advanced (as test_ratelimit.py)."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """The real app on the test database, with no session."""
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


@dataclass(frozen=True, slots=True)
class Made:
    id: str
    name: str
    start_date: date
    rider_slug: str
    viewer_slug: str


class TripFactory:
    """Inserts trips/stops/memberships straight into the tables; deletes them on teardown."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.run = secrets.token_hex(4)
        self.trip_ids: list[str] = []
        self.user_ids: list[str] = []

    def tid(self, suffix: str) -> str:
        """A trip id for this run. The suffix controls ``id`` asc order within one run."""
        return f"tl-{self.run}-{suffix}"

    async def trip(
        self,
        suffix: str,
        *,
        visibility: str = "public",
        delay_hours: int = 0,
        stops: list[datetime] | None = None,
    ) -> Made:
        made = Made(
            id=self.tid(suffix),
            name=f"List trip {suffix}",
            start_date=date(2026, 6, 1),
            rider_slug=secrets.token_urlsafe(16),
            viewer_slug=secrets.token_urlsafe(16),
        )
        self.trip_ids.append(made.id)
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.trips.insert().values(
                    id=made.id,
                    name=made.name,
                    rider_slug=made.rider_slug,
                    viewer_slug=made.viewer_slug,
                    start_date=made.start_date,
                    visibility=visibility,
                    public_delay_hours=delay_hours,
                )
            )
            for arrived_at in stops or []:
                await conn.execute(
                    tables.stops.insert().values(
                        id=str(uuid.uuid4()),
                        trip_id=made.id,
                        name="Stop",
                        lat=-16.25,
                        lng=133.37,
                        location_source="gps",
                        arrived_at=arrived_at,
                        notes=None,
                    )
                )
        return made

    async def account(self) -> SignedInAccount:
        account = await create_signed_in_account(self.engine)
        self.user_ids.append(account.user_id)
        return account

    async def cleanup(self) -> None:
        if self.user_ids:
            await delete_accounts(self.engine, self.user_ids)
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.trip_members.delete().where(tables.trip_members.c.trip_id.in_(self.trip_ids))
            )
            await conn.execute(tables.trips.delete().where(tables.trips.c.id.in_(self.trip_ids)))


@pytest.fixture
async def factory(migrated_engine: AsyncEngine) -> AsyncIterator[TripFactory]:
    f = TripFactory(migrated_engine)
    try:
        yield f
    finally:
        await f.cleanup()


async def public_trip_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                select(func.count())
                .select_from(tables.trips)
                .where(tables.trips.c.visibility == "public")
            )
        ).scalar_one()


def ago(hours: float) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


def parse_ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


async def get(client: AsyncClient, **kwargs: Any) -> httpx.Response:
    """One request with an empty cookie jar: only a cookie passed in headers is sent."""
    client.cookies.clear()
    response = await client.get(LIST, **kwargs)
    client.cookies.clear()
    return response


async def walk(
    client: AsyncClient, *, limit: int | None = None, headers: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Every page, following ``nextCursor`` to the end. Each page is the parsed body."""
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(1000):
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        response = await get(client, params=params, headers=headers or {})
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append(body)
        cursor = body["nextCursor"]
        if cursor is None:
            return pages
    raise AssertionError("the cursor never ended")


def ids_of(pages: list[dict[str, Any]]) -> list[str]:
    return [item["id"] for page in pages for item in page["items"]]


def mine(pages: list[dict[str, Any]], factory: TripFactory) -> list[dict[str, Any]]:
    wanted = set(factory.trip_ids)
    return [item for page in pages for item in page["items"] if item["id"] in wanted]


def assert_validation_error(response: httpx.Response) -> None:
    assert response.status_code == 422, (response.status_code, response.text)
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
    """The contract's order as a Python key: lastPublicStopAt desc, nulls last, id asc."""
    ts = parse_ts(item["lastPublicStopAt"])
    return (1, 0.0, item["id"]) if ts is None else (0, -ts.timestamp(), item["id"])


# --------------------------------------------------------------------------
# Sort order and paging
# --------------------------------------------------------------------------


async def test_sort_order_desc_nulls_last_ties_by_id(
    client: AsyncClient, factory: TripFactory
) -> None:
    tie = ago(5)
    # Inserted in scrambled order so insertion order cannot pass for sort order.
    await factory.trip("e-null", stops=[])
    await factory.trip("c-tie", stops=[tie])
    await factory.trip("a-old", stops=[ago(30)])
    await factory.trip("b-null", stops=[])
    await factory.trip("d-tie", stops=[tie])
    await factory.trip("f-new", stops=[ago(1)])
    await factory.trip("b-tie", stops=[tie, ago(50)])

    pages = await walk(client)
    got = [item["id"] for item in mine(pages, factory)]
    expected = [
        factory.tid(s) for s in ("f-new", "b-tie", "c-tie", "d-tie", "a-old", "b-null", "e-null")
    ]
    assert got == expected
    # And the whole listing, not just this test's rows, obeys the contract's order.
    all_items = [item for page in pages for item in page["items"]]
    assert all_items == sorted(all_items, key=sort_key)


async def test_paging_limit_2_three_pages_no_duplicates_no_gaps(
    client: AsyncClient, factory: TripFactory, migrated_engine: AsyncEngine
) -> None:
    # Five public trips, including a null and a timestamp tie across a page boundary.
    tie = ago(3)
    await factory.trip("a", stops=[ago(1)])
    await factory.trip("b", stops=[tie])
    await factory.trip("c", stops=[tie])
    await factory.trip("d", stops=[ago(10)])
    await factory.trip("e", stops=[])
    await factory.trip("p", visibility="private", stops=[ago(2)])

    total = await public_trip_count(migrated_engine)
    paged = await walk(client, limit=2)
    whole = await walk(client, limit=50)

    assert len(paged) == (total + 1) // 2  # three pages when only these five exist
    assert all(len(page["items"]) == 2 for page in paged[:-1])
    assert 1 <= len(paged[-1]["items"]) <= 2
    assert paged[-1]["nextCursor"] is None
    assert all(isinstance(page["nextCursor"], str) for page in paged[:-1])

    paged_ids = ids_of(paged)
    assert len(paged_ids) == len(set(paged_ids)) == total  # no duplicates, no gaps
    assert paged_ids == ids_of(whole)
    assert [i["id"] for i in mine(paged, factory)] == [factory.tid(s) for s in "abcde"]


async def test_paging_with_only_this_tests_trips_is_exactly_three_pages(
    client: AsyncClient, factory: TripFactory, migrated_engine: AsyncEngine
) -> None:
    """The literal AC, where the table allows it: 5 public trips, limit=2 -> 2, 2, 1."""
    if await public_trip_count(migrated_engine) != 0:
        pytest.skip("other public trips exist; covered relatively by the test above")
    for i, suffix in enumerate("abcde"):
        await factory.trip(suffix, stops=[ago(1 + i)] if suffix != "e" else [])
    pages = await walk(client, limit=2)
    assert [len(p["items"]) for p in pages] == [2, 2, 1]
    assert [p["nextCursor"] is None for p in pages] == [False, False, True]
    assert ids_of(pages) == [factory.tid(s) for s in "abcde"]


async def test_limit_defaults_to_20(
    client: AsyncClient, factory: TripFactory, migrated_engine: AsyncEngine
) -> None:
    for i in range(21):
        await factory.trip(f"{i:02d}", stops=[ago(1 + i)])
    total = await public_trip_count(migrated_engine)
    assert total >= 21

    response = await get(client)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 20
    assert body["nextCursor"] is not None

    # Walking the default pages reaches everything.
    assert len(ids_of(await walk(client))) == total


@pytest.mark.parametrize("limit", [1, 50])
async def test_limit_bounds_are_accepted(
    client: AsyncClient, factory: TripFactory, limit: int
) -> None:
    await factory.trip("a", stops=[ago(1)])
    await factory.trip("b", stops=[ago(2)])
    response = await get(client, params={"limit": limit})
    assert response.status_code == 200
    assert 1 <= len(response.json()["items"]) <= limit


# --------------------------------------------------------------------------
# 422s
# --------------------------------------------------------------------------


@pytest.mark.parametrize("limit", ["0", "51", "-1", "abc", "2.5"])
async def test_limit_out_of_range_or_not_an_integer_is_422(client: AsyncClient, limit: str) -> None:
    assert_validation_error(await get(client, params={"limit": limit}))


@pytest.mark.parametrize(
    "cursor",
    [
        "garbage!!not-base64",
        "",
        "%%%",
        b64url(b"not a sort key"),
        b64url(b"\xff\xfe\x00binary"),
        b64url(b"{}"),
        b64url(b"[]"),
        b64url(json.dumps({"t": "yesterday", "id": "x"}).encode()),
        # Pass the shape checks, but once reached Postgres and failed there as a 500:
        "W251bGwsIlx1ZDgwMCJd",  # [null,"\ud800"]: lone-surrogate id
        "WyI5OTk5LTEyLTMxVDIzOjU5OjU5LTIzOjU5IiwiYSJd",  # UTC instant past year 9999
        "WyIwMDAxLTAxLTAxVDAwOjAwOjAwKzIzOjU5IiwiYSJd",  # UTC instant before year 1
    ],
    ids=[
        "garbage",
        "empty",
        "percent",
        "b64-text",
        "b64-binary",
        "b64-empty-obj",
        "b64-list",
        "b64-bad-ts",
        "lone-surrogate-id",
        "instant-overflows-9999",
        "instant-underflows-year-1",
    ],
)
async def test_malformed_cursor_is_422(client: AsyncClient, cursor: str) -> None:
    assert_validation_error(await get(client, params={"cursor": cursor}))


async def test_validly_encoded_but_edited_cursor_is_422(
    client: AsyncClient, factory: TripFactory
) -> None:
    for i, suffix in enumerate("abc"):
        await factory.trip(suffix, stops=[ago(1 + i)])
    first = await get(client, params={"limit": 1})
    cursor = first.json()["nextCursor"]
    assert cursor
    raw = b64url_decode(cursor)

    edits = [
        raw[: len(raw) // 2],  # truncated
        raw + b"trailing",  # extended
        raw.replace(b"2", b"x", 1) if b"2" in raw else raw[::-1],  # digits of the timestamp
    ]
    for edited in edits:
        assert edited != raw
        assert_validation_error(await get(client, params={"cursor": b64url(edited)}))


# --------------------------------------------------------------------------
# Private trips never appear
# --------------------------------------------------------------------------


async def test_private_trip_never_appears_even_when_it_would_sort_first(
    client: AsyncClient, factory: TripFactory
) -> None:
    newest = ago(0.1)
    private = await factory.trip("0-private", visibility="private", stops=[newest])
    private_null = await factory.trip("0-private-null", visibility="private", stops=[])
    await factory.trip("1-public", stops=[ago(2)])
    await factory.trip("2-public-null", stops=[])

    for limit in (None, 1, 2, 50):
        pages = await walk(client, limit=limit)
        ids = ids_of(pages)
        assert private.id not in ids and private_null.id not in ids
        assert [i["id"] for i in mine(pages, factory)] == [
            factory.tid("1-public"),
            factory.tid("2-public-null"),
        ]
        for page in pages:
            assert private.name not in json.dumps(page)


async def test_private_trip_invisible_to_its_own_members(
    client: AsyncClient, factory: TripFactory, migrated_engine: AsyncEngine
) -> None:
    private = await factory.trip("0-private", visibility="private", stops=[ago(0.1)])
    await factory.trip("1-public", stops=[ago(2)])
    leader = await factory.account()
    rider = await factory.account()
    await grant_membership(migrated_engine, private.id, leader.user_id, role="leader")
    await grant_membership(migrated_engine, private.id, rider.user_id, role="rider")

    for account in (leader, rider):
        pages = await walk(client, limit=1, headers=account.headers)
        assert private.id not in ids_of(pages)
        assert factory.tid("1-public") in ids_of(pages)


async def test_hand_built_cursor_at_a_private_trips_sort_key_never_yields_it(
    client: AsyncClient, factory: TripFactory
) -> None:
    """
    Implementation-following for the cursor's inner shape: a cursor is taken from
    a real response and its sort key rewritten to sit on, and just before, the
    private trip's own (timestamp, id). Whatever the server does with it, the
    private trip must not come back.
    """
    at = ago(4)
    await factory.trip("a-public", stops=[ago(1)])
    private = await factory.trip("m-private", visibility="private", stops=[at])
    await factory.trip("z-public", stops=[at])  # ties with the private trip, sorts after it
    await factory.trip("n-public-null", stops=[])

    real = (await get(client, params={"limit": 1})).json()["nextCursor"]
    assert real
    cursors = forge_cursors(real, at, private.id)
    assert cursors, "could not build a cursor from the real one"

    accepted = 0
    for cursor in cursors:
        response = await get(client, params={"cursor": cursor, "limit": 2})
        if response.status_code == 422:
            continue
        accepted += 1
        assert response.status_code == 200, response.text
        # Follow the whole tail from the forged position.
        seen = [i["id"] for i in response.json()["items"]]
        nxt = response.json()["nextCursor"]
        while nxt:
            page = (await get(client, params={"cursor": nxt, "limit": 2})).json()
            seen += [i["id"] for i in page["items"]]
            nxt = page["nextCursor"]
        assert private.id not in seen, cursor
        # Positioned on the private trip's own key, the public tie after it still comes back
        # (a null-tail position is past every timestamped trip, so only the null one follows).
        if json.loads(b64url_decode(cursor))[0] is not None:
            assert factory.tid("z-public") in seen, cursor
        assert factory.tid("n-public-null") in seen, cursor
    # Not vacuous: the forged positions were real cursors the server accepted.
    assert accepted == len(cursors)


def forge_cursors(real: str, at: datetime, private_id: str) -> list[str]:
    """
    Cursors pointing at/just before ``(at, private_id)``, in the real cursor's own encoding.

    Implementation-following: supports a JSON object or list, or a
    ``<timestamp><sep><id>`` string. Returns [] if the real cursor is none of those.
    """
    try:
        raw = b64url_decode(real).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return []
    before_id = private_id[:-1]  # sorts just before the private id
    keys: list[tuple[str | None, str]] = [
        (at.isoformat(), private_id),
        (at.isoformat(), before_id),
        ((at + timedelta(microseconds=1)).isoformat(), private_id),
        (None, private_id),
    ]
    out: list[str] = []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = None
    for ts, tid in keys:
        if isinstance(decoded, list) and len(decoded) == 2:
            out.append(b64url(json.dumps([ts, tid], separators=(",", ":")).encode()))
        elif isinstance(decoded, dict) and len(decoded) == 2:
            ts_key, id_key = _dict_keys(decoded)
            if ts_key and id_key:
                out.append(
                    b64url(json.dumps({ts_key: ts, id_key: tid}, separators=(",", ":")).encode())
                )
        elif decoded is None:
            for sep in ("|", "\n", ",", " "):
                if sep in raw:
                    out.append(b64url(f"{ts or ''}{sep}{tid}".encode()))
                    break
    return out


def _dict_keys(decoded: dict[str, Any]) -> tuple[str | None, str | None]:
    ts_key = id_key = None
    for key, value in decoded.items():
        if isinstance(value, str) and value.startswith("tl-"):
            id_key = key
        else:
            ts_key = key
    return ts_key, id_key


# --------------------------------------------------------------------------
# Public delay and lastPublicStopAt
# --------------------------------------------------------------------------


async def test_public_delay_0_vs_24_and_hidden_only_stop_gives_null(
    client: AsyncClient, factory: TripFactory, migrated_engine: AsyncEngine
) -> None:
    one_hour = ago(1)
    two_days = ago(48)
    no_delay = await factory.trip("a-delay0", delay_hours=0, stops=[one_hour])
    hidden = await factory.trip("b-delay24-hidden", delay_hours=24, stops=[one_hour])
    mixed = await factory.trip("c-delay24-mixed", delay_hours=24, stops=[one_hour, two_days])

    # A member of every trip sees the same public value: the delay is not lifted.
    member = await factory.account()
    for trip in (no_delay, hidden, mixed):
        await grant_membership(migrated_engine, trip.id, member.user_id, role="leader")

    for headers in ({}, member.headers):
        items = {i["id"]: i for i in mine(await walk(client, headers=headers), factory)}
        assert set(items) == {no_delay.id, hidden.id, mixed.id}
        assert parse_ts(items[no_delay.id]["lastPublicStopAt"]) == one_hour
        assert items[hidden.id]["lastPublicStopAt"] is None
        assert parse_ts(items[mixed.id]["lastPublicStopAt"]) == two_days
        # Hidden-only trip sorts with the nulls, after the 48h-old one.
        order = [i["id"] for i in mine(await walk(client, headers=headers), factory)]
        assert order == [no_delay.id, mixed.id, hidden.id]


async def test_last_public_stop_at_is_timezone_aware(
    client: AsyncClient, factory: TripFactory
) -> None:
    await factory.trip("a", stops=[ago(2)])
    [item] = mine(await walk(client), factory)
    ts = parse_ts(item["lastPublicStopAt"])
    assert ts is not None and ts.tzinfo is not None


# --------------------------------------------------------------------------
# riderCount
# --------------------------------------------------------------------------


async def test_rider_count_includes_leaders_and_excludes_revoked(
    client: AsyncClient, factory: TripFactory, migrated_engine: AsyncEngine
) -> None:
    trip = await factory.trip("a", stops=[ago(1)])
    empty = await factory.trip("b", stops=[ago(2)])
    leader, rider, revoked_rider, revoked_leader = [await factory.account() for _ in range(4)]
    await grant_membership(migrated_engine, trip.id, leader.user_id, role="leader")
    await grant_membership(migrated_engine, trip.id, rider.user_id, role="rider")
    await grant_membership(migrated_engine, trip.id, revoked_rider.user_id, revoked=True)
    await grant_membership(
        migrated_engine, trip.id, revoked_leader.user_id, role="leader", revoked=True
    )
    # A revoked-then-rejoined user counts once.
    await grant_membership(migrated_engine, empty.id, rider.user_id, revoked=True)
    await grant_membership(migrated_engine, empty.id, rider.user_id)

    items = {i["id"]: i for i in mine(await walk(client), factory)}
    assert items[trip.id]["riderCount"] == 2
    assert items[empty.id]["riderCount"] == 1


async def test_rider_count_zero_with_no_members(client: AsyncClient, factory: TripFactory) -> None:
    await factory.trip("a", stops=[])
    [item] = mine(await walk(client), factory)
    assert item["riderCount"] == 0


# --------------------------------------------------------------------------
# Same for every caller; shape; no slugs
# --------------------------------------------------------------------------


async def test_session_is_ignored_member_and_anonymous_get_identical_bytes(
    client: AsyncClient, factory: TripFactory, migrated_engine: AsyncEngine
) -> None:
    a = await factory.trip("a", delay_hours=24, stops=[ago(1), ago(30)])
    b = await factory.trip("b", stops=[])
    await factory.trip("c", visibility="private", stops=[ago(0.1)])
    member = await factory.account()
    await grant_membership(migrated_engine, a.id, member.user_id, role="leader")
    await grant_membership(migrated_engine, b.id, member.user_id)
    garbage = {"Cookie": f"__Host-btj_session={secrets.token_urlsafe(32)}"}

    for params in ({}, {"limit": 1}):
        anon = await get(client, params=params)
        signed_in = await get(client, params=params, headers=member.headers)
        bad_cookie = await get(client, params=params, headers=garbage)
        assert anon.status_code == signed_in.status_code == bad_cookie.status_code == 200
        assert anon.content == signed_in.content == bad_cookie.content
        assert "set-cookie" not in bad_cookie.headers


async def test_response_shape_matches_trip_page_out_and_contains_no_slug(
    client: AsyncClient, factory: TripFactory
) -> None:
    trips = [
        await factory.trip("a", stops=[ago(1)]),
        await factory.trip("b", stops=[]),
        await factory.trip("c", visibility="private", stops=[ago(1)]),
    ]
    pages = await walk(client, limit=1)
    for page in pages:
        assert set(page) == PAGE_KEYS
        TripPageOut.model_validate(page)
        for item in page["items"]:
            assert set(item) == SUMMARY_KEYS
            assert isinstance(item["riderCount"], int)
            date.fromisoformat(item["startDate"])
        text = json.dumps(page)
        for trip in trips:
            assert trip.rider_slug not in text
            assert trip.viewer_slug not in text
        for word in ("slug", "Slug", "visibility", "publicDelayHours", "viewer"):
            assert word not in text

    [a, b] = mine(pages, factory)
    assert a == {
        "id": trips[0].id,
        "name": trips[0].name,
        "startDate": "2026-06-01",
        "riderCount": 0,
        "lastPublicStopAt": a["lastPublicStopAt"],
    }
    assert a["lastPublicStopAt"] is not None
    assert b["lastPublicStopAt"] is None


# --------------------------------------------------------------------------
# HEAD and rate limit
# --------------------------------------------------------------------------


async def test_head_is_200_with_empty_body(client: AsyncClient, factory: TripFactory) -> None:
    await factory.trip("a", stops=[ago(1)])
    client.cookies.clear()
    response = await client.head(LIST)
    assert response.status_code == 200
    assert response.content == b""
    head_limit = await client.head(LIST, params={"limit": 1})
    assert head_limit.status_code == 200 and head_limit.content == b""


@pytest.fixture
def clock() -> FrozenClock:
    frozen = FrozenClock()
    ratelimit.registry.clock = frozen
    return frozen


def assert_rate_limited(response: httpx.Response) -> None:
    assert response.status_code == 429, (response.status_code, response.text)
    header = response.headers.get("retry-after")
    assert header is not None and header.isdigit() and int(header) >= 1
    if response.request.method != "HEAD":
        assert response.json() == {"error": {"code": "RATE_LIMITED", "message": LIMITER_MESSAGE}}
    else:
        assert response.content == b""


@pytest.mark.parametrize("drain_with", ["GET", "HEAD"])
async def test_public_read_exhausted_gives_429_on_get_and_head(
    client: AsyncClient, clock: FrozenClock, drain_with: str
) -> None:
    ip = {"X-Forwarded-For": f"198.19.{secrets.randbelow(256)}.{secrets.randbelow(256)}"}
    for i in range(120):
        response = await client.request(drain_with, LIST, headers=ip)
        assert response.status_code == 200, f"request {i + 1} was {response.status_code}"

    assert_rate_limited(await client.get(LIST, headers=ip))
    assert_rate_limited(await client.head(LIST, headers=ip))
    # A bad query still hits the limiter first: no token, no validation.
    assert_rate_limited(await client.get(LIST, params={"limit": 0}, headers=ip))

    # Another IP is unaffected; after a refill this one reads again.
    other = {"X-Forwarded-For": "198.19.255.254"}
    assert (await client.get(LIST, headers=other)).status_code == 200
    clock.advance(1)
    assert (await client.get(LIST, headers=ip)).status_code == 200


def test_openapi_declares_get_and_head_429() -> None:
    doc = app.main.app.openapi()
    operations = doc["paths"][LIST]
    assert "get" in operations
    assert "429" in operations["get"]["responses"]
    assert "422" in operations["get"]["responses"]
    # HEAD is a schema-excluded sibling (decision-log Entry 11), so its 429 is
    # enforced by the limiter audit, not the document.
    assert "head" not in operations
