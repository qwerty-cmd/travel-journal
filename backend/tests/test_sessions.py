"""
Session primitives and the ``require_session`` gate (task t-am-identity-core).

Assertions come from docs/api-contract.md, "Sessions", and the task AC:

- the token is 32 bytes from ``secrets``, base64url (43 characters, no padding);
- the database holds only ``SHA-256(token)``, 32 bytes, read here straight from
  the ``sessions`` table (never through the repository under test);
- a session is valid iff ``now < last_used_at + 90 d`` and
  ``now < absolute_expires_at`` (``created_at + 365 d``): both sides of each
  boundary are checked;
- ``last_used_at`` is written only when it is more than 24 h old, and that write
  re-issues ``__Host-btj_session=...; Path=/; Secure; HttpOnly; SameSite=Lax;
  Max-Age=7776000``;
- a missing, garbage, idle-expired, over-cap or deleted token, or a disabled
  account, is ``401 UNAUTHENTICATED`` with ``WWW-Authenticate:
  Cookie realm="bike-trip-journal"``, and the cookie is cleared (``Max-Age=0``)
  only when one was sent;
- ``ApiError`` refuses a 401 without ``WWW-Authenticate`` and a 429 without
  ``Retry-After``;
- no password, token or hash reaches any log record.

Session rows the boundary tests need are inserted *directly* into the table with
chosen timestamps, so what is under test (``resolve_session``) is never also the
thing that set up its own input. Every user created here is deleted on
teardown; its sessions go with it (``ON DELETE CASCADE``).

A real token is never put into an assertion message: comparisons are ``==`` on
values pytest does not echo for these types, or on hashes.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core import passwords
from app.core.errors import ApiError, register_exception_handlers
from app.core.security import require_session
from app.core.sessions import SessionUser, create_session, new_token, resolve_session
from app.data import tables
from app.data.db import get_session
from app.data.repositories import users as users_repo
from app.models.common import ErrorCode
from tests.conftest import make_async_client

COOKIE = "__Host-btj_session"
MAX_AGE_90D = 7_776_000
WWW_AUTHENTICATE = 'Cookie realm="bike-trip-journal"'
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

# Postgres keeps microseconds; whole seconds keep round-trips exact.
T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def sha256(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def sessionmaker(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(migrated_engine, expire_on_commit=False)


@dataclass
class Users:
    """Creates accounts for one test and deletes them all on teardown."""

    sessionmaker: async_sessionmaker[AsyncSession]
    created: list[str]

    async def make(self) -> str:
        async with self.sessionmaker() as db:
            record = await users_repo.create(
                db,
                username=f"t{secrets.token_hex(8)}",
                display_name="Test Rider",
                # Not a real hash: these tests never verify a password.
                password_hash="$argon2id$v=19$m=19456,t=2,p=1$placeholder$placeholder",
                recovery_code_hash=None,
            )
            await db.commit()
        self.created.append(record.id)
        return record.id


@pytest.fixture
async def users(
    sessionmaker: async_sessionmaker[AsyncSession], migrated_engine: AsyncEngine
) -> AsyncIterator[Users]:
    factory = Users(sessionmaker, [])
    try:
        yield factory
    finally:
        if factory.created:
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    tables.users.delete().where(tables.users.c.id.in_(factory.created))
                )


async def insert_session_row(
    engine: AsyncEngine,
    user_id: str,
    *,
    created_at: datetime,
    last_used_at: datetime,
    absolute_expires_at: datetime,
) -> str:
    """Put a session row with chosen timestamps straight into the table; return its token."""
    token = secrets.token_urlsafe(32)
    async with engine.begin() as conn:
        await conn.execute(
            tables.sessions.insert().values(
                token_hash=sha256(token),
                user_id=user_id,
                created_at=created_at,
                last_used_at=last_used_at,
                absolute_expires_at=absolute_expires_at,
            )
        )
    return token


async def session_rows(engine: AsyncEngine, user_id: str) -> list[dict]:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(tables.sessions).where(tables.sessions.c.user_id == user_id)
        )
        return [dict(row._mapping) for row in result]


async def last_used(engine: AsyncEngine, token: str) -> datetime:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                select(tables.sessions.c.last_used_at).where(
                    tables.sessions.c.token_hash == sha256(token)
                )
            )
        ).scalar_one()


async def resolve(sessionmaker: async_sessionmaker[AsyncSession], token: str, now: datetime):
    async with sessionmaker() as db:
        return await resolve_session(db, token, now=now)


# --------------------------------------------------------------------------
# Token
# --------------------------------------------------------------------------


def test_token_is_43_char_base64url_of_32_bytes() -> None:
    tokens = {new_token() for _ in range(200)}

    assert len(tokens) == 200
    for token in tokens:
        assert TOKEN_RE.fullmatch(token)
        assert len(base64.urlsafe_b64decode(token + "=")) == 32


def test_token_bytes_come_from_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    drawn: list[int] = []
    real = secrets.token_bytes

    def spy(nbytes: int | None = None) -> bytes:
        drawn.append(nbytes)  # type: ignore[arg-type]
        return real(nbytes)

    monkeypatch.setattr(secrets, "token_bytes", spy)
    new_token()

    assert drawn == [32]


# --------------------------------------------------------------------------
# Stored form: the row holds sha256(token) and nothing else token-derived
# --------------------------------------------------------------------------


async def test_create_session_stores_only_sha256_of_token(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_engine: AsyncEngine,
    users: Users,
) -> None:
    user_id = await users.make()
    async with sessionmaker() as db:
        token = await create_session(db, user_id, now=T0)
        await db.commit()

    assert TOKEN_RE.fullmatch(token)
    rows = await session_rows(migrated_engine, user_id)
    assert len(rows) == 1
    row = rows[0]

    assert isinstance(row["token_hash"], bytes)
    assert len(row["token_hash"]) == 32
    assert row["token_hash"] == sha256(token)
    assert row["created_at"] == T0
    assert row["last_used_at"] == T0
    assert row["absolute_expires_at"] == T0 + timedelta(days=365)

    # Nothing in the row is, contains, or decodes to the token.
    token_raw = base64.urlsafe_b64decode(token + "=")
    for value in row.values():
        rendered = value if isinstance(value, bytes) else str(value).encode()
        assert token.encode() not in rendered
        assert token_raw not in rendered
    # The table has no other column a token could hide in.
    assert set(row) == {
        "token_hash",
        "user_id",
        "created_at",
        "last_used_at",
        "absolute_expires_at",
    }


async def test_create_session_does_not_commit(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_engine: AsyncEngine,
    users: Users,
) -> None:
    """The caller owns the transaction (signup issues a session inside its own)."""
    user_id = await users.make()
    async with sessionmaker() as db:
        await create_session(db, user_id, now=T0)
        await db.rollback()

    assert await session_rows(migrated_engine, user_id) == []


async def test_created_session_resolves_to_its_user(
    sessionmaker: async_sessionmaker[AsyncSession], users: Users
) -> None:
    user_id = await users.make()
    other_id = await users.make()
    async with sessionmaker() as db:
        token = await create_session(db, user_id, now=T0)
        other = await create_session(db, other_id, now=T0)
        await db.commit()

    resolved = await resolve(sessionmaker, token, T0 + timedelta(hours=1))
    assert resolved is not None
    assert resolved.user.user_id == user_id
    assert resolved.refreshed is False

    resolved_other = await resolve(sessionmaker, other, T0 + timedelta(hours=1))
    assert resolved_other is not None
    assert resolved_other.user.user_id == other_id


# --------------------------------------------------------------------------
# Validity boundaries
# --------------------------------------------------------------------------

SECOND = timedelta(seconds=1)


@pytest.mark.parametrize(
    ("offset", "valid"),
    [
        (timedelta(days=90) - SECOND, True),
        (timedelta(days=90) - timedelta(microseconds=1), True),
        (timedelta(days=90), False),  # strict: now < last_used_at + 90 d
        (timedelta(days=90) + SECOND, False),
    ],
    ids=["90d-1s", "90d-1us", "exactly-90d", "90d+1s"],
)
async def test_idle_boundary(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_engine: AsyncEngine,
    users: Users,
    offset: timedelta,
    valid: bool,
) -> None:
    user_id = await users.make()
    last = T0
    token = await insert_session_row(
        migrated_engine,
        user_id,
        created_at=last - timedelta(days=10),
        last_used_at=last,
        absolute_expires_at=last + timedelta(days=300),  # cap far away
    )

    resolved = await resolve(sessionmaker, token, last + offset)

    assert (resolved is not None) is valid


@pytest.mark.parametrize(
    ("offset", "valid"),
    [
        (-SECOND, True),
        (-timedelta(microseconds=1), True),
        (timedelta(0), False),  # strict: now < absolute_expires_at
        (SECOND, False),
    ],
    ids=["cap-1s", "cap-1us", "exactly-cap", "cap+1s"],
)
async def test_absolute_cap_boundary(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_engine: AsyncEngine,
    users: Users,
    offset: timedelta,
    valid: bool,
) -> None:
    user_id = await users.make()
    created = T0
    cap = created + timedelta(days=365)
    token = await insert_session_row(
        migrated_engine,
        user_id,
        created_at=created,
        last_used_at=cap - timedelta(hours=1),  # idle window nowhere near
        absolute_expires_at=cap,
    )

    resolved = await resolve(sessionmaker, token, cap + offset)

    assert (resolved is not None) is valid


async def test_daily_use_cannot_extend_past_the_365_day_cap(
    sessionmaker: async_sessionmaker[AsyncSession], users: Users
) -> None:
    """Sliding renewal keeps it alive daily, but the cap from creation still ends it."""
    user_id = await users.make()
    async with sessionmaker() as db:
        token = await create_session(db, user_id, now=T0)
        await db.commit()

    day = T0
    while day + timedelta(days=30) < T0 + timedelta(days=365):
        day += timedelta(days=30)
        assert await resolve(sessionmaker, token, day) is not None

    assert await resolve(sessionmaker, token, T0 + timedelta(days=365) - SECOND) is not None
    assert await resolve(sessionmaker, token, T0 + timedelta(days=365)) is None


async def test_garbage_unknown_and_deleted_tokens_resolve_to_nothing(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_engine: AsyncEngine,
    users: Users,
) -> None:
    user_id = await users.make()
    async with sessionmaker() as db:
        token = await create_session(db, user_id, now=T0)
        await db.commit()
    now = T0 + timedelta(hours=1)

    for garbage in ["", "x", "not a token at all", "é" * 50, new_token(), token + "A", token[:-1]]:
        assert await resolve(sessionmaker, garbage, now) is None

    assert await resolve(sessionmaker, token, now) is not None
    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.sessions.delete().where(tables.sessions.c.token_hash == sha256(token))
        )
    assert await resolve(sessionmaker, token, now) is None


async def test_disabled_user_resolves_to_nothing(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_engine: AsyncEngine,
    users: Users,
) -> None:
    user_id = await users.make()
    async with sessionmaker() as db:
        token = await create_session(db, user_id, now=T0)
        await db.commit()
    now = T0 + timedelta(hours=1)
    assert await resolve(sessionmaker, token, now) is not None

    async with migrated_engine.begin() as conn:
        await conn.execute(
            update(tables.users).where(tables.users.c.id == user_id).values(disabled_at=T0)
        )

    assert await resolve(sessionmaker, token, now) is None


# --------------------------------------------------------------------------
# last_used_at: written only when more than 24 h old
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "written"),
    [
        (timedelta(hours=1), False),
        (timedelta(hours=24) - SECOND, False),
        (timedelta(hours=24), False),  # "more than 24 h", so exactly 24 h is not
        (timedelta(hours=24) + SECOND, True),
        (timedelta(days=89), True),
    ],
    ids=["1h", "24h-1s", "exactly-24h", "24h+1s", "89d"],
)
async def test_last_used_written_only_when_more_than_24h_old(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_engine: AsyncEngine,
    users: Users,
    age: timedelta,
    written: bool,
) -> None:
    user_id = await users.make()
    last = T0
    token = await insert_session_row(
        migrated_engine,
        user_id,
        created_at=last - timedelta(days=1),
        last_used_at=last,
        absolute_expires_at=last + timedelta(days=300),
    )
    now = last + age

    resolved = await resolve(sessionmaker, token, now)

    assert resolved is not None
    assert resolved.refreshed is written
    # Read back through a separate connection: the write was committed.
    assert await last_used(migrated_engine, token) == (now if written else last)


async def test_second_request_after_a_touch_does_not_write_again(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_engine: AsyncEngine,
    users: Users,
) -> None:
    user_id = await users.make()
    token = await insert_session_row(
        migrated_engine,
        user_id,
        created_at=T0 - timedelta(days=5),
        last_used_at=T0 - timedelta(days=2),
        absolute_expires_at=T0 + timedelta(days=300),
    )

    first = await resolve(sessionmaker, token, T0)
    second = await resolve(sessionmaker, token, T0 + timedelta(minutes=5))
    third = await resolve(sessionmaker, token, T0 + timedelta(hours=23))

    assert first is not None and first.refreshed is True
    assert second is not None and second.refreshed is False
    assert third is not None and third.refreshed is False
    assert await last_used(migrated_engine, token) == T0


async def test_touch_slides_the_idle_window(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_engine: AsyncEngine,
    users: Users,
) -> None:
    """A use on day 60 moves the 90-day window: day 100 is then still valid."""
    user_id = await users.make()
    token = await insert_session_row(
        migrated_engine,
        user_id,
        created_at=T0,
        last_used_at=T0,
        absolute_expires_at=T0 + timedelta(days=365),
    )

    assert await resolve(sessionmaker, token, T0 + timedelta(days=60)) is not None
    assert await resolve(sessionmaker, token, T0 + timedelta(days=100)) is not None
    assert await resolve(sessionmaker, token, T0 + timedelta(days=190)) is None


# --------------------------------------------------------------------------
# require_session over HTTP
# --------------------------------------------------------------------------


def _probe_app(sessionmaker: async_sessionmaker[AsyncSession]) -> FastAPI:
    """A tiny app with the real error handlers and one route behind the gate."""
    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.get("/probe")
    async def whoami(user: SessionUser = Depends(require_session)) -> dict[str, str]:  # noqa: B008
        return {"userId": user.user_id}

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as db:
            yield db

    probe.dependency_overrides[get_session] = session_override
    return probe


@pytest.fixture
async def client(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncClient]:
    async with make_async_client(_probe_app(sessionmaker)) as http_client:
        yield http_client


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _cookie_attrs(set_cookie: str) -> tuple[str, str, dict[str, str]]:
    """Split one Set-Cookie value into (name, value, {lowercased attr: value})."""
    first, *rest = [part.strip() for part in set_cookie.split(";")]
    name, _, value = first.partition("=")
    attrs: dict[str, str] = {}
    for part in rest:
        key, _, val = part.partition("=")
        attrs[key.strip().lower()] = val.strip()
    return name, value, attrs


def _assert_host_cookie_attrs(attrs: dict[str, str]) -> None:
    """What a browser requires to accept (or overwrite) a ``__Host-`` cookie."""
    assert "secure" in attrs
    assert attrs.get("path") == "/"
    assert "domain" not in attrs
    assert "httponly" in attrs
    assert attrs.get("samesite", "").lower() == "lax"


async def test_valid_fresh_session_passes_without_set_cookie(
    client: AsyncClient, migrated_engine: AsyncEngine, users: Users
) -> None:
    user_id = await users.make()
    now = _now()
    token = await insert_session_row(
        migrated_engine,
        user_id,
        created_at=now - timedelta(days=3),
        last_used_at=now - timedelta(hours=2),
        absolute_expires_at=now + timedelta(days=300),
    )

    response = await client.get("/probe", headers={"Cookie": f"{COOKIE}={token}"})

    assert response.status_code == 200
    assert response.json() == {"userId": user_id}
    assert "set-cookie" not in response.headers
    assert await last_used(migrated_engine, token) == now - timedelta(hours=2)


async def test_stale_session_is_touched_and_cookie_reissued_once(
    client: AsyncClient, migrated_engine: AsyncEngine, users: Users
) -> None:
    user_id = await users.make()
    now = _now()
    token = await insert_session_row(
        migrated_engine,
        user_id,
        created_at=now - timedelta(days=30),
        last_used_at=now - timedelta(hours=25),
        absolute_expires_at=now + timedelta(days=300),
    )

    first = await client.get("/probe", headers={"Cookie": f"{COOKIE}={token}"})

    assert first.status_code == 200
    assert first.json() == {"userId": user_id}
    set_cookies = first.headers.get_list("set-cookie")
    assert len(set_cookies) == 1
    name, value, attrs = _cookie_attrs(set_cookies[0])
    assert name == COOKIE
    assert value == token  # the same session, re-issued; not a new token
    assert attrs.get("max-age") == str(MAX_AGE_90D)
    _assert_host_cookie_attrs(attrs)
    touched = await last_used(migrated_engine, token)
    assert now <= touched <= datetime.now(UTC)

    second = await client.get("/probe", headers={"Cookie": f"{COOKIE}={token}"})

    assert second.status_code == 200
    assert "set-cookie" not in second.headers
    assert await last_used(migrated_engine, token) == touched


async def _expect_401(response, *, cookie_sent: bool) -> None:
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": ErrorCode.UNAUTHENTICATED.value,
            "message": response.json()["error"]["message"],
        }
    }
    assert response.json()["error"]["message"]
    assert response.headers.get("www-authenticate") == WWW_AUTHENTICATE
    set_cookies = response.headers.get_list("set-cookie")
    if not cookie_sent:
        assert set_cookies == []
        return
    assert len(set_cookies) == 1
    name, value, attrs = _cookie_attrs(set_cookies[0])
    assert name == COOKIE
    assert value in ("", '""')
    assert attrs.get("max-age") == "0"
    # A clearing header a browser ignores clears nothing: it must match the
    # __Host- rules the original was set under.
    assert "secure" in attrs
    assert attrs.get("path") == "/"
    assert "domain" not in attrs


async def test_missing_cookie_is_401_without_set_cookie(client: AsyncClient) -> None:
    response = await client.get("/probe")

    await _expect_401(response, cookie_sent=False)


async def test_other_cookies_only_is_401_without_set_cookie(client: AsyncClient) -> None:
    response = await client.get("/probe", headers={"Cookie": "theme=dark; btj_session=nope"})

    await _expect_401(response, cookie_sent=False)


@pytest.mark.parametrize("value", ["", "garbage", "a" * 43, "%00%ff", "x" * 4000])
async def test_garbage_cookie_is_401_and_cleared(client: AsyncClient, value: str) -> None:
    response = await client.get("/probe", headers={"Cookie": f"{COOKIE}={value}"})

    await _expect_401(response, cookie_sent=True)


SetupFn = Callable[[AsyncEngine, str, datetime], Awaitable[str]]


async def _idle_expired(engine: AsyncEngine, user_id: str, now: datetime) -> str:
    return await insert_session_row(
        engine,
        user_id,
        created_at=now - timedelta(days=100),
        last_used_at=now - timedelta(days=90, seconds=1),
        absolute_expires_at=now + timedelta(days=200),
    )


async def _over_cap(engine: AsyncEngine, user_id: str, now: datetime) -> str:
    return await insert_session_row(
        engine,
        user_id,
        created_at=now - timedelta(days=365, seconds=1),
        last_used_at=now - timedelta(hours=1),
        absolute_expires_at=now - SECOND,
    )


async def _deleted(engine: AsyncEngine, user_id: str, now: datetime) -> str:
    token = await insert_session_row(
        engine,
        user_id,
        created_at=now - timedelta(days=1),
        last_used_at=now - timedelta(hours=1),
        absolute_expires_at=now + timedelta(days=300),
    )
    async with engine.begin() as conn:
        await conn.execute(
            tables.sessions.delete().where(tables.sessions.c.token_hash == sha256(token))
        )
    return token


async def _disabled_user(engine: AsyncEngine, user_id: str, now: datetime) -> str:
    token = await insert_session_row(
        engine,
        user_id,
        created_at=now - timedelta(days=1),
        last_used_at=now - timedelta(hours=1),
        absolute_expires_at=now + timedelta(days=300),
    )
    async with engine.begin() as conn:
        await conn.execute(
            update(tables.users).where(tables.users.c.id == user_id).values(disabled_at=now)
        )
    return token


@pytest.mark.parametrize(
    "setup",
    [_idle_expired, _over_cap, _deleted, _disabled_user],
    ids=["idle-expired", "over-cap", "deleted", "disabled-user"],
)
async def test_invalid_session_is_401_and_cleared(
    client: AsyncClient, migrated_engine: AsyncEngine, users: Users, setup: SetupFn
) -> None:
    user_id = await users.make()
    token = await setup(migrated_engine, user_id, _now())

    response = await client.get("/probe", headers={"Cookie": f"{COOKIE}={token}"})

    await _expect_401(response, cookie_sent=True)


async def test_every_401_cause_is_indistinguishable(
    client: AsyncClient, migrated_engine: AsyncEngine, users: Users
) -> None:
    """Same status, body and WWW-Authenticate whatever the reason."""
    now = _now()
    responses = [
        await client.get("/probe"),
        await client.get("/probe", headers={"Cookie": f"{COOKIE}=garbage"}),
    ]
    for setup in (_idle_expired, _over_cap, _deleted, _disabled_user):
        token = await setup(migrated_engine, await users.make(), now)
        responses.append(await client.get("/probe", headers={"Cookie": f"{COOKIE}={token}"}))

    bodies = {r.content for r in responses}
    assert len(bodies) == 1
    assert {r.status_code for r in responses} == {401}
    assert {r.headers.get("www-authenticate") for r in responses} == {WWW_AUTHENTICATE}


# --------------------------------------------------------------------------
# ApiError: 401 and 429 cannot go out without their header
# --------------------------------------------------------------------------


def test_bare_401_raises() -> None:
    with pytest.raises(ValueError, match="WWW-Authenticate"):
        ApiError(401, ErrorCode.UNAUTHENTICATED, "no")
    with pytest.raises(ValueError, match="WWW-Authenticate"):
        ApiError(401, ErrorCode.UNAUTHENTICATED, "no", headers={"Retry-After": "5"})


def test_bare_429_raises() -> None:
    with pytest.raises(ValueError, match="Retry-After"):
        ApiError(429, ErrorCode.RATE_LIMITED, "slow down")
    with pytest.raises(ValueError, match="Retry-After"):
        ApiError(429, ErrorCode.RATE_LIMITED, "slow down", headers={})


def test_401_and_429_with_their_header_construct() -> None:
    assert (
        ApiError(
            401, ErrorCode.UNAUTHENTICATED, "no", headers={"www-authenticate": WWW_AUTHENTICATE}
        ).status_code
        == 401
    )
    assert (
        ApiError(429, ErrorCode.RATE_LIMITED, "slow", headers={"Retry-After": "3"}).status_code
        == 429
    )
    assert ApiError.unauthenticated("no").headers == {"WWW-Authenticate": WWW_AUTHENTICATE}
    assert ApiError.rate_limited("slow", 3).headers == {"Retry-After": "3"}


def test_other_statuses_need_no_header() -> None:
    ApiError(403, ErrorCode.FORBIDDEN, "no")
    ApiError(404, ErrorCode.NOT_FOUND, "no")


# --------------------------------------------------------------------------
# Nothing sensitive in any log record
# --------------------------------------------------------------------------


async def test_no_password_token_or_hash_in_logs(
    caplog: pytest.LogCaptureFixture, database_url: str, migrated_engine: AsyncEngine, users: Users
) -> None:
    """
    Drive hash, verify, dummy verify, session create/resolve (incl. a touch)
    and the gate (success, refresh and 401) with every logger at DEBUG, then
    look for each secret in every record.

    The engine here is built like the app's (``hide_parameters=True``), so
    SQLAlchemy's own DEBUG statement logging is exercised as it would be in
    production rather than switched off.
    """
    caplog.set_level(logging.DEBUG)
    for name in ("sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool", "asyncio", "app", "argon2"):
        caplog.set_level(logging.DEBUG, logger=name)

    engine = create_async_engine(database_url, hide_parameters=True)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        password = "Correct-Horse-" + secrets.token_hex(8)
        presented_variant = "".join(
            chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in password
        )

        phc = await passwords.hash_password(password)
        assert await passwords.verify_password(phc, presented_variant) is True
        assert await passwords.verify_password(phc, password + "x") is False
        await passwords.verify_dummy(password)

        user_id = await users.make()
        async with maker() as db:
            token = await create_session(db, user_id)
            await db.commit()
        async with maker() as db:
            assert await resolve_session(db, token) is not None
            # Past the touch threshold, so the write path is logged too.
            assert await resolve_session(db, token, now=datetime.now(UTC) + timedelta(days=2))

        # Through the prod-like engine: this is test setup, and the fixture
        # engine (no hide_parameters) would log the hash it inserts.
        stale = await insert_session_row(
            engine,
            user_id,
            created_at=_now() - timedelta(days=5),
            last_used_at=_now() - timedelta(days=2),
            absolute_expires_at=_now() + timedelta(days=100),
        )
        async with make_async_client(_probe_app(maker)) as http:
            assert (
                await http.get("/probe", headers={"Cookie": f"{COOKIE}={stale}"})
            ).status_code == 200
            assert (
                await http.get("/probe", headers={"Cookie": f"{COOKIE}={token}"})
            ).status_code == 200
            bad = "Bad" + secrets.token_urlsafe(32)
            assert (
                await http.get("/probe", headers={"Cookie": f"{COOKIE}={bad}"})
            ).status_code == 401
    finally:
        await engine.dispose()

    secrets_to_find: dict[str, str] = {"password": password, "presented variant": presented_variant}
    phc_digest = phc.rsplit("$", 1)[1]
    secrets_to_find |= {"password hash": phc, "password hash digest": phc_digest}
    for label, tok in (("token", token), ("stale token", stale), ("bad token", bad)):
        digest = sha256(tok)
        secrets_to_find |= {
            label: tok,
            f"{label} sha256 hex": digest.hex(),
            f"{label} sha256 raw repr": repr(digest)[2:-1],
            f"{label} sha256 base64": base64.b64encode(digest).decode(),
        }

    assert caplog.records, "nothing was logged at all; the capture is not wired"
    for record in caplog.records:
        parts = [record.getMessage(), repr(record.args), record.exc_text or ""]
        if record.exc_info and record.exc_info[1] is not None:
            parts.append(repr(record.exc_info[1]))
        text = "\n".join(parts)
        for label, secret in secrets_to_find.items():
            # Report the label and logger only; never the secret itself.
            # A plain bool, so pytest's assertion rewriting can't echo the secret.
            leaked = secret in text
            assert not leaked, f"{label} leaked in a record from logger {record.name!r}"
