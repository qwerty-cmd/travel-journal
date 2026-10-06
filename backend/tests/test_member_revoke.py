"""
A leader revokes a rider, effective on the next request (t-am-member-revoke).

Written from ``docs/api-contract.md`` (the ``DELETE .../members/{userId}`` row,
"Members -> Revoke", "The gate runs before the replay lookup") and decision-log
Entry 29's no-grace rule:

- Obligation 5: after a leader's revoke, the rider's very next request from
  their still-live session is ``403`` "You're no longer a rider on this trip"
  on every legacy and v2 stop, photo and bike write, replays of stored ids
  included, and nothing reaches the database or S3.
- Obligation 6 (leader-removal half): ``DELETE`` on a leader, yourself
  included, is ``403`` "Leaders can't remove another leader" and changes nothing.
- A rider target: ``204``, the row kept with ``revoked_at`` / ``revoked_by``;
  a repeat is ``204`` and writes nothing. Never a member: ``404``.
- The caller is re-checked under the trip-row lock; a revoke racing a promote
  of the same rider ends in one consistent state.

The gate cells (401 / 404 / 403 per identity) and the matrix completeness guard
are ``test_access_matrix.py`` §15 / §18 and are not repeated here.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
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
from jpeg_fixtures import minimal_jpeg
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.main
from app.api.routes.v2 import join_requests as join_routes
from app.data import tables
from app.data.db import get_session
from app.data.repositories import memberships
from app.storage.s3_client import BUCKET_NAME, get_s3_client

# Contract literals, not the implementation's constants.
NO_LONGER_A_RIDER = "You're no longer a rider on this trip."
NOT_A_LEADER = "You're not a leader on this trip."
LEADER_TARGET = "Leaders can't remove another leader"
CSRF_MESSAGE = "This request came from another site and was blocked."

RACE_RUNS = 5


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
    async with make_async_client(app.main.app, raise_app_exceptions=False) as http_client:
        yield http_client


@dataclass
class World:
    """Trips and accounts made by one test, removed on teardown."""

    engine: AsyncEngine
    trip_ids: list[str] = field(default_factory=list)
    user_ids: list[str] = field(default_factory=list)
    slugs: dict[str, str] = field(default_factory=dict)

    async def trip(self, visibility: str = "private") -> str:
        trip_id = str(uuid4())
        rider_slug = secrets.token_urlsafe(16)
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.trips.insert().values(
                    id=trip_id,
                    name="Revoke trip",
                    start_date=date(2026, 6, 1),
                    visibility=visibility,
                    public_delay_hours=0,
                    rider_slug=rider_slug,
                    viewer_slug=secrets.token_urlsafe(16),
                )
            )
        self.trip_ids.append(trip_id)
        self.slugs[trip_id] = rider_slug
        return trip_id

    async def account(self, name: str = "Revoke Caller") -> SignedInAccount:
        account = await create_signed_in_account(self.engine, display_name=name)
        self.user_ids.append(account.user_id)
        return account

    async def member(
        self,
        trip_id: str,
        user_id: str,
        role: str = "rider",
        *,
        revoked_by: str | None = None,
        revoked: bool = False,
    ) -> None:
        values: dict[str, Any] = {
            "id": str(uuid4()),
            "trip_id": trip_id,
            "user_id": user_id,
            "role": role,
        }
        if revoked or revoked_by is not None:
            values["revoked_at"] = datetime.now(UTC) - timedelta(hours=1)
            values["revoked_by"] = revoked_by
        async with self.engine.begin() as conn:
            await conn.execute(tables.trip_members.insert().values(**values))

    async def pending_request(self, trip_id: str, user_id: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.join_requests.insert().values(
                    id=str(uuid4()), trip_id=trip_id, user_id=user_id, state="pending"
                )
            )

    async def stop_and_bike(self, trip_id: str) -> tuple[str, str]:
        stop_id, bike_id = f"rv-stop-{uuid4()}", f"rv-bike-{uuid4()}"
        async with self.engine.begin() as conn:
            await conn.execute(
                tables.stops.insert().values(
                    id=stop_id,
                    trip_id=trip_id,
                    name="Seeded stop",
                    lat=-14.46,
                    lng=132.26,
                    location_source="gps",
                    arrived_at=datetime(2026, 6, 3, 9, 0, tzinfo=UTC),
                )
            )
            await conn.execute(
                tables.bikes.insert().values(
                    id=bike_id,
                    trip_id=trip_id,
                    rider_name="Seed",
                    make="Honda",
                    model="XR650L",
                    year=2020,
                    specs="original",
                )
            )
        return stop_id, bike_id

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
                    "SELECT id, user_id, state, xmin::text FROM join_requests "
                    "WHERE trip_id = :t ORDER BY id"
                ),
                {"t": trip_id},
            )
            return [tuple(row) for row in rows]

    async def content_snapshot(self, trip_id: str) -> dict[str, Any]:
        """Row counts plus every row's id and ``xmin`` for the trip's stops, photos and bikes."""
        async with self.engine.connect() as conn:
            out: dict[str, Any] = {}
            for name, sql in (
                ("stops", "SELECT id, xmin::text FROM stops WHERE trip_id = :t"),
                ("bikes", "SELECT id, xmin::text FROM bikes WHERE trip_id = :t"),
                (
                    "photos",
                    (
                        "SELECT p.id, p.xmin::text FROM photos p "
                        "JOIN stops s ON s.id = p.stop_id WHERE s.trip_id = :t"
                    ),
                ),
            ):
                rows = sorted(tuple(r) for r in await conn.execute(text(sql), {"t": trip_id}))
                out[f"{name}_count"] = len(rows)
                out[name] = rows
            return out

    async def row(self, trip_id: str, user_id: str) -> Any:
        async with self.engine.connect() as conn:
            return (
                await conn.execute(
                    select(tables.trip_members).where(
                        tables.trip_members.c.trip_id == trip_id,
                        tables.trip_members.c.user_id == user_id,
                    )
                )
            ).one()


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


@pytest.fixture
def swept(s3_bucket: None, world: World) -> Iterator[None]:
    """Remove every object written under this test's trips."""
    yield
    s3 = get_s3_client()
    for trip_id in world.trip_ids:
        for key in s3_listing(trip_id):
            s3.delete_object(Bucket=BUCKET_NAME, Key=key)


