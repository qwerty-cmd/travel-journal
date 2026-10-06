"""
Operator CLIs: ``grant_leader``, ``reset_account`` and ``revoke_member`` (t-am-operator-clis).

Access control and data integrity, so the assertions come from the task's AC and
the contract (Sessions > Revocation, Recover, the ``trip_members.revoked_by``
comment), not from the implementation:

- ``grant_leader`` inserts or upgrades one active leader row and is idempotent.
- ``reset_account`` writes a new recovery-code hash, deletes **all** the user's
  sessions and clears the lockout in one transaction, and prints the code once,
  only after commit, never into a log record (obligations 10 and 16).
- ``reset_account --disable`` sets ``disabled_at``, deletes the sessions, clears
  the lockout and issues no code.
- ``revoke_member`` can revoke a leader, refuses the last one (exit 1), and
  records an operator revocation as ``revoked_at`` set with ``revoked_by`` NULL.
- An unknown username or trip is a non-zero exit with no traceback.

``main()`` calls ``asyncio.run``, so the end-to-end CLI runs go through
``asyncio.to_thread`` against the real database (``DATABASE_URL``). The core
functions take an injected ``AsyncSession`` and are driven directly where the
test needs to inspect state inside the transaction or race two of them.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy.orm.session as orm_session
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.main
from app.core.recovery_codes import hash_recovery_code, new_recovery_code
from app.data import grant_leader as grant_module
from app.data import reset_account as reset_module
from app.data import revoke_member as revoke_module
from app.data import tables
from app.data.db import get_session
from app.data.grant_leader import OperatorError
from tests.conftest import (
    SeededTrip,
    SignedInAccount,
    create_signed_in_account,
    delete_accounts,
    grant_membership,
    make_async_client,
)

RECOVER = "/api/v2/auth/recover"
ME = "/api/v2/auth/me"
NEW_PASSWORD = "a brand new passphrase 42"


# --------------------------------------------------------------------------- helpers


@pytest.fixture
async def make_account(
    migrated_engine: AsyncEngine,
) -> AsyncIterator[Callable[..., Any]]:
    """Factory for signed-in accounts, all removed on teardown."""
    made: list[str] = []

    async def _make(display_name: str = "Operator Test") -> SignedInAccount:
        account = await create_signed_in_account(migrated_engine, display_name=display_name)
        made.append(account.user_id)
        return account

    try:
        yield _make
    finally:
        if made:
            await delete_accounts(migrated_engine, made)


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[Any]:
    """The real app with ``get_session`` pointed at the test database."""
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


async def username_of(engine: AsyncEngine, user_id: str) -> str:
    async with engine.connect() as conn:
        return await conn.scalar(
            select(tables.users.c.username).where(tables.users.c.id == user_id)
        )


async def user_row(engine: AsyncEngine, user_id: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(select(tables.users).where(tables.users.c.id == user_id))).one()


async def session_count(engine: AsyncEngine, user_id: str) -> int:
    async with engine.connect() as conn:
        return await conn.scalar(
            select(func.count())
            .select_from(tables.sessions)
            .where(tables.sessions.c.user_id == user_id)
        )


async def member_rows(engine: AsyncEngine, trip_id: str, user_id: str) -> list[Any]:
    """Every ``trip_members`` row (active and revoked) for the pair, oldest first."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(tables.trip_members)
            .where(
                tables.trip_members.c.trip_id == trip_id,
                tables.trip_members.c.user_id == user_id,
            )
            .order_by(tables.trip_members.c.joined_at, tables.trip_members.c.id)
        )
        return list(result)


async def active_leaders(engine: AsyncEngine, trip_id: str) -> list[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(tables.trip_members.c.user_id).where(
                tables.trip_members.c.trip_id == trip_id,
                tables.trip_members.c.role == "leader",
                tables.trip_members.c.revoked_at.is_(None),
            )
        )
        return sorted(r[0] for r in result)


async def set_user(engine: AsyncEngine, user_id: str, **values: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(tables.users).where(tables.users.c.id == user_id).values(**values)
        )


async def run_main(main: Callable[[list[str]], int], argv: list[str]) -> int:
    """Run a CLI ``main()`` (which calls ``asyncio.run``) off this test's event loop."""
    return await asyncio.to_thread(main, argv)


def assert_no_traceback(captured: pytest.CaptureResult[str]) -> None:
    assert "Traceback" not in captured.out + captured.err


# --------------------------------------------------------------------------- grant_leader


