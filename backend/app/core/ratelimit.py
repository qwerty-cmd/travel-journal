"""
Rate limits — in-process token buckets (decision-log Entry 29).

**Context.** ``docs/api-contract.md``, "Rate limits and lockout", gives every
``/api`` route except ``/api/health``, the ``/api`` catch-all and ``signout``
one limiter, from a fixed table of buckets. This module holds that table, the
bucket arithmetic, and the FastAPI dependencies the routes declare. It is
separate from the per-account *lockout*, which lives in Postgres
(``app/api/routes/v2/auth.py``) and survives a restart; these buckets do not.

**How it works.**

- **Token bucket.** A bucket of N per window holds up to N tokens and refills
  continuously at N/window per second. A request spends one token. A request
  that finds fewer than one gets ``429 RATE_LIMITED`` with ``Retry-After`` set
  to the whole seconds until one token is back (rounded up, at least 1, by
  ``ApiError.rate_limited``). A refused request spends nothing.
- **Keys.** Each bucket is keyed by the client address (``client_ip``), by the
  signed-in account, or globally (one key for everyone).
- **One dependency per route.** A route declares exactly one ``RateLimit`` in
  its ``dependencies=[...]``; signup's is the one that spends ``signup-ip`` and
  ``signup-global`` together. ``tests/test_ratelimit_audit.py`` holds every
  route to that.
- **Order: the limiter runs before the access gate.** FastAPI solves a route's
  ``dependencies=[...]`` ahead of its parameter dependencies, so the limiter
  answers before the slug, session or membership is looked at. That order leaks
  nothing: whether a request is refused depends only on its key's own recent
  traffic, never on whether a trip, slug or account exists. The opposite order
  would let a caller the gate refuses (``401``/``403``/``404``) flood a route
  for free, because a gate that raises means the limiter never runs.
- **``writes`` for a caller with no session.** ``writes`` is per account, but
  the limiter runs before the gate, so it looks up the session cookie itself
  (one indexed read, no side effects). A cookie that names a stored session is
  charged to that account. No cookie, or one that names nothing, is charged to
  the client address in the same bucket instead (key ``ip:<address>`` beside
  ``user:<id>``), so garbage cookies cannot mint fresh buckets. The gate then
  answers such a request ``401`` until its address runs dry, then ``429``.
- **Memory.** The registry holds at most ``MAX_KEYS`` buckets. When it is full,
  buckets that have refilled completely are dropped first: a full bucket is
  indistinguishable from a missing one, so that loses nothing. If that frees too
  little, the least recently used buckets go, down to a low-water mark so the
  sweep runs once per batch of new keys rather than on every one.
- **Concurrency.** Every read-modify-write happens under one ``threading.Lock``
  with no ``await`` inside it, so two requests can never both spend the last
  token, whether they run on the event loop or in a worker thread.
- **Time is injectable.** ``RateLimitRegistry.clock`` is a zero-argument
  callable returning seconds (default ``time.monotonic``). Tests replace it to
  refill buckets without sleeping, and ``reset()`` empties the registry.

The buckets reset on restart and on scale-to-zero (contract). Entry 29's reopen
trigger: before running more than one replica, move them to Postgres, because
in-process buckets would split per replica.

**Related.** ``app/core/errors.py`` (``ApiError.rate_limited``), ``app/core/
config.py`` (``trusted_proxy_hops``), ``app/core/security.py`` (the gates that
run after this).
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from fastapi import Request

from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.sessions import SESSION_COOKIE_NAME, hash_token
from app.data.db import SessionDep
from app.data.repositories import sessions as sessions_repo
from app.data.repositories import trips as trips_repo
from app.models.trip import CANONICAL_UUID_PATTERN

# The rider-facing message on every limiter 429. Deliberately different from the
# lockout's ("Too many failed sign-in attempts. ..."), so the two can be told apart.
RATE_LIMITED_MESSAGE = "Too many requests. Please wait a moment and try again."

# Floating-point slack when checking for a whole token: a bucket refilled for
# exactly one token's worth of time can come out at 0.9999999999999999.
_EPSILON = 1e-9

MINUTE = 60.0
HOUR = 60 * MINUTE
DAY = 24 * HOUR


class KeyKind(StrEnum):
    """What a bucket is keyed by (the contract table's "Key" column)."""

    IP = "ip"
    USER = "user"
    GLOBAL = "global"


@dataclass(frozen=True, slots=True)
class Bucket:
    """One row of the contract's bucket table: ``capacity`` tokens per ``window`` seconds."""

    name: str
    capacity: int
    window: float
    key: KeyKind

    @property
    def rate(self) -> float:
        """Tokens refilled per second."""
        return self.capacity / self.window


# The contract's table, "Rate limits and lockout". Changing a number here changes
# the contract; change docs/api-contract.md in the same patch.
PUBLIC_READ = Bucket("public-read", 120, MINUTE, KeyKind.IP)
SIGNUP_IP = Bucket("signup-ip", 5, HOUR, KeyKind.IP)
SIGNUP_GLOBAL = Bucket("signup-global", 50, DAY, KeyKind.GLOBAL)
SIGNIN = Bucket("signin", 10, 15 * MINUTE, KeyKind.IP)
TRIP_CREATE = Bucket("trip-create", 3, DAY, KeyKind.USER)
JOIN = Bucket("join", 10, HOUR, KeyKind.USER)
WRITES = Bucket("writes", 600, HOUR, KeyKind.USER)

# Upper bound on buckets held in memory, and where an over-full registry is
# trimmed back to. Roughly 200 bytes a bucket, so the cap is ~10 MB.
MAX_KEYS = 50_000
_LOW_WATER = int(MAX_KEYS * 0.9)


@dataclass(slots=True)
class _State:
    bucket: Bucket
    tokens: float
    updated: float

    def full_at(self, now: float) -> bool:
        """Whether the bucket will have refilled to capacity by ``now``."""
        elapsed = max(0.0, now - self.updated)
        return self.tokens + elapsed * self.bucket.rate >= self.bucket.capacity


class RateLimitRegistry:
    """
    Every live bucket, keyed by ``(bucket name, key)``, in least-recently-used order.

    ``clock`` is read on every call, so assigning a new one takes effect at once.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_KEYS,
        low_water: int = _LOW_WATER,
    ) -> None:
        self.clock = clock
        self.max_keys = max_keys
        self.low_water = low_water
        self._states: OrderedDict[tuple[str, str], _State] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._states)

    def reset(self) -> None:
        """Forget every bucket: every key starts full again."""
        with self._lock:
            self._states.clear()

    def acquire(self, spends: Sequence[tuple[Bucket, str]]) -> float | None:
        """
        Spend one token from each ``(bucket, key)``, all or nothing.

        Returns ``None`` if every bucket had a token (and each was spent), or the
        seconds until the emptiest one has a token again (and nothing was spent).
        All-or-nothing matters for signup: an address refused by the global
        bucket must not also lose one of its own five.
        """
        with self._lock:
            now = self.clock()
            states = [self._refilled(bucket, key, now) for bucket, key in spends]
            waits = [
                (1 - state.tokens) / bucket.rate
                for (bucket, _), state in zip(spends, states, strict=True)
                if state.tokens < 1 - _EPSILON
            ]
            if waits:
                return max(waits)
            for state in states:
                state.tokens = max(0.0, state.tokens - 1)
            return None

    def _refilled(self, bucket: Bucket, key: str, now: float) -> _State:
        """The bucket's state brought up to ``now``, created full if absent. Caller holds the lock."""
        slot = (bucket.name, key)
        state = self._states.get(slot)
        if state is None:
            self._make_room(now)
            state = _State(bucket=bucket, tokens=float(bucket.capacity), updated=now)
            self._states[slot] = state
        else:
            elapsed = max(0.0, now - state.updated)
            state.tokens = min(float(bucket.capacity), state.tokens + elapsed * bucket.rate)
            state.updated = now
            self._states.move_to_end(slot)
        return state

    def _make_room(self, now: float) -> None:
        """Keep the registry under ``max_keys`` before a new bucket is added. Caller holds the lock."""
        if len(self._states) < self.max_keys:
            return
        # Lossless first: a bucket that has refilled to capacity behaves exactly
        # like one that was never created.
        for slot in [slot for slot, state in self._states.items() if state.full_at(now)]:
            del self._states[slot]
        # Then lossy, oldest first, down to the low-water mark. Bounded memory is
        # worth more than the buckets of the least recently seen keys.
        while len(self._states) > self.low_water:
            self._states.popitem(last=False)


# The one registry the app uses. Tests reset it before each test (tests/conftest.py).
registry = RateLimitRegistry()


def client_ip(request: Request) -> str:
    """
    The address a rate limit is keyed by.

    **Why the right-most ``X-Forwarded-For`` hop** (decision-log Entry 29). Every
    proxy *appends* the address it received the connection from, so the header
    reads ``<whatever the client claimed>, ..., <what our ingress saw>``. Only the
    entries our own proxies appended can be trusted. Everything to their left
    was sent by the client and can be anything: keying on the left-most entry
    would let one client mint a fresh bucket per request by varying it. Azure
    Container Apps' ingress appends exactly one hop, hence
    ``trusted_proxy_hops = 1``, and the key is the right-most entry. The socket
    address is no use there: uvicorn runs without ``--forwarded-allow-ips``, so
    it is the ingress proxy's, the same for every client. If the ingress premise
    turns out wrong (more or fewer hops), re-derive this key (Entry 29 reopen
    trigger).

    With ``trusted_proxy_hops = 0`` (nothing trusted in front of the app) the
    header is ignored and the socket address is used. So it is when the header is
    absent, or has fewer entries than the trusted hops, which means the request
    did not come through all of them.
    """
    hops = get_settings().trusted_proxy_hops
    if hops > 0:
        # Several X-Forwarded-For headers are one comma-separated list, in order.
        entries = [
            entry.strip()
            for header in request.headers.getlist("x-forwarded-for")
            for entry in header.split(",")
            if entry.strip()
        ]
        if len(entries) >= hops:
            return entries[-hops]
    return request.client.host if request.client else "unknown"


def _raise_if_refused(spends: Sequence[tuple[Bucket, str]]) -> None:
    wait = registry.acquire(spends)
    if wait is not None:
        raise ApiError.rate_limited(RATE_LIMITED_MESSAGE, wait)


class RateLimit:
    """
    A route's limiter dependency: ``dependencies=[Depends(limit_public_read)]``.

    Base class of every limiter, so the audit can find them by type. This one
    spends from IP-keyed and global buckets only, and needs no database.
    """

    def __init__(self, *buckets: Bucket) -> None:
        if any(bucket.key is KeyKind.USER for bucket in buckets):
            raise ValueError("A user-keyed bucket needs UserRateLimit.")
        self.buckets = buckets

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({', '.join(b.name for b in self.buckets)})"

    def _key(self, bucket: Bucket, request: Request) -> str:
        return "*" if bucket.key is KeyKind.GLOBAL else client_ip(request)

    async def __call__(self, request: Request) -> None:
        _raise_if_refused([(bucket, self._key(bucket, request)) for bucket in self.buckets])


class UserRateLimit(RateLimit):
    """
    A limiter on one user-keyed bucket, run before the access gate.

    Keyed ``user:<id>`` when the session cookie names a stored session, else
    ``ip:<client address>`` (see the module docstring, "``writes`` for a caller
    with no session"). The lookup reads the session row only: no validity check
    and no ``last_used_at`` bump, both of which stay the gate's job. An expired
    session is still charged to its account, which only its holder can do.
    """

    def __init__(self, bucket: Bucket) -> None:
        if bucket.key is not KeyKind.USER:
            raise ValueError("UserRateLimit takes a user-keyed bucket.")
        self.buckets = (bucket,)

    async def __call__(self, request: Request, db: SessionDep) -> None:  # type: ignore[override]
        token = request.cookies.get(SESSION_COOKIE_NAME)
        row = await sessions_repo.get_with_user(db, hash_token(token)) if token else None
        key = f"user:{row.user_id}" if row is not None else f"ip:{client_ip(request)}"
        _raise_if_refused([(self.buckets[0], key)])


class TripCreateRateLimit(UserRateLimit):
    """
    ``trip-create`` for ``POST /api/v2/trips``: a replay spends no token.

    **Why the replay check lives here.** The contract says "A replay spends no
    token", but the limiter runs before the handler (see "Order" above), so by
    the time the handler knows a request is a replay the token would already be
    gone. Charging after the handler instead was rejected: it would let every
    request the handler refuses (``401``, ``409``, ``422``) through for free,
    which is exactly the flood the limiter-first order exists to stop.

    So the limiter makes the same cheap check the handler makes first: when the
    cookie names a stored session and the body's ``id`` is a trip that account
    created **and** is still an active member of
    (``trips.creator_replay_role``, one indexed read), the request is let
    through without spending or checking a token. It is also let through with
    the bucket empty, because a replay changes nothing and is exactly what the
    offline queue sends after a lost ``201``. Anything else, including someone
    else's id and a non-canonical id, spends a token as usual.

    The body is the one FastAPI has already read and cached on the request; a
    body that is not a JSON object with a canonical-UUID ``id`` is simply not
    a replay. The session row is not validated here (that stays the gate's
    job): an expired session replaying its own trip is let through and then
    answered ``401`` by the gate, having spent nothing it could have abused.

    Two parallel sends of one new id both spend a token (neither sees a stored
    trip yet); the handler makes one a create and the other a replay.
    """

    async def __call__(self, request: Request, db: SessionDep) -> None:  # type: ignore[override]
        token = request.cookies.get(SESSION_COOKIE_NAME)
        row = await sessions_repo.get_with_user(db, hash_token(token)) if token else None
        if row is not None and await _is_trip_create_replay(request, db, row.user_id):
            return
        key = f"user:{row.user_id}" if row is not None else f"ip:{client_ip(request)}"
        _raise_if_refused([(self.buckets[0], key)])


async def _is_trip_create_replay(request: Request, db: SessionDep, user_id: str) -> bool:
    """Whether the body's ``id`` is a trip ``user_id`` created and is still an active member of."""
    try:
        body = await request.json()
    except ValueError:  # not JSON (json.JSONDecodeError and UnicodeDecodeError both are)
        return False
    trip_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(trip_id, str) or re.fullmatch(CANONICAL_UUID_PATTERN, trip_id) is None:
        return False
    return await trips_repo.creator_replay_role(db, trip_id, user_id) is not None


# The dependencies routes declare. `join` is defined above and attached by the
# task that builds its routes.
limit_public_read = RateLimit(PUBLIC_READ)
limit_signup = RateLimit(SIGNUP_IP, SIGNUP_GLOBAL)
limit_signin = RateLimit(SIGNIN)
limit_writes = UserRateLimit(WRITES)
limit_trip_create = TripCreateRateLimit(TRIP_CREATE)
