"""
Peer leadership on v2 trips: members list, promote, step-down, leave (t-am-trip-leadership).

Written from ``docs/api-contract.md`` ("The gates", the v2 route rows and the
"Members" notes) and the task's acceptance criteria:

- ``GET .../members`` returns **active** members in ``joinedAt`` order, only to
  active members; a non-member never sees user ids, usernames or slugs.
- Promote: the target must be an active rider, else ``404``; an existing leader
  is a ``200`` and idempotent.
- Step-down and leave: the last active leader gets ``409`` "Promote another
  rider to leader first", with nothing changed. Leave sets ``revoked_at`` with
  ``revoked_by`` = self and starts no cooldown (``join_requests`` untouched).
- Every membership-changing statement locks the trip row first, so concurrent
  step-downs can't leave a trip with no leader.

The gate cells (401 / 404 / 403 per identity) are ``test_access_matrix.py`` §15
and are not repeated here.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
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
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.main
from app.data import tables
from app.data.db import get_session
from app.data.repositories import memberships

LAST_LEADER_MESSAGE = "Promote another rider to leader first"
CSRF_MESSAGE = "This request came from another site and was blocked."
NO_LONGER_A_RIDER_MESSAGE = "You're no longer a rider on this trip."
NOT_A_LEADER_MESSAGE = "You're not a leader on this trip."

MEMBER_KEYS = {"userId", "displayName", "role", "joinedAt"}

# How many times each race is run: one interleaving proves little.
RACE_RUNS = 8


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


def _session_override(engine: AsyncEngine) -> Callable[[], AsyncIterator[AsyncSession]]:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    return override


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """The real app on the test database; a crash comes back as the 500 a caller sees."""
    application = app.main.app
    application.dependency_overrides[get_session] = _session_override(migrated_engine)
    try:
        async with make_async_client(application, raise_app_exceptions=False) as http_client:
            yield http_client
    finally:
        application.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def second_client(client: AsyncClient) -> AsyncIterator[AsyncClient]:
    """A second, independent client on the same app (the override is set by ``client``)."""
    async with make_async_client(app.main.app, raise_app_exceptions=False) as http_client:
        yield http_client


@dataclass
class World:
    """Trips and accounts made by one test, removed on teardown."""

    engine: AsyncEngine
    trip_ids: list[str] = field(default_factory=list)
    user_ids: list[str] = field(default_factory=list)

    async def trip(self, visibility: str = "private") -> str:
        trip_id = str(uuid4())
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.trips.insert().values(
                    id=trip_id,
                    name="Leadership trip",
                    start_date=date(2026, 6, 1),
                    visibility=visibility,
                    public_delay_hours=0,
                    rider_slug=secrets.token_urlsafe(16),
                    viewer_slug=secrets.token_urlsafe(16),
                )
            )
        self.trip_ids.append(trip_id)
        return trip_id

    async def account(self, name: str = "Leadership Caller") -> SignedInAccount:
        account = await create_signed_in_account(self.engine, display_name=name)
        self.user_ids.append(account.user_id)
        return account

    async def member(
        self,
        trip_id: str,
        user_id: str,
        role: str = "rider",
        *,
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

    async def members_snapshot(self, trip_id: str) -> list[tuple[Any, ...]]:
        """Every ``trip_members`` row of the trip, every column, plus ``xmin`` (any write moves it)."""
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT id, user_id, role, joined_at, revoked_at, revoked_by, xmin::text "
                    "FROM trip_members WHERE trip_id = :t ORDER BY id"
                ),
                {"t": trip_id},
            )
            return [tuple(row) for row in rows]

    async def join_requests_snapshot(self, trip_id: str) -> list[tuple[Any, ...]]:
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT id, user_id, state, created_at, decided_at, decided_by, xmin::text "
                    "FROM join_requests WHERE trip_id = :t ORDER BY id"
                ),
                {"t": trip_id},
            )
            return [tuple(row) for row in rows]

    async def row(self, trip_id: str, user_id: str) -> Any:
        """The user's single row on the trip (tests here give each user at most one)."""
        async with self.engine.connect() as conn:
            return (
                await conn.execute(
                    select(tables.trip_members).where(
                        tables.trip_members.c.trip_id == trip_id,
                        tables.trip_members.c.user_id == user_id,
                    )
                )
            ).one()

    async def active_leaders(self, trip_id: str) -> list[str]:
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                select(tables.trip_members.c.user_id).where(
                    tables.trip_members.c.trip_id == trip_id,
                    tables.trip_members.c.role == "leader",
                    tables.trip_members.c.revoked_at.is_(None),
                )
            )
            return sorted(r.user_id for r in rows)

    async def username(self, user_id: str) -> str:
        async with self.engine.connect() as conn:
            return await conn.scalar(
                select(tables.users.c.username).where(tables.users.c.id == user_id)
            )

    async def slugs(self, trip_id: str) -> tuple[str, str]:
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(tables.trips.c.rider_slug, tables.trips.c.viewer_slug).where(
                        tables.trips.c.id == trip_id
                    )
                )
            ).one()
            return row.rider_slug, row.viewer_slug


