"""
Account routes — everything under ``/api/v2/auth``.

**Context.** Decision-log Entry 29 replaced "the slug is the credential" with
local accounts: a username, an argon2id password, a one-time recovery code and
a session cookie (``docs/api-contract.md``, "Sessions", and "Notes per endpoint
(v2)"). These routes create an account, sign a device in and out (one device,
or all of them), tell the frontend who is signed in, change the password,
recover a forgotten one with the recovery code, and rotate that code.

**How it works.**

- **Gates.** signup, signin, signout and recover are anonymous: they declare no
  gate. ``me``, signout-all, password change and recovery-code rotation declare
  ``require_session``. ``tests/test_route_dependency_audit.py`` holds each
  route to that by name.
- **Rate limits** (``app/core/ratelimit.py``), declared in each route's
  ``dependencies=`` so they run before the gate: signup spends ``signup-ip``
  and ``signup-global``; signin, recover, password change and recovery-code
  rotation share the per-address ``signin`` bucket; ``me`` is ``public-read``;
  signout-all is ``writes``; signout has none. These are separate from the
  per-account lockout below, which is per account and persisted.
- **Signing a device in** is ``create_session`` inside the route's transaction,
  then ``set_session_cookie`` on FastAPI's injected ``Response`` after the
  commit. The cookie is ``__Host-btj_session=…; HttpOnly; Max-Age=7776000;
  Path=/; SameSite=lax; Secure``.
- **Lockout** (contract, "Rate limits and lockout"). Read from and written to
  ``users.failed_logins`` / ``users.locked_until`` in Postgres, so it survives a
  restart. Every password check is *claimed* first, by one conditional
  ``UPDATE`` that counts it in ``failed_logins`` only while the account is
  unlocked and under 10, and committed before argon2 runs. So at most 10
  checks run per lock window however many requests race, and no transaction
  or row lock is held across argon2. A refused claim is ``429`` with
  ``Retry-After``, even for the correct password. A wrong password keeps its
  claim; the one that brings the count to 10 sets a 15-minute lock (from the
  time it failed) and resets the counter. A right password gives its claim back
  by resetting the counter, but only if no lock is in force: if a concurrent
  failure locked the account meanwhile, the answer is ``429`` and no session
  is issued. A disabled account claims nothing and is never counted.
  Recover, password change and recovery-code rotation go through the same
  claim: a wrong recovery code (``401``) or a wrong current password (``403``)
  keeps its claim, and a locked account is ``429`` on all three, even for the
  right credential.
- **Recovery codes are compared as SHA-256, and used once.** What the user
  typed is mapped to the canonical form first (uppercase, hyphens and spaces
  dropped, ``O``/``I``/``L`` read as ``0``/``1``/``1``), then hashed. The code
  is checked *and* replaced by one conditional ``UPDATE`` on the stored hash
  (``users_repo.consume_recovery_code``), so two recoveries racing with one
  code can't both succeed. The new password is hashed before the account is
  even looked up, so an unknown username costs the same argon2 run as a real
  one and gets the same ``401``.
- **Revocation.** signout-all, a password change and a recovery delete every
  session of the account; the latter two then issue exactly one new session
  to this device, in the same transaction.
- **One answer for every wrong credential.** Unknown username, wrong password
  and disabled account all get the same ``401`` and message. An unknown
  username still costs one argon2id verification (``verify_dummy``), so timing
  doesn't reveal whether the account exists either.
- **Nothing sensitive leaves.** No handler logs. The password, token and
  recovery code are never in an error message; the token leaves only in
  ``Set-Cookie``, the recovery code only in the body of signup, recover and
  rotation.
- **Status-code handlers only.** Every handler returns a model or ``None`` with
  the status set on the decorator, never a ``Response`` of its own, so any
  cookie set on the injected ``Response`` (the sign-in cookie, and the
  ``require_session`` refresh on ``me``) reaches the client.

**Related.** ``app/core/passwords.py``, ``app/core/sessions.py``,
``app/core/recovery_codes.py``, ``app/core/security.py`` (``require_session``),
``app/data/repositories/users.py`` and ``sessions.py``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api.responses import error_responses
from app.core.errors import ApiError
from app.core.passwords import hash_password, normalise, verify_dummy, verify_password
from app.core.ratelimit import limit_public_read, limit_signin, limit_signup, limit_writes
from app.core.recovery_codes import hash_recovery_code, new_recovery_code
from app.core.security import SIGN_IN_REQUIRED_MESSAGE, require_session
from app.core.sessions import (
    IDLE_TIMEOUT,
    SESSION_COOKIE_NAME,
    SessionUser,
    cleared_session_cookie_header,
    create_session,
    hash_token,
    set_session_cookie,
)
from app.data.db import SessionDep
from app.data.repositories import sessions as sessions_repo
from app.data.repositories import users as users_repo
from app.models.account import (
    AccountCreate,
    AccountRecover,
    MeOut,
    PasswordChange,
    RecoveryCodeCreate,
    RecoveryCodeIssuedOut,
    SessionCreate,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Per-account lockout (contract, "Rate limits and lockout").
LOCKOUT_THRESHOLD = 10
LOCKOUT_DURATION = timedelta(minutes=15)

# One message for every wrong signin: unknown username, wrong password, disabled
# account. Identical strings, so the answers can't be told apart.
BAD_CREDENTIALS_MESSAGE = "Username or password is incorrect."

# Says the account is locked for now, and nothing about why beyond that.
LOCKED_MESSAGE = "Too many failed sign-in attempts. Try again later."

# No echo of the name (contract, "Error envelope": the CONFLICT leak boundary).
USERNAME_TAKEN_MESSAGE = "That username is taken."

# One message for every failed recovery: unknown username, wrong or used code,
# disabled account. Identical strings, so the answers can't be told apart.
BAD_RECOVERY_MESSAGE = "Username or recovery code is incorrect."

# A signed-in caller re-confirming their password (password change, rotation).
WRONG_PASSWORD_MESSAGE = "That password is incorrect."

SAME_PASSWORD_MESSAGE = "The new password must be different from the current one."

# What a user may type for a recovery code, mapped onto the canonical form the
# stored hash was made from (contract, "Sessions" → Recovery code).
_RECOVERY_CODE_READS = str.maketrans({"O": "0", "I": "1", "L": "1", "-": None, " ": None})

_SIGNED_IN_COOKIE = (
    "Signs this device in: `Set-Cookie: __Host-btj_session=…; HttpOnly; Max-Age=7776000; "
    "Path=/; SameSite=lax; Secure`. The cookie is HttpOnly, so JavaScript never sees the "
    "session token."
)


def _utcnow() -> datetime:
    """The app clock. One function, so a test can move time forward."""
    return datetime.now(UTC)


def _retry_after(locked_until: datetime, now: datetime) -> int:
    """Whole seconds until ``locked_until``, rounded up, at least 1."""
    return max(1, math.ceil((locked_until - now).total_seconds()))


async def _locked_error(db: SessionDep, user_id: str, now: datetime) -> ApiError:
    """The 429 for a locked account, with ``Retry-After`` read from the lock as stored now."""
    locked_until = await users_repo.get_locked_until(db, user_id)
    await db.commit()
    if locked_until is None or locked_until <= now:
        # Unlocked again between the refusal and this read: the wait is spent.
        return ApiError.rate_limited(LOCKED_MESSAGE, 1)
    return ApiError.rate_limited(LOCKED_MESSAGE, _retry_after(locked_until, now))


def _me(user_id: str, username: str, display_name: str, created_at: datetime) -> MeOut:
    return MeOut(id=user_id, username=username, displayName=display_name, createdAt=created_at)


def _canonical_recovery_code(typed: str) -> str | None:
    """
    ``typed`` in the form ``new_recovery_code`` issues, or ``None`` if it can't be one.

    Case-insensitive, hyphens and spaces ignored, ``O``/``I``/``L`` read as
    ``0``/``1``/``1``. Non-ASCII input can't name a code and isn't uppercased:
    ``str.upper()`` maps some non-ASCII letters onto ASCII ones.
    """
    if not typed.isascii():
        return None
    return typed.upper().translate(_RECOVERY_CODE_READS)


async def _claim_or_refuse(db: SessionDep, user_id: str, now: datetime) -> None:
    """
    Claim one credential check against the lockout, and commit the claim, or raise ``429``.

    The same claim signin makes (see the module docstring): at most
    ``LOCKOUT_THRESHOLD`` checks per lock window, however many requests race.
    """
    claimed = await users_repo.reserve_login_attempt(
        db, user_id, threshold=LOCKOUT_THRESHOLD, now=now
    )
    if not claimed:
        await users_repo.lock_if_saturated(
            db, user_id, threshold=LOCKOUT_THRESHOLD, now=now, lock_until=now + LOCKOUT_DURATION
        )
        await db.commit()
        raise await _locked_error(db, user_id, now)
    await db.commit()


async def _count_failure(db: SessionDep, user_id: str, now: datetime) -> None:
    """A failed check keeps its claim; lock the account if it was the last one. Commits."""
    await users_repo.lock_if_saturated(
        db, user_id, threshold=LOCKOUT_THRESHOLD, now=now, lock_until=now + LOCKOUT_DURATION
    )
    await db.commit()


async def _hand_back_claim(db: SessionDep, user_id: str, now: datetime) -> None:
    """
    A successful check resets the counter, in the caller's transaction, or raises ``429``.

    Refused (and committed) when a concurrent failure locked the account while
    this check ran: the lock stands even for the right credential. The
    ``UPDATE`` also takes the account's row lock, so two concurrent password
    changes or rotations run their writes one after the other.
    """
    if not await users_repo.reset_lockout(db, user_id, now=now):
        await db.commit()
        raise await _locked_error(db, user_id, now)


async def _confirm_own_password(db: SessionDep, user_id: str, presented: str) -> datetime:
    """
    Check a signed-in caller's password against the lockout. Returns the time it was checked.

    ``429`` if locked, ``403`` if wrong (counted). On success the claim is still
    held: the caller hands it back with ``_hand_back_claim`` in the transaction
    that does its work.
    """
    now = _utcnow()
    user = await users_repo.get_credentials_by_id(db, user_id)
    if user is None:
        await db.commit()
        raise ApiError.unauthenticated(SIGN_IN_REQUIRED_MESSAGE)

    await _claim_or_refuse(db, user_id, now)
    password_ok = await verify_password(user.password_hash, presented)
    now = _utcnow()  # after argon2: a lock runs from the failure that set it
    if not password_ok:
        await _count_failure(db, user_id, now)
        raise ApiError.forbidden(WRONG_PASSWORD_MESSAGE)
    return now


@router.post(
    "/signup",
    dependencies=[Depends(limit_signup)],
    status_code=HTTPStatus.CREATED,
    summary="Create an account and sign this device in",
    response_description="The new account and its recovery code, shown this once. "
    + _SIGNED_IN_COOKIE,
    responses=error_responses(
        {
            HTTPStatus.CONFLICT: "That username is taken. The message doesn't repeat the name. "
            "Signup is not idempotent: a retry whose first response was lost gets this too, "
            "and the user then signs in instead.",
            HTTPStatus.UNPROCESSABLE_ENTITY: "The username, display name or password breaks "
            "its rule (see the request body's field descriptions).",
            HTTPStatus.TOO_MANY_REQUESTS: "Too many signups from this address, or overall "
            "(the `signup-ip` and `signup-global` limits). Retry after `Retry-After` seconds.",
        }
    ),
    description="""
