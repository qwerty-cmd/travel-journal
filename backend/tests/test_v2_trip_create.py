"""
Trip creation, trip settings and "your trips" (task ``t-am-trip-create``).

Written from ``docs/api-contract.md`` and the task's acceptance criteria before
the implementation was read:

- "Notes per endpoint (v2)", ``POST /api/v2/trips``: one transaction stores the
  trip (both slugs ``NULL``, ``created_by`` = caller, ``public_delay_hours`` 24,
  ``visibility`` default ``public``) and the caller's ``leader`` membership;
  ``201`` with ``viewer.role = "leader"``.
- "Idempotency: additions": the parent of a trip id is the creating user. A
  replay is ``200`` only if the caller created it **and** is still an active
  member; any other existing id is ``409``. The id must be a canonical
  lowercase UUID, else ``422``.
- "Rate limits": ``trip-create`` is 3/day per user plus a lifetime cap of 20
  trips created (``409``). A replay spends no token.
- ``PATCH /api/v2/trips/{tripId}``: omit = no change, explicit ``null`` = 422,
  empty body = 200, ``publicDelayHours`` 0-168.
- ``GET /api/v2/me/trips``: session only; active memberships with their role.

The PATCH identity x trip matrix cells (leader columns), the byte-identical
anonymous 401 across trip ids, the private-404 statement count and the
cross-trip PATCH live in ``test_access_matrix.py`` section 14 and are not
repeated here. Section 14 runs on legacy (slugged) trips; the one gate test
below runs the same refusals on an app-created, slugless trip.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from conftest import (
    SignedInAccount,
    create_signed_in_account,
    delete_accounts,
    make_async_client,
)
from httpx import AsyncClient, Response
from sqlalchemy import event, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core import ratelimit
from app.data import tables
from app.data.db import get_session

# Contract / AC literals, never the implementation's constants.
NOT_A_LEADER = "You're not a leader on this trip."
NOT_A_RIDER = "You're not a rider on this trip."
NO_LONGER_A_RIDER = "You're no longer a rider on this trip."
CSRF_MESSAGE = "This request came from another site and was blocked."
WWW_AUTHENTICATE = 'Cookie realm="bike-trip-journal"'
TRIP_CREATE_PER_DAY = 3
TRIP_CREATE_RETRY_AFTER = 86_400 // TRIP_CREATE_PER_DAY  # one token every 8 h
LIFETIME_CAP = 20
CANONICAL = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CROSS_SITE = [{"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example"}]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@dataclass
class Harness:
    """Makes accounts and trips, and removes every one of them on teardown."""

    engine: AsyncEngine
    user_ids: list[str] = field(default_factory=list)
    trip_ids: list[str] = field(default_factory=list)

    async def account(self, name: str = "Trip Creator") -> SignedInAccount:
        account = await create_signed_in_account(self.engine, display_name=name)
        self.user_ids.append(account.user_id)
        return account

    def track(self, trip_id: str) -> str:
        self.trip_ids.append(trip_id)
        return trip_id

    async def seed_trip(
        self,
        *,
        created_by: str | None = None,
        visibility: str = "public",
        slugs: bool = False,
        name: str = "Seeded trip",
    ) -> str:
        """A trip row straight into the table, with a canonical UUID id."""
        trip_id = self.track(str(uuid4()))
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.trips.insert().values(
                    id=trip_id,
                    name=name,
                    start_date=date(2026, 7, 1),
                    rider_slug=f"r-{uuid4()}" if slugs else None,
                    viewer_slug=f"v-{uuid4()}" if slugs else None,
                    visibility=visibility,
                    created_by=created_by,
                )
            )
        return trip_id

    async def member(
        self,
        trip_id: str,
        user_id: str,
        *,
        role: str = "rider",
        joined_at: datetime | None = None,
        revoked_by: str | None = None,
        revoked: bool = False,
    ) -> None:
        values: dict[str, Any] = {
            "id": str(uuid4()),
            "trip_id": trip_id,
            "user_id": user_id,
            "role": role,
        }
        if joined_at is not None:
            values["joined_at"] = joined_at
        if revoked or revoked_by is not None:
            values["revoked_at"] = datetime.now(UTC)
            values["revoked_by"] = revoked_by
        async with self.engine.begin() as conn:
            await conn.execute(tables.trip_members.insert().values(**values))


@pytest.fixture
async def harness(migrated_engine: AsyncEngine) -> AsyncIterator[Harness]:
    h = Harness(migrated_engine)
    try:
        yield h
    finally:
        # Trips first (members, requests and stops cascade), while created_by
        # still points at our accounts; then the accounts.
        async with migrated_engine.begin() as conn:
            await conn.execute(
                tables.trips.delete().where(
                    or_(
                        tables.trips.c.id.in_(h.trip_ids),
                        tables.trips.c.created_by.in_(h.user_ids),
                    )
                )
            )
        await delete_accounts(migrated_engine, h.user_ids)


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


class DayPerCall:
    """A clock that jumps a day every time it is read: every bucket is always full."""

    def __init__(self) -> None:
        self.now = 50_000.0

    def __call__(self) -> float:
        self.now += 86_400
        return self.now


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def body(trip_id: str | None = None, **overrides: Any) -> dict[str, Any]:
    return {
        "id": str(uuid4()) if trip_id is None else trip_id,
        "name": "Stuart Hwy",
        "startDate": "2026-07-01",
        **overrides,
    }


async def create(
    client: AsyncClient,
    harness: Harness,
    headers: dict[str, str],
    trip_id: str | None = None,
    **overrides: Any,
) -> Response:
    payload = body(trip_id, **overrides)
    if isinstance(payload.get("id"), str) and re.fullmatch(CANONICAL, payload["id"]):
        harness.track(payload["id"])
    return await client.post("/api/v2/trips", json=payload, headers=headers)


async def patch(
    client: AsyncClient, trip_id: str, headers: dict[str, str], payload: Any
) -> Response:
    return await client.patch(f"/api/v2/trips/{trip_id}", json=payload, headers=headers)


async def trip_row(engine: AsyncEngine, trip_id: str) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(tables.trips).where(tables.trips.c.id == trip_id))
        ).one_or_none()
    return None if row is None else dict(row._mapping)


async def member_rows(engine: AsyncEngine, trip_id: str) -> set[tuple[Any, ...]]:
    """Every membership row on the trip, history included."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(tables.trip_members).where(tables.trip_members.c.trip_id == trip_id)
        )
        return {tuple(r) for r in rows}


