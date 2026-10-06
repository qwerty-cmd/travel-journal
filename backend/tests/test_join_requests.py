"""
The requester's side of the join lifecycle (t-am-join-requester).

Written from ``docs/api-contract.md`` -- "Notes per endpoint (v2)" (Join
request create, Cancel, Me / join-requests), "Idempotency: additions", "Rate
limits and lockout" (``join``) and "CSRF" -- and the task's acceptance criteria
(Obligation 7, requester half). Rejected, blocked and revoked rows are inserted
straight into the tables: the leader's decision endpoint arrives later.

The gate cells (401 anonymous, the private trip's 404 for each identity, 409
for an active rider or leader, 409 for a just-revoked rider with ``revoked_by``
NULL, cancel by each identity) are the access matrix's section 16 in
``test_access_matrix.py`` and are not repeated here.

The cooldown clock is the route module's ``_utcnow``, monkeypatched; the rate
limit clock is the registry's, frozen as in ``test_ratelimit.py``.

Sections 9-14 are the leader's side (t-am-join-leader), written from the
contract's leader-route rows and the "Trip join-requests (leader)", "Decision"
and "Unblock" notes: Obligation 7's leader half, approve, decisions, races,
the leader list and CSRF. Their gate cells are the access matrix's section 17.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Callable
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.main
from app.api.routes.v2 import join_requests as join_routes
from app.core import ratelimit
from app.data import tables
from app.data.db import get_session
from app.data.repositories import memberships as memberships_repo

MY_JOIN_REQUEST_KEYS = {"id", "tripId", "tripName", "state", "message", "createdAt"}
CSRF_MESSAGE = "This request came from another site and was blocked."
COOLDOWN = timedelta(days=7)
RACE_RUNS = 5

# A fixed "now" the route sees; inserted decisions are placed relative to it.
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


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


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> Callable[[datetime], None]:
    """Set the route's clock: ``now(T0 + delta)``. Starts at ``T0``."""
    current = {"at": T0}
    monkeypatch.setattr(join_routes, "_utcnow", lambda: current["at"])

    def set_now(at: datetime) -> None:
        current["at"] = at

    return set_now


class FrozenClock:
    """The rate-limit registry's clock: stands still until advanced."""

    def __init__(self) -> None:
        self.at = 10_000.0

    def __call__(self) -> float:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += seconds


@pytest.fixture
def rl_clock() -> FrozenClock:
    frozen = FrozenClock()
    ratelimit.registry.clock = frozen  # conftest's autouse fixture restores it
    return frozen


@dataclass
class World:
    """Trips, accounts and rows made by one test, removed on teardown."""

    engine: AsyncEngine
    trip_ids: list[str] = field(default_factory=list)
    user_ids: list[str] = field(default_factory=list)

    async def trip(self, visibility: str = "public", name: str = "Join trip") -> str:
        trip_id = str(uuid4())
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.trips.insert().values(
                    id=trip_id,
                    name=name,
                    start_date=date(2026, 6, 1),
                    visibility=visibility,
                    public_delay_hours=0,
                )
            )
        self.trip_ids.append(trip_id)
        return trip_id

    async def trips(self, count: int) -> list[str]:
        ids = [str(uuid4()) for _ in range(count)]
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.trips.insert(),
                [
                    {
                        "id": i,
                        "name": "Bulk trip",
                        "start_date": date(2026, 6, 1),
                        "visibility": "public",
                    }
                    for i in ids
                ],
            )
        self.trip_ids.extend(ids)
        return ids

    async def account(self, name: str = "Requester") -> SignedInAccount:
        account = await create_signed_in_account(self.engine, display_name=name)
        self.user_ids.append(account.user_id)
        return account

    async def bare_users(self, count: int) -> list[str]:
        """Users with no session: fillers that only own rows."""
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
        self,
        trip_id: str,
        user_id: str,
        state: str,
        *,
        decided_at: datetime | None = None,
        created_at: datetime | None = None,
        message: str | None = None,
        request_id: str | None = None,
        via: str = "direct",
    ) -> str:
        request_id = request_id or str(uuid4())
        values: dict[str, Any] = {
            "id": request_id,
            "trip_id": trip_id,
            "user_id": user_id,
            "state": state,
            "message": message,
            "via": via,
            "decided_at": None if state == "pending" else (decided_at or T0),
        }
        if created_at is not None:
            values["created_at"] = created_at
        async with self.engine.begin() as conn:
            await conn.execute(tables.join_requests.insert().values(**values))
        return request_id

    async def requests_bulk(self, rows: list[tuple[str, str]], state: str = "pending") -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.join_requests.insert(),
                [
                    {
                        "id": str(uuid4()),
                        "trip_id": t,
                        "user_id": u,
                        "state": state,
                        "decided_at": None if state == "pending" else T0,
                    }
                    for t, u in rows
                ],
            )

    async def revoked_member(
        self, trip_id: str, user_id: str, *, revoked_at: datetime, revoked_by: str | None
    ) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.trip_members.insert().values(
                    id=str(uuid4()),
                    trip_id=trip_id,
                    user_id=user_id,
                    role="rider",
                    joined_at=revoked_at - timedelta(days=30),
                    revoked_at=revoked_at,
                    revoked_by=revoked_by,
                )
            )

    async def rows(self, **where: str) -> list[Any]:
        """``join_requests`` rows matching ``trip_id``/``user_id``, ordered by id."""
        query = select(tables.join_requests)
        for column, value in where.items():
            query = query.where(tables.join_requests.c[column] == value)
        async with self.engine.connect() as conn:
            return list((await conn.execute(query.order_by(tables.join_requests.c.id))).all())

    async def pending_count(self, **where: str) -> int:
        query = select(func.count()).where(tables.join_requests.c.state == "pending")
        for column, value in where.items():
            query = query.where(tables.join_requests.c[column] == value)
        async with self.engine.connect() as conn:
            return await conn.scalar(query)


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
        if made.user_ids:
            await delete_accounts(migrated_engine, made.user_ids)
        if made.trip_ids:
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    tables.trips.delete().where(tables.trips.c.id.in_(made.trip_ids))
                )