**Context.** Accounts are how a rider is known (decision-log Entry 29). There is
no email, so the account's only way back after a forgotten password is the
recovery code this response carries, and this is the **only** time it is shown.

**How it works.** The username is lowercased and must be unique; the password is
NFKC-normalised and stored as an argon2id hash. A random recovery code (26
Crockford base32 characters) is generated and only its SHA-256 is stored. In
one transaction the account and its first session are created; a session
already on the request is deleted in the same transaction. The response is `201`
with the account and the code, and a `Set-Cookie` that signs this device in.
A taken username is `409` and nothing is stored.

**Related APIs.** `POST /api/v2/auth/signin` for later devices,
`GET /api/v2/auth/me` for who is signed in, `POST /api/v2/auth/signout`.
""",
)
async def signup(
    body: AccountCreate, request: Request, response: Response, db: SessionDep
) -> RecoveryCodeIssuedOut:
    """Create the account, issue its recovery code, and sign this device in."""
    # The slow, database-free work first, so no transaction is held open over it.
    password_hash = await hash_password(body.password)
    recovery_code = new_recovery_code()

    previous_token = request.cookies.get(SESSION_COOKIE_NAME)
    if previous_token:
        await sessions_repo.delete(db, hash_token(previous_token))

    try:
        user = await users_repo.create(
            db,
            username=body.username,
            display_name=body.displayName,
            password_hash=password_hash,
            recovery_code_hash=hash_recovery_code(recovery_code),
        )
    except users_repo.UsernameTakenError:
        await db.rollback()
        raise ApiError.conflict(USERNAME_TAKEN_MESSAGE) from None

    token = await create_session(db, user.id)
    await db.commit()

    set_session_cookie(response, token)
    return RecoveryCodeIssuedOut(
        account=_me(user.id, user.username, user.display_name, user.created_at),
        recoveryCode=recovery_code,
    )


@router.post(
    "/signin",
    dependencies=[Depends(limit_signin)],
    summary="Sign this device in with a username and password",
    response_description="The signed-in account. " + _SIGNED_IN_COOKIE,
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: "The username or password is wrong, or the account is "
            "disabled. One message for all of these. A wrong password counts toward the "
            'account lockout. Carries `WWW-Authenticate: Cookie realm="bike-trip-journal"`.',
            HTTPStatus.UNPROCESSABLE_ENTITY: "The body is missing a field, or the password is "
            "over 1024 characters.",
            HTTPStatus.TOO_MANY_REQUESTS: "The account is locked after 10 failed attempts, "
            "for 15 minutes, even for the correct password. Also the `signin` per-address "
            "limit. Retry after `Retry-After` seconds.",
        }
    ),
    description="""