def s3_listing(trip_id: str) -> dict[str, str]:
    """Every object under the trip's prefix: key -> ETag."""
    paginator = get_s3_client().get_paginator("list_objects_v2")
    return {
        obj["Key"]: obj["ETag"]
        for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix=f"{trip_id}/")
        for obj in page.get("Contents", [])
    }


async def send(
    client: AsyncClient,
    method: str,
    path: str,
    account: SignedInAccount | None,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> Response:
    """One request whose only cookie is ``account``'s."""
    client.cookies.clear()
    all_headers = {**(account.headers if account else {}), **(headers or {})}
    response = await client.request(method, path, headers=all_headers, **kwargs)
    client.cookies.clear()
    return response


def revoke_path(trip_id: str, user_id: str) -> str:
    return f"/api/v2/trips/{trip_id}/members/{user_id}"


def promote_path(trip_id: str, user_id: str) -> str:
    return f"/api/v2/trips/{trip_id}/members/{user_id}/promote"


def wire(response: Response) -> tuple[int, list[tuple[str, str]], bytes]:
    headers = [(k.lower(), v) for k, v in response.headers.multi_items() if k.lower() != "date"]
    return response.status_code, headers, response.content


def assert_error(response: Response, status: int, code: str, message: str | None = None) -> None:
    assert response.status_code == status, (response.status_code, response.text)
    error = response.json()["error"]
    assert error["code"] == code, error
    if message is not None:
        assert error["message"] == message, error


def assert_leader_target_403(response: Response) -> None:
    assert_error(response, 403, "FORBIDDEN")
    # The contract quotes the message without its sentence-final period.
    assert response.json()["error"]["message"].rstrip(".") == LEADER_TARGET, response.text


# --------------------------------------------------------------------------
# 1. Obligation 5: the next request after a revoke is 403, nothing written
# --------------------------------------------------------------------------


def write_request(
    surface: str, write: str, trip_id: str, slug: str, stop_id: str, bike_id: str, record_id: str
) -> tuple[str, str, dict[str, Any]]:
    """One legacy or v2 write as ``(method, path, request kwargs)``."""
    base = f"/api/trips/{slug}" if surface == "legacy" else f"/api/v2/trips/{trip_id}"
    if write == "create_stop":
        body = {
            "id": record_id,
            "name": f"Stop {record_id[:8]}",
            "lat": -14.5,
            "lng": 132.3,
            "locationSource": "gps",
            "arrivedAt": "2026-06-14T15:15:00+09:30",
        }
        return "POST", f"{base}/stops", {"json": body}
    if write == "upload_photo":
        # Bytes vary by id, so an S3 overwrite on a replay would move the ETag.
        variant = int(record_id[:6], 16) % 50 + 1
        return (
            "POST",
            f"{base}/stops/{stop_id}/photos",
            {
                "data": {"id": record_id, "takenAt": "2026-06-14T10:00:00+09:30"},
                "files": {"file": ("p.jpg", minimal_jpeg(variant), "image/jpeg")},
            },
        )
    if write == "create_bike":
        body = {"id": record_id, "riderName": "Kim", "make": "BMW", "model": "R80", "year": 1985}
        return "POST", f"{base}/bikes", {"json": body}
    if write == "patch_bike":
        return "PATCH", f"{base}/bikes/{bike_id}", {"json": {"specs": f"patched {record_id}"}}
    raise AssertionError(write)


FRESH_WRITES = ("create_stop", "upload_photo", "create_bike", "patch_bike")
REPLAYS = ("replay_stop", "replay_photo")
OBLIGATION_5_CASES = [
    (surface, case) for surface in ("legacy", "v2") for case in (*FRESH_WRITES, *REPLAYS)
]


@pytest.mark.parametrize(("surface", "case"), OBLIGATION_5_CASES)
async def test_the_revoked_riders_next_write_is_403_and_writes_nothing(
    client: AsyncClient, world: World, swept: None, surface: str, case: str
) -> None:
    # Public so the v2 write would locate the trip for anyone; the legacy slug locates it anyway.
    trip_id = await world.trip("public")
    slug = world.slugs[trip_id]
    leader = await world.account("Revoking Leader")
    rider = await world.account("Doomed Rider")
    await world.member(trip_id, leader.user_id, "leader")
    await world.member(trip_id, rider.user_id, "rider")
    stop_id, bike_id = await world.stop_and_bike(trip_id)

    write = {"replay_stop": "create_stop", "replay_photo": "upload_photo"}.get(case, case)
    # Before the revoke the same live session writes successfully, so the 403
    # below can only be the revocation. For a replay case this stores the id.
    record_id = str(uuid4())
    method, path, kwargs = write_request(surface, write, trip_id, slug, stop_id, bike_id, record_id)
    first = await send(client, method, path, rider, **kwargs)
    assert first.status_code in (200, 201), first.text

    revoked = await send(client, "DELETE", revoke_path(trip_id, rider.user_id), leader)
    assert revoked.status_code == 204, revoked.text

    db_before = await world.content_snapshot(trip_id)
    members_before = await world.members_snapshot(trip_id)
    s3_before = s3_listing(trip_id)
    if case in REPLAYS:
        assert db_before[{"replay_stop": "stops", "replay_photo": "photos"}[case] + "_count"] >= 1
        next_id = record_id  # the id already stored
        # A different photo body under the same id: an overwrite would move the ETag.
        method, path, kwargs = write_request(
            surface, write, trip_id, slug, stop_id, bike_id, next_id
        )
        if write == "upload_photo":
            kwargs["files"] = {"file": ("p.jpg", minimal_jpeg(77), "image/jpeg")}
    else:
        next_id = str(uuid4())
        method, path, kwargs = write_request(
            surface, write, trip_id, slug, stop_id, bike_id, next_id
        )

    refused = await send(client, method, path, rider, **kwargs)

    assert_error(refused, 403, "FORBIDDEN", NO_LONGER_A_RIDER)
    if case in REPLAYS:
        assert next_id not in refused.text, "the stored body leaked to a revoked rider"
    assert await world.content_snapshot(trip_id) == db_before
    assert await world.members_snapshot(trip_id) == members_before
    assert s3_listing(trip_id) == s3_before


# --------------------------------------------------------------------------
# 2. Obligation 6, leader-removal half: a leader target is 403
# --------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["other_leader", "self"])
async def test_revoking_a_leader_including_yourself_is_403_and_changes_nothing(
    client: AsyncClient, world: World, target: str
) -> None:
    trip_id = await world.trip()
    caller = await world.account("Caller Leader")
    other = await world.account("Peer Leader")
    await world.member(trip_id, caller.user_id, "leader")
    await world.member(trip_id, other.user_id, "leader")
    target_id = caller.user_id if target == "self" else other.user_id
    before = await world.members_snapshot(trip_id)

    response = await send(client, "DELETE", revoke_path(trip_id, target_id), caller)

    assert_leader_target_403(response)
    assert await world.members_snapshot(trip_id) == before


# --------------------------------------------------------------------------
# 3. A rider target: 204, the row kept; repeat is 204 with nothing written
# --------------------------------------------------------------------------


async def test_revoking_a_rider_is_204_and_keeps_the_row_with_revoked_at_and_by(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    leader = await world.account("Revoking Leader")
    second_leader = await world.account("Second Leader")
    rider = await world.account("Target Rider")
    bystander = await world.account("Bystander Rider")
    for user_id, role in (
        (leader.user_id, "leader"),
        (second_leader.user_id, "leader"),
        (rider.user_id, "rider"),
        (bystander.user_id, "rider"),
    ):
        await world.member(trip_id, user_id, role)
    before_row = await world.row(trip_id, rider.user_id)
    others_before = [r for r in await world.members_snapshot(trip_id) if r[1] != rider.user_id]

    started = datetime.now(UTC)
    response = await send(client, "DELETE", revoke_path(trip_id, rider.user_id), leader)
    finished = datetime.now(UTC)

    assert response.status_code == 204, response.text
    assert response.content == b""
    after_row = await world.row(trip_id, rider.user_id)  # .one(): kept, and no second row
    assert after_row.id == before_row.id
    assert after_row.role == "rider"
    assert after_row.joined_at == before_row.joined_at
    assert after_row.revoked_by == leader.user_id
    assert after_row.revoked_at is not None
    assert started - timedelta(seconds=5) <= after_row.revoked_at <= finished + timedelta(seconds=5)
    # Nobody else's row was touched.
    others_after = [r for r in await world.members_snapshot(trip_id) if r[1] != rider.user_id]
    assert others_after == others_before

    # Repeats, by the same leader and by another, are 204 and write nothing (xmin included).
    settled = await world.members_snapshot(trip_id)
    for repeater in (leader, second_leader):
        again = await send(client, "DELETE", revoke_path(trip_id, rider.user_id), repeater)
        assert again.status_code == 204, again.text
        assert again.content == b""
        assert await world.members_snapshot(trip_id) == settled


async def test_revoking_a_rider_who_left_on_their_own_is_204_and_writes_nothing(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    leader = await world.account("Leader")
    leaver = await world.account("Self Leaver")
    await world.member(trip_id, leader.user_id, "leader")
    await world.member(trip_id, leaver.user_id, "rider", revoked_by=leaver.user_id)
    before = await world.members_snapshot(trip_id)

    response = await send(client, "DELETE", revoke_path(trip_id, leaver.user_id), leader)

    assert response.status_code == 204, response.text
    # revoked_by stays the leaver's own id, so no cooldown is started by this call.
    assert await world.members_snapshot(trip_id) == before


@pytest.mark.parametrize("target", ["unknown_id", "never_member", "pending_only", "other_trip"])
async def test_revoking_someone_who_was_never_a_member_is_404_and_writes_nothing(
    client: AsyncClient, world: World, target: str
) -> None:
    trip_id = await world.trip()
    other_trip = await world.trip()
    leader = await world.account("Leader")
    stranger = await world.account("Stranger")
    await world.member(trip_id, leader.user_id, "leader")
    if target == "pending_only":
        await world.pending_request(trip_id, stranger.user_id)
    if target == "other_trip":
        await world.member(other_trip, stranger.user_id, "rider")
    target_id = str(uuid4()) if target == "unknown_id" else stranger.user_id
    members_before = await world.members_snapshot(trip_id)
    other_before = await world.members_snapshot(other_trip)
    requests_before = await world.join_requests_snapshot(trip_id)

    response = await send(client, "DELETE", revoke_path(trip_id, target_id), leader)
    unknown = await send(client, "DELETE", revoke_path(trip_id, str(uuid4())), leader)

    assert_error(response, 404, "NOT_FOUND")
    # An existing account and a made-up id are indistinguishable.
    assert wire(response) == wire(unknown)
    assert await world.members_snapshot(trip_id) == members_before
    assert await world.members_snapshot(other_trip) == other_before
    assert await world.join_requests_snapshot(trip_id) == requests_before


# --------------------------------------------------------------------------
# 4. A leader revocation starts the 7-day re-request cooldown (wiring only)
# --------------------------------------------------------------------------


async def test_a_revoked_rider_can_re_request_only_after_the_cooldown(
    client: AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full cooldown table is ``test_join_requests.py`` §3; this checks a real revoke feeds it."""
    trip_id = await world.trip("public")
    leader = await world.account("Leader")
    rider = await world.account("Revoked Rider")
    await world.member(trip_id, leader.user_id, "leader")
    await world.member(trip_id, rider.user_id, "rider")
    revoked = await send(client, "DELETE", revoke_path(trip_id, rider.user_id), leader)
    assert revoked.status_code == 204, revoked.text
    revoked_at = (await world.row(trip_id, rider.user_id)).revoked_at
    path = f"/api/v2/trips/{trip_id}/join-requests"

    monkeypatch.setattr(join_routes, "_utcnow", lambda: revoked_at + timedelta(days=1))
    held = await send(client, "POST", path, rider, json={})
    assert_error(held, 409, "CONFLICT")
    assert await world.join_requests_snapshot(trip_id) == []

    monkeypatch.setattr(join_routes, "_utcnow", lambda: revoked_at + timedelta(days=7, minutes=1))
    allowed = await send(client, "POST", path, rider, json={})
    assert allowed.status_code == 201, allowed.text


# --------------------------------------------------------------------------
# 5. The caller is re-checked under the trip-row lock
# --------------------------------------------------------------------------


@pytest.fixture
def before_lock(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[[], Awaitable[None]]], None]:
    """Run a callback (committed on its own connection) just before the trip-row lock."""
    original = memberships._lock_trip

    def install(callback: Callable[[], Awaitable[None]]) -> None:
        async def patched(session: AsyncSession, trip_id: str) -> None:
            await callback()
            await original(session, trip_id)

        monkeypatch.setattr(memberships, "_lock_trip", patched)

    return install


async def _revoke_directly(engine: AsyncEngine, trip_id: str, user_id: str) -> None:
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


async def _demote_directly(engine: AsyncEngine, trip_id: str, user_id: str) -> None:
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
    ("change", "message"), [("revoke", NO_LONGER_A_RIDER), ("demote", NOT_A_LEADER)]
)
async def test_a_caller_changed_between_gate_and_lock_gets_the_gates_403(
    client: AsyncClient,
    world: World,
    before_lock: Callable[[Callable[[], Awaitable[None]]], None],
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
    path = revoke_path(trip_id, rider.user_id)
    mutate = _revoke_directly if change == "revoke" else _demote_directly
    calls: list[int] = []

    async def injected() -> None:
        calls.append(1)
        await mutate(world.engine, trip_id, caller.user_id)

    before_lock(injected)
    response = await send(client, "DELETE", path, caller)
    after_race = await world.members_snapshot(trip_id)

    # What the gate itself says on the next request, with no race.
    before_lock(lambda: asyncio.sleep(0))
    gate = await send(client, "DELETE", path, caller)

    assert calls, "the revoke never took the trip-row lock through memberships._lock_trip"
    assert gate.status_code == 403, gate.text
    assert_error(response, 403, "FORBIDDEN", message)
    assert (response.status_code, response.content) == (gate.status_code, gate.content)
    assert await world.members_snapshot(trip_id) == after_race
    target = await world.row(trip_id, rider.user_id)
    assert (target.role, target.revoked_at, target.revoked_by) == ("rider", None, None)


# --------------------------------------------------------------------------
# 6. A revoke racing a promote of the same rider ends consistently
# --------------------------------------------------------------------------


@pytest.mark.parametrize("run", range(RACE_RUNS))
async def test_a_revoke_racing_a_promote_of_the_same_rider_ends_consistently(
    client: AsyncClient, second_client: AsyncClient, world: World, run: int
) -> None:
    trip_id = await world.trip()
    revoker = await world.account("Revoking Leader")
    promoter = await world.account("Promoting Leader")
    rider = await world.account("Contested Rider")
    await world.member(trip_id, revoker.user_id, "leader")
    await world.member(trip_id, promoter.user_id, "leader")
    await world.member(trip_id, rider.user_id, "rider")

    revoked, promoted = await asyncio.gather(
        send(client, "DELETE", revoke_path(trip_id, rider.user_id), revoker),
        send(second_client, "POST", promote_path(trip_id, rider.user_id), promoter),
    )

    outcome = (revoked.status_code, promoted.status_code)
    row = await world.row(trip_id, rider.user_id)
    if outcome == (204, 404):
        # Revoke first: the promote found no active rider.
        assert_error(promoted, 404, "NOT_FOUND")
        assert (row.role, row.revoked_by) == ("rider", revoker.user_id)
        assert row.revoked_at is not None
    elif outcome == (403, 200):
        # Promote first: the rider is now a leader, and a leader can't be revoked.
        assert_leader_target_403(revoked)
        assert (row.role, row.revoked_at, row.revoked_by) == ("leader", None, None)
    else:
        raise AssertionError((outcome, revoked.text, promoted.text))


# --------------------------------------------------------------------------
# 7. CSRF: a cross-site revoke is blocked before it does anything
# --------------------------------------------------------------------------


async def test_a_cross_site_revoke_is_403_and_changes_nothing(
    client: AsyncClient, world: World
) -> None:
    trip_id = await world.trip()
    leader = await world.account("CSRF Leader")
    rider = await world.account("CSRF Target")
    await world.member(trip_id, leader.user_id, "leader")
    await world.member(trip_id, rider.user_id, "rider")
    path = revoke_path(trip_id, rider.user_id)
    before = await world.members_snapshot(trip_id)

    for cross_site in (
        {"Origin": "https://evil.example"},
        {"Origin": "https://testserver", "Sec-Fetch-Site": "cross-site"},
    ):
        blocked = await send(client, "DELETE", path, leader, headers=cross_site)
        assert_error(blocked, 403, "FORBIDDEN", CSRF_MESSAGE)
        assert await world.members_snapshot(trip_id) == before

    # Control: the same request same-origin reaches the route and revokes.
    allowed = await send(client, "DELETE", path, leader)
    assert allowed.status_code == 204, allowed.text
    assert (await world.row(trip_id, rider.user_id)).revoked_by == leader.user_id
