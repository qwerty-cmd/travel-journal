"""
``/api/v2/auth`` — signup, signin, signout and me (task t-am-auth-sessions).

Written from ``docs/api-contract.md`` ("Sessions", "CSRF", "Rate limits and
lockout", "Error envelope", the v2 endpoint table and "Notes per endpoint (v2)")
and the task AC, not from the handlers:

- every status and body in the table for the four routes: signup 201/409/422,
  signin 200/401/422, signout 204, me 200/401 (the rate-limit 429s belong to
  ``t-am-rate-limits``; the lockout 429 is here);
- ``Set-Cookie`` is exactly ``__Host-btj_session=<43 base64url chars>; HttpOnly;
  Max-Age=7776000; Path=/; SameSite=lax; Secure``, one header, no ``Domain``;
- every 401 carries ``WWW-Authenticate: Cookie realm="bike-trip-journal"``, and
  unknown username, wrong password and disabled account are one identical 401;
- the per-account lockout: the 10th failure locks for 15 minutes, a locked
  account is ``429`` with ``Retry-After`` = seconds left even for the right
  password, the lock is read from Postgres (so a fresh engine and client still
  see it), success resets the counter, and ``now == locked_until`` is no longer
  locked ("while locked" is ``now < locked_until``);
- the recovery code is shown once (signup only) and only its SHA-256 is stored;
- no password, token, recovery code or hash in any response body, log record or
  captured output.

State is read straight from the ``users`` and ``sessions`` tables, never through
the repositories under test. Every account created here is deleted on teardown
(its sessions go with it, ``ON DELETE CASCADE``). No real token, code or
password is ever put into an assertion message: comparisons are on values pytest
does not echo, or the message is explicit and generic.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.main
from app.api.routes.v2 import auth as auth_routes
from app.core import passwords
from app.data import tables
from app.data.db import get_session
from tests.conftest import make_async_client

SIGNUP = "/api/v2/auth/signup"
SIGNIN = "/api/v2/auth/signin"
SIGNOUT = "/api/v2/auth/signout"
ME = "/api/v2/auth/me"

COOKIE = "__Host-btj_session"
WWW_AUTHENTICATE = 'Cookie realm="bike-trip-journal"'
ISSUED_COOKIE_RE = re.compile(
    r"^__Host-btj_session=([A-Za-z0-9_-]{43}); HttpOnly; Max-Age=7776000; Path=/; "
    r"SameSite=lax; Secure$"
)
CLEARED_COOKIE_RE = re.compile(
    r'^__Host-btj_session=(""|); HttpOnly; Max-Age=0; Path=/; SameSite=lax; Secure$'
)
RECOVERY_CODE_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")  # Crockford base32, uppercase
ME_KEYS = {"id", "username", "displayName", "createdAt"}

BAD_CREDENTIALS = "Username or password is incorrect."
PASSWORD = "correct horse battery staple"  # 28 code points
WRONG_PASSWORD = "incorrect horse battery staple"


def sha256_bytes(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def new_username() -> str:
    return f"t-auth-{secrets.token_hex(6)}"


def issued_token(response: httpx.Response) -> str:
    """The token of the one issuing ``Set-Cookie`` on ``response``; asserts the exact format."""
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1, f"expected exactly one Set-Cookie, got {len(cookies)}"
    match = ISSUED_COOKIE_RE.fullmatch(cookies[0])
    # Never echo the header: it carries a live token.
    assert match is not None, "Set-Cookie is not the contract's exact session cookie string"
    assert "domain" not in cookies[0].lower()
    return match.group(1)


def assert_envelope(response: httpx.Response, status: int, code: str, message: str | None = None):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    if message is not None:
        assert body["error"]["message"] == message
    return body["error"]


def cookie_header(token: str) -> dict[str, str]:
    return {"Cookie": f"{COOKIE}={token}"}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@dataclass
class Accounts:
    """Tracks every username a test signs up, for teardown."""

    usernames: list[str] = field(default_factory=list)

    def name(self) -> str:
        username = new_username()
        self.usernames.append(username)
        return username


@pytest.fixture
async def accounts(migrated_engine: AsyncEngine) -> AsyncIterator[Accounts]:
    tracker = Accounts()
    try:
        yield tracker
    finally:
        if tracker.usernames:
            # A fresh engine: a test may have disposed `migrated_engine` on purpose.
            engine = create_async_engine(migrated_engine.url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        tables.users.delete().where(tables.users.c.username.in_(tracker.usernames))
                    )
            finally:
                await engine.dispose()


def build_client(engine: AsyncEngine, **kwargs: Any) -> AsyncClient:
    """The real app with ``get_session`` pointed at ``engine``."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.main.app.dependency_overrides[get_session] = session_override
    return make_async_client(app.main.app, **kwargs)


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    try:
        async with build_client(migrated_engine) as http_client:
            yield http_client
    finally:
        app.main.app.dependency_overrides.pop(get_session, None)