async def test_grant_leader_inserts_an_active_leader(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    make_account: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)
    trip = seeded_trips[0]

    code = await run_main(grant_module.main, ["--trip-id", trip.id, "--username", username])

    assert code == 0
    rows = await member_rows(migrated_engine, trip.id, account.user_id)
    assert [(r.role, r.revoked_at) for r in rows] == [("leader", None)]
    # Only the named trip.
    assert await member_rows(migrated_engine, seeded_trips[1].id, account.user_id) == []
    assert_no_traceback(capsys.readouterr())


async def test_grant_leader_upgrades_a_rider_in_place(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    make_account: Callable[..., Any],
) -> None:
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)
    trip = seeded_trips[0]
    await grant_membership(migrated_engine, trip.id, account.user_id, role="rider")
    [before] = await member_rows(migrated_engine, trip.id, account.user_id)

    code = await run_main(grant_module.main, ["--trip-id", trip.id, "--username", username])

    assert code == 0
    [after] = await member_rows(migrated_engine, trip.id, account.user_id)
    assert after.id == before.id
    assert after.joined_at == before.joined_at
    assert after.role == "leader"
    assert after.revoked_at is None


async def test_grant_leader_on_an_existing_leader_is_a_noop_exit_0(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    make_account: Callable[..., Any],
) -> None:
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)
    trip = seeded_trips[0]
    await grant_membership(migrated_engine, trip.id, account.user_id, role="leader")
    before = await member_rows(migrated_engine, trip.id, account.user_id)

    code = await run_main(grant_module.main, ["--trip-id", trip.id, "--username", username])

    assert code == 0
    assert await member_rows(migrated_engine, trip.id, account.user_id) == before


async def test_grant_leader_is_idempotent_across_two_runs(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    make_account: Callable[..., Any],
) -> None:
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)
    trip = seeded_trips[0]
    argv = ["--trip-id", trip.id, "--username", username]

    assert await run_main(grant_module.main, argv) == 0
    first = await member_rows(migrated_engine, trip.id, account.user_id)
    assert await run_main(grant_module.main, argv) == 0
    second = await member_rows(migrated_engine, trip.id, account.user_id)

    assert len(second) == 1
    assert second == first


async def test_grant_leader_unknown_user_exits_nonzero_without_traceback(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    capsys: pytest.CaptureFixture[str],
) -> None:
    trip = seeded_trips[0]
    async with migrated_engine.connect() as conn:
        before = await conn.scalar(
            select(func.count())
            .select_from(tables.trip_members)
            .where(tables.trip_members.c.trip_id == trip.id)
        )

    code = await run_main(
        grant_module.main, ["--trip-id", trip.id, "--username", f"ghost{uuid.uuid4().hex[:10]}"]
    )

    assert code != 0
    captured = capsys.readouterr()
    assert_no_traceback(captured)
    assert captured.err.strip()
    async with migrated_engine.connect() as conn:
        after = await conn.scalar(
            select(func.count())
            .select_from(tables.trip_members)
            .where(tables.trip_members.c.trip_id == trip.id)
        )
    assert after == before


async def test_grant_leader_unknown_trip_exits_nonzero_without_traceback(
    migrated_engine: AsyncEngine,
    make_account: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)

    code = await run_main(
        grant_module.main, ["--trip-id", f"no-such-trip-{uuid.uuid4()}", "--username", username]
    )

    assert code != 0
    captured = capsys.readouterr()
    assert_no_traceback(captured)
    assert captured.err.strip()
    async with migrated_engine.connect() as conn:
        count = await conn.scalar(
            select(func.count())
            .select_from(tables.trip_members)
            .where(tables.trip_members.c.user_id == account.user_id)
        )
    assert count == 0


async def test_grant_leader_gives_a_revoked_ex_member_a_new_active_row(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    make_account: Callable[..., Any],
) -> None:
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)
    trip = seeded_trips[0]
    await grant_membership(migrated_engine, trip.id, account.user_id, role="leader", revoked=True)
    [old] = await member_rows(migrated_engine, trip.id, account.user_id)

    code = await run_main(grant_module.main, ["--trip-id", trip.id, "--username", username])

    assert code == 0
    rows = await member_rows(migrated_engine, trip.id, account.user_id)
    assert len(rows) == 2
    history = next(r for r in rows if r.id == old.id)
    assert history.revoked_at == old.revoked_at  # history untouched
    [active] = [r for r in rows if r.revoked_at is None]
    assert active.id != old.id
    assert active.role == "leader"


# --------------------------------------------------------------------------- reset_account