**Context.** How a rider signs in on a new device, or again after a session
expired or was signed out (decision-log Entry 29).

**How it works.** The username is compared case-insensitively and the password
is NFKC-normalised before argon2id verifies it. A wrong username or password is
`401` with "Username or password is incorrect.", identical in both cases; an
unknown username still runs a full argon2id verification so the timing matches.
A disabled account gets the same `401`. Each wrong password increments the
account's failure counter in Postgres; the 10th locks the account for 15
minutes, during which every signin, even with the right password, is `429`
with `Retry-After` set to the seconds left. The lock survives a server restart.
Success resets the counter, deletes this account's expired sessions, and signs
this device in with a `Set-Cookie`.

**Related APIs.** `POST /api/v2/auth/signup`, `POST /api/v2/auth/signout`,
`GET /api/v2/auth/me`.
""",
)
async def signin(body: SessionCreate, response: Response, db: SessionDep) -> MeOut:
    """Check the credentials against the lockout and the password, then sign the device in."""
    now = _utcnow()
    # Stored usernames are ASCII. A non-ASCII input can't name an account, and
    # isn't lowercased: `str.lower()` maps some non-ASCII letters onto ASCII ones.
    username = body.username.lower() if body.username.isascii() else None
    user = await users_repo.get_credentials_by_username(db, username) if username else None

    if user is None:
        await db.commit()  # end the read before argon2: no connection held across it
        await verify_dummy(body.password)
        raise ApiError.unauthenticated(BAD_CREDENTIALS_MESSAGE)

    if user.disabled_at is not None:
        # Can't sign in whatever the password, so nothing is claimed or counted.
        # The lock is still honoured and a verify still runs, so the answers and
        # their timing match an enabled account's.
        await db.commit()
        if user.locked_until is not None and user.locked_until > now:
            raise ApiError.rate_limited(LOCKED_MESSAGE, _retry_after(user.locked_until, now))
        await verify_password(user.password_hash, body.password)
        raise ApiError.unauthenticated(BAD_CREDENTIALS_MESSAGE)

    # Claim this password check before running it, and commit the claim, so at
    # most LOCKOUT_THRESHOLD checks run per lock window however many requests
    # race, and no transaction is held open across argon2.
    claimed = await users_repo.reserve_login_attempt(
        db, user.id, threshold=LOCKOUT_THRESHOLD, now=now
    )
    if not claimed:
        # Locked, or LOCKOUT_THRESHOLD checks are already counted and still in
        # flight. The second case becomes a lock now, so a claim stranded by a
        # crashed request can't leave the account refusing signins forever.
        await users_repo.lock_if_saturated(
            db, user.id, threshold=LOCKOUT_THRESHOLD, now=now, lock_until=now + LOCKOUT_DURATION
        )
        await db.commit()
        raise await _locked_error(db, user.id, now)
    await db.commit()

    password_ok = await verify_password(user.password_hash, body.password)
    # The clock after argon2, not the request's start: a lock runs from the
    # failure that set it.
    now = _utcnow()

    if not password_ok:
        # The claim already counted this failure. Lock if it was the last one.
        await users_repo.lock_if_saturated(
            db, user.id, threshold=LOCKOUT_THRESHOLD, now=now, lock_until=now + LOCKOUT_DURATION
        )
        await db.commit()
        raise ApiError.unauthenticated(BAD_CREDENTIALS_MESSAGE)

    if not await users_repo.reset_lockout(db, user.id, now=now):
        # A concurrent failure locked the account while this check ran. The lock
        # stands, even for the right password, and no session is issued.
        await db.commit()
        raise await _locked_error(db, user.id, now)
    await sessions_repo.delete_expired_for_user(
        db, user.id, idle_cutoff=now - IDLE_TIMEOUT, now=now
    )
    token = await create_session(db, user.id, now=now)
    await db.commit()

    set_session_cookie(response, token)
    return _me(user.id, user.username, user.display_name, user.created_at)


@router.post(
    "/signout",
    status_code=HTTPStatus.NO_CONTENT,
    response_class=Response,
    summary="Sign this device out",
    response_description="Signed out. Always clears the session cookie "
    '(`Set-Cookie: __Host-btj_session=""; Max-Age=0; …`), whether or not a session was '
    "present.",
    description="""