async def send(client: AsyncClient, method: str, path: str, **kwargs: Any) -> httpx.Response:
    """One request with an empty cookie jar, so the only cookie sent is the one passed."""
    client.cookies.clear()
    response = await client.request(method, path, **kwargs)
    client.cookies.clear()
    return response


async def signup(
    client: AsyncClient, username: str, password: str = PASSWORD, **kwargs: Any
) -> httpx.Response:
    return await send(
        client,
        "POST",
        SIGNUP,
        json={"username": username, "displayName": "Test Rider", "password": password},
        **kwargs,
    )


async def signin(client: AsyncClient, username: str, password: str) -> httpx.Response:
    return await send(client, "POST", SIGNIN, json={"username": username, "password": password})


async def user_row(engine: AsyncEngine, username: str) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(select(tables.users).where(tables.users.c.username == username))
        ).one()


async def session_rows(engine: AsyncEngine, user_id: str) -> list[Any]:
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    select(tables.sessions).where(tables.sessions.c.user_id == user_id)
                )
            ).all()
        )


class Clock:
    """A settable replacement for ``app.api.routes.v2.auth._utcnow``."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    # Whole seconds: Postgres keeps microseconds, but whole seconds keep
    # Retry-After arithmetic exact.
    fixed = Clock(datetime.now(UTC).replace(microsecond=0))
    monkeypatch.setattr(auth_routes, "_utcnow", fixed)
    return fixed


# --------------------------------------------------------------------------
# Signup
# --------------------------------------------------------------------------


async def test_signup_creates_account_signs_in_and_stores_only_hashes(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    username = accounts.name()
    response = await send(
        client,
        "POST",
        SIGNUP,
        json={"username": username.upper(), "displayName": "  Test Rider  ", "password": PASSWORD},
    )

    assert response.status_code == 201
    token = issued_token(response)
    body = response.json()
    assert set(body) == {"account", "recoveryCode"}
    assert set(body["account"]) == ME_KEYS
    assert body["account"]["username"] == username  # stored lowercase
    assert body["account"]["displayName"] == "Test Rider"  # trimmed
    code = body["recoveryCode"]
    assert RECOVERY_CODE_RE.fullmatch(code), "recovery code is not 26 Crockford base32 chars"

    row = await user_row(migrated_engine, username)
    assert row.id == body["account"]["id"]
    assert datetime.fromisoformat(body["account"]["createdAt"]) == row.created_at
    # The one-time code in the body is the one whose SHA-256 is stored.
    assert row.recovery_code_hash == hashlib.sha256(code.encode()).hexdigest()
    assert row.password_hash.startswith("$argon2id$")
    assert PASSWORD not in row.password_hash
    assert row.failed_logins == 0 and row.locked_until is None

    # The cookie's token is stored only as its SHA-256, and it signs this device in.
    sessions = await session_rows(migrated_engine, row.id)
    assert [s.token_hash for s in sessions] == [sha256_bytes(token)]
    me = await send(client, "GET", ME, headers=cookie_header(token))
    assert me.status_code == 200
    assert me.json()["username"] == username


async def test_signup_taken_username_is_409_without_echo_or_cookie(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    username = accounts.name()
    assert (await signup(client, username)).status_code == 201

    # Different case, same stored username.
    response = await signup(client, username.upper(), password="another long password")

    assert_envelope(response, 409, "CONFLICT", "That username is taken.")
    assert username not in response.text.lower()
    assert "set-cookie" not in response.headers
    async with migrated_engine.connect() as conn:
        count = len(
            (
                await conn.execute(
                    select(tables.users.c.id).where(tables.users.c.username == username)
                )
            ).all()
        )
    assert count == 1


@pytest.mark.parametrize(
    ("body", "field_name"),
    [
        ({"username": "ab", "displayName": "R", "password": PASSWORD}, "username"),
        ({"username": "-leading", "displayName": "R", "password": PASSWORD}, "username"),
        ({"username": "ok-name", "displayName": "R", "password": "too short pw"}, "password"),
        (
            {"username": "ok-name", "displayName": "R", "password": "control\x07character pw"},
            "password",
        ),
        ({"username": "ok-name", "displayName": "   ", "password": PASSWORD}, "displayName"),
        ({"username": "ok-name", "displayName": "R" * 41, "password": PASSWORD}, "displayName"),
        ({"username": "ok-name", "displayName": "R"}, "password"),
    ],
    ids=[
        "username-too-short",
        "username-bad-first-char",
        "password-too-short",
        "password-control-char",
        "displayname-blank",
        "displayname-too-long",
        "password-missing",
    ],
)
async def test_signup_invalid_body_is_422_without_value_error_prefix(
    client: AsyncClient, body: dict[str, str], field_name: str
) -> None:
    response = await send(client, "POST", SIGNUP, json=body)

    error = assert_envelope(response, 422, "VALIDATION_ERROR")
    assert "Value error" not in error["message"]
    assert field_name in error["message"]
    assert "set-cookie" not in response.headers
    if "password" in body:
        assert body["password"] not in response.text


async def test_signup_with_existing_session_deletes_it_first(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    first = await signup(client, accounts.name())
    old_token = issued_token(first)

    second_name = accounts.name()
    response = await signup(client, second_name, headers=cookie_header(old_token))

    assert response.status_code == 201
    new_token = issued_token(response)
    assert new_token != old_token
    async with migrated_engine.connect() as conn:
        old_row = (
            await conn.execute(
                select(tables.sessions).where(
                    tables.sessions.c.token_hash == sha256_bytes(old_token)
                )
            )
        ).first()
    assert old_row is None, "the session already on the signup request was not deleted"
    assert (await send(client, "GET", ME, headers=cookie_header(old_token))).status_code == 401
    me = await send(client, "GET", ME, headers=cookie_header(new_token))
    assert me.json()["username"] == second_name


# --------------------------------------------------------------------------
# Signin
# --------------------------------------------------------------------------


async def test_signin_success_returns_me_and_sets_cookie(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    username = accounts.name()
    created = (await signup(client, username)).json()["account"]

    response = await signin(client, username.upper(), PASSWORD)  # case-insensitive

    assert response.status_code == 200
    assert response.json() == created
    token = issued_token(response)
    hashes = {s.token_hash for s in await session_rows(migrated_engine, created["id"])}
    assert sha256_bytes(token) in hashes


async def test_signin_nfkc_normalises_the_presented_password(
    client: AsyncClient, accounts: Accounts
) -> None:
    username = accounts.name()
    fullwidth = "ｃｏｒｒｅｃｔ ｈｏｒｓｅ ｂａｔｔｅｒｙ"  # NFKC → ASCII
    assert (await signup(client, username, password=fullwidth)).status_code == 201

    assert (await signin(client, username, "correct horse battery")).status_code == 200
    assert (await signin(client, username, fullwidth)).status_code == 200


async def test_signin_deletes_this_users_expired_sessions(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    username = accounts.name()
    created = await signup(client, username)
    live_hash = sha256_bytes(issued_token(created))
    user_id = created.json()["account"]["id"]

    now = datetime.now(UTC)
    idle_hash = sha256_bytes(secrets.token_urlsafe(32))
    capped_hash = sha256_bytes(secrets.token_urlsafe(32))
    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.sessions.insert(),
            [
                {
                    "token_hash": idle_hash,
                    "user_id": user_id,
                    "created_at": now - timedelta(days=100),
                    "last_used_at": now - timedelta(days=91),
                    "absolute_expires_at": now + timedelta(days=265),
                },
                {
                    "token_hash": capped_hash,
                    "user_id": user_id,
                    "created_at": now - timedelta(days=366),
                    "last_used_at": now - timedelta(days=1),
                    "absolute_expires_at": now - timedelta(days=1),
                },
            ],
        )

    response = await signin(client, username, PASSWORD)
    assert response.status_code == 200
    new_hash = sha256_bytes(issued_token(response))

    remaining = {s.token_hash for s in await session_rows(migrated_engine, user_id)}
    assert remaining == {live_hash, new_hash}


async def test_signin_422s(client: AsyncClient) -> None:
    missing = await send(client, "POST", SIGNIN, json={"username": "someone"})
    assert "Value error" not in assert_envelope(missing, 422, "VALIDATION_ERROR")["message"]

    too_long = await send(
        client, "POST", SIGNIN, json={"username": "someone", "password": "x" * 1025}
    )
    assert_envelope(too_long, 422, "VALIDATION_ERROR")

    # A short or odd password is *not* format-checked on signin: it is a 401.
    short = await send(client, "POST", SIGNIN, json={"username": "someone", "password": "a"})
    assert_envelope(short, 401, "UNAUTHENTICATED", BAD_CREDENTIALS)


async def test_unknown_username_runs_a_dummy_verify(
    client: AsyncClient, accounts: Accounts, monkeypatch: pytest.MonkeyPatch
) -> None:
    dummy = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_routes, "verify_dummy", dummy)

    unknown = await signin(client, new_username(), WRONG_PASSWORD)
    assert_envelope(unknown, 401, "UNAUTHENTICATED", BAD_CREDENTIALS)
    dummy.assert_awaited_once()
    assert dummy.await_args.args[0] == WRONG_PASSWORD

    # A non-ASCII username can name no account; it still costs a verify.
    dummy.reset_mock()
    await signin(client, "Kelvin-rider", WRONG_PASSWORD)
    dummy.assert_awaited_once()

    # A real account with a wrong password verifies against its own hash instead.
    username = accounts.name()
    assert (await signup(client, username)).status_code == 201
    dummy.reset_mock()
    assert (await signin(client, username, WRONG_PASSWORD)).status_code == 401
    dummy.assert_not_awaited()


async def test_unknown_user_wrong_password_and_disabled_are_one_identical_401(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    username = accounts.name()
    disabled = accounts.name()
    assert (await signup(client, username)).status_code == 201
    assert (await signup(client, disabled)).status_code == 201
    async with migrated_engine.begin() as conn:
        await conn.execute(
            update(tables.users)
            .where(tables.users.c.username == disabled)
            .values(disabled_at=datetime.now(UTC))
        )

    responses = {
        "unknown": await signin(client, new_username(), PASSWORD),
        "wrong-password": await signin(client, username, WRONG_PASSWORD),
        "disabled-correct-password": await signin(client, disabled, PASSWORD),
        "disabled-wrong-password": await signin(client, disabled, WRONG_PASSWORD),
    }

    for response in responses.values():
        assert_envelope(response, 401, "UNAUTHENTICATED", BAD_CREDENTIALS)
        assert response.headers["www-authenticate"] == WWW_AUTHENTICATE
        assert "set-cookie" not in response.headers

    reference = responses["unknown"]
    for name, response in responses.items():
        assert response.status_code == reference.status_code, name
        assert response.content == reference.content, name
        assert sorted(response.headers.multi_items()) == sorted(reference.headers.multi_items()), (
            name
        )


# --------------------------------------------------------------------------
# Lockout
# --------------------------------------------------------------------------


async def fail_times(client: AsyncClient, username: str, times: int) -> None:
    for attempt in range(times):
        response = await signin(client, username, WRONG_PASSWORD)
        assert response.status_code == 401, f"failure #{attempt + 1} was {response.status_code}"


def assert_locked(response: httpx.Response, retry_after: int) -> None:
    assert_envelope(response, 429, "RATE_LIMITED")
    assert response.headers["retry-after"] == str(retry_after)
    assert "set-cookie" not in response.headers


async def test_ten_failures_lock_even_the_correct_password_and_survive_restart(
    client: AsyncClient,
    accounts: Accounts,
    migrated_engine: AsyncEngine,
    clock: Clock,
) -> None:
    username = accounts.name()
    assert (await signup(client, username)).status_code == 201
    t0 = clock.now

    await fail_times(client, username, 9)
    row = await user_row(migrated_engine, username)
    assert (row.failed_logins, row.locked_until) == (9, None)

    # The 10th failure is itself a 401, and sets the lock and resets the counter.
    await fail_times(client, username, 1)
    row = await user_row(migrated_engine, username)
    assert row.failed_logins == 0
    assert row.locked_until == t0 + timedelta(minutes=15)

    assert_locked(await signin(client, username, PASSWORD), 900)
    assert_locked(await signin(client, username, WRONG_PASSWORD), 900)
    clock.now = t0 + timedelta(seconds=0, microseconds=500_000)
    assert_locked(await signin(client, username, PASSWORD), 900)  # ceil of 899.5

    # "Restart": drop every pooled connection, then a new engine and a new client.
    await migrated_engine.dispose()
    fresh_engine = create_async_engine(migrated_engine.url)
    try:
        async with build_client(fresh_engine) as fresh_client:
            clock.now = t0 + timedelta(minutes=5)
            assert_locked(await signin(fresh_client, username, PASSWORD), 600)
    finally:
        app.main.app.dependency_overrides.pop(get_session, None)
        await fresh_engine.dispose()


async def test_lock_boundary_and_expiry(
    client: AsyncClient,
    accounts: Accounts,
    migrated_engine: AsyncEngine,
    clock: Clock,
) -> None:
    username = accounts.name()
    assert (await signup(client, username)).status_code == 201
    t0 = clock.now
    await fail_times(client, username, 10)
    unlock_at = t0 + timedelta(minutes=15)

    # One second before the lock ends: still locked, Retry-After is the 1 s left.
    clock.now = unlock_at - timedelta(seconds=1)
    assert_locked(await signin(client, username, PASSWORD), 1)
    # Less than a second left still says at least 1.
    clock.now = unlock_at - timedelta(microseconds=1)
    assert_locked(await signin(client, username, PASSWORD), 1)

    # At locked_until the lock is over ("while locked" is now < locked_until).
    # The counter was reset when the lock was set, so a wrong password now is
    # failure #1 of a new run, not an instant re-lock.
    clock.now = unlock_at
    assert_envelope(
        await signin(client, username, WRONG_PASSWORD), 401, "UNAUTHENTICATED", BAD_CREDENTIALS
    )
    assert (await user_row(migrated_engine, username)).failed_logins == 1

    response = await signin(client, username, PASSWORD)
    assert response.status_code == 200
    issued_token(response)
    row = await user_row(migrated_engine, username)
    assert (row.failed_logins, row.locked_until) == (0, None)


async def test_success_resets_the_failure_counter(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, clock: Clock
) -> None:
    username = accounts.name()
    assert (await signup(client, username)).status_code == 201

    await fail_times(client, username, 5)
    assert (await user_row(migrated_engine, username)).failed_logins == 5
    assert (await signin(client, username, PASSWORD)).status_code == 200
    assert (await user_row(migrated_engine, username)).failed_logins == 0

    # Without the reset, 5 + 9 = 14 would have locked the account.
    await fail_times(client, username, 9)
    row = await user_row(migrated_engine, username)
    assert (row.failed_logins, row.locked_until) == (9, None)
    assert (await signin(client, username, PASSWORD)).status_code == 200


@pytest.fixture
def own_hash_semaphore(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A fresh argon2 semaphore for a test that makes requests *wait* on it.

    ``app.core.passwords._semaphore`` binds to the first event loop that waits on
    it, and then refuses every other loop (see that module's docstring). Each
    test here runs in its own loop, so a concurrency test waiting on the shared
    semaphore would break the next test that does.
    """
    monkeypatch.setattr(passwords, "_semaphore", asyncio.Semaphore(passwords.MAX_CONCURRENT_HASHES))


