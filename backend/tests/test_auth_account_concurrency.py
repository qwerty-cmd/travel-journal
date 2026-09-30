"""
Races on the account routes (task t-am-auth-account): single-use under concurrency.

The contract says a recovery code "is single-use: a successful recovery
replaces it" and that a password change leaves exactly one session
(``docs/api-contract.md``, "Sessions"). Sequential tests can't tell a
check-then-write from an atomic one, so these hold every racing request at the
point where it writes, release them together, and count what got through.

State is read straight from the tables. No password, code or token is put into
an assertion message.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.data.repositories import users as users_repo
from tests.test_auth_endpoints import (
    PASSWORD,
    Accounts,
    accounts,  # noqa: F401  (fixture)
    client,  # noqa: F401  (fixture)
    cookie_header,
    issued_token,
    own_hash_semaphore,  # noqa: F401  (fixture)
    send,
    session_rows,
    sha256_bytes,
    signin,
    signup,
    user_row,
)

RECOVER = "/api/v2/auth/recover"
PASSWORD_CHANGE = "/api/v2/auth/password"
RACERS = 5


class Barrier:
    """Wraps an async function so ``parties`` callers enter it only once all have arrived."""

    def __init__(self, real: Any, parties: int) -> None:
        self.real = real
        self.parties = parties
        self.arrived = 0
        self.all_here = asyncio.Event()

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.arrived += 1
        if self.arrived >= self.parties:
            self.all_here.set()
        await asyncio.wait_for(self.all_here.wait(), timeout=10)
        return await self.real(*args, **kwargs)


def new_password(i: int) -> str:
    return f"racing recovery password {i:02d}"


async def test_parallel_recoveries_with_one_code_succeed_exactly_once(
    client: AsyncClient,  # noqa: F811
    accounts: Accounts,  # noqa: F811
    migrated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    own_hash_semaphore: None,  # noqa: F811
) -> None:
    username = accounts.name()
    created = await signup(client, username)
    assert created.status_code == 201
    code = created.json()["recoveryCode"]

    # Every racer reaches the conditional UPDATE before any of them runs it.
    barrier = Barrier(users_repo.consume_recovery_code, RACERS)
    monkeypatch.setattr(users_repo, "consume_recovery_code", barrier)

    async def recover(i: int) -> httpx.Response:
        return await send(
            client,
            "POST",
            RECOVER,
            json={"username": username, "recoveryCode": code, "newPassword": new_password(i)},
        )

    responses = await asyncio.gather(*(recover(i) for i in range(RACERS)))

    statuses = [response.status_code for response in responses]
    assert barrier.arrived == RACERS, "not every racer reached the code check"
    assert statuses.count(200) == 1, f"statuses: {statuses}"
    assert statuses.count(401) == RACERS - 1, f"statuses: {statuses}"
    winner = statuses.index(200)
    won = responses[winner]

    # The stored code is the one the winner was shown, and one session exists: the winner's.
    row = await user_row(migrated_engine, username)
    new_code = won.json()["recoveryCode"]
    assert new_code != code
    assert row.recovery_code_hash == sha256_bytes(new_code).hex()
    token = issued_token(won)
    sessions = await session_rows(migrated_engine, row.id)
    assert [s.token_hash for s in sessions] == [sha256_bytes(token)]

    # The winner's password is the account's password now; the old one and the
    # losers' are not.
    assert (await signin(client, username, new_password(winner))).status_code == 200
    assert (await signin(client, username, PASSWORD)).status_code == 401
    loser = (winner + 1) % RACERS
    assert (await signin(client, username, new_password(loser))).status_code == 401


async def test_parallel_password_changes_leave_exactly_one_session(
    client: AsyncClient,  # noqa: F811
    accounts: Accounts,  # noqa: F811
    migrated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    own_hash_semaphore: None,  # noqa: F811
) -> None:
    username = accounts.name()
    created = await signup(client, username)
    assert created.status_code == 201
    token = issued_token(created)

    # Every racer has verified the current password before any of them writes.
    barrier = Barrier(users_repo.reset_lockout, 2)
    monkeypatch.setattr(users_repo, "reset_lockout", barrier)

    async def change(i: int) -> httpx.Response:
        return await send(
            client,
            "POST",
            PASSWORD_CHANGE,
            json={"currentPassword": PASSWORD, "newPassword": new_password(i)},
            headers=cookie_header(token),
        )

    responses = await asyncio.gather(change(0), change(1))

    assert [r.status_code for r in responses] == [200, 200]
    row = await user_row(migrated_engine, username)
    sessions = await session_rows(migrated_engine, row.id)
    assert len(sessions) == 1, f"{len(sessions)} sessions after two racing password changes"
    issued = {sha256_bytes(issued_token(r)) for r in responses}
    assert sessions[0].token_hash in issued