async def trips_created_by(engine: AsyncEngine, user_id: str) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                select(func.count())
                .select_from(tables.trips)
                .where(tables.trips.c.created_by == user_id)
            )
        ).scalar_one()


async def memberships_of(engine: AsyncEngine, user_id: str) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                select(func.count())
                .select_from(tables.trip_members)
                .where(tables.trip_members.c.user_id == user_id)
            )
        ).scalar_one()


def without_date(response: Response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k != "date"}


def assert_error(response: Response, status: int, code: str, message: str | None = None) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code, response.text
    if message is not None:
        assert error["message"] == message


def assert_401(response: Response) -> None:
    assert_error(response, 401, "UNAUTHENTICATED")
    assert response.headers.get("www-authenticate") == WWW_AUTHENTICATE


# --------------------------------------------------------------------------
# 1. Create: 201, the stored rows, one transaction
# --------------------------------------------------------------------------


async def test_create_is_201_leader_public_with_the_contract_defaults(
    client: AsyncClient, migrated_engine: AsyncEngine, harness: Harness
) -> None:
    me = await harness.account()
    trip_id = str(uuid4())

    response = await create(client, harness, me.headers, trip_id, name="  Stuart Hwy  ")

    assert response.status_code == 201, response.text
    out = response.json()
    assert out["id"] == trip_id
    assert out["name"] == "Stuart Hwy"
    assert out["startDate"] == "2026-07-01"
    assert out["viewer"] == {"role": "leader"}
    assert out["access"] == "rider"
    assert out["visibility"] == "public"
    assert out["publicDelayHours"] == 24
    assert out["riderCount"] == 1
    assert out["lastPublicStopAt"] is None
    assert out["bikes"] == []
    assert not any("slug" in key.lower() for key in out), out

    row = await trip_row(migrated_engine, trip_id)
    assert row is not None
    assert row["rider_slug"] is None and row["viewer_slug"] is None
    assert row["created_by"] == me.user_id
    assert row["visibility"] == "public"
    assert row["public_delay_hours"] == 24
    assert row["name"] == "Stuart Hwy"
    assert row["start_date"] == date(2026, 7, 1)

    members = await member_rows(migrated_engine, trip_id)
    assert len(members) == 1
    (member,) = members
    columns = [c.name for c in tables.trip_members.columns]
    member_by_name = dict(zip(columns, member, strict=True))
    assert member_by_name["user_id"] == me.user_id
    assert member_by_name["role"] == "leader"
    assert member_by_name["revoked_at"] is None


async def test_create_private_is_hidden_from_the_public_and_open_to_the_creator(
    client: AsyncClient, harness: Harness
) -> None:
    me = await harness.account()
    trip_id = str(uuid4())

    response = await create(client, harness, me.headers, trip_id, visibility="private")
    assert response.status_code == 201, response.text
    assert response.json()["visibility"] == "private"

    anonymous = await client.get(f"/api/v2/trips/{trip_id}")
    missing = await client.get(f"/api/v2/trips/{uuid4()}")
    assert_error(anonymous, 404, "NOT_FOUND")
    assert anonymous.content == missing.content

    own = await client.get(f"/api/v2/trips/{trip_id}", headers=me.headers)
    assert own.status_code == 200, own.text
    assert own.json()["viewer"] == {"role": "leader"}