class CountingVerifier:
    """
    Wraps ``auth_routes.verify_password``: counts calls, and can hold one password.

    A held password's verify waits on ``release`` before running, which lets a
    test pin the moment a concurrent failure lands inside another request's
    argon2 window. The count is what a guessing attacker is buying.
    """

    def __init__(self, real: Callable[..., Any], hold: str | None = None) -> None:
        self.real = real
        self.hold = hold
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, password_hash: str, password: str) -> bool:
        self.calls += 1
        if password == self.hold:
            self.started.set()
            await self.release.wait()
        return await self.real(password_hash, password)


async def test_parallel_wrong_signins_verify_at_most_ten_times(
    client: AsyncClient,
    accounts: Accounts,
    migrated_engine: AsyncEngine,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    own_hash_semaphore: None,
) -> None:
    """
    20 wrong signins at once run at most 10 password checks, then the account is locked.

    Each check is claimed in Postgres before argon2 runs, so racing requests
    can't all pass a lock check that none of them has tripped yet.
    """
    username = accounts.name()
    assert (await signup(client, username)).status_code == 201
    verifier = CountingVerifier(auth_routes.verify_password)
    monkeypatch.setattr(auth_routes, "verify_password", verifier)

    responses = await asyncio.gather(*(signin(client, username, WRONG_PASSWORD) for _ in range(20)))

    statuses = [response.status_code for response in responses]
    assert verifier.calls <= 10, f"{verifier.calls} password checks ran"
    assert statuses.count(401) == verifier.calls
    assert statuses.count(429) == 20 - verifier.calls
    for response in responses:
        if response.status_code == 429:
            assert_locked(response, 900)
    row = await user_row(migrated_engine, username)
    assert row.locked_until == clock.now + timedelta(minutes=15)
    assert row.failed_logins == 0

    # Locked: the right password is refused and not checked.
    calls = verifier.calls
    assert_locked(await signin(client, username, PASSWORD), 900)
    assert verifier.calls == calls