async def _prime_for_reset(engine: AsyncEngine, account: SignedInAccount) -> str:
    """Give ``account`` an old recovery code and a lock in force; return the old code."""
    old_code = new_recovery_code()
    await set_user(
        engine,
        account.user_id,
        recovery_code_hash=hash_recovery_code(old_code),
        failed_logins=7,
        locked_until=datetime.now(UTC) + timedelta(hours=1),
    )
    return old_code


async def _add_session(engine: AsyncEngine, user_id: str) -> None:
    """A second live session for the user, so "all sessions" means more than one."""
    from app.core.sessions import ABSOLUTE_LIFETIME, hash_token, new_token

    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            tables.sessions.insert().values(
                token_hash=hash_token(new_token()),
                user_id=user_id,
                created_at=now,
                last_used_at=now,
                absolute_expires_at=now + ABSOLUTE_LIFETIME,
            )
        )


@pytest.fixture
def recorded_codes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every recovery code ``reset_account`` generates, so a test can search for it."""
    codes: list[str] = []

    def _recording() -> str:
        code = new_recovery_code()
        codes.append(code)
        return code

    monkeypatch.setattr(reset_module, "new_recovery_code", _recording)
    return codes


async def test_reset_rotates_code_deletes_all_sessions_and_clears_lock_while_locked(
    migrated_engine: AsyncEngine,
    make_account: Callable[..., Any],
    recorded_codes: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    account = await make_account()
    bystander = await make_account()
    await _add_session(migrated_engine, account.user_id)
    username = await username_of(migrated_engine, account.user_id)
    await _prime_for_reset(migrated_engine, account)
    before = await user_row(migrated_engine, account.user_id)
    assert await session_count(migrated_engine, account.user_id) == 2

    code = await run_main(reset_module.main, ["--username", username])

    assert code == 0
    [issued] = recorded_codes
    after = await user_row(migrated_engine, account.user_id)
    assert after.recovery_code_hash == hash_recovery_code(issued)
    assert after.recovery_code_hash != before.recovery_code_hash
    assert after.failed_logins == 0
    assert after.locked_until is None
    assert after.disabled_at is None
    assert await session_count(migrated_engine, account.user_id) == 0
    # Only this user's sessions.
    assert await session_count(migrated_engine, bystander.user_id) == 1

    captured = capsys.readouterr()
    assert captured.out.count(issued) == 1
    assert issued not in captured.err
    assert_no_traceback(captured)


async def test_reset_core_does_everything_in_the_callers_transaction(
    migrated_engine: AsyncEngine, make_account: Callable[..., Any]
) -> None:
    """Rolled back, nothing of the reset survives: it is one transaction, not several."""
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)
    await _prime_for_reset(migrated_engine, account)
    before = await user_row(migrated_engine, account.user_id)

    async with AsyncSession(migrated_engine) as session, session.begin():
        code = await reset_module.reset_account(session, username=username, disable=False)
        assert code
        await session.rollback()

    assert await user_row(migrated_engine, account.user_id) == before
    assert await session_count(migrated_engine, account.user_id) == 1


async def test_reset_prints_nothing_when_the_commit_fails(
    migrated_engine: AsyncEngine,
    make_account: Callable[..., Any],
    recorded_codes: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)
    await _prime_for_reset(migrated_engine, account)
    before = await user_row(migrated_engine, account.user_id)

    def _failing_commit(self: Any) -> None:
        raise OperationalError("COMMIT", {}, Exception("connection lost at commit"))

    monkeypatch.setattr(orm_session.SessionTransaction, "commit", _failing_commit)

    code = await run_main(reset_module.main, ["--username", username])

    assert code != 0
    assert len(recorded_codes) == 1, "the reset should have reached commit"
    captured = capsys.readouterr()
    assert recorded_codes[0] not in captured.out + captured.err
    assert captured.out == ""
    assert_no_traceback(captured)
    # Nothing committed either.
    assert await user_row(migrated_engine, account.user_id) == before
    assert await session_count(migrated_engine, account.user_id) == 1


async def test_reset_code_is_never_in_a_log_record(
    migrated_engine: AsyncEngine,
    make_account: Callable[..., Any],
    recorded_codes: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)
    caplog.set_level(logging.DEBUG)

    assert await run_main(reset_module.main, ["--username", username]) == 0

    [issued] = recorded_codes
    for record in caplog.records:
        rendered = f"{record.getMessage()} {record.args!r} {record.__dict__!r}"
        assert issued not in rendered, f"recovery code in log record from {record.name}"


async def test_reset_code_works_with_recover_and_the_old_code_does_not(
    migrated_engine: AsyncEngine,
    make_account: Callable[..., Any],
    recorded_codes: list[str],
    client: Any,
) -> None:
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)
    old_code = await _prime_for_reset(migrated_engine, account)

    assert await run_main(reset_module.main, ["--username", username]) == 0
    [issued] = recorded_codes

    client.cookies.clear()
    old = await client.post(
        RECOVER,
        json={"username": username, "recoveryCode": old_code, "newPassword": NEW_PASSWORD},
    )
    assert old.status_code == 401

    client.cookies.clear()
    new = await client.post(
        RECOVER,
        json={"username": username, "recoveryCode": issued, "newPassword": NEW_PASSWORD},
    )
    assert new.status_code == 200, new.text


async def test_reset_makes_old_cookies_401(
    migrated_engine: AsyncEngine,
    make_account: Callable[..., Any],
    client: Any,
) -> None:
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)

    client.cookies.clear()
    assert (await client.get(ME, headers=account.headers)).status_code == 200

    assert await run_main(reset_module.main, ["--username", username]) == 0

    client.cookies.clear()
    response = await client.get(ME, headers=account.headers)
    assert response.status_code == 401


async def test_disable_sets_disabled_at_deletes_sessions_clears_lock_and_issues_no_code(
    migrated_engine: AsyncEngine,
    make_account: Callable[..., Any],
    recorded_codes: list[str],
    capsys: pytest.CaptureFixture[str],
    client: Any,
) -> None:
    account = await make_account()
    await _add_session(migrated_engine, account.user_id)
    username = await username_of(migrated_engine, account.user_id)
    await _prime_for_reset(migrated_engine, account)
    before = await user_row(migrated_engine, account.user_id)

    code = await run_main(reset_module.main, ["--username", username, "--disable"])

    assert code == 0
    assert recorded_codes == []
    after = await user_row(migrated_engine, account.user_id)
    assert after.disabled_at is not None
    assert after.failed_logins == 0
    assert after.locked_until is None
    assert after.recovery_code_hash == before.recovery_code_hash
    assert await session_count(migrated_engine, account.user_id) == 0
    assert_no_traceback(capsys.readouterr())

    client.cookies.clear()
    assert (await client.get(ME, headers=account.headers)).status_code == 401


async def test_disable_keeps_an_earlier_disabled_at(
    migrated_engine: AsyncEngine, make_account: Callable[..., Any]
) -> None:
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)
    earlier = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    await set_user(migrated_engine, account.user_id, disabled_at=earlier)

    assert await run_main(reset_module.main, ["--username", username, "--disable"]) == 0

    assert (await user_row(migrated_engine, account.user_id)).disabled_at == earlier
    assert await session_count(migrated_engine, account.user_id) == 0


async def test_reset_unknown_user_exits_nonzero_without_traceback(
    recorded_codes: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    for extra in ([], ["--disable"]):
        code = await run_main(
            reset_module.main, ["--username", f"ghost{uuid.uuid4().hex[:10]}", *extra]
        )
        assert code != 0
        captured = capsys.readouterr()
        assert_no_traceback(captured)
        assert captured.err.strip()
    assert recorded_codes == []


# --------------------------------------------------------------------------- revoke_member


async def test_revoke_can_remove_a_leader_when_another_remains(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    make_account: Callable[..., Any],
) -> None:
    trip = seeded_trips[0]
    target = await make_account()
    other = await make_account()
    await grant_membership(migrated_engine, trip.id, target.user_id, role="leader")
    await grant_membership(migrated_engine, trip.id, other.user_id, role="leader")
    username = await username_of(migrated_engine, target.user_id)

    code = await run_main(revoke_module.main, ["--trip-id", trip.id, "--username", username])

    assert code == 0
    [row] = await member_rows(migrated_engine, trip.id, target.user_id)
    assert row.revoked_at is not None
    assert row.revoked_by is None  # operator revocation
    assert await active_leaders(migrated_engine, trip.id) == [other.user_id]


async def test_revoke_sets_revoked_by_null_for_a_rider(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    make_account: Callable[..., Any],
) -> None:
    trip = seeded_trips[0]
    rider = await make_account()
    await grant_membership(migrated_engine, trip.id, rider.user_id, role="rider")
    username = await username_of(migrated_engine, rider.user_id)

    assert await run_main(revoke_module.main, ["--trip-id", trip.id, "--username", username]) == 0

    [row] = await member_rows(migrated_engine, trip.id, rider.user_id)
    assert row.revoked_at is not None
    assert row.revoked_by is None


async def test_revoke_refuses_the_last_leader_and_changes_nothing(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    make_account: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    trip = seeded_trips[0]
    leader = await make_account()
    rider = await make_account()
    await grant_membership(migrated_engine, trip.id, leader.user_id, role="leader")
    await grant_membership(migrated_engine, trip.id, rider.user_id, role="rider")
    username = await username_of(migrated_engine, leader.user_id)
    before = await member_rows(migrated_engine, trip.id, leader.user_id)

    code = await run_main(revoke_module.main, ["--trip-id", trip.id, "--username", username])

    assert code == 1
    assert await member_rows(migrated_engine, trip.id, leader.user_id) == before
    captured = capsys.readouterr()
    assert_no_traceback(captured)
    assert captured.err.strip()


async def test_revoke_already_revoked_exits_0_without_writing(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    make_account: Callable[..., Any],
) -> None:
    trip = seeded_trips[0]
    account = await make_account()
    await grant_membership(migrated_engine, trip.id, account.user_id, role="rider", revoked=True)
    username = await username_of(migrated_engine, account.user_id)
    before = await member_rows(migrated_engine, trip.id, account.user_id)

    code = await run_main(revoke_module.main, ["--trip-id", trip.id, "--username", username])

    assert code == 0
    assert await member_rows(migrated_engine, trip.id, account.user_id) == before


async def test_revoke_a_never_member_exits_1(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    make_account: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)

    code = await run_main(
        revoke_module.main, ["--trip-id", seeded_trips[0].id, "--username", username]
    )

    assert code == 1
    assert await member_rows(migrated_engine, seeded_trips[0].id, account.user_id) == []
    assert_no_traceback(capsys.readouterr())


async def test_revoke_unknown_user_or_trip_exits_nonzero_without_traceback(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    make_account: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    account = await make_account()
    username = await username_of(migrated_engine, account.user_id)
    cases = [
        ["--trip-id", seeded_trips[0].id, "--username", f"ghost{uuid.uuid4().hex[:10]}"],
        ["--trip-id", f"no-such-trip-{uuid.uuid4()}", "--username", username],
    ]
    for argv in cases:
        assert await run_main(revoke_module.main, argv) != 0
        captured = capsys.readouterr()
        assert_no_traceback(captured)
        assert captured.err.strip()


async def test_revoked_member_next_api_write_is_403(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    make_account: Callable[..., Any],
    client: Any,
) -> None:
    trip = seeded_trips[0]
    target = await make_account()
    other = await make_account()
    await grant_membership(migrated_engine, trip.id, target.user_id, role="leader")
    await grant_membership(migrated_engine, trip.id, other.user_id, role="leader")
    username = await username_of(migrated_engine, target.user_id)

    path = f"/api/v2/trips/{trip.id}"
    client.cookies.clear()
    assert (await client.patch(path, json={}, headers=target.headers)).status_code == 200

    assert await run_main(revoke_module.main, ["--trip-id", trip.id, "--username", username]) == 0

    client.cookies.clear()
    response = await client.patch(path, json={}, headers=target.headers)
    assert response.status_code == 403
    client.cookies.clear()
    assert (await client.patch(path, json={}, headers=other.headers)).status_code == 200


async def test_concurrent_revokes_of_the_last_two_leaders_leave_one(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    make_account: Callable[..., Any],
) -> None:
    trip = seeded_trips[0]
    a = await make_account()
    b = await make_account()
    await grant_membership(migrated_engine, trip.id, a.user_id, role="leader")
    await grant_membership(migrated_engine, trip.id, b.user_id, role="leader")
    names = [await username_of(migrated_engine, u.user_id) for u in (a, b)]

    async def _revoke(username: str) -> object:
        try:
            async with AsyncSession(migrated_engine) as session, session.begin():
                return await revoke_module.revoke_member(
                    session, trip_id=trip.id, username=username
                )
        except OperatorError as exc:
            return exc

    for _ in range(5):
        results = await asyncio.gather(*(_revoke(n) for n in names))

        leaders = await active_leaders(migrated_engine, trip.id)
        assert len(leaders) == 1, results
        assert sum(isinstance(r, OperatorError) for r in results) == 1, results

        # Restore both as leaders for the next round.
        async with migrated_engine.begin() as conn:
            await conn.execute(
                tables.trip_members.delete().where(tables.trip_members.c.trip_id == trip.id)
            )
        await grant_membership(migrated_engine, trip.id, a.user_id, role="leader")
        await grant_membership(migrated_engine, trip.id, b.user_id, role="leader")
