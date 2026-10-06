"""
Per-session confirmation limit on the signed-in routes (t-am-session-confirm-limit).

Written from ``docs/api-contract.md`` ("Rate limits and lockout" → "Per-session
confirmation limit", "Sessions" → Revocation, "Notes per endpoint (v2)" →
Password change and Recovery-code rotation) and decision-log Entry 33, before
the handlers were read:

- a wrong ``currentPassword`` (``POST /auth/password``) or rotation ``password``
  (``POST /auth/recovery-code``) increments ``sessions.failed_confirmations`` of
  the requesting session, claimed before argon2, so parallel wrong requests run
  at most 10 checks;
- attempts 1-9 are 403; the 10th deletes the session and is 401 with the
  contract message, ``Max-Age=0`` and ``WWW-Authenticate``; a session already at
  10 gets the same 401 without a check; the next request is 401;
- a correct password resets the counter; if that reset finds the row gone (a
  concurrent 10th failure) the request is 401 and writes nothing;
- the counter is per session, shared by both routes; a password change's fresh
  session starts at 0;
- these routes never read or write ``users.failed_logins`` / ``locked_until``.

State is read straight from the tables. No password or token appears in an
assertion message.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.routes.v2 import auth as auth_routes
from app.data import tables
from tests import test_auth_endpoints as base
from tests.test_auth_account_endpoints import (
    NEW_PASSWORD,
    OTHER_PASSWORD,
    Account,
    change_password,
    me_status,
    new_account,
    rotate,
    token_hashes,
)
from tests.test_auth_endpoints import (
    PASSWORD,
    WRONG_PASSWORD,
    WWW_AUTHENTICATE,
    Accounts,
    CountingVerifier,
    assert_cleared,
    assert_envelope,
    issued_token,
    sha256_bytes,
    signin,
    user_row,
)

# The shared fixtures, re-bound so pytest finds them in this module.
accounts = base.accounts
client = base.client
own_hash_semaphore = base.own_hash_semaphore

SIGNED_OUT = "Signed out after too many wrong passwords. Sign in again."
ROUTES = ("password", "recovery-code")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def confirm(client: AsyncClient, route: str, token: str, password: str) -> httpx.Response:
    """One confirmation of ``password`` on ``route``; a password change picks a new one."""
    if route == "password":
        new = OTHER_PASSWORD if password == NEW_PASSWORD else NEW_PASSWORD
        return await change_password(client, token, password, new)
    return await rotate(client, token, password)


async def counter(engine: AsyncEngine, token: str) -> int | None:
    """``failed_confirmations`` of the session for ``token``; ``None`` if the row is gone."""
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT failed_confirmations FROM sessions WHERE token_hash = :h"),
                {"h": sha256_bytes(token)},
            )
        ).scalar_one_or_none()


def assert_signed_out(response: httpx.Response, message: str | None = None) -> None:
    assert_envelope(response, 401, "UNAUTHENTICATED", message)
    assert response.headers["www-authenticate"] == WWW_AUTHENTICATE
    assert_cleared(response)


async def fail(client: AsyncClient, route: str, token: str, times: int) -> None:
    for attempt in range(times):
        response = await confirm(client, route, token, WRONG_PASSWORD)
        assert_envelope(response, 403, "FORBIDDEN")
        assert "set-cookie" not in response.headers, f"403 #{attempt + 1} touched the cookie"


def lockout_state(row: Any) -> tuple[Any, ...]:
    return (row.failed_logins, row.locked_until)


def secrets_state(row: Any) -> tuple[Any, ...]:
    return (row.password_hash, row.recovery_code_hash)


# --------------------------------------------------------------------------
# Counting and revocation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
async def test_parallel_wrong_attempts_verify_at_most_ten_times_and_revoke(
    client: AsyncClient,
    accounts: Accounts,
    migrated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    own_hash_semaphore: None,
    route: str,
) -> None:
    alice = await new_account(client, accounts)
    verifier = CountingVerifier(auth_routes.verify_password)
    monkeypatch.setattr(auth_routes, "verify_password", verifier)

    responses = await asyncio.gather(
        *(confirm(client, route, alice.token, WRONG_PASSWORD) for _ in range(20))
    )

    statuses = [response.status_code for response in responses]
    assert verifier.calls <= 10, f"{verifier.calls} password checks ran"
    assert statuses.count(403) == 9, statuses
    assert statuses.count(401) == 11, statuses
    for response in responses:
        if response.status_code == 401:
            assert_signed_out(response)
    assert await counter(migrated_engine, alice.token) is None
    assert await token_hashes(migrated_engine, alice.id) == set()


@pytest.mark.parametrize("route", ROUTES)
async def test_nine_403s_then_the_tenth_signs_out_and_the_eleventh_is_401(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, route: str
) -> None:
    alice = await new_account(client, accounts)
    for n in range(1, 10):
        await fail(client, route, alice.token, 1)
        assert await counter(migrated_engine, alice.token) == n

    tenth = await confirm(client, route, alice.token, WRONG_PASSWORD)
    assert_signed_out(tenth, SIGNED_OUT)
    assert await counter(migrated_engine, alice.token) is None

    eleventh = await confirm(client, route, alice.token, PASSWORD)
    assert_signed_out(eleventh)
    assert await me_status(client, alice.token) == 401


async def test_both_routes_share_the_session_counter(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    alice = await new_account(client, accounts)
    await fail(client, "password", alice.token, 5)
    await fail(client, "recovery-code", alice.token, 4)
    assert await counter(migrated_engine, alice.token) == 9

    assert_signed_out(await confirm(client, "password", alice.token, WRONG_PASSWORD), SIGNED_OUT)
    assert await counter(migrated_engine, alice.token) is None


@pytest.mark.parametrize("route", ROUTES)
async def test_session_already_at_ten_is_401_without_a_check_even_for_the_right_password(
    client: AsyncClient,
    accounts: Accounts,
    migrated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    alice = await new_account(client, accounts)
    async with migrated_engine.begin() as conn:
        await conn.execute(
            update(tables.sessions)
            .where(tables.sessions.c.token_hash == sha256_bytes(alice.token))
            .values(failed_confirmations=10)
        )
    before = await user_row(migrated_engine, alice.username)
    verifier = CountingVerifier(auth_routes.verify_password)
    monkeypatch.setattr(auth_routes, "verify_password", verifier)

    assert_signed_out(await confirm(client, route, alice.token, PASSWORD), SIGNED_OUT)

    assert verifier.calls == 0
    assert await counter(migrated_engine, alice.token) is None
    assert secrets_state(await user_row(migrated_engine, alice.username)) == secrets_state(before)


# --------------------------------------------------------------------------
# Decoupled from the account lockout
# --------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
async def test_revocation_leaves_the_account_lockout_alone_and_signin_works(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, route: str
) -> None:
    alice = await new_account(client, accounts)
    # A non-zero count, so "unchanged" can't pass vacuously as "reset to 0".
    for _ in range(3):
        assert (await signin(client, alice.username, WRONG_PASSWORD)).status_code == 401
    before = lockout_state(await user_row(migrated_engine, alice.username))
    assert before == (3, None)

    await fail(client, route, alice.token, 9)
    assert_signed_out(await confirm(client, route, alice.token, WRONG_PASSWORD), SIGNED_OUT)

    assert lockout_state(await user_row(migrated_engine, alice.username)) == before
    response = await signin(client, alice.username, PASSWORD)
    assert response.status_code == 200
    assert await me_status(client, issued_token(response)) == 200


@pytest.mark.parametrize("route", ROUTES)
async def test_locked_account_wrong_is_403_and_right_succeeds(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, route: str
) -> None:
    alice = await new_account(client, accounts)
    for _ in range(10):
        assert (await signin(client, alice.username, WRONG_PASSWORD)).status_code == 401
    before = lockout_state(await user_row(migrated_engine, alice.username))
    assert before[1] is not None, "the account should be locked"

    await fail(client, route, alice.token, 1)
    assert (await confirm(client, route, alice.token, PASSWORD)).status_code == 200

    assert lockout_state(await user_row(migrated_engine, alice.username)) == before


# --------------------------------------------------------------------------
# Per session, and reset by a correct password
# --------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
async def test_another_session_of_the_same_user_is_unaffected(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, route: str
) -> None:
    alice = await new_account(client, accounts)
    other = issued_token(await signin(client, alice.username, PASSWORD))

    await fail(client, route, alice.token, 9)
    assert await counter(migrated_engine, other) == 0
    assert_signed_out(await confirm(client, route, alice.token, WRONG_PASSWORD), SIGNED_OUT)

    assert await token_hashes(migrated_engine, alice.id) == {sha256_bytes(other)}
    assert await counter(migrated_engine, other) == 0
    assert await me_status(client, other) == 200


@pytest.mark.parametrize("route", ROUTES)
async def test_correct_rotation_resets_the_counter(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, route: str
) -> None:
    alice = await new_account(client, accounts)
    await fail(client, route, alice.token, 9)

    assert (await rotate(client, alice.token, PASSWORD)).status_code == 200
    assert await counter(migrated_engine, alice.token) == 0

    await fail(client, route, alice.token, 9)
    assert await counter(migrated_engine, alice.token) == 9
    assert await me_status(client, alice.token) == 200


@pytest.mark.parametrize("route", ROUTES)
async def test_correct_password_change_issues_a_fresh_session_at_zero(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, route: str
) -> None:
    alice = await new_account(client, accounts)
    await fail(client, route, alice.token, 9)

    response = await change_password(client, alice.token, PASSWORD, NEW_PASSWORD)
    assert response.status_code == 200
    fresh = issued_token(response)
    assert await token_hashes(migrated_engine, alice.id) == {sha256_bytes(fresh)}
    assert await counter(migrated_engine, fresh) == 0

    await fail(client, route, fresh, 9)
    assert await me_status(client, fresh) == 200


# --------------------------------------------------------------------------
# The race: a correct password whose reset finds the session revoked
# --------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
async def test_correct_password_racing_the_tenth_failure_is_401_and_writes_nothing(
    client: AsyncClient,
    accounts: Accounts,
    migrated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    own_hash_semaphore: None,
    route: str,
) -> None:
    """
    8 failures; the right password claims #9 and is held inside its check; a
    wrong one claims #10 and revokes the session. When the held check
    finishes, its reset finds no row: 401, and no password, code or session is
    written.
    """
    alice: Account = await new_account(client, accounts)
    await fail(client, route, alice.token, 8)
    before = await user_row(migrated_engine, alice.username)
    verifier = CountingVerifier(auth_routes.verify_password, hold=PASSWORD)
    monkeypatch.setattr(auth_routes, "verify_password", verifier)

    correct = asyncio.create_task(confirm(client, route, alice.token, PASSWORD))
    try:
        await asyncio.wait_for(verifier.started.wait(), timeout=10)
        tenth = await asyncio.wait_for(
            confirm(client, route, alice.token, WRONG_PASSWORD), timeout=10
        )
        assert_signed_out(tenth, SIGNED_OUT)
        assert await counter(migrated_engine, alice.token) is None
    finally:
        verifier.release.set()
    response = await asyncio.wait_for(correct, timeout=10)

    assert_signed_out(response)
    assert "recoveryCode" not in response.text
    after = await user_row(migrated_engine, alice.username)
    assert secrets_state(after) == secrets_state(before)
    assert lockout_state(after) == lockout_state(before)
    assert await token_hashes(migrated_engine, alice.id) == set()