**Context.** Ends the session on this device only (decision-log Entry 29). Other
devices stay signed in.

**How it works.** Anonymous: no session is required, and the answer is `204`
whether or not one was sent. If the request carries a session cookie, its
session row is deleted. The response always clears the cookie. Nothing is
rate-limited here.

**Related APIs.** `POST /api/v2/auth/signin`, `GET /api/v2/auth/me`.
""",
)
async def signout(request: Request, response: Response, db: SessionDep) -> None:
    """Delete this request's session row, if any, and clear the cookie."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        await sessions_repo.delete(db, hash_token(token))
        await db.commit()
    response.headers.append("set-cookie", cleared_session_cookie_header())


@router.get(
    "/me",
    dependencies=[Depends(limit_public_read)],
    summary="The signed-in account",
    response_description="The caller's own account. The only response that ever contains a "
    "username.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: "No valid session: none sent, expired, signed out or "
            "revoked, or the account is disabled. If a session cookie was sent it is cleared "
            '(`Max-Age=0`). Carries `WWW-Authenticate: Cookie realm="bike-trip-journal"`.',
            HTTPStatus.TOO_MANY_REQUESTS: "The `public-read` per-address limit. Retry after "
            "`Retry-After` seconds.",
        }
    ),
    description="""
**Context.** Tells the frontend who is signed in on this device, so it can show
the account and decide whether to offer sign-in (decision-log Entry 29).

**How it works.** Gated by `require_session`. Returns the caller's `id`,
`username`, `displayName` and `createdAt`. At most once a day a valid session's
`last_used_at` is bumped, and this response then re-issues the session cookie
with a fresh `Max-Age`. No session is `401`.

**Related APIs.** `POST /api/v2/auth/signin`, `POST /api/v2/auth/signup`,
`POST /api/v2/auth/signout`.
""",
)
async def get_me(user: Annotated[SessionUser, Depends(require_session)]) -> MeOut:
    """The account behind this request's session."""
    return _me(user.user_id, user.username, user.display_name, user.created_at)