async def test_correct_password_racing_a_lock_is_refused_and_leaves_the_lock(
    client: AsyncClient,
    accounts: Accounts,
    migrated_engine: AsyncEngine,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    own_hash_semaphore: None,
) -> None:
    """
    A right password whose check straddles the 10th failure gets 429, no session.

    9 failures, then the right password starts its check and is held inside it,
    then a 10th failure locks the account. When the held check finishes, the
    success must neither sign in nor clear the lock.
    """
    username = accounts.name()
    created = await signup(client, username)
    user_id = created.json()["account"]["id"]
    await fail_times(client, username, 9)
    verifier = CountingVerifier(auth_routes.verify_password, hold=PASSWORD)
    monkeypatch.setattr(auth_routes, "verify_password", verifier)

    correct = asyncio.create_task(signin(client, username, PASSWORD))
    await asyncio.wait_for(verifier.started.wait(), timeout=10)
    clock.now += timedelta(seconds=30)
    lock_until = clock.now + timedelta(minutes=15)
    # The correct attempt holds claim #10, so the next attempt is refused and
    # the saturated count becomes a lock.
    assert_locked(await signin(client, username, WRONG_PASSWORD), 900)
    row = await user_row(migrated_engine, username)
    assert (row.failed_logins, row.locked_until) == (0, lock_until)

    clock.now += timedelta(seconds=60)
    verifier.release.set()
    response = await asyncio.wait_for(correct, timeout=10)

    assert_locked(response, 840)
    row = await user_row(migrated_engine, username)
    assert row.locked_until == lock_until, "a success cleared a lock it didn't set"
    assert len(await session_rows(migrated_engine, user_id)) == 1  # signup's only


