"""
The legacy rider-link claim, ``POST /api/v2/trips/claim`` (t-am-legacy-claim).

Written from ``docs/api-contract.md`` -- the v2 endpoint row for claim, "Claim",
"Join request create", "Me / join-requests", "Private means invisible, not
forbidden", the identity x trip matrix (the ``pending`` row and the legacy-slug
sentence under it), "Rate limits and lockout" (``join``) and "CSRF" -- and the
task's acceptance criteria (Obligation 7, claim half). Route order is
``test_claim_route_order.py`` and is not repeated here.

A claim turns a rider slug into a **pending** join request and never into
access. Sections:

1. Create and repeat: 201 then 200, same id, ``via='legacy_rider_link'``.
2. Obligation 7, claim half: a pending claimant is refused on every writer
   route of both surfaces and every v2 read of a private trip, until a leader
   approves.
3. Slugs that are not a rider slug: one byte-identical 404.
4. Every join rule applies through claim.
5. Gates: anonymous 401, CSRF 403, the shared ``join`` bucket.
6. The slug in no response body, header or log record.
7. ``/me/join-requests`` shows the claim with the trip's current name.

Rows are inserted straight into the tables where the setup is not the thing
under test; every trip, account, membership and request is removed on teardown.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
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
from jpeg_fixtures import minimal_jpeg
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.main
from app.core.security import UNKNOWN_TRIP_MESSAGE
from app.data import tables
from app.data.db import get_session

CLAIM_PATH = "/api/v2/trips/claim"
ME_PATH = "/api/v2/me/join-requests"
MY_JOIN_REQUEST_KEYS = {"id", "tripId", "tripName", "state", "message", "createdAt"}
CSRF_MESSAGE = "This request came from another site and was blocked."


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """The real app on the test database; a crash comes back as the 500 a caller sees."""
    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.main.app.dependency_overrides[get_session] = override
    try:
        async with make_async_client(app.main.app, raise_app_exceptions=False) as http_client:
            yield http_client
    finally:
        app.main.app.dependency_overrides.pop(get_session, None)


@dataclass(frozen=True)
class Trip:
    id: str
    rider_slug: str
    viewer_slug: str
    stop_id: str
    bike_id: str


@dataclass
class World:
    """Trips, accounts and rows made by one test, removed on teardown."""

    engine: AsyncEngine
    trip_ids: list[str] = field(default_factory=list)
    user_ids: list[str] = field(default_factory=list)

    async def trip(self, visibility: str = "private", name: str = "Claim trip") -> Trip:
        """A trip with legacy slugs, one stop and one bike (so every write has a target)."""
        made = Trip(
            id=str(uuid4()),
            rider_slug=f"r{secrets.token_urlsafe(18)}",
            viewer_slug=f"v{secrets.token_urlsafe(18)}",
            stop_id=str(uuid4()),
            bike_id=str(uuid4()),
        )
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.trips.insert().values(
                    id=made.id,
                    name=name,
                    rider_slug=made.rider_slug,
                    viewer_slug=made.viewer_slug,
                    start_date=date(2026, 6, 1),
                    visibility=visibility,
                    public_delay_hours=0,
                )
            )
            await conn.execute(
                tables.stops.insert().values(
                    id=made.stop_id,
                    trip_id=made.id,
                    name="Seed stop",
                    lat=-14.5,
                    lng=132.3,
                    location_source="gps",
                    arrived_at=datetime(2026, 6, 2, tzinfo=UTC),
                )
            )
            await conn.execute(
                tables.bikes.insert().values(
                    id=made.bike_id,
                    trip_id=made.id,
                    rider_name="Kim",
                    make="BMW",
                    model="R80",
                    year=1985,
                )
            )
        self.trip_ids.append(made.id)
        return made

    async def bare_trips(self, count: int) -> list[str]:
        ids = [str(uuid4()) for _ in range(count)]
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.trips.insert(),
                [
                    {
                        "id": i,
                        "name": "Bulk",
                        "start_date": date(2026, 6, 1),
                        "visibility": "public",
                    }
                    for i in ids
                ],
            )
        self.trip_ids.extend(ids)
        return ids

    async def account(self, name: str = "Claimant") -> SignedInAccount:
        account = await create_signed_in_account(self.engine, display_name=name)
        self.user_ids.append(account.user_id)
        return account

    async def bare_users(self, count: int) -> list[str]:
        ids = [str(uuid4()) for _ in range(count)]
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.users.insert(),
                [
                    {
                        "id": i,
                        "username": f"f{secrets.token_hex(8)}",
                        "display_name": "Filler",
                        "password_hash": "not-a-password-hash",
                    }
                    for i in ids
                ],
            )
        self.user_ids.extend(ids)
        return ids

    async def request(
        self, trip_id: str, user_id: str, state: str, *, decided_at: datetime | None = None
    ) -> str:
        request_id = str(uuid4())
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.join_requests.insert().values(
                    id=request_id,
                    trip_id=trip_id,
                    user_id=user_id,
                    state=state,
                    via="direct",
                    decided_at=None if state == "pending" else (decided_at or datetime.now(UTC)),
                )
            )
        return request_id

    async def requests_bulk(self, rows: list[tuple[str, str]]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.join_requests.insert(),
                [
                    {"id": str(uuid4()), "trip_id": t, "user_id": u, "state": "pending"}
                    for t, u in rows
                ],
            )

    async def rows(self, **where: str) -> list[Any]:
        query = select(tables.join_requests)
        for column, value in where.items():
            query = query.where(tables.join_requests.c[column] == value)
        async with self.engine.connect() as conn:
            return list((await conn.execute(query.order_by(tables.join_requests.c.id))).all())

    async def members(self, trip_id: str, user_id: str) -> list[Any]:
        async with self.engine.connect() as conn:
            return list(
                (
                    await conn.execute(
                        select(tables.trip_members)
                        .where(tables.trip_members.c.trip_id == trip_id)
                        .where(tables.trip_members.c.user_id == user_id)
                    )
                ).all()
            )

    async def rename(self, trip_id: str, name: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.trips.update().where(tables.trips.c.id == trip_id).values(name=name)
            )


@pytest.fixture
async def world(migrated_engine: AsyncEngine) -> AsyncIterator[World]:
    made = World(engine=migrated_engine)
    try:
        yield made
    finally:
        async with migrated_engine.begin() as conn:
            if made.trip_ids:
                await conn.execute(
                    tables.join_requests.delete().where(
                        tables.join_requests.c.trip_id.in_(made.trip_ids)
                    )
                )
                await conn.execute(
                    tables.trip_members.delete().where(
                        tables.trip_members.c.trip_id.in_(made.trip_ids)
                    )
                )
        if made.user_ids:
            await delete_accounts(migrated_engine, made.user_ids)
        if made.trip_ids:
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    tables.trips.delete().where(tables.trips.c.id.in_(made.trip_ids))
                )


async def claim(
    client: AsyncClient,
    slug: Any,
    who: SignedInAccount | None,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> Response:
    return await client.post(
        CLAIM_PATH,
        json={"riderSlug": slug} if body is None else body,
        headers={**(who.headers if who else {}), **(headers or {})},
    )


def without_date(response: Response) -> tuple[int, list[tuple[str, str]], bytes]:
    headers = [(k.lower(), v) for k, v in response.headers.multi_items() if k.lower() != "date"]
    return response.status_code, headers, response.content


def assert_conflict(response: Response) -> None:
    assert response.status_code == 409, (response.status_code, response.text)
    assert response.json()["error"]["code"] == "CONFLICT", response.text


async def leader_of(world: World, trip_id: str) -> SignedInAccount:
    leader = await world.account("Leader")
    await grant_membership(world.engine, trip_id, leader.user_id, role="leader")
    return leader


# Every writer route, on both surfaces. ``base`` is ``/api/trips/{slug}`` or
# ``/api/v2/trips/{tripId}``; the paths below it are identical on the two.
WRITES = ["create_stop", "upload_photo", "create_bike", "patch_bike"]


async def write(
    client: AsyncClient, kind: str, base: str, trip: Trip, headers: dict[str, str]
) -> Response:
    record_id = str(uuid4())
    if kind == "create_stop":
        return await client.post(
            f"{base}/stops",
            json={
                "id": record_id,
                "name": "Claimant stop",
                "lat": -14.5,
                "lng": 132.3,
                "locationSource": "gps",
                "arrivedAt": "2026-06-14T15:15:00+09:30",
            },
            headers=headers,
        )
    if kind == "upload_photo":
        return await client.post(
            f"{base}/stops/{trip.stop_id}/photos",
            data={"id": record_id, "takenAt": "2026-06-14T10:00:00+09:30"},
            files={"file": ("p.jpg", minimal_jpeg(), "image/jpeg")},
            headers=headers,
        )
    if kind == "create_bike":
        return await client.post(
            f"{base}/bikes",
            json={"id": record_id, "riderName": "Kim", "make": "BMW", "model": "R", "year": 1985},
            headers=headers,
        )
    if kind == "patch_bike":
        return await client.patch(
            f"{base}/bikes/{trip.bike_id}", json={"specs": "claimant"}, headers=headers
        )
    raise AssertionError(kind)


# Every v2 GET a private trip's non-member must not see, as a path under the trip.
V2_READS = [
    "",
    "/stops",
    "/stops/{stop}/photos",
    "/map",
    "/bikes",
    "/members",
    "/join-requests",
]


async def trip_state(world: World, trip: Trip) -> tuple[Any, ...]:
    """Stops, bikes and photos on the trip: what a refused write must leave alone."""
    async with world.engine.connect() as conn:
        stops = (
            await conn.execute(select(tables.stops).where(tables.stops.c.trip_id == trip.id))
        ).all()
        bikes = (
            await conn.execute(select(tables.bikes).where(tables.bikes.c.trip_id == trip.id))
        ).all()
        photos = (
            await conn.execute(select(tables.photos).where(tables.photos.c.stop_id == trip.stop_id))
        ).all()
    return sorted(map(tuple, stops)), sorted(map(tuple, bikes)), sorted(map(tuple, photos))


# --------------------------------------------------------------------------
# 1. Create and repeat
# --------------------------------------------------------------------------


@pytest.mark.parametrize("visibility", ["private", "public"])
async def test_a_rider_slug_claim_is_201_pending_then_a_repeat_is_200_with_the_same_id(
    client: AsyncClient, world: World, visibility: str
) -> None:
    trip = await world.trip(visibility, name="Outback run")
    me = await world.account()

    first = await claim(client, trip.rider_slug, me)
    assert first.status_code == 201, first.text
    body = first.json()
    assert set(body) == MY_JOIN_REQUEST_KEYS
    assert body["tripId"] == trip.id
    assert body["tripName"] == "Outback run"
    assert body["state"] == "pending"
    assert body["message"] is None

    (row,) = await world.rows(trip_id=trip.id, user_id=me.user_id)
    assert row.id == body["id"]
    assert row.state == "pending"
    assert row.via == "legacy_rider_link"

    again = await claim(client, trip.rider_slug, me)
    assert again.status_code == 200, again.text
    assert again.json() == body
    assert len(await world.rows(trip_id=trip.id)) == 1

    # The leader sees how it arrived.
    leader = await leader_of(world, trip.id)
    listed = await client.get(f"/api/v2/trips/{trip.id}/join-requests", headers=leader.headers)
    assert listed.status_code == 200, listed.text
    (entry,) = listed.json()
    assert entry["id"] == body["id"]
    assert entry["via"] == "legacy_rider_link"
    assert entry["requester"] == {"userId": me.user_id, "displayName": "Claimant"}


async def test_a_claim_grants_no_membership(client: AsyncClient, world: World) -> None:
    trip = await world.trip()
    me = await world.account()
    assert (await claim(client, trip.rider_slug, me)).status_code == 201
    assert await world.members(trip.id, me.user_id) == []


async def test_parallel_repeat_claims_leave_exactly_one_pending_row(
    client: AsyncClient, world: World
) -> None:
    trip = await world.trip()
    me = await world.account()
    responses = await asyncio.gather(*(claim(client, trip.rider_slug, me) for _ in range(5)))
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 200, 200, 200, 201], [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(await world.rows(trip_id=trip.id)) == 1


async def test_parallel_claim_and_direct_create_share_one_pending_row(
    client: AsyncClient, world: World
) -> None:
    """Claim and direct create race on one (trip, user); same lock order, so no deadlock 500."""
    for _ in range(3):
        trip = await world.trip("public")
        me = await world.account()
        responses = await asyncio.gather(
            claim(client, trip.rider_slug, me),
            client.post(f"/api/v2/trips/{trip.id}/join-requests", json={}, headers=me.headers),
            claim(client, trip.rider_slug, me),
            client.post(f"/api/v2/trips/{trip.id}/join-requests", json={}, headers=me.headers),
        )
        assert sorted(r.status_code for r in responses) == [200, 200, 200, 201], [
            r.text for r in responses
        ]
        assert len({r.json()["id"] for r in responses}) == 1
        assert len(await world.rows(trip_id=trip.id)) == 1


# --------------------------------------------------------------------------
# 2. Obligation 7, claim half
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", WRITES)
async def test_a_pending_claimants_v2_write_on_a_private_trip_is_the_missing_trip_404(
    client: AsyncClient, world: World, kind: str
) -> None:
    trip = await world.trip("private")
    me = await world.account()
    assert (await claim(client, trip.rider_slug, me)).status_code == 201
    before = await trip_state(world, trip)

    refused = await write(client, kind, f"/api/v2/trips/{trip.id}", trip, me.headers)
    missing = await write(client, kind, f"/api/v2/trips/{uuid4()}", trip, me.headers)

    assert refused.status_code == 404, refused.text
    assert without_date(refused) == without_date(missing)
    assert await trip_state(world, trip) == before


@pytest.mark.parametrize("kind", WRITES)
@pytest.mark.parametrize("slug_kind", ["rider", "viewer"])
async def test_a_pending_claimants_legacy_write_on_a_private_trip_is_403(
    client: AsyncClient, world: World, kind: str, slug_kind: str
) -> None:
    """
    Contract: legacy slug writes follow the matrix "with the trip always located"
    -- slug first, then 401, then 403. A pending claimant is not a member, so 403.
    """
    trip = await world.trip("private")
    me = await world.account()
    assert (await claim(client, trip.rider_slug, me)).status_code == 201
    before = await trip_state(world, trip)

    slug = trip.rider_slug if slug_kind == "rider" else trip.viewer_slug
    refused = await write(client, kind, f"/api/trips/{slug}", trip, me.headers)

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "FORBIDDEN"
    assert await trip_state(world, trip) == before


@pytest.mark.parametrize("path", V2_READS)
async def test_a_pending_claimants_v2_read_of_a_private_trip_is_the_missing_trip_404(
    client: AsyncClient, world: World, path: str
) -> None:
    trip = await world.trip("private")
    me = await world.account()
    assert (await claim(client, trip.rider_slug, me)).status_code == 201

    suffix = path.format(stop=trip.stop_id)
    refused = await client.get(f"/api/v2/trips/{trip.id}{suffix}", headers=me.headers)
    missing = await client.get(f"/api/v2/trips/{uuid4()}{suffix}", headers=me.headers)

    assert refused.status_code == 404, refused.text
    assert without_date(refused) == without_date(missing)


@pytest.mark.parametrize("surface", ["legacy", "v2"])
@pytest.mark.parametrize("kind", WRITES)
async def test_a_pending_claimants_write_on_a_public_trip_is_403(
    client: AsyncClient, world: World, kind: str, surface: str
) -> None:
    trip = await world.trip("public")
    me = await world.account()
    assert (await claim(client, trip.rider_slug, me)).status_code == 201
    before = await trip_state(world, trip)

    base = f"/api/trips/{trip.rider_slug}" if surface == "legacy" else f"/api/v2/trips/{trip.id}"
    refused = await write(client, kind, base, trip, me.headers)

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "FORBIDDEN"
    assert await trip_state(world, trip) == before


@pytest.mark.parametrize("visibility", ["private", "public"])
async def test_after_a_leader_approves_the_claim_the_claimants_writes_succeed(
    client: AsyncClient, world: World, visibility: str
) -> None:
    trip = await world.trip(visibility)
    me = await world.account()
    request_id = (await claim(client, trip.rider_slug, me)).json()["id"]
    leader = await leader_of(world, trip.id)

    approved = await client.post(
        f"/api/v2/trips/{trip.id}/join-requests/{request_id}/decision",
        json={"action": "approve"},
        headers=leader.headers,
    )
    assert approved.status_code == 200, approved.text
    (membership,) = await world.members(trip.id, me.user_id)
    assert membership.role == "rider" and membership.revoked_at is None

    legacy = f"/api/trips/{trip.rider_slug}"
    v2 = f"/api/v2/trips/{trip.id}"
    assert (await write(client, "create_stop", legacy, trip, me.headers)).status_code == 201
    assert (await write(client, "create_bike", v2, trip, me.headers)).status_code == 201
    assert (await write(client, "patch_bike", v2, trip, me.headers)).status_code == 200
    assert (await client.get(v2, headers=me.headers)).status_code == 200


# --------------------------------------------------------------------------
# 3. Not a rider slug: one byte-identical 404
# --------------------------------------------------------------------------


async def test_viewer_unknown_nul_and_private_viewer_slugs_are_one_identical_404(
    client: AsyncClient, world: World
) -> None:
    public = await world.trip("public")
    private = await world.trip("private")
    me = await world.account()

    cases = {
        "viewer slug": public.viewer_slug,
        "private trip's viewer slug": private.viewer_slug,
        "unknown slug": f"r{secrets.token_urlsafe(18)}",
        "NUL in slug": public.rider_slug[:5] + "\x00" + public.rider_slug[5:],
        "NUL alone": "\x00",
    }
    answers = {name: await claim(client, slug, me) for name, slug in cases.items()}

    for name, answer in answers.items():
        assert answer.status_code == 404, (name, answer.status_code, answer.text)
        assert answer.json() == {"error": {"code": "NOT_FOUND", "message": UNKNOWN_TRIP_MESSAGE}}, (
            name
        )
    reference = without_date(answers["unknown slug"])
    for name, answer in answers.items():
        assert without_date(answer) == reference, name

    # Same message as an unknown legacy link.
    legacy = await client.get(f"/api/trips/{secrets.token_urlsafe(18)}")
    assert legacy.json()["error"]["message"] == UNKNOWN_TRIP_MESSAGE

    assert await world.rows(user_id=me.user_id) == []


# --------------------------------------------------------------------------
# 4. Every join rule applies through claim
# --------------------------------------------------------------------------


async def test_a_rejection_under_7_days_old_holds_a_claim_and_an_older_one_does_not(
    client: AsyncClient, world: World
) -> None:
    held, free = await world.trip(), await world.trip()
    me = await world.account()
    await world.request(
        held.id, me.user_id, "rejected", decided_at=datetime.now(UTC) - timedelta(days=6)
    )
    await world.request(
        free.id, me.user_id, "rejected", decided_at=datetime.now(UTC) - timedelta(days=8)
    )

    assert_conflict(await claim(client, held.rider_slug, me))
    assert [r.state for r in await world.rows(trip_id=held.id)] == ["rejected"]
    assert (await claim(client, free.rider_slug, me)).status_code == 201


async def test_a_revocation_by_someone_else_under_7_days_old_holds_a_claim(
    client: AsyncClient, world: World
) -> None:
    trip = await world.trip()
    me = await world.account()
    leader = await leader_of(world, trip.id)
    async with world.engine.begin() as conn:
        await conn.execute(
            tables.trip_members.insert().values(
                id=str(uuid4()),
                trip_id=trip.id,
                user_id=me.user_id,
                role="rider",
                joined_at=datetime.now(UTC) - timedelta(days=30),
                revoked_at=datetime.now(UTC) - timedelta(days=1),
                revoked_by=leader.user_id,
            )
        )
    assert_conflict(await claim(client, trip.rider_slug, me))
    assert await world.rows(trip_id=trip.id) == []


async def test_a_blocked_request_refuses_a_claim(client: AsyncClient, world: World) -> None:
    trip = await world.trip()
    me = await world.account()
    await world.request(
        trip.id, me.user_id, "blocked", decided_at=datetime.now(UTC) - timedelta(days=60)
    )
    assert_conflict(await claim(client, trip.rider_slug, me))
    assert [r.state for r in await world.rows(trip_id=trip.id)] == ["blocked"]


async def test_a_claim_past_20_pending_requests_is_409(client: AsyncClient, world: World) -> None:
    trip = await world.trip()
    me = await world.account()
    others = await world.bare_trips(20)
    await world.requests_bulk([(t, me.user_id) for t in others])
    assert_conflict(await claim(client, trip.rider_slug, me))
    assert await world.rows(trip_id=trip.id) == []


async def test_a_claim_on_a_trip_with_100_pending_requests_is_409(
    client: AsyncClient, world: World
) -> None:
    trip = await world.trip()
    me = await world.account()
    fillers = await world.bare_users(100)
    await world.requests_bulk([(trip.id, u) for u in fillers])
    assert_conflict(await claim(client, trip.rider_slug, me))
    assert await world.rows(trip_id=trip.id, user_id=me.user_id) == []


@pytest.mark.parametrize("role", ["rider", "leader"])
@pytest.mark.parametrize("visibility", ["private", "public"])
async def test_an_active_member_claiming_is_409(
    client: AsyncClient, world: World, role: str, visibility: str
) -> None:
    trip = await world.trip(visibility)
    me = await world.account()
    await grant_membership(world.engine, trip.id, me.user_id, role=role)
    assert_conflict(await claim(client, trip.rider_slug, me))
    assert await world.rows(trip_id=trip.id) == []


async def test_a_claim_duplicating_a_direct_pending_request_is_200_with_that_request(
    client: AsyncClient, world: World
) -> None:
    trip = await world.trip("public")
    me = await world.account()
    direct = await client.post(
        f"/api/v2/trips/{trip.id}/join-requests", json={"message": "hi"}, headers=me.headers
    )
    assert direct.status_code == 201, direct.text

    claimed = await claim(client, trip.rider_slug, me)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json() == direct.json()
    (row,) = await world.rows(trip_id=trip.id)
    assert row.message == "hi"


# --------------------------------------------------------------------------
# 5. Gates: anonymous, CSRF, the join bucket, validation
# --------------------------------------------------------------------------


async def test_anonymous_claim_is_401_for_real_and_unknown_slugs_alike(
    client: AsyncClient, world: World
) -> None:
    trip = await world.trip()
    real = await claim(client, trip.rider_slug, None)
    unknown = await claim(client, f"r{secrets.token_urlsafe(18)}", None)
    assert real.status_code == 401, real.text
    assert real.json()["error"]["code"] == "UNAUTHENTICATED"
    assert without_date(real) == without_date(unknown)
    assert await world.rows(trip_id=trip.id) == []


@pytest.mark.parametrize(
    "headers",
    [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}],
    ids=["foreign-origin", "sec-fetch-cross-site"],
)
async def test_a_cross_site_claim_is_403_and_writes_nothing(
    client: AsyncClient, world: World, headers: dict[str, str]
) -> None:
    trip = await world.trip()
    me = await world.account()
    refused = await claim(client, trip.rider_slug, me, headers=headers)
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"error": {"code": "FORBIDDEN", "message": CSRF_MESSAGE}}
    assert await world.rows(trip_id=trip.id) == []


async def test_claims_spend_the_join_bucket_shared_with_direct_create(
    client: AsyncClient, world: World
) -> None:
    me = await world.account()
    trips = [await world.trip("public") for _ in range(11)]
    for trip in trips[:10]:
        assert (await claim(client, trip.rider_slug, me)).status_code == 201
    limited = await client.post(
        f"/api/v2/trips/{trips[10].id}/join-requests", json={}, headers=me.headers
    )
    assert limited.status_code == 429, limited.text
    limited_claim = await claim(client, trips[10].rider_slug, me)
    assert limited_claim.status_code == 429, limited_claim.text
    assert int(limited_claim.headers["Retry-After"]) >= 1
    assert await world.rows(trip_id=trips[10].id) == []


@pytest.mark.parametrize(
    "body", [{}, {"riderSlug": None}, {"riderSlug": 12345}], ids=["missing", "null", "number"]
)
async def test_a_malformed_claim_body_is_422(
    client: AsyncClient, world: World, body: dict[str, Any]
) -> None:
    me = await world.account()
    response = await claim(client, None, me, body=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------
# 6. The slug is never echoed and never logged
# --------------------------------------------------------------------------


async def test_the_slug_appears_in_no_response_and_no_log_record(
    client: AsyncClient,
    world: World,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    public, private = await world.trip("public"), await world.trip("private")
    me, member, outsider = await world.account(), await world.account(), await world.account()
    await grant_membership(world.engine, public.id, member.user_id)
    unknown = f"r{secrets.token_urlsafe(18)}"
    slugs = [
        public.rider_slug,
        public.viewer_slug,
        private.rider_slug,
        private.viewer_slug,
        unknown,
    ]
    nul_slug = private.rider_slug[:6] + "\x00" + private.rider_slug[6:]

    responses = [
        await claim(client, public.rider_slug, me),  # 201
        await claim(client, public.rider_slug, me),  # 200
        await claim(client, private.rider_slug, me),  # 201
        await claim(client, public.viewer_slug, me),  # 404
        await claim(client, private.viewer_slug, me),  # 404
        await claim(client, unknown, me),  # 404
        await claim(client, nul_slug, me),  # 404
        await claim(client, public.rider_slug, member),  # 409
        await claim(client, private.rider_slug, None),  # 401
        await claim(client, private.rider_slug, outsider, headers={"Origin": "https://x.example"}),
        await claim(client, None, outsider, body={"riderSlug": [private.rider_slug]}),  # 422
        await client.get(ME_PATH, headers=me.headers),
    ]
    assert [r.status_code for r in responses] == [
        201, 200, 201, 404, 404, 404, 404, 409, 401, 403, 422, 200,
    ]  # fmt: skip

    for response in responses:
        headers_text = "\n".join(f"{k}: {v}" for k, v in response.headers.multi_items())
        for slug in [*slugs, nul_slug]:
            assert slug not in response.text, (response.status_code, response.text)
            assert slug not in headers_text, (response.status_code, headers_text)

    captured = capsys.readouterr()
    logged = [captured.out, captured.err]
    for record in caplog.records:
        logged.append(record.getMessage())
        logged.append(repr(record.args))
        logged.append(record.exc_text or "")
        logged.append(repr(record.__dict__))
    everything = "\n".join(logged)
    for slug in [*slugs, nul_slug]:
        assert slug not in everything, "a slug reached a log record or stdout/stderr"


# --------------------------------------------------------------------------
# 7. /me/join-requests shows the claim, with the trip's current name
# --------------------------------------------------------------------------


async def test_my_join_requests_shows_a_private_claim_under_the_trips_current_name(
    client: AsyncClient, world: World
) -> None:
    """Contract ruling (t-am-legacy-claim): ``tripName`` is current, renames included."""
    trip = await world.trip("private", name="Before rename")
    me = await world.account()
    claimed = (await claim(client, trip.rider_slug, me)).json()

    listed = await client.get(ME_PATH, headers=me.headers)
    assert listed.status_code == 200, listed.text
    assert listed.json() == [claimed]

    await world.rename(trip.id, "After rename")
    (entry,) = (await client.get(ME_PATH, headers=me.headers)).json()
    assert entry["id"] == claimed["id"]
    assert entry["state"] == "pending"
    assert entry["tripName"] == "After rename"