# HEAD sibling: the same handler, schema-excluded (decision-log Entry 11).
router.add_api_route(
    "/me",
    get_me,
    methods=["HEAD"],
    dependencies=[Depends(limit_public_read)],
    include_in_schema=False,
)


@router.post(
    "/signout-all",
    dependencies=[Depends(limit_writes)],
    status_code=HTTPStatus.NO_CONTENT,
    response_class=Response,
    summary="Sign every device out of this account",
    response_description="Signed out everywhere. Every session of the account is deleted, and "
    'this device\'s cookie is cleared (`Set-Cookie: __Host-btj_session=""; Max-Age=0; …`).',
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: "No valid session: none sent, expired, signed out or "
            "revoked, or the account is disabled. If a session cookie was sent it is cleared "
            '(`Max-Age=0`). Carries `WWW-Authenticate: Cookie realm="bike-trip-journal"`.',
            HTTPStatus.TOO_MANY_REQUESTS: "The `writes` per-account limit. Retry after "
            "`Retry-After` seconds.",
        }
    ),
    description="""
**Context.** For a lost or shared device: ends every session of the account at
once, this one included (decision-log Entry 29; contract, "Sessions" →
Revocation).

**How it works.** Gated by `require_session`. Deletes every session row of the
signed-in account and clears this device's cookie. Any other device's cookie
gets `401` on its next request. The account, its password and its recovery code
are unchanged.

**Related APIs.** `POST /api/v2/auth/signout` (this device only),
`POST /api/v2/auth/signin`, `POST /api/v2/auth/password` (also revokes every
session).
""",
)
async def signout_all(
    user: Annotated[SessionUser, Depends(require_session)], response: Response, db: SessionDep
) -> None:
    """Delete every session of the signed-in account and clear this device's cookie."""
    await sessions_repo.delete_all_for_user(db, user.user_id)
    await db.commit()
    response.headers.append("set-cookie", cleared_session_cookie_header())