@pytest.mark.parametrize(
    "name", ["", "   ", "x" * 101, None], ids=["empty", "blank", "101", "null"]
)
async def test_an_invalid_name_is_422_and_creates_nothing(
    client: AsyncClient, migrated_engine: AsyncEngine, harness: Harness, name: str | None
) -> None:
    me = await harness.account()
    trip_id = str(uuid4())
    response = await create(client, harness, me.headers, trip_id, name=name)
    assert_error(response, 422, "VALIDATION_ERROR")
    assert await trip_row(migrated_engine, trip_id) is None


async def test_a_100_character_name_is_accepted(client: AsyncClient, harness: Harness) -> None:
    me = await harness.account()
    response = await create(client, harness, me.headers, name="x" * 100)
    assert response.status_code == 201, response.text


async def test_create_is_one_transaction_a_failed_membership_leaves_no_trip(
    client: AsyncClient, migrated_engine: AsyncEngine, harness: Harness
) -> None:
    """If the leader membership can't be written, the trip must not be left behind."""
    me = await harness.account()
    trip_id = str(uuid4())
    seen: list[str] = []

    def fail_membership(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(statement)
        if statement.lstrip().upper().startswith("INSERT INTO TRIP_MEMBERS"):
            raise RuntimeError("injected membership failure")

    event.listen(migrated_engine.sync_engine, "before_cursor_execute", fail_membership)
    try:
        response = await create(client, harness, me.headers, trip_id)
    finally:
        event.remove(migrated_engine.sync_engine, "before_cursor_execute", fail_membership)

    assert any(s.lstrip().upper().startswith("INSERT INTO TRIPS") for s in seen), (
        "the trip insert never ran -- this check would be vacuous"
    )
    assert_error(response, 500, "INTERNAL_ERROR")
    assert await trip_row(migrated_engine, trip_id) is None
    assert await member_rows(migrated_engine, trip_id) == set()


# --------------------------------------------------------------------------
# 2. Replay and the 409 branch
# --------------------------------------------------------------------------


async def test_a_replay_by_the_creator_is_200_with_the_stored_trip_even_if_the_body_differs(
    client: AsyncClient, migrated_engine: AsyncEngine, harness: Harness
) -> None:
    me = await harness.account()
    trip_id = str(uuid4())
    first = await create(client, harness, me.headers, trip_id)
    assert first.status_code == 201, first.text
    row_before = await trip_row(migrated_engine, trip_id)
    members_before = await member_rows(migrated_engine, trip_id)

    replay = await create(
        client,
        harness,
        me.headers,
        trip_id,
        name="Something else",
        startDate="2027-01-01",
        visibility="private",
    )

    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert await trip_row(migrated_engine, trip_id) == row_before
    assert await member_rows(migrated_engine, trip_id) == members_before


async def test_a_replay_by_a_creator_who_stepped_down_but_is_still_active_is_200(
    client: AsyncClient, migrated_engine: AsyncEngine, harness: Harness
) -> None:
    """Contract: created_by is the caller **and** still an active member. Role doesn't matter."""
    me = await harness.account()
    trip_id = str(uuid4())
    assert (await create(client, harness, me.headers, trip_id)).status_code == 201
    async with migrated_engine.begin() as conn:
        await conn.execute(
            update(tables.trip_members)
            .where(tables.trip_members.c.trip_id == trip_id)
            .values(role="rider")
        )
    members_before = await member_rows(migrated_engine, trip_id)

    replay = await create(client, harness, me.headers, trip_id)

    assert replay.status_code == 200, replay.text
    assert replay.json()["viewer"] == {"role": "rider"}
    assert await member_rows(migrated_engine, trip_id) == members_before


@pytest.mark.parametrize("case", ["other_users_id", "legacy_trip_id", "revoked", "left"])
async def test_an_id_that_is_not_the_callers_replay_is_409_and_changes_nothing(
    client: AsyncClient, migrated_engine: AsyncEngine, harness: Harness, case: str
) -> None:
    me = await harness.account("Caller")
    other = await harness.account("Someone Else")

    if case == "other_users_id":
        trip_id = str(uuid4())
        made = await create(client, harness, other.headers, trip_id, name="Secret Name")
        assert made.status_code == 201, made.text
    elif case == "legacy_trip_id":
        trip_id = await harness.seed_trip(slugs=True, visibility="private", name="Secret Name")
    else:
        trip_id = str(uuid4())
        made = await create(client, harness, me.headers, trip_id, name="Secret Name")
        assert made.status_code == 201, made.text
        # Revoked by another leader, or left (revoked_by = self).
        await harness.member(trip_id, other.user_id, role="leader")
        async with migrated_engine.begin() as conn:
            await conn.execute(
                update(tables.trip_members)
                .where(tables.trip_members.c.trip_id == trip_id)
                .where(tables.trip_members.c.user_id == me.user_id)
                .values(
                    revoked_at=datetime.now(UTC),
                    revoked_by=other.user_id if case == "revoked" else me.user_id,
                )
            )

    row_before = await trip_row(migrated_engine, trip_id)
    members_before = await member_rows(migrated_engine, trip_id)
    mine_before = await memberships_of(migrated_engine, me.user_id)

    response = await create(client, harness, me.headers, trip_id, name="Mine now")

    assert_error(response, 409, "CONFLICT")
    message = response.json()["error"]["message"]
    for leaked in (trip_id, "Secret Name", other.user_id, other.display_name):
        assert leaked not in message
    assert await trip_row(migrated_engine, trip_id) == row_before
    assert await member_rows(migrated_engine, trip_id) == members_before
    assert await memberships_of(migrated_engine, me.user_id) == mine_before

    # Fails identically on retry (contract, CONFLICT).
    again = await create(client, harness, me.headers, trip_id, name="Mine now")
    assert again.status_code == 409 and again.content == response.content


_CANONICAL = "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b"


@pytest.mark.parametrize(
    "bad_id",
    [
        "not-a-uuid",
        _CANONICAL.upper(),
        "{" + _CANONICAL + "}",
        _CANONICAL.replace("-", ""),
        "urn:uuid:" + _CANONICAL,
        _CANONICAL + "\n",
        " " + _CANONICAL,
        _CANONICAL[:-1] + "\x00",
        "",
        12345,
    ],
    ids=[
        "non-uuid",
        "uppercase",
        "braced",
        "unhyphenated",
        "urn",
        "trailing-newline",
        "leading-space",
        "nul",
        "empty",
        "number",
    ],
)
async def test_a_non_canonical_id_is_422_and_creates_nothing(
    client: AsyncClient, migrated_engine: AsyncEngine, harness: Harness, bad_id: Any
) -> None:
    me = await harness.account()
    response = await create(client, harness, me.headers, bad_id)
    assert_error(response, 422, "VALIDATION_ERROR")
    assert await trips_created_by(migrated_engine, me.user_id) == 0
    assert await trip_row(migrated_engine, _CANONICAL) is None
    assert await memberships_of(migrated_engine, me.user_id) == 0


async def test_a_missing_id_is_422(client: AsyncClient, harness: Harness) -> None:
    me = await harness.account()
    response = await client.post(
        "/api/v2/trips", json={"name": "No id", "startDate": "2026-07-01"}, headers=me.headers
    )
    assert_error(response, 422, "VALIDATION_ERROR")


# --------------------------------------------------------------------------
# 3. Session and CSRF
# --------------------------------------------------------------------------


async def test_anonymous_create_is_401_and_creates_nothing(
    client: AsyncClient, migrated_engine: AsyncEngine, harness: Harness
) -> None:
    trip_id = str(uuid4())
    response = await create(client, harness, {}, trip_id)
    assert_401(response)
    assert await trip_row(migrated_engine, trip_id) is None


@pytest.mark.parametrize("cross_site", CROSS_SITE, ids=["sec-fetch-site", "origin"])
async def test_a_cross_site_create_is_403_and_creates_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    harness: Harness,
    cross_site: dict[str, str],
) -> None:
    me = await harness.account()
    trip_id = str(uuid4())
    response = await create(client, harness, {**me.headers, **cross_site}, trip_id)
    assert_error(response, 403, "FORBIDDEN", CSRF_MESSAGE)
    assert await trip_row(migrated_engine, trip_id) is None


