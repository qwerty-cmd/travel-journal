"""
Session tokens, their lifetime, and the ``__Host-btj_session`` cookie.

**Context.** A signed-in device is identified by an opaque random token in an
HttpOnly cookie (decision-log Entry 29 §3; ``docs/api-contract.md``,
"Sessions"). This module makes tokens, decides whether a presented token is
still a session, and formats the cookie. ``require_session`` in
``app/core/security.py`` is the request-facing half. The auth routes use
``create_session`` and ``session_cookie_header`` to sign a device in.

**Why a cookie and not a bearer token (Entry 29 §3 and §14).** The token lives
in an HttpOnly cookie rather than in an ``Authorization: Bearer`` header that
JavaScript attaches:

- JavaScript can't read an HttpOnly cookie, so an XSS bug can't exfiltrate the
  session. A bearer token would have to sit in JS-reachable storage.
- The offline queue persists every pending write in IndexedDB. A bearer token
  would be copied into each queued entry (or kept beside them), so a device's
  queue would become a store of long-lived credentials. With a cookie, queued
  entries carry no credential and a replay uses whatever session exists at send
  time.
- The existing client already sends it (``credentials: 'same-origin'``), so
  clients from before the upgrade authenticate with no code change.

The cost is CSRF exposure, closed by ``SameSite=Lax`` plus the Fetch-Metadata
middleware in ``app/core/csrf.py``. Entry 29's trade-off table (§14) marks
this choice *costly* to reverse; reread these reasons before proposing a bearer
token.

**How it works.**

- **Token.** 32 bytes from ``secrets``, base64url-encoded without padding
  (``secrets.token_urlsafe(32)``, 43 characters).
- **Stored form.** Only ``SHA-256(token)`` is stored, as the 32-byte
  ``sessions.token_hash``. The hash is taken over the cookie value's UTF-8 bytes,
  the exact string the browser sends back, so any presented value (garbage
  included) hashes without a decoding step that could fail. There is no signing
  secret and no environment variable: a database read gives an attacker hashes,
  and a hash is not a cookie.
- **Lifetime.** Valid while ``now < last_used_at + 90 days`` and
  ``now < absolute_expires_at`` (``created_at + 365 days``). ``last_used_at`` is
  written only when it is more than 24 hours old, and each such write re-issues
  the cookie with a fresh ``Max-Age`` (contract default 12). Reads stay cheap on
  most requests, and a daily rider's cookie never runs out before the server's
  90-day window does.
- **Account state.** A session whose account has ``disabled_at`` set resolves
  to nothing, the same as an unknown token.
- **Nothing is logged.** No token, hash or cookie value is logged or put into an
  exception message.

**Related.** ``app/core/security.py`` (``require_session``),
``app/data/repositories/sessions.py`` (rows), ``app/core/csrf.py``.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.data.repositories import sessions as sessions_repo

# `__Host-` makes the browser refuse the cookie unless it is Secure, has Path=/
# and has no Domain, so no subdomain can set or shadow it.
SESSION_COOKIE_NAME = "__Host-btj_session"

TOKEN_BYTES = 32
IDLE_TIMEOUT = timedelta(days=90)
ABSOLUTE_LIFETIME = timedelta(days=365)
TOUCH_INTERVAL = timedelta(hours=24)

# The cookie's Max-Age: the 90-day idle window, in seconds (7776000).
SESSION_COOKIE_MAX_AGE = int(IDLE_TIMEOUT.total_seconds())


@dataclass(frozen=True, slots=True)
class SessionUser:
    """
    The account a valid session belongs to. What ``require_session`` returns.

    ``username`` is private: it goes only to its owner (``MeOut``).
    ``display_name`` is the public name.
    """

    user_id: str
    username: str
    display_name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResolvedSession:
    """A valid session's account, and whether this request re-issues the cookie."""

    user: SessionUser
    # True when this resolution wrote `last_used_at`. The caller then re-issues
    # the cookie with a fresh Max-Age.
    refreshed: bool


def new_token() -> str:
    """A fresh session token: 32 random bytes, base64url-encoded without padding."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> bytes:
    """The 32-byte SHA-256 of a token: the only form the database stores or is queried by."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def create_session(db: AsyncSession, user_id: str, *, now: datetime | None = None) -> str:
    """
    Store a new session for ``user_id`` and return its token. Does not commit.

    The token is returned exactly once, to be put into the cookie with
    ``set_session_cookie``. Only its hash reaches the database. The caller
    commits, so a signup or recovery can issue the session in the same
    transaction as the rest of its work.
    """
    now = now or _utcnow()
    token = new_token()
    await sessions_repo.insert(
        db,
        token_hash=hash_token(token),
        user_id=user_id,
        created_at=now,
        absolute_expires_at=now + ABSOLUTE_LIFETIME,
    )
    return token


async def resolve_session(
    db: AsyncSession, token: str, *, now: datetime | None = None
) -> ResolvedSession | None:
    """
    The account behind ``token``, or ``None`` if it is not a valid session.

    ``None`` covers every failure alike: unknown or garbage token, deleted row,
    idle for 90 days, past the 365-day cap, disabled account. The caller answers
    all of them with the same 401, so none can be told apart.

    When the session is valid and ``last_used_at`` is more than 24 hours old,
    it is bumped to ``now`` (committed) and ``refreshed`` is True.
    """
    now = now or _utcnow()
    token_hash = hash_token(token)
    row = await sessions_repo.get_with_user(db, token_hash)
    if row is None:
        return None
    if row.user_disabled_at is not None:
        return None
    if not (now < row.last_used_at + IDLE_TIMEOUT and now < row.absolute_expires_at):
        return None

    refreshed = now - row.last_used_at > TOUCH_INTERVAL
    if refreshed:
        await sessions_repo.touch(db, token_hash, now)

    return ResolvedSession(
        user=SessionUser(
            user_id=row.user_id,
            username=row.username,
            display_name=row.display_name,
            created_at=row.user_created_at,
        ),
        refreshed=refreshed,
    )


def _cookie_header(value: str, max_age: int) -> str:
    """
    One ``Set-Cookie`` value with the contract's attributes.

    Formatted by Starlette's own ``set_cookie`` on a scratch response, so the
    issuing, refreshing and clearing headers share one formatter and can't drift
    apart: ``HttpOnly; Max-Age=...; Path=/; SameSite=lax; Secure``, no ``Domain``.
    """
    scratch = Response()
    scratch.set_cookie(
        SESSION_COOKIE_NAME,
        value,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return scratch.headers["set-cookie"]


def session_cookie_header(token: str) -> str:
    """The ``Set-Cookie`` value that issues or refreshes a session cookie."""
    return _cookie_header(token, SESSION_COOKIE_MAX_AGE)


def cleared_session_cookie_header() -> str:
    """The ``Set-Cookie`` value that clears the session cookie (empty, ``Max-Age=0``)."""
    return _cookie_header("", 0)


def set_session_cookie(response: Response, token: str) -> None:
    """Add the session cookie to ``response``, alongside any other ``Set-Cookie``."""
    response.headers.append("set-cookie", session_cookie_header(token))