@router.post(
    "/recover",
    dependencies=[Depends(limit_signin)],
    summary="Set a new password with the recovery code, and sign this device in",
    response_description="The account and its new recovery code, shown this once; the code "
    "just used has stopped working. " + _SIGNED_IN_COOKIE,
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: "The username or recovery code is wrong, the code was "
            "already used, or the account is disabled. One message for all of these. A wrong "
            "code counts toward the account lockout. Carries "
            '`WWW-Authenticate: Cookie realm="bike-trip-journal"`.',
            HTTPStatus.UNPROCESSABLE_ENTITY: "The body is missing a field, the recovery code is "
            "over 1024 characters, or the new password breaks its rule (see the request "
            "body's field descriptions).",
            HTTPStatus.TOO_MANY_REQUESTS: "The account is locked after 10 failed attempts, "
            "for 15 minutes, even for the correct code. Also the `signin` per-address limit. "
            "Retry after `Retry-After` seconds.",
        }
    ),
    description="""
**Context.** Accounts have no email, so a forgotten password is recovered with
the one-time recovery code shown at signup, after the last recovery or rotation,
or handed over by the operator (decision-log Entry 29).

**How it works.** Anonymous. The code is read case-insensitively, with hyphens
and spaces ignored and `O`, `I` and `L` read as `0`, `1` and `1`, and compared
by its SHA-256. A wrong username, a wrong or already-used code and a disabled
account all get the same `401` with "Username or recovery code is incorrect.".
A wrong code counts toward the same lockout as signin: the 10th failure locks
the account for 15 minutes, during which recover is `429` with `Retry-After`,
even for the correct code. On success, in one transaction: the new password is
set, every session of the account is revoked, a new recovery code replaces the
used one, and the lockout is reset. Then this device is signed in with a
`Set-Cookie`. A code works once: of two recoveries racing with the same code,
only one succeeds.

**Related APIs.** `POST /api/v2/auth/recovery-code` (rotate the code while
signed in), `POST /api/v2/auth/signin`, `POST /api/v2/auth/signup`.
""",
)
async def recover(
    body: AccountRecover, response: Response, db: SessionDep
) -> RecoveryCodeIssuedOut:
    """Check the recovery code, and if it is right, reset the password and sign the device in."""
    # The slow, database-free work first, on every path: an unknown username
    # costs the same argon2 run as a real one, and no transaction is held over it.
    new_password_hash = await hash_password(body.newPassword)
    new_code = new_recovery_code()
    typed_code = _canonical_recovery_code(body.recoveryCode)
    code_hash = hash_recovery_code(typed_code) if typed_code is not None else None

    now = _utcnow()
    username = body.username.lower() if body.username.isascii() else None
    user = await users_repo.get_credentials_by_username(db, username) if username else None

    if user is None:
        await db.commit()
        raise ApiError.unauthenticated(BAD_RECOVERY_MESSAGE)

    if user.disabled_at is not None:
        # As signin: nothing claimed or counted, the lock still honoured.
        await db.commit()
        if user.locked_until is not None and user.locked_until > now:
            raise ApiError.rate_limited(LOCKED_MESSAGE, _retry_after(user.locked_until, now))
        raise ApiError.unauthenticated(BAD_RECOVERY_MESSAGE)

    await _claim_or_refuse(db, user.id, now)

    # Check and replace the code in one conditional UPDATE: single use, even
    # under a race. It also resets the lockout (the claim handed back).
    recovered = code_hash is not None and await users_repo.consume_recovery_code(
        db,
        user.id,
        code_hash=code_hash,
        new_password_hash=new_password_hash,
        new_code_hash=hash_recovery_code(new_code),
        now=now,
    )
    if not recovered:
        if code_hash is not None and await users_repo.recovery_code_matches(db, user.id, code_hash):
            # The right code, refused only because a concurrent failure locked
            # the account meanwhile. The lock stands, as for signin.
            await db.commit()
            raise await _locked_error(db, user.id, now)
        await _count_failure(db, user.id, now)
        raise ApiError.unauthenticated(BAD_RECOVERY_MESSAGE)

    await sessions_repo.delete_all_for_user(db, user.id)
    token = await create_session(db, user.id, now=now)
    await db.commit()

    set_session_cookie(response, token)
    return RecoveryCodeIssuedOut(
        account=_me(user.id, user.username, user.display_name, user.created_at),
        recoveryCode=new_code,
    )