@pytest.mark.parametrize("cross_site", CROSS_SITE, ids=["sec-fetch-site", "origin"])
async def test_a_cross_site_patch_is_403_and_changes_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    harness: Harness,
    cross_site: dict[str, str],
) -> None:
    me = await harness.account()
    trip_id = str(uuid4())
    assert (await create(client, harness, me.headers, trip_id)).status_code == 201
    before = await trip_row(migrated_engine, trip_id)
    response = await patch(client, trip_id, {**me.headers, **cross_site}, {"visibility": "private"})
    assert_error(response, 403, "FORBIDDEN", CSRF_MESSAGE)
    assert await trip_row(migrated_engine, trip_id) == before


# --------------------------------------------------------------------------
# 4. The trip-create bucket: 3 a day per user, a replay spends nothing
# --------------------------------------------------------------------------


async def test_the_fourth_create_in_a_day_is_429_and_a_replay_still_answers_200(
    client: AsyncClient, migrated_engine: AsyncEngine, harness: Harness
) -> None:
    clock = FrozenClock()
    ratelimit.registry.clock = clock
    me = await harness.account()
    ids = [str(uuid4()) for _ in range(TRIP_CREATE_PER_DAY)]
    for trip_id in ids:
        made = await create(client, harness, me.headers, trip_id)
        assert made.status_code == 201, made.text

    fourth_id = str(uuid4())
    limited = await create(client, harness, me.headers, fourth_id)
    assert_error(limited, 429, "RATE_LIMITED")
    assert limited.headers["retry-after"] == str(TRIP_CREATE_RETRY_AFTER)
    assert await trip_row(migrated_engine, fourth_id) is None

    # Bucket empty: a replay is still answered.
    replay = await create(client, harness, me.headers, ids[0])
    assert replay.status_code == 200, replay.text

    # Per user: someone else still has a full bucket.
    other = await harness.account("Other")
    assert (await create(client, harness, other.headers)).status_code == 201

    clock.now += TRIP_CREATE_RETRY_AFTER
    assert (await create(client, harness, me.headers, fourth_id)).status_code == 201
    assert (await create(client, harness, me.headers)).status_code == 429