def create_path(trip_id: str) -> str:
    return f"/api/v2/trips/{trip_id}/join-requests"


def cancel_path(request_id: str) -> str:
    return f"/api/v2/join-requests/{request_id}/cancel"


ME_PATH = "/api/v2/me/join-requests"


async def ask(
    client: AsyncClient,
    trip_id: str,
    who: SignedInAccount,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    return await client.post(
        create_path(trip_id),
        json={} if body is None else body,
        headers={**who.headers, **(headers or {})},
    )


async def cancel(
    client: AsyncClient,
    request_id: str,
    who: SignedInAccount,
    headers: dict[str, str] | None = None,
) -> Response:
    return await client.post(cancel_path(request_id), headers={**who.headers, **(headers or {})})


async def mine(client: AsyncClient, who: SignedInAccount) -> Response:
    return await client.get(ME_PATH, headers=who.headers)


def assert_conflict(response: Response) -> None:
    assert response.status_code == 409, (response.status_code, response.text)
    assert response.json()["error"]["code"] == "CONFLICT", response.text


def without_date(response: Response) -> tuple[int, list[tuple[str, str]], bytes]:
    headers = [(k.lower(), v) for k, v in response.headers.multi_items() if k.lower() != "date"]
    return response.status_code, headers, response.content


# --------------------------------------------------------------------------
# 1. Create: new, duplicate, message rules
# --------------------------------------------------------------------------


async def test_create_returns_201_with_my_join_request_out(
    client: AsyncClient, world: World, now: Callable[[datetime], None]
) -> None:
    trip_id = await world.trip(name="Coast to coast")
    me = await world.account()

    response = await ask(client, trip_id, me, {"message": "  Can I come?  "})

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == MY_JOIN_REQUEST_KEYS
    assert body["tripId"] == trip_id
    assert body["tripName"] == "Coast to coast"
    assert body["state"] == "pending"
    assert body["message"] == "Can I come?"  # trimmed
    [row] = await world.rows(trip_id=trip_id)
    assert (row.id, row.user_id, row.state, row.via, row.message) == (
        body["id"],
        me.user_id,
        "pending",
        "direct",
        "Can I come?",
    )


async def test_many_sequential_new_creates_all_succeed(client: AsyncClient, world: World) -> None:
    """
    Every new request is a 201, however many came before it on the pool.

    Eight inserts in a row: past the 5 custom-plan executions after which
    Postgres may switch a prepared statement to a generic plan.
    """
    me = await world.account()
    statuses = [(await ask(client, t, me)).status_code for t in await world.trips(8)]
    assert statuses == [201] * 8
    assert await world.pending_count(user_id=me.user_id) == 8


@pytest.mark.parametrize("body", [{}, {"message": None}, {"message": ""}, {"message": "   "}])
async def test_an_absent_or_empty_message_is_stored_as_null(
    client: AsyncClient, world: World, body: dict[str, Any]
) -> None:
    trip_id = await world.trip()
    me = await world.account()

    response = await ask(client, trip_id, me, body)

    assert response.status_code == 201, response.text
    assert response.json()["message"] is None
    [row] = await world.rows(trip_id=trip_id)
    assert row.message is None


async def test_a_message_over_280_characters_is_422_and_nothing_is_written(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    me = await world.account()

    response = await ask(client, trip_id, me, {"message": "x" * 281})

    assert response.status_code == 422, response.text
    assert await world.rows(trip_id=trip_id) == []
    assert (await ask(client, trip_id, me, {"message": "x" * 280})).status_code == 201


async def test_a_duplicate_create_while_pending_is_200_with_the_same_request_unchanged(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    me = await world.account()
    first = await ask(client, trip_id, me, {"message": "first"})
    assert first.status_code == 201, first.text
    [before] = await world.rows(trip_id=trip_id)

    second = await ask(client, trip_id, me, {"message": "second"})

    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert second.json()["message"] == "first"
    assert await world.rows(trip_id=trip_id) == [before]


async def test_parallel_duplicate_creates_leave_exactly_one_pending_row(
    client: AsyncClient, world: World
) -> None:
    for _ in range(RACE_RUNS):
        trip_id = await world.trip()
        me = await world.account()

        responses = await asyncio.gather(*(ask(client, trip_id, me) for _ in range(4)))

        statuses = sorted(r.status_code for r in responses)
        assert statuses == [200, 200, 200, 201], [r.text for r in responses]
        assert len({r.json()["id"] for r in responses}) == 1
        rows = await world.rows(trip_id=trip_id)
        assert [(r.user_id, r.state) for r in rows] == [(me.user_id, "pending")]


# --------------------------------------------------------------------------
# 2. Rejected cooldown, blocked
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (COOLDOWN - timedelta(seconds=1), 409),
        # "less than 7 days ago" is the refusal; exactly 7 days is not less.
        (COOLDOWN, 201),
        (COOLDOWN + timedelta(seconds=1), 201),
    ],
    ids=["7d-1s", "7d-exact", "7d+1s"],
)
async def test_a_rejection_holds_a_re_request_for_7_days(
    client: AsyncClient,
    world: World,
    now: Callable[[datetime], None],
    elapsed: timedelta,
    expected: int,
) -> None:
    trip_id = await world.trip()
    me = await world.account()
    old = await world.request(trip_id, me.user_id, "rejected", decided_at=T0)
    now(T0 + elapsed)

    response = await ask(client, trip_id, me)

    if expected == 409:
        assert_conflict(response)
        assert [r.id for r in await world.rows(trip_id=trip_id)] == [old]
    else:
        assert response.status_code == 201, response.text
        assert response.json()["id"] != old
        assert await world.pending_count(trip_id=trip_id) == 1


async def test_a_recent_rejection_counts_even_with_an_older_one_present(
    client: AsyncClient, world: World, now: Callable[[datetime], None]
) -> None:
    trip_id = await world.trip()
    me = await world.account()
    await world.request(trip_id, me.user_id, "rejected", decided_at=T0 - timedelta(days=30))
    await world.request(trip_id, me.user_id, "rejected", decided_at=T0 - timedelta(days=1))

    assert_conflict(await ask(client, trip_id, me))
    assert await world.pending_count(trip_id=trip_id) == 0


@pytest.mark.parametrize("age", [timedelta(0), timedelta(days=365)], ids=["fresh", "a-year-old"])
async def test_a_blocked_request_refuses_every_create(
    client: AsyncClient, world: World, now: Callable[[datetime], None], age: timedelta
) -> None:
    trip_id = await world.trip()
    me = await world.account()
    await world.request(trip_id, me.user_id, "blocked", decided_at=T0 - age)

    assert_conflict(await ask(client, trip_id, me))
    assert await world.pending_count(trip_id=trip_id) == 0


async def test_a_rejection_on_another_trip_does_not_hold_this_one(
    client: AsyncClient, world: World, now: Callable[[datetime], None]
) -> None:
    rejected_on, asking_on = await world.trip(), await world.trip()
    me = await world.account()
    await world.request(rejected_on, me.user_id, "blocked")

    assert (await ask(client, asking_on, me)).status_code == 201


# --------------------------------------------------------------------------
# 3. Revocation cooldown
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("revoker", "elapsed", "expected"),
    [
        ("other", timedelta(days=1), 409),
        ("other", COOLDOWN - timedelta(seconds=1), 409),
        ("other", COOLDOWN + timedelta(seconds=1), 201),
        ("self", timedelta(minutes=1), 201),
        # NULL is the operator CLI (migration 0003): someone else, not a self-leave.
        ("operator", timedelta(days=1), 409),
        ("operator", COOLDOWN + timedelta(seconds=1), 201),
    ],
)
async def test_a_revocation_by_someone_else_holds_a_re_request_for_7_days(
    client: AsyncClient,
    world: World,
    now: Callable[[datetime], None],
    revoker: str,
    elapsed: timedelta,
    expected: int,
) -> None:
    trip_id = await world.trip()
    me = await world.account()
    leader = await world.account("Leader")
    revoked_by = {"other": leader.user_id, "self": me.user_id, "operator": None}[revoker]
    await world.revoked_member(trip_id, me.user_id, revoked_at=T0, revoked_by=revoked_by)
    now(T0 + elapsed)

    response = await ask(client, trip_id, me)

    if expected == 409:
        assert_conflict(response)
        assert await world.rows(trip_id=trip_id) == []
    else:
        assert response.status_code == 201, response.text
        assert await world.pending_count(trip_id=trip_id) == 1