@router.post(
    "/password",
    dependencies=[Depends(limit_signin)],
    summary="Change the password, signing every other device out",
    response_description="The signed-in account. Every earlier session is revoked. "
    + _SIGNED_IN_COOKIE,
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: "No valid session: none sent, expired, signed out or "
            "revoked, or the account is disabled. If a session cookie was sent it is cleared "
            '(`Max-Age=0`). Carries `WWW-Authenticate: Cookie realm="bike-trip-journal"`.',
            HTTPStatus.FORBIDDEN: "`currentPassword` is wrong. Counts toward the account lockout.",
            HTTPStatus.UNPROCESSABLE_ENTITY: "The body is missing a field, `currentPassword` is "
            "over 1024 characters, the new password breaks its rule, or it equals the current "
            "one.",
            HTTPStatus.TOO_MANY_REQUESTS: "The account is locked after 10 failed attempts, "
            "for 15 minutes, even for the correct password. Also the `signin` per-address "
            "limit. Retry after `Retry-After` seconds.",
        }
    ),
    description="""
**Context.** Lets a signed-in rider change their password, and cuts off anyone
else who was signed in to the account (decision-log Entry 29; contract,
"Sessions" → Revocation).

**How it works.** Gated by `require_session`. `currentPassword` is
NFKC-normalised and verified; a wrong one is `403`, counted toward the same
lockout as signin, and a locked account is `429` with `Retry-After`. A
`newPassword` equal to the current one is `422`. On success, in one
transaction: the new password is stored as an argon2id hash, every session of
the account is deleted (this one included), and exactly one new session is
issued to this device with a `Set-Cookie`. The recovery code is unchanged.

**Related APIs.** `POST /api/v2/auth/recover` (without the current password),
`POST /api/v2/auth/signout-all`, `GET /api/v2/auth/me`.
""",
)
async def change_password(
    body: PasswordChange,
    user: Annotated[SessionUser, Depends(require_session)],
    response: Response,
    db: SessionDep,
) -> MeOut:
    """Verify the current password, set the new one, and re-issue this device's session only."""
    now = await _confirm_own_password(db, user.user_id, body.currentPassword)

    # `newPassword` is already NFKC-normalised by the model; compare like with like.
    if normalise(body.currentPassword) == body.newPassword:
        await _hand_back_claim(db, user.user_id, now)
        await db.commit()
        raise ApiError.validation(SAME_PASSWORD_MESSAGE)

    new_password_hash = await hash_password(body.newPassword)
    now = _utcnow()
    await _hand_back_claim(db, user.user_id, now)
    await users_repo.set_password(db, user.user_id, password_hash=new_password_hash, now=now)
    await sessions_repo.delete_all_for_user(db, user.user_id)
    token = await create_session(db, user.user_id, now=now)
    await db.commit()

    set_session_cookie(response, token)
    return _me(user.user_id, user.username, user.display_name, user.created_at)