async def test_a_replay_spends_no_token(client: AsyncClient, harness: Harness) -> None:
    ratelimit.registry.clock = FrozenClock()
    me = await harness.account()
    trip_id = str(uuid4())
    assert (await create(client, harness, me.headers, trip_id)).status_code == 201

    for _ in range(10):
        replay = await create(client, harness, me.headers, trip_id)
        assert replay.status_code == 200, replay.text

    # Still two tokens after ten replays.
    assert (await create(client, harness, me.headers)).status_code == 201
    assert (await create(client, harness, me.headers)).status_code == 201
    assert (await create(client, harness, me.headers)).status_code == 429


# Decided from the contract: "Rate limits" exempts **only** a replay from the
# trip-create bucket ("A replay spends no token"), so any other request by a
# signed-in user -- a 409 and a 422 included -- spends one. A 401 has no user
# for the per-user key, so it can't spend anyone's token.
@pytest.mark.parametrize("refusal", ["conflict_409", "invalid_422"])
async def test_contract_reading_a_refused_create_still_spends_a_token(
    client: AsyncClient, harness: Harness, refusal: str
) -> None:
    ratelimit.registry.clock = FrozenClock()
    me = await harness.account()
    other = await harness.account("Other")
    taken = str(uuid4())
    assert (await create(client, harness, other.headers, taken)).status_code == 201

    for _ in range(TRIP_CREATE_PER_DAY):
        if refusal == "conflict_409":
            refused = await create(client, harness, me.headers, taken)
            assert refused.status_code == 409, refused.text
        else:
            refused = await create(client, harness, me.headers, "NOT-A-UUID")
            assert refused.status_code == 422, refused.text

    limited = await create(client, harness, me.headers)
    assert_error(limited, 429, "RATE_LIMITED")


async def test_a_departed_creators_resend_is_not_a_replay_and_spends_a_token(
    client: AsyncClient, migrated_engine: AsyncEngine, harness: Harness
) -> None:
    """
    Contract, "Idempotency: additions": a replay needs created_by = caller **and** an
    active membership. A creator who has left is a 409, and a 409 is not a replay, so
    it spends from trip-create like any other refusal ("A replay spends no token").
    """
    ratelimit.registry.clock = FrozenClock()
    me = await harness.account()
    trip_id = str(uuid4())
    assert (await create(client, harness, me.headers, trip_id)).status_code == 201  # 2 left
    async with migrated_engine.begin() as conn:
        await conn.execute(
            update(tables.trip_members)
            .where(tables.trip_members.c.trip_id == trip_id)
            .values(revoked_at=datetime.now(UTC), revoked_by=me.user_id)
        )

    statuses = [(await create(client, harness, me.headers, trip_id)).status_code for _ in range(3)]

    assert statuses == [409, 409, 429]


async def test_contract_reading_anonymous_requests_spend_no_users_token(
    client: AsyncClient, harness: Harness
) -> None:
    """
    An anonymous request has no user to charge. The limiter's documented
    no-session fallback charges it to the client address instead (``ip:<addr>``,
    ``app/core/ratelimit.py``), so past 3 a day it is 429 rather than 401; that
    part is the fallback's, not asserted here beyond the first 3.
    """
    ratelimit.registry.clock = FrozenClock()
    statuses = [(await create(client, harness, {})).status_code for _ in range(5)]
    assert statuses[:TRIP_CREATE_PER_DAY] == [401] * TRIP_CREATE_PER_DAY
    assert set(statuses) <= {401, 429}
    me = await harness.account()
    for _ in range(TRIP_CREATE_PER_DAY):
        assert (await create(client, harness, me.headers)).status_code == 201