# --------------------------------------------------------------------------
# 4. Caps: 20 pending per user, 100 pending per trip
# --------------------------------------------------------------------------


async def test_the_21st_pending_request_across_trips_is_409(
    client: AsyncClient, world: World
) -> None:
    me = await world.account()
    held = await world.trips(20)
    await world.requests_bulk([(t, me.user_id) for t in held])
    extra = await world.trip()

    assert_conflict(await ask(client, extra, me))
    assert await world.pending_count(user_id=me.user_id) == 20
    # Non-pending rows don't count: cancel one and the slot is free.
    [one] = await world.rows(trip_id=held[0])
    assert (await cancel(client, one.id, me)).status_code == 200
    assert (await ask(client, extra, me)).status_code == 201


async def test_the_101st_pending_request_on_a_trip_is_409(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    fillers = await world.bare_users(100)
    await world.requests_bulk([(trip_id, u) for u in fillers])
    me = await world.account()

    assert_conflict(await ask(client, trip_id, me))
    assert await world.pending_count(trip_id=trip_id) == 100


async def test_parallel_creates_at_19_pending_do_not_exceed_the_user_cap(
    client: AsyncClient, world: World
) -> None:
    for _ in range(RACE_RUNS):
        me = await world.account()
        await world.requests_bulk([(t, me.user_id) for t in await world.trips(19)])
        a, b, c = await world.trips(3)

        responses = await asyncio.gather(*(ask(client, t, me) for t in (a, b, c)))

        assert sorted(r.status_code for r in responses) == [201, 409, 409], [
            r.text for r in responses
        ]
        assert await world.pending_count(user_id=me.user_id) == 20


async def test_parallel_creates_at_99_pending_do_not_exceed_the_trip_cap(
    client: AsyncClient, world: World
) -> None:
    for _ in range(RACE_RUNS):
        trip_id = await world.trip()
        await world.requests_bulk([(trip_id, u) for u in await world.bare_users(99)])
        askers = [await world.account() for _ in range(3)]

        responses = await asyncio.gather(*(ask(client, trip_id, who) for who in askers))

        assert sorted(r.status_code for r in responses) == [201, 409, 409], [
            r.text for r in responses
        ]
        assert await world.pending_count(trip_id=trip_id) == 100


# --------------------------------------------------------------------------
# 5. Cancel
# --------------------------------------------------------------------------


async def test_cancel_then_an_immediate_re_request_is_201(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    me = await world.account()
    first = (await ask(client, trip_id, me, {"message": "hi"})).json()

    cancelled = await cancel(client, first["id"], me)

    assert cancelled.status_code == 200, cancelled.text
    assert set(cancelled.json()) == MY_JOIN_REQUEST_KEYS
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["id"] == first["id"]
    [row] = await world.rows(trip_id=trip_id)
    assert row.state == "cancelled" and row.decided_at is not None

    again = await ask(client, trip_id, me)
    assert again.status_code == 201, again.text
    assert again.json()["id"] != first["id"]
    states = sorted(r.state for r in await world.rows(trip_id=trip_id))
    assert states == ["cancelled", "pending"]


async def test_cancelling_an_already_cancelled_request_is_200_and_writes_nothing(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    me = await world.account()
    request_id = (await ask(client, trip_id, me)).json()["id"]
    first = await cancel(client, request_id, me)
    before = await world.rows(trip_id=trip_id)

    second = await cancel(client, request_id, me)

    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert await world.rows(trip_id=trip_id) == before


@pytest.mark.parametrize("state", ["approved", "rejected", "blocked"])
async def test_cancelling_a_decided_request_is_409_and_writes_nothing(
    client: AsyncClient, world: World, state: str
) -> None:
    trip_id = await world.trip()
    me = await world.account()
    request_id = await world.request(trip_id, me.user_id, state)
    before = await world.rows(trip_id=trip_id)

    assert_conflict(await cancel(client, request_id, me))
    assert await world.rows(trip_id=trip_id) == before


async def test_someone_elses_request_and_an_unknown_id_get_the_same_404(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    owner, stranger = await world.account("Owner"), await world.account("Stranger")
    request_id = (await ask(client, trip_id, owner)).json()["id"]
    before = await world.rows(trip_id=trip_id)

    theirs = await cancel(client, request_id, stranger)
    unknown = await cancel(client, str(uuid4()), stranger)

    assert theirs.status_code == 404, theirs.text
    assert theirs.json()["error"]["code"] == "NOT_FOUND"
    assert without_date(theirs) == without_date(unknown)
    assert await world.rows(trip_id=trip_id) == before


# --------------------------------------------------------------------------
# 6. GET /me/join-requests
# --------------------------------------------------------------------------


async def test_my_join_requests_lists_only_mine_newest_first_with_blocked_as_rejected(
    client: AsyncClient, world: World
) -> None:
    me, other = await world.account(), await world.account("Other")
    t_blocked = await world.trip(name="Blocked trip")
    t_rejected = await world.trip(name="Rejected trip")
    t_pending = await world.trip(name="Pending trip")
    blocked = await world.request(
        t_blocked, me.user_id, "blocked", created_at=T0 - timedelta(days=3), message="b"
    )
    rejected = await world.request(
        t_rejected, me.user_id, "rejected", created_at=T0 - timedelta(days=2)
    )
    pending = await world.request(
        t_pending, me.user_id, "pending", created_at=T0 - timedelta(days=1), message="p"
    )
    await world.request(t_pending, other.user_id, "pending")

    response = await mine(client, me)

    assert response.status_code == 200, response.text
    body = response.json()
    assert all(set(item) == MY_JOIN_REQUEST_KEYS for item in body)
    assert [(i["id"], i["tripId"], i["tripName"], i["state"], i["message"]) for i in body] == [
        (pending, t_pending, "Pending trip", "pending", "p"),
        (rejected, t_rejected, "Rejected trip", "rejected", None),
        (blocked, t_blocked, "Blocked trip", "rejected", "b"),
    ]
    assert "blocked" not in response.text


async def test_my_join_requests_is_at_most_100(client: AsyncClient, world: World) -> None:
    me = await world.account()
    trip_id = await world.trip()
    for days in range(101):
        await world.request(trip_id, me.user_id, "cancelled", created_at=T0 - timedelta(days=days))

    body = (await mine(client, me)).json()

    assert len(body) == 100
    newest_first = [datetime.fromisoformat(i["createdAt"]) for i in body]
    assert newest_first == sorted(newest_first, reverse=True)
    assert newest_first[-1] == T0 - timedelta(days=99)


async def test_a_trip_that_went_private_still_shows_its_name_on_my_request(
    client: AsyncClient, world: World
) -> None:
    """
    The contract lists ``tripName`` for every one of your requests, with no
    visibility exception, and a legacy claim creates requests on private trips
    by design. This pins the current behaviour: the requester, now a
    non-member of a private trip, sees its (current) name here while the trip
    itself is their 404.
    """
    me = await world.account()
    trip_id = await world.trip(name="Was public")
    request_id = (await ask(client, trip_id, me)).json()["id"]
    async with world.engine.begin() as conn:
        await conn.execute(
            tables.trips.update()
            .where(tables.trips.c.id == trip_id)
            .values(visibility="private", name="Renamed while private")
        )

    trip = await client.get(f"/api/v2/trips/{trip_id}", headers=me.headers)
    [item] = (await mine(client, me)).json()

    assert trip.status_code == 404
    assert (item["id"], item["tripName"]) == (request_id, "Renamed while private")


# --------------------------------------------------------------------------
# 7. The `join` limiter: 10 an hour per account
# --------------------------------------------------------------------------


async def test_join_is_10_an_hour_per_account(
    client: AsyncClient, world: World, rl_clock: FrozenClock
) -> None:
    trip_id = await world.trip()
    me, someone = await world.account(), await world.account("Someone")

    for _ in range(10):
        assert (await ask(client, trip_id, me)).status_code in (200, 201)

    limited = await ask(client, trip_id, me)
    assert limited.status_code == 429, limited.text
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    retry_after = int(limited.headers["retry-after"])
    assert retry_after == 360  # one token of 10/hour

    # Per user, not per IP: another account on the same address is untouched.
    assert (await ask(client, trip_id, someone)).status_code == 201

    rl_clock.advance(retry_after - 1)
    assert (await ask(client, trip_id, me)).status_code == 429
    rl_clock.advance(1)
    assert (await ask(client, trip_id, me)).status_code == 200


async def test_cancel_does_not_spend_join_tokens(
    client: AsyncClient, world: World, rl_clock: FrozenClock
) -> None:
    trip_id = await world.trip()
    me = await world.account()
    for _ in range(5):
        request_id = (await ask(client, trip_id, me)).json()["id"]
        assert (await cancel(client, request_id, me)).status_code == 200
    for _ in range(5):
        assert (await ask(client, trip_id, me)).status_code in (200, 201)
    assert (await ask(client, trip_id, me)).status_code == 429


# --------------------------------------------------------------------------
# 8. CSRF: both unsafe routes, nothing written
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}],
    ids=["foreign-origin", "sec-fetch-cross-site"],
)
async def test_a_cross_site_create_or_cancel_is_403_and_writes_nothing(
    client: AsyncClient, world: World, headers: dict[str, str]
) -> None:
    trip_id = await world.trip()
    me = await world.account()

    created = await ask(client, trip_id, me, headers=headers)
    assert created.status_code == 403, created.text
    assert created.json() == {"error": {"code": "FORBIDDEN", "message": CSRF_MESSAGE}}
    assert await world.rows(trip_id=trip_id) == []

    request_id = (await ask(client, trip_id, me)).json()["id"]
    before = await world.rows(trip_id=trip_id)
    cancelled = await cancel(client, request_id, me, headers=headers)
    assert cancelled.status_code == 403, cancelled.text
    assert cancelled.json()["error"]["message"] == CSRF_MESSAGE
    assert await world.rows(trip_id=trip_id) == before


# ==========================================================================
# The leader's side (t-am-join-leader)
# ==========================================================================

TRIP_JOIN_REQUEST_KEYS = {"id", "requester", "state", "via", "message", "createdAt"}
PERSON_KEYS = {"userId", "displayName"}
ACTION_STATE = {"approve": "approved", "reject": "rejected", "reject_and_block": "blocked"}


async def leader_of(world: World, trip_id: str, name: str = "Leader") -> SignedInAccount:
    account = await world.account(name)
    await grant_membership(world.engine, trip_id, account.user_id, role="leader")
    return account


async def slugged_trip(world: World) -> tuple[str, str]:
    """A public trip that also has legacy slugs; returns ``(trip_id, rider_slug)``."""
    trip_id = await world.trip()
    rider_slug = f"r{secrets.token_hex(12)}"
    async with world.engine.begin() as conn:
        await conn.execute(
            tables.trips.update()
            .where(tables.trips.c.id == trip_id)
            .values(rider_slug=rider_slug, viewer_slug=f"v{secrets.token_hex(12)}")
        )
    return trip_id, rider_slug


def decision_path(trip_id: str, request_id: str) -> str:
    return f"/api/v2/trips/{trip_id}/join-requests/{request_id}/decision"


def unblock_path(trip_id: str, request_id: str) -> str:
    return f"/api/v2/trips/{trip_id}/join-requests/{request_id}/unblock"


async def decide(
    client: AsyncClient,
    trip_id: str,
    request_id: str,
    action: str,
    who: SignedInAccount,
    headers: dict[str, str] | None = None,
) -> Response:
    return await client.post(
        decision_path(trip_id, request_id),
        json={"action": action},
        headers={**who.headers, **(headers or {})},
    )


async def unblock(
    client: AsyncClient,
    trip_id: str,
    request_id: str,
    who: SignedInAccount,
    headers: dict[str, str] | None = None,
) -> Response:
    return await client.post(
        unblock_path(trip_id, request_id), headers={**who.headers, **(headers or {})}
    )


async def leader_list(
    client: AsyncClient, trip_id: str, who: SignedInAccount, state: str | None = None
) -> Response:
    params = {} if state is None else {"state": state}
    return await client.get(create_path(trip_id), params=params, headers=who.headers)


async def request_row(world: World, request_id: str) -> Any:
    async with world.engine.connect() as conn:
        return (
            await conn.execute(
                select(tables.join_requests).where(tables.join_requests.c.id == request_id)
            )
        ).one()


async def memberships(world: World, trip_id: str, user_id: str) -> list[Any]:
    """Every ``trip_members`` row (active or revoked) for this user on this trip."""
    async with world.engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    select(tables.trip_members)
                    .where(tables.trip_members.c.trip_id == trip_id)
                    .where(tables.trip_members.c.user_id == user_id)
                    .order_by(tables.trip_members.c.id)
                )
            ).all()
        )


