"""
``/api/v2/auth`` account routes: signout-all, password, recover, recovery-code (t-am-auth-account).

Written from ``docs/api-contract.md`` ("Sessions", "CSRF", "Rate limits and
lockout", "Error envelope", the v2 endpoint table and "Notes per endpoint (v2)")
and the task AC, before the handlers were read:

- every status in the table for the four routes: signout-all 204/401, recover
  200/401/422/429, password 200/401/403/422/429, recovery-code 200/401/403/422/429.
  The ``writes`` and ``signin`` bucket 429s belong to ``t-am-rate-limits``; the
  lockout 429 is here;
- revocation: signout deletes only its own row; signout-all, a password change
  and a recovery delete all of the user's rows (and no one else's); a password
  change and a recovery then leave exactly one row, the one whose cookie was
  issued; every revoked cookie is a 401;
- recovery: single-use, rotation, the typed form (case, hyphens and spaces,
  O/I/L), no non-ASCII look-alikes, and one identical 401 for unknown user, wrong
  code, used code and disabled account;
- the shared ``failed_logins`` counter and the lock across signin and recover;
  wrong current/rotation passwords never touch it (Entry 33, per-session limit
  in ``test_auth_session_confirm.py``);
- only recover and recovery-code return ``recoveryCode``; nothing sensitive in
  bodies, logs or captured output;
- a session older than 24 h on signout-all and password change: the refresh
  ``Set-Cookie`` survives and comes first;
- CSRF: a cross-site request to each route is a 403 and changes nothing.

Tests whose expectation the contract does not state are marked
**implementation-defined** in their docstring. The races (parallel recover,
parallel password change) live in ``test_auth_account_concurrency.py``.

State is read straight from the tables. No password, code or token is put into
an assertion message.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

import app.main
from app.data import tables
from app.data.db import get_session
from tests import test_auth_endpoints as base
from tests.test_auth_endpoints import (
    BAD_CREDENTIALS,
    CLEARED_COOKIE_RE,
    ISSUED_COOKIE_RE,
    ME,
    ME_KEYS,
    PASSWORD,
    RECOVERY_CODE_RE,
    SIGNOUT,
    WRONG_PASSWORD,
    WWW_AUTHENTICATE,
    Accounts,
    Clock,
    assert_cleared,
    assert_envelope,
    assert_locked,
    build_client,
    cookie_header,
    issued_token,
    new_username,
    send,
    session_rows,
    sha256_bytes,
    signin,
    signup,
    user_row,
)

# The shared fixtures, re-bound so pytest finds them in this module.
accounts = base.accounts
client = base.client
clock = base.clock

SIGNOUT_ALL = "/api/v2/auth/signout-all"
RECOVER = "/api/v2/auth/recover"
PASSWORD_CHANGE = "/api/v2/auth/password"
RECOVERY_CODE = "/api/v2/auth/recovery-code"

NEW_PASSWORD = "a brand new passphrase for the trip"
OTHER_PASSWORD = "yet another passphrase entirely"
CSRF_MESSAGE = "This request came from another site and was blocked."

# A valid Crockford code (26 chars) that contains 0, 1, K and S, so the typed-form
# and look-alike cases have something to substitute.
KNOWN_CODE = "0123456789ABCDEFGHJKMNPQRS"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class Account:
    def __init__(self, username: str, response: httpx.Response) -> None:
        self.username = username
        self.token = issued_token(response)
        self.id: str = response.json()["account"]["id"]
        self.code: str = response.json()["recoveryCode"]
        self.me: dict[str, Any] = response.json()["account"]


async def new_account(client: AsyncClient, accounts: Accounts) -> Account:
    username = accounts.name()
    response = await signup(client, username)
    assert response.status_code == 201
    return Account(username, response)


async def signout_all(client: AsyncClient, token: str | None) -> httpx.Response:
    headers = cookie_header(token) if token is not None else {}
    return await send(client, "POST", SIGNOUT_ALL, headers=headers)


async def recover(
    client: AsyncClient, username: str, code: str, new_password: str = NEW_PASSWORD
) -> httpx.Response:
    return await send(
        client,
        "POST",
        RECOVER,
        json={"username": username, "recoveryCode": code, "newPassword": new_password},
    )


async def change_password(
    client: AsyncClient, token: str | None, current: str, new: str = NEW_PASSWORD
) -> httpx.Response:
    headers = cookie_header(token) if token is not None else {}
    return await send(
        client,
        "POST",
        PASSWORD_CHANGE,
        json={"currentPassword": current, "newPassword": new},
        headers=headers,
    )


async def rotate(client: AsyncClient, token: str | None, password: str) -> httpx.Response:
    headers = cookie_header(token) if token is not None else {}
    return await send(client, "POST", RECOVERY_CODE, json={"password": password}, headers=headers)


async def me_status(client: AsyncClient, token: str) -> int:
    return (await send(client, "GET", ME, headers=cookie_header(token))).status_code


async def assert_revoked(client: AsyncClient, token: str) -> None:
    response = await send(client, "GET", ME, headers=cookie_header(token))
    assert_envelope(response, 401, "UNAUTHENTICATED")
    assert response.headers["www-authenticate"] == WWW_AUTHENTICATE
    assert_cleared(response)


async def token_hashes(engine: AsyncEngine, user_id: str) -> set[bytes]:
    return {s.token_hash for s in await session_rows(engine, user_id)}


async def set_known_code(engine: AsyncEngine, user_id: str, code: str = KNOWN_CODE) -> None:
    """Store ``code`` as the account's recovery code, as ``reset_account`` would."""
    async with engine.begin() as conn:
        await conn.execute(
            update(tables.users)
            .where(tables.users.c.id == user_id)
            .values(recovery_code_hash=hashlib.sha256(code.encode()).hexdigest())
        )