# --------------------------------------------------------------------------
# 5. The lifetime cap: 20 trips created
# --------------------------------------------------------------------------


async def seed_created(harness: Harness, account: SignedInAccount, count: int) -> list[str]:
    """``count`` trips created by ``account``; every other one it has since left."""
    ids = []
    for n in range(count):
        trip_id = await harness.seed_trip(created_by=account.user_id)
        await harness.member(
            trip_id,
            account.user_id,
            role="leader",
            revoked_by=account.user_id if n % 2 else None,
        )
        ids.append(trip_id)
    return ids


async def test_the_21st_lifetime_create_is_409_and_left_trips_still_count(
    client: AsyncClient, migrated_engine: AsyncEngine, harness: Harness
) -> None:
    ratelimit.registry.clock = DayPerCall()
    me = await harness.account()
    seeded = await seed_created(harness, me, LIFETIME_CAP)
    mine_before = await memberships_of(migrated_engine, me.user_id)

    trip_id = str(uuid4())
    response = await create(client, harness, me.headers, trip_id)

    assert_error(response, 409, "CONFLICT")
    assert trip_id not in response.json()["error"]["message"]
    assert await trip_row(migrated_engine, trip_id) is None
    assert await trips_created_by(migrated_engine, me.user_id) == LIFETIME_CAP
    assert await memberships_of(migrated_engine, me.user_id) == mine_before

    again = await create(client, harness, me.headers, trip_id)
    assert again.status_code == 409 and again.content == response.content

    # A replay at the cap is still a replay (seeded[0] is an active membership).
    replay = await create(client, harness, me.headers, seeded[0])
    assert replay.status_code == 200, replay.text


async def test_parallel_creates_at_19_give_exactly_one_201(
    client: AsyncClient, migrated_engine: AsyncEngine, harness: Harness
) -> None:
    ratelimit.registry.clock = DayPerCall()  # the daily bucket is not what's under test
    me = await harness.account()
    await seed_created(harness, me, LIFETIME_CAP - 1)
    ids = [str(uuid4()) for _ in range(6)]

    responses = await asyncio.gather(*(create(client, harness, me.headers, i) for i in ids))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201, 409, 409, 409, 409, 409], [r.text for r in responses]
    assert await trips_created_by(migrated_engine, me.user_id) == LIFETIME_CAP
    (winner,) = [i for i, r in zip(ids, responses, strict=True) if r.status_code == 201]
    for loser in set(ids) - {winner}:
        assert await trip_row(migrated_engine, loser) is None
        assert await member_rows(migrated_engine, loser) == set()

    replay = await create(client, harness, me.headers, winner)
    assert replay.status_code == 200, replay.text


# --------------------------------------------------------------------------
# 6. PATCH /api/v2/trips/{tripId}
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Made:
    leader: SignedInAccount
    trip_id: str


@pytest.fixture
async def made(client: AsyncClient, harness: Harness) -> Made:
    """A public trip created through the API: slugless, delay 24."""
    leader = await harness.account("Leader")
    trip_id = str(uuid4())
    response = await create(client, harness, leader.headers, trip_id)
    assert response.status_code == 201, response.text
    return Made(leader, trip_id)


SETTINGS = ("name", "visibility", "public_delay_hours")


async def settings(engine: AsyncEngine, trip_id: str) -> dict[str, Any]:
    row = await trip_row(engine, trip_id)
    assert row is not None
    return {k: row[k] for k in SETTINGS}


@pytest.mark.parametrize(
    ("payload", "column", "stored", "out_key"),
    [
        ({"name": "  New name  "}, "name", "New name", "name"),
        ({"visibility": "private"}, "visibility", "private", "visibility"),
        ({"publicDelayHours": 0}, "public_delay_hours", 0, "publicDelayHours"),
    ],
    ids=["name", "visibility", "publicDelayHours"],
)
async def test_each_field_alone_changes_only_itself(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    made: Made,
    payload: dict[str, Any],
    column: str,
    stored: Any,
    out_key: str,
) -> None:
    before = await settings(migrated_engine, made.trip_id)
    response = await patch(client, made.trip_id, made.leader.headers, payload)

    assert response.status_code == 200, response.text
    assert response.json()[out_key] == stored
    assert response.json()["viewer"] == {"role": "leader"}
    assert await settings(migrated_engine, made.trip_id) == {**before, column: stored}