@pytest.fixture
async def world(migrated_engine: AsyncEngine) -> AsyncIterator[World]:
    made = World(engine=migrated_engine)
    try:
        yield made
    finally:
        if made.user_ids:
            await delete_accounts(migrated_engine, made.user_ids)
        if made.trip_ids:
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    tables.trips.delete().where(tables.trips.c.id.in_(made.trip_ids))
                )


async def send(
    client: AsyncClient,
    method: str,
    path: str,
    account: SignedInAccount | None,
    json: Any = None,
    headers: dict[str, str] | None = None,
) -> Response:
    """One request whose only cookie is ``account``'s: the jar is emptied around it."""
    client.cookies.clear()
    all_headers = {**(account.headers if account else {}), **(headers or {})}
    kwargs: dict[str, Any] = {"headers": all_headers}
    if json is not None:
        kwargs["json"] = json
    response = await client.request(method, path, **kwargs)
    client.cookies.clear()
    return response


def wire(response: Response) -> tuple[int, list[tuple[str, str]], bytes]:
    """Everything a caller can observe of a response, except ``date``."""
    headers = [(k.lower(), v) for k, v in response.headers.multi_items() if k.lower() != "date"]
    return response.status_code, headers, response.content


def promote_path(trip_id: str, user_id: str) -> str:
    return f"/api/v2/trips/{trip_id}/members/{user_id}/promote"


def step_down_path(trip_id: str) -> str:
    return f"/api/v2/trips/{trip_id}/step-down"


def leave_path(trip_id: str) -> str:
    return f"/api/v2/trips/{trip_id}/leave"


def members_path(trip_id: str) -> str:
    return f"/api/v2/trips/{trip_id}/members"


def assert_error(response: Response, status: int, code: str, message: str | None = None) -> None:
    assert response.status_code == status, (response.status_code, response.text)
    error = response.json()["error"]
    assert error["code"] == code, error
    if message is not None:
        assert error["message"] == message, error


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


# --------------------------------------------------------------------------
# 1. The last leader: 409, nothing changed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["step-down", "leave"])
async def test_the_last_leader_cannot_step_down_or_leave(
    client: AsyncClient, world: World, action: str
) -> None:
    trip_id = await world.trip()
    leader = await world.account("Only Leader")
    rider = await world.account("Some Rider")
    departed = await world.account("Old Leader")
    await world.member(trip_id, leader.user_id, "leader")
    # Riders and a departed leader don't count as "another leader".
    await world.member(trip_id, rider.user_id, "rider")
    await world.member(trip_id, departed.user_id, "leader", revoked_by=departed.user_id)
    before = await world.members_snapshot(trip_id)

    path = step_down_path(trip_id) if action == "step-down" else leave_path(trip_id)
    response = await send(client, "POST", path, leader)

    assert_error(response, 409, "CONFLICT", LAST_LEADER_MESSAGE)
    assert await world.members_snapshot(trip_id) == before

    # Fails the same way on retry: still nothing changed.
    again = await send(client, "POST", path, leader)
    assert_error(again, 409, "CONFLICT", LAST_LEADER_MESSAGE)
    assert await world.members_snapshot(trip_id) == before