async def insert_stale_session(engine: AsyncEngine, user_id: str) -> str:
    """A session whose ``last_used_at`` is 25 h ago, so the next use refreshes it."""
    token = secrets.token_urlsafe(32)
    stale = datetime.now(UTC) - timedelta(hours=25)
    async with engine.begin() as conn:
        await conn.execute(
            tables.sessions.insert().values(
                token_hash=sha256_bytes(token),
                user_id=user_id,
                created_at=stale,
                last_used_at=stale,
                absolute_expires_at=stale + timedelta(days=365),
            )
        )
    return token


def final_cookie(response: httpx.Response) -> str | None:
    """
    The session cookie a browser holds after applying every ``Set-Cookie`` in order.

    ``None`` means cleared (``Max-Age=0``). Only the contract's two exact forms
    are accepted.
    """
    value: str | None = None
    for header in response.headers.get_list("set-cookie"):
        issued = ISSUED_COOKIE_RE.fullmatch(header)
        if issued:
            value = issued.group(1)
        else:
            assert CLEARED_COOKIE_RE.fullmatch(header), "a Set-Cookie of neither contract form"
            value = None
    return value


# --------------------------------------------------------------------------
# Signout-all
# --------------------------------------------------------------------------


async def test_signout_all_revokes_every_session_of_the_user_only(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    alice = await new_account(client, accounts)
    alice_second = issued_token(await signin(client, alice.username, PASSWORD))
    alice_third = issued_token(await signin(client, alice.username, PASSWORD))
    bob = await new_account(client, accounts)

    response = await signout_all(client, alice_second)

    assert response.status_code == 204
    assert response.content == b""
    assert_cleared(response)
    assert await session_rows(migrated_engine, alice.id) == []
    for token in (alice.token, alice_second, alice_third):
        await assert_revoked(client, token)
    # Another user's session is untouched.
    assert await token_hashes(migrated_engine, bob.id) == {sha256_bytes(bob.token)}
    assert await me_status(client, bob.token) == 200


@pytest.mark.parametrize("cookie", [None, "not-a-real-token"], ids=["none", "unknown"])
async def test_signout_all_without_a_valid_session_is_401(
    client: AsyncClient, cookie: str | None
) -> None:
    response = await signout_all(client, cookie)

    assert_envelope(response, 401, "UNAUTHENTICATED")
    assert response.headers["www-authenticate"] == WWW_AUTHENTICATE
    if cookie is None:
        assert "set-cookie" not in response.headers
    else:
        assert_cleared(response)


async def test_signout_all_twice_is_401_the_second_time(
    client: AsyncClient, accounts: Accounts
) -> None:
    alice = await new_account(client, accounts)
    assert (await signout_all(client, alice.token)).status_code == 204

    response = await signout_all(client, alice.token)

    assert_envelope(response, 401, "UNAUTHENTICATED")
    assert_cleared(response)


async def test_signout_deletes_only_its_own_row_across_users(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    alice = await new_account(client, accounts)
    alice_other = issued_token(await signin(client, alice.username, PASSWORD))
    bob = await new_account(client, accounts)

    assert (
        await send(client, "POST", SIGNOUT, headers=cookie_header(alice.token))
    ).status_code == (204)

    assert await token_hashes(migrated_engine, alice.id) == {sha256_bytes(alice_other)}
    assert await token_hashes(migrated_engine, bob.id) == {sha256_bytes(bob.token)}
    await assert_revoked(client, alice.token)
    assert await me_status(client, alice_other) == 200
    assert await me_status(client, bob.token) == 200


async def test_signout_all_on_a_day_old_session_refreshes_then_clears(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    """Added AC: the ``require_session`` refresh survives, and the clear comes after it."""
    alice = await new_account(client, accounts)
    stale = await insert_stale_session(migrated_engine, alice.id)

    response = await signout_all(client, stale)

    assert response.status_code == 204
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2, f"expected refresh + clear, got {len(cookies)} Set-Cookie headers"
    refresh = ISSUED_COOKIE_RE.fullmatch(cookies[0])
    assert refresh is not None, "the first Set-Cookie is not the refresh"
    assert refresh.group(1) == stale, "the refresh is not for the presented token"
    assert CLEARED_COOKIE_RE.fullmatch(cookies[1]), "the second Set-Cookie is not the clear"
    assert final_cookie(response) is None
    assert await session_rows(migrated_engine, alice.id) == []
    await assert_revoked(client, stale)
    await assert_revoked(client, alice.token)


# --------------------------------------------------------------------------
# Password change
# --------------------------------------------------------------------------


async def test_password_change_revokes_all_then_issues_exactly_one_session(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    alice = await new_account(client, accounts)
    alice_second = issued_token(await signin(client, alice.username, PASSWORD))
    bob = await new_account(client, accounts)

    response = await change_password(client, alice_second, PASSWORD)

    assert response.status_code == 200
    assert response.json() == alice.me  # MeOut
    new_token = issued_token(response)
    assert new_token not in (alice.token, alice_second)
    assert await token_hashes(migrated_engine, alice.id) == {sha256_bytes(new_token)}
    for token in (alice.token, alice_second):
        await assert_revoked(client, token)
    assert await me_status(client, new_token) == 200
    assert await token_hashes(migrated_engine, bob.id) == {sha256_bytes(bob.token)}

    # The new password is set; the old one no longer signs in.
    assert_envelope(
        await signin(client, alice.username, PASSWORD), 401, "UNAUTHENTICATED", BAD_CREDENTIALS
    )
    assert (await signin(client, alice.username, NEW_PASSWORD)).status_code == 200
    # The recovery code is not touched by a password change.
    assert (await user_row(migrated_engine, alice.username)).recovery_code_hash == (
        hashlib.sha256(alice.code.encode()).hexdigest()
    )


@pytest.mark.parametrize("cookie", [None, "not-a-real-token"], ids=["none", "unknown"])
async def test_password_change_without_a_valid_session_is_401(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, cookie: str | None
) -> None:
    alice = await new_account(client, accounts)
    before = await user_row(migrated_engine, alice.username)

    response = await change_password(client, cookie, PASSWORD)

    assert_envelope(response, 401, "UNAUTHENTICATED")
    assert response.headers["www-authenticate"] == WWW_AUTHENTICATE
    if cookie is None:
        assert "set-cookie" not in response.headers
    else:
        assert_cleared(response)
    after = await user_row(migrated_engine, alice.username)
    assert after.password_hash == before.password_hash


async def test_password_change_wrong_current_is_403_and_changes_nothing(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    alice = await new_account(client, accounts)
    before = await user_row(migrated_engine, alice.username)

    response = await change_password(client, alice.token, WRONG_PASSWORD)

    assert_envelope(response, 403, "FORBIDDEN")
    assert "set-cookie" not in response.headers
    after = await user_row(migrated_engine, alice.username)
    assert after.password_hash == before.password_hash
    assert after.failed_logins == 0, "counted per session, not against the account (Entry 33)"
    assert await token_hashes(migrated_engine, alice.id) == {sha256_bytes(alice.token)}
    assert await me_status(client, alice.token) == 200
    assert (await signin(client, alice.username, PASSWORD)).status_code == 200


async def test_password_change_to_the_same_password_is_422(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    alice = await new_account(client, accounts)
    before = await user_row(migrated_engine, alice.username)
    # NFKC-equal to PASSWORD: new passwords are normalised before comparison/hashing.
    fullwidth = "ｃｏｒｒｅｃｔ ｈｏｒｓｅ ｂａｔｔｅｒｙ ｓｔａｐｌｅ"

    for new in (PASSWORD, fullwidth):
        response = await change_password(client, alice.token, PASSWORD, new)
        error = assert_envelope(response, 422, "VALIDATION_ERROR")
        assert PASSWORD not in error["message"]
        assert "set-cookie" not in response.headers

    after = await user_row(migrated_engine, alice.username)
    assert after.password_hash == before.password_hash
    assert after.failed_logins == 0, "an equal-password 422 is not a wrong password"
    assert await token_hashes(migrated_engine, alice.id) == {sha256_bytes(alice.token)}


@pytest.mark.parametrize(
    "body",
    [
        {"currentPassword": PASSWORD},
        {"newPassword": NEW_PASSWORD},
        {"currentPassword": PASSWORD, "newPassword": "too short pw"},
        {"currentPassword": PASSWORD, "newPassword": "control\x07character pw"},
        {"currentPassword": "x" * 1025, "newPassword": NEW_PASSWORD},
    ],
    ids=["new-missing", "current-missing", "new-too-short", "new-control-char", "current-1025"],
)
async def test_password_change_invalid_body_is_422(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, body: dict[str, str]
) -> None:
    alice = await new_account(client, accounts)

    response = await send(
        client, "POST", PASSWORD_CHANGE, json=body, headers=cookie_header(alice.token)
    )

    error = assert_envelope(response, 422, "VALIDATION_ERROR")
    assert "Value error" not in error["message"]
    for value in body.values():
        assert value not in response.text
    assert (await user_row(migrated_engine, alice.username)).failed_logins == 0


async def test_password_change_nfkc_normalises_the_current_password(
    client: AsyncClient, accounts: Accounts
) -> None:
    alice = await new_account(client, accounts)
    fullwidth_current = "ｃｏｒｒｅｃｔ ｈｏｒｓｅ ｂａｔｔｅｒｙ ｓｔａｐｌｅ"

    response = await change_password(client, alice.token, fullwidth_current)

    assert response.status_code == 200


async def test_password_change_on_a_day_old_session_refreshes_then_issues(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    """Added AC: the refresh ``Set-Cookie`` survives and precedes the new session's cookie."""
    alice = await new_account(client, accounts)
    stale = await insert_stale_session(migrated_engine, alice.id)

    response = await change_password(client, stale, PASSWORD)

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2, f"expected refresh + new cookie, got {len(cookies)}"
    first = ISSUED_COOKIE_RE.fullmatch(cookies[0])
    second = ISSUED_COOKIE_RE.fullmatch(cookies[1])
    assert first is not None and second is not None, "a Set-Cookie of neither contract form"
    assert first.group(1) == stale, "the first Set-Cookie is not the refresh"
    new_token = second.group(1)
    assert new_token != stale
    assert final_cookie(response) == new_token
    assert await token_hashes(migrated_engine, alice.id) == {sha256_bytes(new_token)}
    await assert_revoked(client, stale)
    assert await me_status(client, new_token) == 200


# --------------------------------------------------------------------------
# Recover
# --------------------------------------------------------------------------


async def test_recover_sets_password_rotates_code_and_revokes_every_session(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    alice = await new_account(client, accounts)
    alice_second = issued_token(await signin(client, alice.username, PASSWORD))
    bob = await new_account(client, accounts)
    # Some earlier failures: a successful recovery resets the counter.
    for _ in range(3):
        assert (await signin(client, alice.username, WRONG_PASSWORD)).status_code == 401

    response = await recover(client, alice.username, alice.code)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"account", "recoveryCode"}
    assert body["account"] == alice.me
    new_code = body["recoveryCode"]
    assert RECOVERY_CODE_RE.fullmatch(new_code), "new code is not 26 Crockford base32 chars"
    assert new_code != alice.code
    new_token = issued_token(response)

    row = await user_row(migrated_engine, alice.username)
    assert row.recovery_code_hash == hashlib.sha256(new_code.encode()).hexdigest()
    assert (row.failed_logins, row.locked_until) == (0, None)
    assert await token_hashes(migrated_engine, alice.id) == {sha256_bytes(new_token)}
    for token in (alice.token, alice_second):
        await assert_revoked(client, token)
    assert await me_status(client, new_token) == 200
    assert await token_hashes(migrated_engine, bob.id) == {sha256_bytes(bob.token)}

    # The new password is set; the old one no longer signs in.
    assert_envelope(
        await signin(client, alice.username, PASSWORD), 401, "UNAUTHENTICATED", BAD_CREDENTIALS
    )
    assert (await signin(client, alice.username, NEW_PASSWORD)).status_code == 200


async def test_recovery_code_works_once_and_the_new_one_works(
    client: AsyncClient, accounts: Accounts
) -> None:
    alice = await new_account(client, accounts)
    first = await recover(client, alice.username, alice.code, NEW_PASSWORD)
    assert first.status_code == 200
    new_code = first.json()["recoveryCode"]

    again = await recover(client, alice.username, alice.code, OTHER_PASSWORD)
    assert_envelope(again, 401, "UNAUTHENTICATED")
    assert "set-cookie" not in again.headers
    # The failed reuse did not change the password.
    assert (await signin(client, alice.username, NEW_PASSWORD)).status_code == 200

    second = await recover(client, alice.username, new_code, OTHER_PASSWORD)
    assert second.status_code == 200
    issued_token(second)
    assert (await signin(client, alice.username, OTHER_PASSWORD)).status_code == 200


@pytest.mark.parametrize(
    "typed",
    [
        "0123456789abcdefghjkmnpqrs",  # lowercase
        "0123-4567-89AB-CDEF-GHJK-MNPQ-RS",  # hyphens
        "0123 4567 89ab cdef ghjk mnpq rs",  # spaces, lowercase
        "O123456789ABCDEFGHJKMNPQRS",  # O for 0
        "oI23456789ABCDEFGHJKMNPQRS",  # o and I for 0 and 1
        "0L23456789ABCDEFGHJKMNPQRS",  # L for 1
        "0l23-4567 89ab-cdef ghjk-mnpq rs",  # everything at once
    ],
    ids=["lower", "hyphens", "spaces", "O", "o-I", "L", "mixed"],
)
async def test_recover_accepts_the_typed_form(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, typed: str
) -> None:
    alice = await new_account(client, accounts)
    await set_known_code(migrated_engine, alice.id)

    response = await recover(client, alice.username.upper(), typed)

    assert response.status_code == 200
    issued_token(response)


@pytest.mark.parametrize(
    "typed",
    [
        "\u041e123456789ABCDEFGHJKMNPQRS",  # Cyrillic capital O for 0
        "\uff10123456789ABCDEFGHJKMNPQRS",  # fullwidth digit zero
        "0\u0131" + KNOWN_CODE[2:],  # dotless i: "\u0131".upper() == "I"
        "0123456789ABCDEFGHJ\u212aMNPQRS",  # Kelvin sign: "K".lower() == "k"
        "0123456789ABCDEFGHJKMNPQR\u017f",  # long s: "\u017f".upper() == "S"
        "0123456789ABCDEFGHJKMNPQRS\u200b",  # zero-width space
    ],
    ids=["cyrillic-O", "fullwidth-0", "dotless-i", "kelvin-K", "long-s", "zero-width-space"],
)
async def test_recover_rejects_non_ascii_look_alikes(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, typed: str
) -> None:
    alice = await new_account(client, accounts)
    await set_known_code(migrated_engine, alice.id)

    response = await recover(client, alice.username, typed)

    assert_envelope(response, 401, "UNAUTHENTICATED")
    assert "set-cookie" not in response.headers
    assert (await user_row(migrated_engine, alice.username)).failed_logins == 1
    # The real code is still unused.
    assert (await recover(client, alice.username, KNOWN_CODE)).status_code == 200


async def test_recover_failures_are_one_identical_401(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    used = await new_account(client, accounts)
    assert (await recover(client, used.username, used.code)).status_code == 200
    wrong = await new_account(client, accounts)
    disabled = await new_account(client, accounts)
    async with migrated_engine.begin() as conn:
        await conn.execute(
            update(tables.users)
            .where(tables.users.c.id == disabled.id)
            .values(disabled_at=datetime.now(UTC))
        )
    disabled_before = await user_row(migrated_engine, disabled.username)

    responses = {
        "unknown-user": await recover(client, new_username(), wrong.code),
        "wrong-code": await recover(client, wrong.username, KNOWN_CODE),
        "used-code": await recover(client, used.username, used.code),
        "disabled-correct-code": await recover(client, disabled.username, disabled.code),
        "garbage-code": await recover(client, wrong.username, "a"),
    }

    reference = responses["unknown-user"]
    for name, response in responses.items():
        assert_envelope(response, 401, "UNAUTHENTICATED")
        assert response.headers["www-authenticate"] == WWW_AUTHENTICATE, name
        assert "set-cookie" not in response.headers, name
        assert response.content == reference.content, name
        assert sorted(response.headers.multi_items()) == sorted(reference.headers.multi_items()), (
            name
        )

    # A disabled account's password and code are unchanged by the attempt.
    disabled_after = await user_row(migrated_engine, disabled.username)
    assert disabled_after.password_hash == disabled_before.password_hash
    assert disabled_after.recovery_code_hash == disabled_before.recovery_code_hash
    # ...and the attempt issued no session (the only row is the one signup made,
    # which this test inserted the disable under directly).
    assert await token_hashes(migrated_engine, disabled.id) == {sha256_bytes(disabled.token)}


@pytest.mark.parametrize(
    "body",
    [
        {"recoveryCode": KNOWN_CODE, "newPassword": NEW_PASSWORD},
        {"username": "someone", "newPassword": NEW_PASSWORD},
        {"username": "someone", "recoveryCode": KNOWN_CODE},
        {"username": "someone", "recoveryCode": "x" * 1025, "newPassword": NEW_PASSWORD},
        {"username": "someone", "recoveryCode": KNOWN_CODE, "newPassword": "too short pw"},
    ],
    ids=["username-missing", "code-missing", "new-missing", "code-1025", "new-too-short"],
)
async def test_recover_invalid_body_is_422(client: AsyncClient, body: dict[str, str]) -> None:
    response = await send(client, "POST", RECOVER, json=body)

    error = assert_envelope(response, 422, "VALIDATION_ERROR")
    assert "Value error" not in error["message"]
    assert "set-cookie" not in response.headers
    if "newPassword" in body:
        assert body["newPassword"] not in response.text


# --------------------------------------------------------------------------
# Recovery-code rotation
# --------------------------------------------------------------------------


async def test_rotation_issues_a_new_code_and_the_old_one_stops_working(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    alice = await new_account(client, accounts)

    response = await rotate(client, alice.token, PASSWORD)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"account", "recoveryCode"}
    assert body["account"] == alice.me
    new_code = body["recoveryCode"]
    assert RECOVERY_CODE_RE.fullmatch(new_code), "new code is not 26 Crockford base32 chars"
    assert new_code != alice.code
    assert "set-cookie" not in response.headers  # a fresh session is not refreshed
    row = await user_row(migrated_engine, alice.username)
    assert row.recovery_code_hash == hashlib.sha256(new_code.encode()).hexdigest()
    # Rotation is not in the contract's revocation list: the session survives.
    assert await me_status(client, alice.token) == 200

    rotated_away = await recover(client, alice.username, alice.code)
    assert_envelope(rotated_away, 401, "UNAUTHENTICATED")
    assert (await recover(client, alice.username, new_code)).status_code == 200


@pytest.mark.parametrize("cookie", [None, "not-a-real-token"], ids=["none", "unknown"])
async def test_rotation_without_a_valid_session_is_401(
    client: AsyncClient, cookie: str | None
) -> None:
    response = await rotate(client, cookie, PASSWORD)

    assert_envelope(response, 401, "UNAUTHENTICATED")
    assert response.headers["www-authenticate"] == WWW_AUTHENTICATE
    assert "recoveryCode" not in response.text


async def test_rotation_wrong_password_is_403_and_keeps_the_code(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    alice = await new_account(client, accounts)
    before = await user_row(migrated_engine, alice.username)

    response = await rotate(client, alice.token, WRONG_PASSWORD)

    assert_envelope(response, 403, "FORBIDDEN")
    assert "recoveryCode" not in response.text
    after = await user_row(migrated_engine, alice.username)
    assert after.recovery_code_hash == before.recovery_code_hash
    assert after.failed_logins == 0, "counted per session, not against the account (Entry 33)"
    assert (await recover(client, alice.username, alice.code)).status_code == 200


@pytest.mark.parametrize(
    "body", [{}, {"password": "x" * 1025}], ids=["password-missing", "password-1025"]
)
async def test_rotation_invalid_body_is_422(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, body: dict[str, str]
) -> None:
    alice = await new_account(client, accounts)

    response = await send(
        client, "POST", RECOVERY_CODE, json=body, headers=cookie_header(alice.token)
    )

    assert_envelope(response, 422, "VALIDATION_ERROR")
    assert "recoveryCode" not in response.text
    assert (await user_row(migrated_engine, alice.username)).failed_logins == 0


# --------------------------------------------------------------------------
# Lockout
# --------------------------------------------------------------------------


async def test_signin_and_recover_share_one_counter_and_lock(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, clock: Clock
) -> None:
    """
    Contract: failed signin and failed recovery each increment ``users.failed_logins``;
    at 10 the account locks for 15 minutes, and signin and recover then answer 429
    even for correct credentials. Wrong current/rotation passwords in the same flow
    are counted per session instead and leave ``failed_logins`` alone (Entry 33).
    """
    alice = await new_account(client, accounts)
    t0 = clock.now
    expected = 0

    async def check(response: httpx.Response, status: int) -> None:
        nonlocal expected
        assert response.status_code == status
        expected += 1
        row = await user_row(migrated_engine, alice.username)
        if expected < 10:
            assert (row.failed_logins, row.locked_until) == (expected, None)

    async def not_counted(response: httpx.Response) -> None:
        assert response.status_code == 403
        row = await user_row(migrated_engine, alice.username)
        assert (row.failed_logins, row.locked_until) == (expected, None)

    for _ in range(5):
        await check(await signin(client, alice.username, WRONG_PASSWORD), 401)
    for _ in range(2):
        await not_counted(await change_password(client, alice.token, WRONG_PASSWORD))
    for _ in range(4):
        await check(await recover(client, alice.username, KNOWN_CODE), 401)
    for _ in range(2):
        await not_counted(await rotate(client, alice.token, WRONG_PASSWORD))
    await check(await recover(client, alice.username, KNOWN_CODE), 401)

    row = await user_row(migrated_engine, alice.username)
    assert (row.failed_logins, row.locked_until) == (0, t0 + timedelta(minutes=15))
    assert_locked(await signin(client, alice.username, PASSWORD), 900)
    clock.now = t0 + timedelta(minutes=5)
    assert_locked(await recover(client, alice.username, alice.code), 600)
    # The lock did not spend the code.
    assert (await user_row(migrated_engine, alice.username)).recovery_code_hash == (
        hashlib.sha256(alice.code.encode()).hexdigest()
    )


async def test_recover_lockout_and_expiry(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, clock: Clock
) -> None:
    alice = await new_account(client, accounts)
    t0 = clock.now
    for attempt in range(10):
        response = await recover(client, alice.username, KNOWN_CODE)
        assert response.status_code == 401, f"failure #{attempt + 1} was {response.status_code}"
    unlock_at = t0 + timedelta(minutes=15)
    assert (await user_row(migrated_engine, alice.username)).locked_until == unlock_at

    # Locked: even the right code is a 429 and is not consumed, no session issued.
    locked = await recover(client, alice.username, alice.code)
    assert_locked(locked, 900)
    assert "recoveryCode" not in locked.text
    clock.now = unlock_at - timedelta(seconds=1)
    assert_locked(await recover(client, alice.username, alice.code), 1)
    assert await token_hashes(migrated_engine, alice.id) == {sha256_bytes(alice.token)}
    assert (await signin(client, alice.username, NEW_PASSWORD)).status_code == 429

    # At locked_until the lock is over, and the same code still works.
    clock.now = unlock_at
    response = await recover(client, alice.username, alice.code)
    assert response.status_code == 200
    issued_token(response)


async def test_recover_success_resets_the_counter(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, clock: Clock
) -> None:
    alice = await new_account(client, accounts)
    for _ in range(9):
        assert (await recover(client, alice.username, KNOWN_CODE)).status_code == 401
    assert (await user_row(migrated_engine, alice.username)).failed_logins == 9

    assert (await recover(client, alice.username, alice.code)).status_code == 200

    row = await user_row(migrated_engine, alice.username)
    assert (row.failed_logins, row.locked_until) == (0, None)
    # Without the reset, 9 + 1 would lock.
    assert (await signin(client, alice.username, WRONG_PASSWORD)).status_code == 401
    assert (await user_row(migrated_engine, alice.username)).locked_until is None


async def test_ten_wrong_current_passwords_revoke_the_session_not_lock_the_account(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    alice = await new_account(client, accounts)

    for attempt in range(9):
        response = await change_password(client, alice.token, WRONG_PASSWORD)
        assert response.status_code == 403, f"failure #{attempt + 1} was {response.status_code}"
    tenth = await change_password(client, alice.token, WRONG_PASSWORD)
    assert_envelope(tenth, 401, "UNAUTHENTICATED")
    assert_cleared(tenth)

    assert await token_hashes(migrated_engine, alice.id) == set()
    row = await user_row(migrated_engine, alice.username)
    assert (row.failed_logins, row.locked_until) == (0, None)
    assert (await signin(client, alice.username, PASSWORD)).status_code == 200


async def test_ten_wrong_rotation_passwords_revoke_the_session_not_lock_the_account(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    alice = await new_account(client, accounts)

    for attempt in range(9):
        response = await rotate(client, alice.token, WRONG_PASSWORD)
        assert response.status_code == 403, f"failure #{attempt + 1} was {response.status_code}"
    tenth = await rotate(client, alice.token, WRONG_PASSWORD)
    assert_envelope(tenth, 401, "UNAUTHENTICATED")
    assert_cleared(tenth)

    assert await token_hashes(migrated_engine, alice.id) == set()
    row = await user_row(migrated_engine, alice.username)
    assert (row.failed_logins, row.locked_until) == (0, None)
    assert (await recover(client, alice.username, alice.code)).status_code == 200


async def test_locked_account_does_not_refuse_password_change_or_rotation(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, clock: Clock
) -> None:
    """Entry 33: the account lockout never refuses the signed-in routes, nor is touched by them."""
    alice = await new_account(client, accounts)
    for _ in range(10):
        assert (await signin(client, alice.username, WRONG_PASSWORD)).status_code == 401
    before = await user_row(migrated_engine, alice.username)
    assert before.locked_until is not None

    assert_envelope(await rotate(client, alice.token, WRONG_PASSWORD), 403, "FORBIDDEN")
    assert (await rotate(client, alice.token, PASSWORD)).status_code == 200
    assert (await change_password(client, alice.token, PASSWORD)).status_code == 200

    after = await user_row(migrated_engine, alice.username)
    assert (after.failed_logins, after.locked_until) == (before.failed_logins, before.locked_until)


async def test_right_password_resets_the_session_counter(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    alice = await new_account(client, accounts)

    for _ in range(9):
        assert (await rotate(client, alice.token, WRONG_PASSWORD)).status_code == 403
    assert (await rotate(client, alice.token, PASSWORD)).status_code == 200
    for _ in range(9):
        assert (await rotate(client, alice.token, WRONG_PASSWORD)).status_code == 403
    assert await me_status(client, alice.token) == 200


# --------------------------------------------------------------------------
# Nothing sensitive in bodies, logs or output
# --------------------------------------------------------------------------


async def test_only_recover_and_rotation_return_a_recovery_code(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine
) -> None:
    alice = await new_account(client, accounts)
    second = issued_token(await signin(client, alice.username, PASSWORD))
    rotated = await rotate(client, alice.token, PASSWORD)
    rotated_code = rotated.json()["recoveryCode"]
    changed = await change_password(client, alice.token, PASSWORD)
    changed_token = issued_token(changed)
    bad_change = await change_password(client, changed_token, WRONG_PASSWORD)
    row = await user_row(migrated_engine, alice.username)
    out_all = await signout_all(client, changed_token)

    assert set(changed.json()) == ME_KEYS
    assert out_all.content == b""

    secrets_ = {
        "old password": PASSWORD,
        "new password": NEW_PASSWORD,
        "wrong password": WRONG_PASSWORD,
        "signup code": alice.code,
        "rotated code": rotated_code,
        "signup token": alice.token,
        "second token": second,
        "changed token": changed_token,
        "password hash": row.password_hash,
        "recovery code hash": row.recovery_code_hash,
        "changed token hash": sha256_bytes(changed_token).hex(),
    }
    bodies = {"password": changed, "bad password": bad_change, "signout-all": out_all}
    for route, response in bodies.items():
        for label, value in secrets_.items():
            assert value not in response.text, f"{label} appears in the {route} body"
        assert "recoveryCode" not in response.text, f"recoveryCode in the {route} body"
        assert '"password' not in response.text.lower(), f"a password field in the {route} body"
        assert "token" not in response.text.lower(), f"a token field in the {route} body"
        assert "hash" not in response.text.lower(), f"a hash field in the {route} body"

    # The rotation body carries the new code and nothing else secret.
    for label in ("old password", "signup code", "signup token", "password hash"):
        assert secrets_[label] not in rotated.text, f"{label} appears in the rotation body"
    assert set(rotated.json()["account"]) == ME_KEYS


async def test_no_secret_in_logs_or_captured_output(
    client: AsyncClient,
    accounts: Accounts,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    alice = await new_account(client, accounts)

    # Recover: unknown user, wrong code, 422, success.
    assert (await recover(client, new_username(), alice.code)).status_code == 401
    assert (await recover(client, alice.username, KNOWN_CODE)).status_code == 401
    assert (await recover(client, alice.username, alice.code, "short pw")).status_code == 422
    recovered = await recover(client, alice.username, alice.code, NEW_PASSWORD)
    assert recovered.status_code == 200
    recovered_code = recovered.json()["recoveryCode"]
    recovered_token = issued_token(recovered)

    # Password change: wrong current, equal new, success.
    assert (await change_password(client, recovered_token, WRONG_PASSWORD)).status_code == 403
    assert (
        await change_password(client, recovered_token, NEW_PASSWORD, NEW_PASSWORD)
    ).status_code == 422
    changed = await change_password(client, recovered_token, NEW_PASSWORD, OTHER_PASSWORD)
    assert changed.status_code == 200
    changed_token = issued_token(changed)

    # Rotation: wrong, success.
    assert (await rotate(client, changed_token, WRONG_PASSWORD)).status_code == 403
    rotated = await rotate(client, changed_token, OTHER_PASSWORD)
    assert rotated.status_code == 200
    rotated_code = rotated.json()["recoveryCode"]
    assert (await signout_all(client, changed_token)).status_code == 204

    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    logged = "\n".join(formatter.format(record) for record in caplog.records)
    captured = capsys.readouterr()
    haystacks = {"log records": logged, "stdout": captured.out, "stderr": captured.err}
    needles: dict[str, Callable[[], str]] = {
        "signup password": lambda: PASSWORD,
        "recovered password": lambda: NEW_PASSWORD,
        "changed password": lambda: OTHER_PASSWORD,
        "wrong password": lambda: WRONG_PASSWORD,
        "short password": lambda: "short pw",
        "signup code": lambda: alice.code,
        "wrong code": lambda: KNOWN_CODE,
        "recovered code": lambda: recovered_code,
        "rotated code": lambda: rotated_code,
        "signup token": lambda: alice.token,
        "recovered token": lambda: recovered_token,
        "changed token": lambda: changed_token,
    }
    for where, text in haystacks.items():
        for label, value in needles.items():
            assert value() not in text, f"the {label} appears in {where}"


# --------------------------------------------------------------------------
# CSRF and OpenAPI
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [SIGNOUT_ALL, RECOVER, PASSWORD_CHANGE, RECOVERY_CODE])
async def test_cross_site_account_posts_are_blocked_and_change_nothing(
    client: AsyncClient, accounts: Accounts, migrated_engine: AsyncEngine, path: str
) -> None:
    alice = await new_account(client, accounts)
    before = await user_row(migrated_engine, alice.username)
    body = {
        SIGNOUT_ALL: None,
        RECOVER: {
            "username": alice.username,
            "recoveryCode": alice.code,
            "newPassword": NEW_PASSWORD,
        },
        PASSWORD_CHANGE: {"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD},
        RECOVERY_CODE: {"password": PASSWORD},
    }[path]
    try:
        async with build_client(
            migrated_engine,
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        ) as evil:
            response = await evil.post(path, json=body, headers=cookie_header(alice.token))
    finally:
        # Hand `client` its override back (build_client replaced it with an identical one).
        app.main.app.dependency_overrides.pop(get_session, None)

    assert_envelope(response, 403, "FORBIDDEN", CSRF_MESSAGE)
    assert "set-cookie" not in response.headers
    after = await user_row(migrated_engine, alice.username)
    assert after.password_hash == before.password_hash
    assert after.recovery_code_hash == before.recovery_code_hash
    assert after.failed_logins == 0
    async with migrated_engine.connect() as conn:
        live = (
            await conn.execute(
                select(tables.sessions.c.token_hash).where(tables.sessions.c.user_id == alice.id)
            )
        ).all()
    assert [r.token_hash for r in live] == [sha256_bytes(alice.token)]


@pytest.mark.parametrize(
    ("path", "statuses"),
    [
        (SIGNOUT_ALL, {"204", "401", "429"}),
        (RECOVER, {"200", "401", "422", "429"}),
        (PASSWORD_CHANGE, {"200", "401", "403", "422", "429"}),
        (RECOVERY_CODE, {"200", "401", "403", "422", "429"}),
    ],
)
def test_openapi_declares_the_contract_statuses(path: str, statuses: set[str]) -> None:
    operation = app.main.app.openapi()["paths"][path]["post"]
    assert set(operation["responses"]) == statuses
    assert "Retry-After" in operation["responses"]["429"].get("headers", {})
    for status in statuses - {"200", "204"}:
        schema = operation["responses"][status]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("/ErrorEnvelope")