# --------------------------------------------------------------------------
# 9. Obligation 7, leader half: block through the endpoint, then unblock
# --------------------------------------------------------------------------


async def test_a_block_refuses_every_create_until_unblock_then_the_original_cooldown_holds(
    client: AsyncClient, world: World, now: Callable[[datetime], None]
) -> None:
    trip_id = await world.trip()
    leader = await leader_of(world, trip_id)
    me = await world.account()
    request_id = (await ask(client, trip_id, me)).json()["id"]

    blocked = await decide(client, trip_id, request_id, "reject_and_block", leader)
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["state"] == "blocked"
    decided_at = (await request_row(world, request_id)).decided_at
    assert decided_at is not None

    # Blocked: refused at any age, and nothing is written.
    for age in (timedelta(0), timedelta(days=1), timedelta(days=8), timedelta(days=365)):
        now(decided_at + age)
        assert_conflict(await ask(client, trip_id, me))
    assert [r.id for r in await world.rows(trip_id=trip_id)] == [request_id]

    now(decided_at + timedelta(days=3))
    unblocked = await unblock(client, trip_id, request_id, leader)
    assert unblocked.status_code == 200, unblocked.text
    assert unblocked.json()["state"] == "rejected"
    assert set(unblocked.json()) == TRIP_JOIN_REQUEST_KEYS
    row = await request_row(world, request_id)
    assert (row.state, row.decided_at) == ("rejected", decided_at)

    # The 7-day cooldown runs from the original decision, not from the unblock.
    assert_conflict(await ask(client, trip_id, me))
    now(decided_at + COOLDOWN - timedelta(seconds=1))
    assert_conflict(await ask(client, trip_id, me))
    assert await world.pending_count(trip_id=trip_id) == 0

    now(decided_at + COOLDOWN + timedelta(seconds=1))
    again = await ask(client, trip_id, me)
    assert again.status_code == 201, again.text
    assert again.json()["id"] != request_id
    assert await world.pending_count(trip_id=trip_id) == 1