async def test_late_failure_locks_from_the_time_it_failed(
    client: AsyncClient,
    accounts: Accounts,
    migrated_engine: AsyncEngine,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    own_hash_semaphore: None,
) -> None:
    """The 10th failure's lock runs from when its check finished, not when the request began."""
    username = accounts.name()
    assert (await signup(client, username)).status_code == 201
    await fail_times(client, username, 9)
    verifier = CountingVerifier(auth_routes.verify_password, hold=WRONG_PASSWORD)
    monkeypatch.setattr(auth_routes, "verify_password", verifier)

    tenth = asyncio.create_task(signin(client, username, WRONG_PASSWORD))
    await asyncio.wait_for(verifier.started.wait(), timeout=10)
    clock.now += timedelta(seconds=45)
    verifier.release.set()
    assert (await asyncio.wait_for(tenth, timeout=10)).status_code == 401

    row = await user_row(migrated_engine, username)
    assert row.locked_until == clock.now + timedelta(minutes=15)


# --------------------------------------------------------------------------
# Signout
# --------------------------------------------------------------------------


def assert_cleared(response: httpx.Response) -> None:
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1
    assert CLEARED_COOKIE_RE.fullmatch(cookies[0]), cookies[0]


async def test_signout_deletes_the_session_and_clears_the_cookie(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    created = await signup(client, accounts.name())
    token = issued_token(created)
    user_id = created.json()["account"]["id"]
    other = issued_token(await signin(client, created.json()["account"]["username"], PASSWORD))

    response = await send(client, "POST", SIGNOUT, headers=cookie_header(token))

    assert response.status_code == 204
    assert response.content == b""
    assert_cleared(response)
    remaining = {s.token_hash for s in await session_rows(migrated_engine, user_id)}
    assert remaining == {sha256_bytes(other)}, "signout must delete this session only"
    assert_envelope(
        await send(client, "GET", ME, headers=cookie_header(token)), 401, "UNAUTHENTICATED"
    )


@pytest.mark.parametrize("cookie", [None, "not-a-real-token", ""], ids=["none", "garbage", "empty"])
async def test_signout_without_a_valid_session_is_still_204_and_clears(
    client: AsyncClient, cookie: str | None
) -> None:
    headers = cookie_header(cookie) if cookie is not None else {}
    response = await send(client, "POST", SIGNOUT, headers=headers)

    assert response.status_code == 204
    assert response.content == b""
    assert_cleared(response)


# --------------------------------------------------------------------------
# Me
# --------------------------------------------------------------------------


async def test_me_returns_the_callers_account_and_head_matches(
    client: AsyncClient, accounts: Accounts
) -> None:
    username = accounts.name()
    created = await signup(client, username)
    token = issued_token(created)

    response = await send(client, "GET", ME, headers=cookie_header(token))
    assert response.status_code == 200
    assert response.json() == created.json()["account"]
    assert response.json()["username"] == username
    # A fresh session is not refreshed.
    assert "set-cookie" not in response.headers

    head = await send(client, "HEAD", ME, headers=cookie_header(token))
    assert head.status_code == 200
    assert head.content == b""


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_me_without_a_session_is_401_without_set_cookie(
    client: AsyncClient, method: str
) -> None:
    response = await send(client, method, ME)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == WWW_AUTHENTICATE
    assert "set-cookie" not in response.headers
    if method == "GET":
        assert_envelope(response, 401, "UNAUTHENTICATED")


async def test_me_with_an_unknown_token_is_401_and_clears_the_cookie(client: AsyncClient) -> None:
    response = await send(client, "GET", ME, headers=cookie_header(secrets.token_urlsafe(32)))

    assert_envelope(response, 401, "UNAUTHENTICATED")
    assert response.headers["www-authenticate"] == WWW_AUTHENTICATE
    assert_cleared(response)


async def test_me_on_a_day_old_session_reissues_exactly_one_cookie(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    created = await signup(client, accounts.name())
    user_id = created.json()["account"]["id"]
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    stale = now - timedelta(hours=25)
    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.sessions.insert().values(
                token_hash=sha256_bytes(token),
                user_id=user_id,
                created_at=stale,
                last_used_at=stale,
                absolute_expires_at=stale + timedelta(days=365),
            )
        )

    response = await send(client, "GET", ME, headers=cookie_header(token))

    assert response.status_code == 200
    assert issued_token(response) == token  # exactly one header, same token, fresh Max-Age
    async with migrated_engine.connect() as conn:
        last_used = (
            await conn.execute(
                select(tables.sessions.c.last_used_at).where(
                    tables.sessions.c.token_hash == sha256_bytes(token)
                )
            )
        ).scalar_one()
    assert last_used >= now - timedelta(minutes=1)


# --------------------------------------------------------------------------
# Nothing sensitive in bodies, logs or output
# --------------------------------------------------------------------------


async def test_no_secret_in_any_response_body(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    username = accounts.name()
    up = await signup(client, username)
    up_token = issued_token(up)
    code = up.json()["recoveryCode"]
    inn = await signin(client, username, PASSWORD)
    in_token = issued_token(inn)
    bad = await signin(client, username, WRONG_PASSWORD)
    me = await send(client, "GET", ME, headers=cookie_header(in_token))
    out = await send(client, "POST", SIGNOUT, headers=cookie_header(in_token))

    row = await user_row(migrated_engine, username)
    secrets_ = {
        "password": PASSWORD,
        "wrong password": WRONG_PASSWORD,
        "signup token": up_token,
        "signin token": in_token,
        "password hash": row.password_hash,
        "recovery code hash": row.recovery_code_hash,
        "signup token hash": sha256_bytes(up_token).hex(),
        "signin token hash": sha256_bytes(in_token).hex(),
    }
    bodies = {"signup": up, "signin": inn, "bad signin": bad, "me": me, "signout": out}
    for route, response in bodies.items():
        for label, value in secrets_.items():
            assert value not in response.text, f"{label} appears in the {route} body"
        assert '"password' not in response.text, f"a password field in the {route} body"
        if route != "signup":
            assert code not in response.text, f"the recovery code appears in the {route} body"
            assert "recoveryCode" not in response.text, f"recoveryCode in the {route} body"


async def test_no_secret_in_logs_or_captured_output(
    client: AsyncClient,
    accounts: Accounts,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    username = accounts.name()

    up = await signup(client, username)
    up_token = issued_token(up)
    code = up.json()["recoveryCode"]
    inn = await signin(client, username, PASSWORD)
    in_token = issued_token(inn)
    assert (await signin(client, username, WRONG_PASSWORD)).status_code == 401
    assert (await signin(client, new_username(), WRONG_PASSWORD)).status_code == 401
    assert (await send(client, "GET", ME, headers=cookie_header(in_token))).status_code == 200

    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    logged = "\n".join(formatter.format(record) for record in caplog.records)
    captured = capsys.readouterr()
    haystacks = {"log records": logged, "stdout": captured.out, "stderr": captured.err}
    needles: dict[str, Callable[[], str]] = {
        "password": lambda: PASSWORD,
        "wrong password": lambda: WRONG_PASSWORD,
        "recovery code": lambda: code,
        "signup token": lambda: up_token,
        "signin token": lambda: in_token,
    }
    for where, text in haystacks.items():
        for label, value in needles.items():
            assert value() not in text, f"the {label} appears in {where}"


# --------------------------------------------------------------------------
# CSRF and OpenAPI
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [SIGNUP, SIGNIN, SIGNOUT])
async def test_cross_site_auth_posts_are_blocked(
    migrated_engine: AsyncEngine, accounts: Accounts, path: str
) -> None:
    username = accounts.name()
    try:
        async with build_client(
            migrated_engine,
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        ) as evil:
            response = await evil.post(
                path,
                json={"username": username, "displayName": "R", "password": PASSWORD},
            )
    finally:
        app.main.app.dependency_overrides.pop(get_session, None)

    assert_envelope(
        response, 403, "FORBIDDEN", "This request came from another site and was blocked."
    )
    assert "set-cookie" not in response.headers
    async with migrated_engine.connect() as conn:
        created = (
            await conn.execute(select(tables.users.c.id).where(tables.users.c.username == username))
        ).first()
    assert created is None


@pytest.mark.parametrize(
    ("method", "path", "statuses"),
    [
        ("post", SIGNUP, {"201", "409", "422", "429"}),
        ("post", SIGNIN, {"200", "401", "422", "429"}),
        ("post", SIGNOUT, {"204"}),
        ("get", ME, {"200", "401", "429"}),
    ],
)
def test_openapi_declares_the_contract_statuses(method: str, path: str, statuses: set[str]) -> None:
    operation = app.main.app.openapi()["paths"][path][method]
    assert set(operation["responses"]) == statuses
    if "429" in statuses:
        assert "Retry-After" in operation["responses"]["429"].get("headers", {})