async def test_an_empty_body_is_200_and_changes_nothing(
    client: AsyncClient, migrated_engine: AsyncEngine, made: Made
) -> None:
    before = await trip_row(migrated_engine, made.trip_id)
    response = await patch(client, made.trip_id, made.leader.headers, {})
    assert response.status_code == 200, response.text
    current = await client.get(f"/api/v2/trips/{made.trip_id}", headers=made.leader.headers)
    assert response.json() == current.json()
    assert await trip_row(migrated_engine, made.trip_id) == before


@pytest.mark.parametrize(
    "payload",
    [
        {"name": None},
        {"visibility": None},
        {"publicDelayHours": None},
        {"publicDelayHours": -1},
        {"publicDelayHours": 169},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 101},
        {"visibility": "secret"},
        # One bad field fails the whole body: the valid one is not applied.
        {"name": "Half applied", "publicDelayHours": 169},
    ],
    ids=[
        "null-name",
        "null-visibility",
        "null-delay",
        "delay--1",
        "delay-169",
        "empty-name",
        "blank-name",
        "name-101",
        "bad-visibility",
        "partly-valid",
    ],
)
async def test_an_invalid_patch_is_422_and_changes_nothing(
    client: AsyncClient, migrated_engine: AsyncEngine, made: Made, payload: dict[str, Any]
) -> None:
    before = await trip_row(migrated_engine, made.trip_id)
    response = await patch(client, made.trip_id, made.leader.headers, payload)
    assert_error(response, 422, "VALIDATION_ERROR")
    assert await trip_row(migrated_engine, made.trip_id) == before


@pytest.mark.parametrize("hours", [0, 168])
async def test_the_delay_bounds_are_inclusive(
    client: AsyncClient, migrated_engine: AsyncEngine, made: Made, hours: int
) -> None:
    response = await patch(client, made.trip_id, made.leader.headers, {"publicDelayHours": hours})
    assert response.status_code == 200, response.text
    assert response.json()["publicDelayHours"] == hours
    assert (await settings(migrated_engine, made.trip_id))["public_delay_hours"] == hours


async def test_going_private_takes_effect_on_the_next_public_read(
    client: AsyncClient, made: Made
) -> None:
    url = f"/api/v2/trips/{made.trip_id}"
    assert (await client.get(url)).status_code == 200

    assert (
        await patch(client, made.trip_id, made.leader.headers, {"visibility": "private"})
    ).status_code == 200
    hidden = await client.get(url)
    missing = await client.get(f"/api/v2/trips/{uuid4()}")
    assert_error(hidden, 404, "NOT_FOUND")
    assert hidden.content == missing.content
    assert without_date(hidden) == without_date(missing)
    assert (await client.get(f"{url}/stops")).status_code == 404

    assert (
        await patch(client, made.trip_id, made.leader.headers, {"visibility": "public"})
    ).status_code == 200
    assert (await client.get(url)).status_code == 200


async def test_the_delay_takes_effect_on_the_next_public_read(
    client: AsyncClient, migrated_engine: AsyncEngine, made: Made
) -> None:
    stop_id = str(uuid4())
    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.stops.insert().values(
                id=stop_id,
                trip_id=made.trip_id,
                name="An hour ago",
                lat=-14.46,
                lng=132.26,
                location_source="gps",
                arrived_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )

    async def public_stop_ids() -> list[str]:
        response = await client.get(f"/api/v2/trips/{made.trip_id}/stops")
        assert response.status_code == 200, response.text
        return [s["id"] for s in response.json()]

    assert await public_stop_ids() == []
    await patch(client, made.trip_id, made.leader.headers, {"publicDelayHours": 0})
    assert await public_stop_ids() == [stop_id]
    await patch(client, made.trip_id, made.leader.headers, {"publicDelayHours": 2})
    assert await public_stop_ids() == []