async def test_unblocking_twice_is_409_the_second_time_and_changes_nothing(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    leader = await leader_of(world, trip_id)
    me = await world.account()
    request_id = await world.request(trip_id, me.user_id, "blocked")

    assert (await unblock(client, trip_id, request_id, leader)).status_code == 200
    before = await request_row(world, request_id)

    second = await unblock(client, trip_id, request_id, leader)
    assert_conflict(second)
    assert await request_row(world, request_id) == before


@pytest.mark.parametrize("state", ["pending", "approved", "rejected", "cancelled"])
async def test_unblocking_a_request_that_is_not_blocked_is_409(
    client: AsyncClient, world: World, state: str
) -> None:
    trip_id = await world.trip()
    leader = await leader_of(world, trip_id)
    me = await world.account()
    request_id = await world.request(trip_id, me.user_id, state)
    before = await request_row(world, request_id)

    assert_conflict(await unblock(client, trip_id, request_id, leader))
    assert await request_row(world, request_id) == before


# --------------------------------------------------------------------------
# 10. Approve: a rider membership, and the requester can write
# --------------------------------------------------------------------------


async def test_approve_makes_the_requester_a_rider_who_can_write(
    client: AsyncClient, world: World
) -> None:
    trip_id, rider_slug = await slugged_trip(world)
    leader = await leader_of(world, trip_id)
    me = await world.account()
    request_id = (await ask(client, trip_id, me)).json()["id"]

    # Before: a pending requester's role is `pending`, and they cannot write.
    trip = await client.get(f"/api/v2/trips/{trip_id}", headers=me.headers)
    assert trip.json()["viewer"]["role"] == "pending"
    bike = {"riderName": "Kim", "make": "BMW", "model": "R80", "year": 1985}
    refused = await client.post(
        f"/api/trips/{rider_slug}/bikes", json={"id": str(uuid4()), **bike}, headers=me.headers
    )
    assert refused.status_code == 403, refused.text

    approved = await decide(client, trip_id, request_id, "approve", leader)

    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert set(body) == TRIP_JOIN_REQUEST_KEYS
    assert body["id"] == request_id
    assert body["state"] == "approved"
    assert body["requester"] == {"userId": me.user_id, "displayName": me.display_name}

    row = await request_row(world, request_id)
    assert row.state == "approved"
    assert row.decided_by == leader.user_id
    assert row.decided_at is not None
    members = await memberships(world, trip_id, me.user_id)
    assert [(m.role, m.revoked_at) for m in members] == [("rider", None)]

    legacy = await client.post(
        f"/api/trips/{rider_slug}/bikes", json={"id": str(uuid4()), **bike}, headers=me.headers
    )
    assert legacy.status_code == 201, legacy.text
    v2 = await client.post(
        f"/api/v2/trips/{trip_id}/bikes", json={"id": str(uuid4()), **bike}, headers=me.headers
    )
    assert v2.status_code == 201, v2.text
    trip = await client.get(f"/api/v2/trips/{trip_id}", headers=me.headers)
    assert trip.json()["viewer"]["role"] == "rider"


async def test_approving_a_requester_who_is_already_a_member_adds_no_second_membership(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    leader = await leader_of(world, trip_id)
    me = await world.account()
    request_id = await world.request(trip_id, me.user_id, "pending")
    await grant_membership(world.engine, trip_id, me.user_id)
    before = await memberships(world, trip_id, me.user_id)

    response = await decide(client, trip_id, request_id, "approve", leader)

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "approved"
    assert await memberships(world, trip_id, me.user_id) == before


# --------------------------------------------------------------------------
# 11. Decisions: idempotent repeat, conflicts, cancelled, other trip's id
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", list(ACTION_STATE))
async def test_a_decision_sets_state_decided_by_and_decided_at_and_a_repeat_changes_nothing(
    client: AsyncClient, world: World, action: str
) -> None:
    trip_id = await world.trip()
    leader = await leader_of(world, trip_id)
    me = await world.account()
    request_id = await world.request(trip_id, me.user_id, "pending")

    first = await decide(client, trip_id, request_id, action, leader)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == ACTION_STATE[action]
    row = await request_row(world, request_id)
    assert (row.state, row.decided_by) == (ACTION_STATE[action], leader.user_id)
    assert row.decided_at is not None
    members = await memberships(world, trip_id, me.user_id)
    assert len(members) == (1 if action == "approve" else 0)

    repeat = await decide(client, trip_id, request_id, action, leader)

    assert repeat.status_code == 200, repeat.text
    assert repeat.json() == first.json()
    assert await request_row(world, request_id) == row
    assert await memberships(world, trip_id, me.user_id) == members


@pytest.mark.parametrize(
    ("first", "second"),
    [(a, b) for a in ACTION_STATE for b in ACTION_STATE if a != b],
)
async def test_a_different_decision_on_a_decided_request_is_409_and_changes_nothing(
    client: AsyncClient, world: World, first: str, second: str
) -> None:
    trip_id = await world.trip()
    leader = await leader_of(world, trip_id)
    me = await world.account()
    request_id = await world.request(trip_id, me.user_id, "pending")
    assert (await decide(client, trip_id, request_id, first, leader)).status_code == 200
    row = await request_row(world, request_id)
    members = await memberships(world, trip_id, me.user_id)

    assert_conflict(await decide(client, trip_id, request_id, second, leader))
    assert await request_row(world, request_id) == row
    assert await memberships(world, trip_id, me.user_id) == members


@pytest.mark.parametrize("action", list(ACTION_STATE))
async def test_deciding_a_cancelled_request_is_409_and_changes_nothing(
    client: AsyncClient, world: World, action: str
) -> None:
    trip_id = await world.trip()
    leader = await leader_of(world, trip_id)
    me = await world.account()
    request_id = (await ask(client, trip_id, me)).json()["id"]
    assert (await cancel(client, request_id, me)).status_code == 200
    row = await request_row(world, request_id)

    assert_conflict(await decide(client, trip_id, request_id, action, leader))
    assert await request_row(world, request_id) == row
    assert await memberships(world, trip_id, me.user_id) == []


async def test_another_trips_request_id_is_the_same_404_as_an_unknown_id(
    client: AsyncClient, world: World
) -> None:
    trip_id, other_trip = await world.trip(), await world.trip()
    leader = await leader_of(world, trip_id)
    me = await world.account()
    pending_elsewhere = await world.request(other_trip, me.user_id, "pending")
    blocked_elsewhere = await world.request(other_trip, (await world.account()).user_id, "blocked")
    unknown = str(uuid4())

    foreign = await decide(client, trip_id, pending_elsewhere, "approve", leader)
    missing = await decide(client, trip_id, unknown, "approve", leader)
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["error"]["code"] == "NOT_FOUND"
    assert without_date(foreign) == without_date(missing)

    foreign = await unblock(client, trip_id, blocked_elsewhere, leader)
    missing = await unblock(client, trip_id, unknown, leader)
    assert foreign.status_code == 404, foreign.text
    assert without_date(foreign) == without_date(missing)

    assert (await request_row(world, pending_elsewhere)).state == "pending"
    assert (await request_row(world, blocked_elsewhere)).state == "blocked"
    assert await memberships(world, trip_id, me.user_id) == []
    assert await memberships(world, other_trip, me.user_id) == []


# --------------------------------------------------------------------------
# 12. Races
# --------------------------------------------------------------------------


async def test_two_leaders_deciding_differently_at_once_exactly_one_wins(
    client: AsyncClient, world: World
) -> None:
    for _ in range(RACE_RUNS):
        trip_id = await world.trip()
        approver = await leader_of(world, trip_id, "Approver")
        blocker = await leader_of(world, trip_id, "Blocker")
        me = await world.account()
        request_id = await world.request(trip_id, me.user_id, "pending")

        approve, block = await asyncio.gather(
            decide(client, trip_id, request_id, "approve", approver),
            decide(client, trip_id, request_id, "reject_and_block", blocker),
        )

        assert sorted([approve.status_code, block.status_code]) == [200, 409], (
            approve.text,
            block.text,
        )
        row = await request_row(world, request_id)
        members = await memberships(world, trip_id, me.user_id)
        if approve.status_code == 200:
            assert_conflict(block)
            assert (row.state, row.decided_by) == ("approved", approver.user_id)
            assert [(m.role, m.revoked_at) for m in members] == [("rider", None)]
        else:
            assert_conflict(approve)
            assert (row.state, row.decided_by) == ("blocked", blocker.user_id)
            assert members == []


async def test_approve_racing_the_requesters_cancel_leaves_a_consistent_result(
    client: AsyncClient, world: World
) -> None:
    for _ in range(RACE_RUNS):
        trip_id = await world.trip()
        leader = await leader_of(world, trip_id)
        me = await world.account()
        request_id = (await ask(client, trip_id, me)).json()["id"]

        approve, cancelled = await asyncio.gather(
            decide(client, trip_id, request_id, "approve", leader),
            cancel(client, request_id, me),
        )

        row = await request_row(world, request_id)
        members = await memberships(world, trip_id, me.user_id)
        if row.state == "approved":
            assert approve.status_code == 200, approve.text
            assert_conflict(cancelled)
            assert [(m.role, m.revoked_at) for m in members] == [("rider", None)]
        else:
            assert row.state == "cancelled", row.state
            assert cancelled.status_code == 200, cancelled.text
            assert_conflict(approve)
            assert members == []


# --------------------------------------------------------------------------
# 13. The leader list
# --------------------------------------------------------------------------


async def test_the_leader_list_has_exactly_the_out_keys_and_never_a_username(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    leader = await leader_of(world, trip_id)
    direct, legacy = await world.account("Direct Asker"), await world.account("Link Asker")
    await world.request(trip_id, direct.user_id, "pending", message="hello")
    await world.request(trip_id, legacy.user_id, "pending", via="legacy_rider_link")
    async with world.engine.connect() as conn:
        usernames = list(
            (
                await conn.execute(
                    select(tables.users.c.username).where(
                        tables.users.c.id.in_([direct.user_id, legacy.user_id, leader.user_id])
                    )
                )
            ).scalars()
        )

    response = await leader_list(client, trip_id, leader)

    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 2
    for item in items:
        assert set(item) == TRIP_JOIN_REQUEST_KEYS
        assert set(item["requester"]) == PERSON_KEYS
        assert item["state"] == "pending"
    by_user = {i["requester"]["userId"]: i for i in items}
    assert by_user[direct.user_id]["requester"]["displayName"] == "Direct Asker"
    assert (by_user[direct.user_id]["via"], by_user[direct.user_id]["message"]) == (
        "direct",
        "hello",
    )
    assert (by_user[legacy.user_id]["via"], by_user[legacy.user_id]["message"]) == (
        "legacy_rider_link",
        None,
    )
    assert "username" not in response.text.lower()
    for username in usernames:
        assert username not in response.text


async def test_the_leader_list_is_oldest_first_then_by_id(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    leader = await leader_of(world, trip_id)
    users = await world.bare_users(3)
    low, high = sorted([str(uuid4()), str(uuid4())])
    oldest = await world.request(trip_id, users[0], "pending", created_at=T0 - timedelta(days=2))
    # Two at the same instant, inserted high id first: id breaks the tie.
    await world.request(
        trip_id, users[1], "pending", created_at=T0 - timedelta(days=1), request_id=high
    )
    await world.request(
        trip_id, users[2], "pending", created_at=T0 - timedelta(days=1), request_id=low
    )

    response = await leader_list(client, trip_id, leader)

    assert response.status_code == 200, response.text
    assert [i["id"] for i in response.json()] == [oldest, low, high]


async def test_the_leader_list_filters_by_state_and_trip(client: AsyncClient, world: World) -> None:
    trip_id, other_trip = await world.trip(), await world.trip()
    leader = await leader_of(world, trip_id)
    users = await world.bare_users(5)
    pending = await world.request(trip_id, users[0], "pending")
    blocked = await world.request(trip_id, users[1], "blocked")
    await world.request(trip_id, users[2], "approved")
    await world.request(trip_id, users[3], "rejected")
    await world.request(trip_id, users[4], "cancelled")
    await world.request(other_trip, users[1], "pending")
    await world.request(other_trip, users[2], "blocked")

    default = await leader_list(client, trip_id, leader)
    explicit = await leader_list(client, trip_id, leader, "pending")
    blocked_list = await leader_list(client, trip_id, leader, "blocked")

    assert default.status_code == 200, default.text
    assert [i["id"] for i in default.json()] == [pending]
    assert explicit.json() == default.json()
    assert blocked_list.status_code == 200, blocked_list.text
    assert [(i["id"], i["state"]) for i in blocked_list.json()] == [(blocked, "blocked")]


@pytest.mark.parametrize("state", ["approved", "rejected", "cancelled", "BLOCKED", "all", ""])
async def test_the_leader_list_rejects_any_other_state_with_422(
    client: AsyncClient, world: World, state: str
) -> None:
    trip_id = await world.trip()
    leader = await leader_of(world, trip_id)

    response = await leader_list(client, trip_id, leader, state)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------
# 14. CSRF: decision and unblock, nothing written
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}],
    ids=["foreign-origin", "sec-fetch-cross-site"],
)
async def test_a_cross_site_decision_or_unblock_is_403_and_writes_nothing(
    client: AsyncClient, world: World, headers: dict[str, str]
) -> None:
    trip_id = await world.trip()
    leader = await leader_of(world, trip_id)
    me, other = await world.account(), await world.account("Blocked One")
    pending = await world.request(trip_id, me.user_id, "pending")
    blocked = await world.request(trip_id, other.user_id, "blocked")
    before = await world.rows(trip_id=trip_id)

    decided = await decide(client, trip_id, pending, "approve", leader, headers=headers)
    assert decided.status_code == 403, decided.text
    assert decided.json() == {"error": {"code": "FORBIDDEN", "message": CSRF_MESSAGE}}

    unblocked = await unblock(client, trip_id, blocked, leader, headers=headers)
    assert unblocked.status_code == 403, unblocked.text
    assert unblocked.json() == {"error": {"code": "FORBIDDEN", "message": CSRF_MESSAGE}}

    assert await world.rows(trip_id=trip_id) == before
    assert await memberships(world, trip_id, me.user_id) == []


# --------------------------------------------------------------------------
# 15. Leadership re-checked under the trip lock
# --------------------------------------------------------------------------

NO_LONGER_A_RIDER_MESSAGE = "You're no longer a rider on this trip."
NOT_A_LEADER_MESSAGE = "You're not a leader on this trip."


@pytest.fixture
def before_lock(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[[], Any]], None]:
    """
    Run a callback (committed on its own connection) just before the trip-row lock.

    A deterministic stand-in for a revocation or demotion landing between the
    leader gate's membership read and the repository's ``FOR UPDATE``.
    """
    original = memberships_repo._lock_trip

    def install(callback: Callable[[], Any]) -> None:
        async def patched(session: AsyncSession, trip_id: str) -> None:
            await callback()
            await original(session, trip_id)

        monkeypatch.setattr(memberships_repo, "_lock_trip", patched)

    return install