async def test_with_two_leaders_one_can_step_down(client: AsyncClient, world: World) -> None:
    trip_id = await world.trip()
    first = await world.account("First Leader")
    second = await world.account("Second Leader")
    await world.member(trip_id, first.user_id, "leader")
    await world.member(trip_id, second.user_id, "leader")
    before = await world.row(trip_id, first.user_id)

    response = await send(client, "POST", step_down_path(trip_id), first)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == MEMBER_KEYS
    assert body["userId"] == first.user_id
    assert body["displayName"] == "First Leader"
    assert body["role"] == "rider"
    assert parse(body["joinedAt"]) == before.joined_at

    after = await world.row(trip_id, first.user_id)
    assert after.role == "rider"
    assert after.revoked_at is None  # still on the trip
    assert after.joined_at == before.joined_at
    assert await world.active_leaders(trip_id) == [second.user_id]

    # Now the second is the last leader.
    last = await send(client, "POST", step_down_path(trip_id), second)
    assert_error(last, 409, "CONFLICT", LAST_LEADER_MESSAGE)
    assert await world.active_leaders(trip_id) == [second.user_id]


async def test_with_two_leaders_one_can_leave(client: AsyncClient, world: World) -> None:
    trip_id = await world.trip()
    first = await world.account("First Leader")
    second = await world.account("Second Leader")
    await world.member(trip_id, first.user_id, "leader")
    await world.member(trip_id, second.user_id, "leader")

    response = await send(client, "POST", leave_path(trip_id), first)

    assert response.status_code == 204, response.text
    assert response.content == b""
    row = await world.row(trip_id, first.user_id)
    assert row.revoked_at is not None
    assert row.revoked_by == first.user_id
    assert await world.active_leaders(trip_id) == [second.user_id]


# --------------------------------------------------------------------------
# 2. Races: the trip-row lock keeps exactly one leader
# --------------------------------------------------------------------------


@pytest.mark.parametrize("run", range(RACE_RUNS))
async def test_concurrent_step_downs_leave_exactly_one_leader(
    client: AsyncClient, second_client: AsyncClient, world: World, run: int
) -> None:
    trip_id = await world.trip()
    first = await world.account("Racing Leader A")
    second = await world.account("Racing Leader B")
    await world.member(trip_id, first.user_id, "leader")
    await world.member(trip_id, second.user_id, "leader")

    responses = await asyncio.gather(
        send(client, "POST", step_down_path(trip_id), first),
        send(second_client, "POST", step_down_path(trip_id), second),
    )

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], [(r.status_code, r.text) for r in responses]
    loser = next(r for r in responses if r.status_code == 409)
    assert_error(loser, 409, "CONFLICT", LAST_LEADER_MESSAGE)
    winner = next(r for r in responses if r.status_code == 200).json()["userId"]
    survivor = second.user_id if winner == first.user_id else first.user_id
    assert await world.active_leaders(trip_id) == [survivor]


@pytest.mark.parametrize("run", range(RACE_RUNS))
async def test_concurrent_leave_and_step_down_leave_exactly_one_leader(
    client: AsyncClient, second_client: AsyncClient, world: World, run: int
) -> None:
    trip_id = await world.trip()
    leaver = await world.account("Leaving Leader")
    stepper = await world.account("Stepping Leader")
    await world.member(trip_id, leaver.user_id, "leader")
    await world.member(trip_id, stepper.user_id, "leader")

    left, stepped = await asyncio.gather(
        send(client, "POST", leave_path(trip_id), leaver),
        send(second_client, "POST", step_down_path(trip_id), stepper),
    )

    outcomes = (left.status_code, stepped.status_code)
    assert outcomes in {(204, 409), (409, 200)}, (left.text, stepped.text)
    leaders = await world.active_leaders(trip_id)
    assert len(leaders) == 1
    if outcomes == (204, 409):
        assert_error(stepped, 409, "CONFLICT", LAST_LEADER_MESSAGE)
        assert leaders == [stepper.user_id]
        assert (await world.row(trip_id, leaver.user_id)).revoked_by == leaver.user_id
    else:
        assert_error(left, 409, "CONFLICT", LAST_LEADER_MESSAGE)
        assert leaders == [leaver.user_id]
        row = await world.row(trip_id, stepper.user_id)
        assert (row.role, row.revoked_at) == ("rider", None)