@pytest.mark.parametrize(
    ("identity", "visibility", "expected"),
    [
        ("rider", "public", (403, "FORBIDDEN", NOT_A_LEADER)),
        ("rider", "private", (403, "FORBIDDEN", NOT_A_LEADER)),
        ("revoked", "public", (403, "FORBIDDEN", NO_LONGER_A_RIDER)),
        ("revoked", "private", (403, "FORBIDDEN", NO_LONGER_A_RIDER)),
        ("revoked_leader", "private", (403, "FORBIDDEN", NO_LONGER_A_RIDER)),
        ("non_member", "public", (403, "FORBIDDEN", NOT_A_RIDER)),
        ("non_member", "private", (404, "NOT_FOUND", None)),
    ],
)
async def test_the_leader_gate_on_a_slugless_trip(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    harness: Harness,
    made: Made,
    identity: str,
    visibility: str,
    expected: tuple[int, str, str | None],
) -> None:
    """Section 14's refusals, on a trip created through the API (no slugs to locate it by)."""
    await patch(client, made.trip_id, made.leader.headers, {"visibility": visibility})
    caller = await harness.account(identity)
    if identity == "rider":
        await harness.member(made.trip_id, caller.user_id)
    elif identity == "revoked":
        await harness.member(made.trip_id, caller.user_id, revoked=True)
    elif identity == "revoked_leader":
        await harness.member(made.trip_id, caller.user_id, role="leader", revoked=True)
    before = await trip_row(migrated_engine, made.trip_id)

    response = await patch(client, made.trip_id, caller.headers, {"name": "Hijacked"})

    status, code, message = expected
    assert_error(response, status, code, message)
    if status == 404:
        missing = await patch(client, str(uuid4()), caller.headers, {"name": "Hijacked"})
        assert response.content == missing.content
        assert without_date(response) == without_date(missing)
    assert await trip_row(migrated_engine, made.trip_id) == before


# --------------------------------------------------------------------------
# 7. GET /api/v2/me/trips
# --------------------------------------------------------------------------


async def test_my_trips_needs_a_session(client: AsyncClient) -> None:
    get = await client.get("/api/v2/me/trips")
    head = await client.head("/api/v2/me/trips")
    assert_401(get)
    assert head.status_code == 401
    assert head.headers.get("www-authenticate") == WWW_AUTHENTICATE


async def test_my_trips_is_empty_for_an_account_with_none(
    client: AsyncClient, harness: Harness
) -> None:
    me = await harness.account()
    response = await client.get("/api/v2/me/trips", headers=me.headers)
    assert response.status_code == 200, response.text
    assert response.json() == []


async def test_my_trips_lists_active_memberships_with_role_newest_first(
    client: AsyncClient, migrated_engine: AsyncEngine, harness: Harness
) -> None:
    me = await harness.account("Me")
    other = await harness.account("Other")
    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    async def trip(name: str, visibility: str = "public") -> str:
        return await harness.seed_trip(created_by=other.user_id, visibility=visibility, name=name)

    leader_trip = await trip("Leader trip")
    rider_private = await trip("Rider private", "private")
    tie_trip = await trip("Tie trip")
    readmitted = await trip("Readmitted")
    revoked = await trip("Revoked")
    left = await trip("Left")
    pending = await trip("Pending")
    others_only = await trip("Others only")

    await harness.member(leader_trip, me.user_id, role="leader", joined_at=t0 + timedelta(days=2))
    await harness.member(rider_private, me.user_id, joined_at=t0 + timedelta(days=4))
    await harness.member(tie_trip, me.user_id, joined_at=t0 + timedelta(days=2))
    await harness.member(
        readmitted, me.user_id, joined_at=t0, revoked_by=other.user_id
    )  # history row
    await harness.member(readmitted, me.user_id, joined_at=t0 + timedelta(days=3))
    await harness.member(revoked, me.user_id, joined_at=t0 + timedelta(days=5), revoked=True)
    await harness.member(left, me.user_id, joined_at=t0 + timedelta(days=5), revoked_by=me.user_id)
    await harness.member(others_only, other.user_id, role="leader")
    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.join_requests.insert().values(
                id=str(uuid4()), trip_id=pending, user_id=me.user_id
            )
        )

    response = await client.get("/api/v2/me/trips", headers=me.headers)

    assert response.status_code == 200, response.text
    tied = sorted([leader_trip, tie_trip])
    role = {leader_trip: "leader", tie_trip: "rider"}
    names = {leader_trip: "Leader trip", tie_trip: "Tie trip"}
    assert response.json() == [
        {"id": rider_private, "name": "Rider private", "startDate": "2026-07-01", "role": "rider"},
        {"id": readmitted, "name": "Readmitted", "startDate": "2026-07-01", "role": "rider"},
        *({"id": t, "name": names[t], "startDate": "2026-07-01", "role": role[t]} for t in tied),
    ]


async def test_a_created_trip_appears_in_my_trips_as_leader(
    client: AsyncClient, made: Made
) -> None:
    response = await client.get("/api/v2/me/trips", headers=made.leader.headers)
    assert response.status_code == 200, response.text
    assert response.json() == [
        {"id": made.trip_id, "name": "Stuart Hwy", "startDate": "2026-07-01", "role": "leader"}
    ]


async def test_head_my_trips_matches_get(client: AsyncClient, made: Made) -> None:
    get = await client.get("/api/v2/me/trips", headers=made.leader.headers)
    head = await client.head("/api/v2/me/trips", headers=made.leader.headers)
    assert head.status_code == get.status_code == 200
    assert head.content == b""
    assert without_date(head) == without_date(get)