async def change_leader(world: World, trip_id: str, user_id: str, change: str) -> None:
    values: dict[str, Any] = (
        {"revoked_at": datetime.now(UTC)} if change == "revoke" else {"role": "rider"}
    )
    async with world.engine.begin() as conn:
        await conn.execute(
            tables.trip_members.update()
            .where(
                tables.trip_members.c.trip_id == trip_id,
                tables.trip_members.c.user_id == user_id,
                tables.trip_members.c.revoked_at.is_(None),
            )
            .values(**values)
        )


@pytest.mark.parametrize("route", ["decide", "unblock"])
@pytest.mark.parametrize(
    ("change", "message"),
    [("revoke", NO_LONGER_A_RIDER_MESSAGE), ("demote", NOT_A_LEADER_MESSAGE)],
)
async def test_a_leader_changed_between_gate_and_lock_gets_the_gates_403(
    client: AsyncClient,
    world: World,
    before_lock: Callable[[Callable[[], Any]], None],
    route: str,
    change: str,
    message: str,
) -> None:
    trip_id = await world.trip()
    caller = await leader_of(world, trip_id, "Changing Leader")
    await leader_of(world, trip_id, "Other Leader")
    me = await world.account()
    request_id = await world.request(
        trip_id, me.user_id, "pending" if route == "decide" else "blocked"
    )
    before = await request_row(world, request_id)

    async def send() -> Response:
        if route == "decide":
            return await decide(client, trip_id, request_id, "approve", caller)
        return await unblock(client, trip_id, request_id, caller)

    before_lock(lambda: change_leader(world, trip_id, caller.user_id, change))
    response = await send()

    # What the gate itself says on the next request, with no race.
    before_lock(lambda: asyncio.sleep(0))
    gate = await send()

    assert gate.status_code == 403, gate.text  # sanity: the gate refuses this caller now
    assert response.status_code == 403, response.text
    assert response.json() == {"error": {"code": "FORBIDDEN", "message": message}}
    assert (response.status_code, response.content) == (gate.status_code, gate.content)
    assert await request_row(world, request_id) == before
    assert await memberships(world, trip_id, me.user_id) == []