@router.post(
    "/recovery-code",
    dependencies=[Depends(limit_signin)],
    summary="Replace the recovery code with a new one",
    response_description="The account and its new recovery code, shown this once; the old "
    "code has stopped working.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: "No valid session: none sent, expired, signed out or "
            "revoked, or the account is disabled. If a session cookie was sent it is cleared "
            '(`Max-Age=0`). Carries `WWW-Authenticate: Cookie realm="bike-trip-journal"`.',
            HTTPStatus.FORBIDDEN: "`password` is wrong. Counts toward the account lockout.",
            HTTPStatus.UNPROCESSABLE_ENTITY: "The body is missing `password`, or it is over "
            "1024 characters.",
            HTTPStatus.TOO_MANY_REQUESTS: "The account is locked after 10 failed attempts, "
            "for 15 minutes, even for the correct password. Also the `signin` per-address "
            "limit. Retry after `Retry-After` seconds.",
        }
    ),
    description="""
**Context.** The recovery code is shown only once. A rider who lost it, or whose
signup response never arrived, gets a new one here from the Account screen
(decision-log Entry 29).

**How it works.** Gated by `require_session`, and the password is re-confirmed:
a wrong one is `403`, counted toward the same lockout as signin, and a locked
account is `429` with `Retry-After`. On success a new random code (26 Crockford
base32 characters) replaces the old one, which stops working at once; only its
SHA-256 is stored, and this response is the only time it is shown. Sessions
are not touched.

**Related APIs.** `POST /api/v2/auth/recover` (where the code is used),
`POST /api/v2/auth/signup` (the first code), `POST /api/v2/auth/password`.
""",
)
async def rotate_recovery_code(
    body: RecoveryCodeCreate,
    user: Annotated[SessionUser, Depends(require_session)],
    db: SessionDep,
) -> RecoveryCodeIssuedOut:
    """Verify the password, then replace the recovery code and show the new one once."""
    now = await _confirm_own_password(db, user.user_id, body.password)
    new_code = new_recovery_code()
    await _hand_back_claim(db, user.user_id, now)
    await users_repo.set_recovery_code(db, user.user_id, code_hash=hash_recovery_code(new_code))
    await db.commit()

    return RecoveryCodeIssuedOut(
        account=_me(user.user_id, user.username, user.display_name, user.created_at),
        recoveryCode=new_code,
    )