# --------------------------------------------------------------------------
# 3. Promote
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target", ["unknown", "revoked", "departed", "pending_only", "other_trip_member"]
)
async def test_promote_on_a_non_active_member_is_404_and_writes_nothing(
    client: AsyncClient, world: World, target: str
) -> None:
    trip_id = await world.trip()
    other_trip = await world.trip()
    leader = await world.account("Promoting Leader")
    await world.member(trip_id, leader.user_id, "leader")

    if target == "unknown":
        target_id = str(uuid4())
    else:
        account = await world.account(f"Target {target}")
        target_id = account.user_id
        if target == "revoked":
            await world.member(trip_id, target_id, "rider", revoked=True)
        elif target == "departed":
            await world.member(trip_id, target_id, "rider", revoked_by=target_id)
        elif target == "pending_only":
            await world.join_request(trip_id, target_id, "pending")
        else:
            await world.member(other_trip, target_id, "rider")
    before = await world.members_snapshot(trip_id)
    before_other = await world.members_snapshot(other_trip)

    response = await send(client, "POST", promote_path(trip_id, target_id), leader)

    assert_error(response, 404, "NOT_FOUND")
    assert await world.members_snapshot(trip_id) == before
    assert await world.members_snapshot(other_trip) == before_other


@pytest.mark.parametrize("target", ["another_leader", "self"])
async def test_promoting_an_existing_leader_is_200_with_no_write(
    client: AsyncClient, world: World, target: str
) -> None:
    trip_id = await world.trip()
    leader = await world.account("Promoting Leader")
    other = await world.account("Already Leader")
    await world.member(trip_id, leader.user_id, "leader")
    await world.member(trip_id, other.user_id, "leader")
    target_account = other if target == "another_leader" else leader
    before = await world.members_snapshot(trip_id)
    row = await world.row(trip_id, target_account.user_id)

    response = await send(client, "POST", promote_path(trip_id, target_account.user_id), leader)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == MEMBER_KEYS
    assert body["userId"] == target_account.user_id
    assert body["displayName"] == target_account.display_name
    assert body["role"] == "leader"
    assert parse(body["joinedAt"]) == row.joined_at
    # xmin included: not even a same-value UPDATE happened.
    assert await world.members_snapshot(trip_id) == before


async def test_promoting_a_rider_makes_them_a_leader(client: AsyncClient, world: World) -> None:
    trip_id = await world.trip()
    leader = await world.account("Promoting Leader")
    rider = await world.account("Rising Rider")
    bystander = await world.account("Bystander Rider")
    await world.member(trip_id, leader.user_id, "leader")
    await world.member(trip_id, rider.user_id, "rider")
    await world.member(trip_id, bystander.user_id, "rider")
    before_row = await world.row(trip_id, rider.user_id)
    before = {r[1]: r for r in await world.members_snapshot(trip_id)}

    response = await send(client, "POST", promote_path(trip_id, rider.user_id), leader)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == MEMBER_KEYS
    assert body == {
        "userId": rider.user_id,
        "displayName": "Rising Rider",
        "role": "leader",
        "joinedAt": body["joinedAt"],
    }
    assert parse(body["joinedAt"]) == before_row.joined_at

    after_row = await world.row(trip_id, rider.user_id)
    assert after_row.role == "leader"
    assert after_row.id == before_row.id  # changed in place, not a new membership
    assert after_row.joined_at == before_row.joined_at
    assert after_row.revoked_at is None
    after = {r[1]: r for r in await world.members_snapshot(trip_id)}
    assert after[leader.user_id] == before[leader.user_id]
    assert after[bystander.user_id] == before[bystander.user_id]
    assert await world.active_leaders(trip_id) == sorted([leader.user_id, rider.user_id])

    # The new leader can now act as one: the old leader may step down.
    step = await send(client, "POST", step_down_path(trip_id), leader)
    assert step.status_code == 200, step.text


# --------------------------------------------------------------------------
# 4. Leave
# --------------------------------------------------------------------------


async def test_a_rider_can_leave_and_join_requests_are_untouched(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    leader = await world.account("Staying Leader")
    rider = await world.account("Leaving Rider")
    await world.member(trip_id, leader.user_id, "leader")
    await world.member(trip_id, rider.user_id, "rider")
    # The rider got in through a request; leaving must not touch it (no cooldown).
    await world.join_request(trip_id, rider.user_id, "approved")
    requests_before = await world.join_requests_snapshot(trip_id)
    leader_before = await world.row(trip_id, leader.user_id)
    rider_before = await world.row(trip_id, rider.user_id)

    response = await send(client, "POST", leave_path(trip_id), rider)

    assert response.status_code == 204, response.text
    assert response.content == b""
    row = await world.row(trip_id, rider.user_id)  # .one(): the row is kept, not deleted
    assert row.id == rider_before.id
    assert row.revoked_at is not None
    assert row.revoked_by == rider.user_id
    assert row.role == "rider"
    assert row.joined_at == rider_before.joined_at
    assert await world.row(trip_id, leader.user_id) == leader_before
    assert await world.join_requests_snapshot(trip_id) == requests_before


async def test_after_leaving_a_private_trip_reads_are_the_missing_404_and_writes_are_403(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip(visibility="private")
    leader = await world.account("Staying Leader")
    other_leader = await world.account("Leaving Leader")
    rider = await world.account("Leaving Rider")
    await world.member(trip_id, leader.user_id, "leader")
    await world.member(trip_id, other_leader.user_id, "leader")
    await world.member(trip_id, rider.user_id, "rider")

    # Before leaving, both can read the trip.
    for account in (rider, other_leader):
        assert (await send(client, "GET", f"/api/v2/trips/{trip_id}", account)).status_code == 200

    for account in (rider, other_leader):
        left = await send(client, "POST", leave_path(trip_id), account)
        assert left.status_code == 204, left.text

    missing = str(uuid4())
    for account in (rider, other_leader):
        for read in ("", "/stops", "/bikes", "/map"):
            hidden = await send(client, "GET", f"/api/v2/trips/{trip_id}{read}", account)
            absent = await send(client, "GET", f"/api/v2/trips/{missing}{read}", account)
            assert hidden.status_code == 404, (read, hidden.text)
            assert wire(hidden) == wire(absent), read

        stop_id = f"after-leave-{uuid4()}"
        writes = [
            (
                "POST",
                f"/api/v2/trips/{trip_id}/stops",
                {
                    "id": stop_id,
                    "name": "Not allowed",
                    "lat": -14.5,
                    "lng": 132.3,
                    "locationSource": "gps",
                    "arrivedAt": "2026-06-14T15:15:00+09:30",
                },
            ),
            (
                "POST",
                f"/api/v2/trips/{trip_id}/bikes",
                {"id": str(uuid4()), "riderName": "K", "make": "BMW", "model": "R80", "year": 1985},
            ),
            ("POST", leave_path(trip_id), None),
            ("POST", step_down_path(trip_id), None),
            ("POST", promote_path(trip_id, leader.user_id), None),
        ]
        for method, path, body in writes:
            refused = await send(client, method, path, account, json=body)
            assert_error(refused, 403, "FORBIDDEN", NO_LONGER_A_RIDER_MESSAGE)

        # The member list: member-read locates the trip by the revoked row, then 403.
        listed = await send(client, "GET", members_path(trip_id), account)
        assert_error(listed, 403, "FORBIDDEN")

    async with world.engine.connect() as conn:
        stops = await conn.scalar(
            select(text("count(*)"))
            .select_from(tables.stops)
            .where(tables.stops.c.trip_id == trip_id)
        )
        bikes = await conn.scalar(
            select(text("count(*)"))
            .select_from(tables.bikes)
            .where(tables.bikes.c.trip_id == trip_id)
        )
    assert (stops, bikes) == (0, 0)
    assert await world.active_leaders(trip_id) == [leader.user_id]


# --------------------------------------------------------------------------
# 5. Members list
# --------------------------------------------------------------------------


async def test_members_list_is_active_members_in_joined_at_then_user_id_order(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip(visibility="public")
    other_trip = await world.trip(visibility="public")
    base = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)

    early = await world.account("Early Leader")
    tied = [await world.account(f"Tied Rider {i}") for i in range(3)]
    late = await world.account("Late Rider")
    revoked = await world.account("Revoked Rider")
    departed = await world.account("Departed Rider")
    pending = await world.account("Pending Requester")
    elsewhere = await world.account("Other Trip Rider")

    # Inserted out of order on purpose, ties in descending user-id order.
    await world.member(trip_id, late.user_id, "rider", joined_at=base + timedelta(days=2))
    for account in sorted(tied, key=lambda a: a.user_id, reverse=True):
        await world.member(trip_id, account.user_id, "rider", joined_at=base + timedelta(days=1))
    await world.member(trip_id, early.user_id, "leader", joined_at=base)
    await world.member(trip_id, revoked.user_id, "rider", joined_at=base, revoked=True)
    await world.member(
        trip_id, departed.user_id, "leader", joined_at=base, revoked_by=departed.user_id
    )
    await world.join_request(trip_id, pending.user_id, "pending")
    await world.member(other_trip, elsewhere.user_id, "leader", joined_at=base)

    expected_order = [
        early.user_id,
        *sorted(a.user_id for a in tied),
        late.user_id,
    ]

    for caller in (early, late):  # a leader and a rider both get the list
        response = await send(client, "GET", members_path(trip_id), caller)
        assert response.status_code == 200, response.text
        body = response.json()
        assert [m["userId"] for m in body] == expected_order
        for member in body:
            assert set(member) == MEMBER_KEYS
        by_id = {m["userId"]: m for m in body}
        assert by_id[early.user_id]["role"] == "leader"
        assert by_id[early.user_id]["displayName"] == "Early Leader"
        assert parse(by_id[early.user_id]["joinedAt"]) == base
        assert by_id[late.user_id]["role"] == "rider"
        assert parse(by_id[late.user_id]["joinedAt"]) == base + timedelta(days=2)

        # Nothing identifying beyond the four fields: no username, email or slug.
        raw = response.text
        for account in (early, *tied, late, revoked, departed, pending, elsewhere):
            assert await world.username(account.user_id) not in raw
        for gone in (revoked, departed, pending, elsewhere):
            assert gone.user_id not in raw
        for slug in (*await world.slugs(trip_id), *await world.slugs(other_trip)):
            assert slug not in raw
        assert "@" not in raw
        assert "email" not in raw.lower()
        assert "username" not in raw.lower()
        assert "slug" not in raw.lower()


# --------------------------------------------------------------------------
# 6. The caller's own row changes between the gate and the lock
# --------------------------------------------------------------------------


@pytest.fixture
def before_lock(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[[], Awaitable[None]]], None]:
    """
    Run a callback (committed on its own connection) just before the trip-row lock.

    Deterministic stand-in for a concurrent request landing between the gate's
    membership read and the repository's ``FOR UPDATE``.
    """
    original = memberships._lock_trip

    def install(callback: Callable[[], Awaitable[None]]) -> None:
        async def patched(session: AsyncSession, trip_id: str) -> None:
            await callback()
            await original(session, trip_id)

        monkeypatch.setattr(memberships, "_lock_trip", patched)

    return install


async def _revoke(engine: AsyncEngine, trip_id: str, user_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(tables.trip_members)
            .where(
                tables.trip_members.c.trip_id == trip_id,
                tables.trip_members.c.user_id == user_id,
                tables.trip_members.c.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )


async def _demote(engine: AsyncEngine, trip_id: str, user_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(tables.trip_members)
            .where(
                tables.trip_members.c.trip_id == trip_id,
                tables.trip_members.c.user_id == user_id,
            )
            .values(role="rider")
        )


@pytest.mark.parametrize(
    ("action", "change", "message"),
    [
        ("step-down", "revoke", NO_LONGER_A_RIDER_MESSAGE),
        ("step-down", "demote", NOT_A_LEADER_MESSAGE),
        ("leave", "revoke", NO_LONGER_A_RIDER_MESSAGE),
        ("promote", "revoke", NO_LONGER_A_RIDER_MESSAGE),
        ("promote", "demote", NOT_A_LEADER_MESSAGE),
    ],
)
async def test_a_caller_changed_between_gate_and_lock_gets_the_gates_403(
    client: AsyncClient,
    world: World,
    before_lock: Callable[[Callable[[], Awaitable[None]]], None],
    action: str,
    change: str,
    message: str,
) -> None:
    trip_id = await world.trip()
    caller = await world.account("Changing Leader")
    other_leader = await world.account("Other Leader")
    rider = await world.account("Target Rider")
    await world.member(trip_id, caller.user_id, "leader")
    await world.member(trip_id, other_leader.user_id, "leader")
    await world.member(trip_id, rider.user_id, "rider")

    path = {
        "step-down": step_down_path(trip_id),
        "leave": leave_path(trip_id),
        "promote": promote_path(trip_id, rider.user_id),
    }[action]
    mutate = _revoke if change == "revoke" else _demote
    before_lock(lambda: mutate(world.engine, trip_id, caller.user_id))

    response = await send(client, "POST", path, caller)
    after_race = await world.members_snapshot(trip_id)

    # What the gate itself says on the next request, with no race.
    before_lock(lambda: asyncio.sleep(0))
    gate = await send(client, "POST", path, caller)

    assert gate.status_code == 403, gate.text  # sanity: the gate refuses this caller now
    assert_error(response, 403, "FORBIDDEN", message)
    assert (response.status_code, response.content) == (gate.status_code, gate.content)
    # Nothing but the injected change happened.
    assert await world.members_snapshot(trip_id) == after_race
    target = await world.row(trip_id, rider.user_id)
    assert (target.role, target.revoked_at) == ("rider", None)


# --------------------------------------------------------------------------
# 7. CSRF: a cross-site leadership request is blocked before it does anything
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["promote", "step-down", "leave"])
async def test_a_cross_site_leadership_request_is_403_and_changes_nothing(
    client: AsyncClient, world: World, action: str
) -> None:
    trip_id = await world.trip()
    caller = await world.account("CSRF Leader")
    other_leader = await world.account("Other Leader")
    rider = await world.account("CSRF Target")
    await world.member(trip_id, caller.user_id, "leader")
    await world.member(trip_id, other_leader.user_id, "leader")
    await world.member(trip_id, rider.user_id, "rider")
    path = {
        "promote": promote_path(trip_id, rider.user_id),
        "step-down": step_down_path(trip_id),
        "leave": leave_path(trip_id),
    }[action]
    before = await world.members_snapshot(trip_id)

    for cross_site in (
        {"Origin": "https://evil.example"},
        {"Origin": "https://testserver", "Sec-Fetch-Site": "cross-site"},
    ):
        blocked = await send(client, "POST", path, caller, headers=cross_site)
        assert_error(blocked, 403, "FORBIDDEN", CSRF_MESSAGE)
        assert await world.members_snapshot(trip_id) == before

    # Control: the same request same-origin reaches the route and succeeds.
    allowed = await send(client, "POST", path, caller)
    assert allowed.status_code in (200, 204), allowed.text
    assert await world.members_snapshot(trip_id) != before
