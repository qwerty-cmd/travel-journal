"""
Operator CLI: reset an account's recovery code, or disable the account.

    cd backend && uv run python -m app.data.reset_account --username <name>
    cd backend && uv run python -m app.data.reset_account --username <name> --disable

**Context.** A rider who lost both password and recovery code has no in-app
way back; the owner runs this and hands them the printed code, which they use
on ``POST /api/v2/auth/recover`` (contract, "Sessions" → Recover: "It works for
operator reset codes too"). ``--disable`` shuts an account out instead.
Owner-only in production; agents never run it there.

**How it works.** One transaction:

- reset: write a fresh recovery-code hash (``app/core/recovery_codes.py``),
  delete every session of the user (contract, "Sessions" → Revocation:
  "an operator reset ... delete[s] all of that user's rows"), and clear the
  lockout (``failed_logins`` = 0, ``locked_until`` = NULL, unconditionally —
  an operator reset lifts a lock in force too);
- ``--disable``: set ``disabled_at`` (kept if already set), delete every
  session, and clear the lockout. Clearing it matters: signin answers a lock
  in force with ``429`` before it looks at ``disabled_at``, so a disabled
  account left locked would answer differently from the identical ``401``
  every disabled account must get. No recovery code is issued: a disabled
  account can't use one.

**The code is printed once, after commit** (decision-log Entry 9): a code
printed from a transaction that then rolled back would not work, and the old
one would already be lost from the operator's view. It goes to stdout only —
this module never logs it, and the code never enters an exception message.

**Related.** ``app/data/grant_leader.py``, ``app/data/revoke_member.py``,
``app/data/repositories/users.py``, ``app/data/repositories/sessions.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from contextlib import suppress

from sqlalchemy import func, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.core.recovery_codes import hash_recovery_code, new_recovery_code
from app.data.db import normalize_database_url
from app.data.grant_leader import OperatorError, describe_db_error, find_user_id
from app.data.repositories import sessions as sessions_repo
from app.data.repositories import users as users_repo
from app.data.tables import users


async def reset_account(session: AsyncSession, *, username: str, disable: bool) -> str | None:
    """
    Reset (or, with ``disable``, disable) ``username``. Does not commit.

    Returns the new recovery code for a reset, ``None`` for a disable. Raises
    ``OperatorError`` for an unknown username.
    """
    user_id = await find_user_id(session, username)
    if user_id is None:
        raise OperatorError(f"no account with username {username!r}. Nothing was changed.")

    values: dict[str, object] = {"failed_logins": 0, "locked_until": None}
    code = None
    if disable:
        values["disabled_at"] = func.coalesce(users.c.disabled_at, func.now())
    else:
        code = new_recovery_code()
        await users_repo.set_recovery_code(session, user_id, code_hash=hash_recovery_code(code))
    await session.execute(update(users).where(users.c.id == user_id).values(**values))
    await sessions_repo.delete_all_for_user(session, user_id)
    return code


async def _run(username: str, disable: bool) -> str | None:
    engine = create_async_engine(normalize_database_url(get_settings().database_url))
    try:
        async with AsyncSession(engine) as session, session.begin():
            code = await reset_account(session, username=username, disable=disable)
        # Committed by the block above; only now may the code leave this process.
        return code
    finally:
        with suppress(Exception):
            await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: 0 on success, 1 on a refusal or failure."""
    parser = argparse.ArgumentParser(
        prog="python -m app.data.reset_account",
        description=(
            "Issue a new recovery code for an account (printed once), signing it out "
            "everywhere and clearing its lockout. With --disable, disable the account instead."
        ),
    )
    parser.add_argument("--username", required=True, help="The account's username.")
    parser.add_argument(
        "--disable",
        action="store_true",
        help="Disable the account (no recovery code is issued).",
    )
    args = parser.parse_args(argv)

    try:
        code = asyncio.run(_run(args.username, args.disable))
    except OperatorError as exc:
        print(f"reset_account refused: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(
            f"reset_account failed: {describe_db_error(exc)}. Nothing was changed.", file=sys.stderr
        )
        return 1

    if code is None:
        print(f"{args.username} is disabled, signed out everywhere, and its lockout cleared.")
        return 0
    try:
        sys.stdout.write(
            f"{args.username} is reset: signed out everywhere and its lockout cleared.\n"
            f"New recovery code (shown once, use it at sign-in recovery): {code}\n"
        )
        sys.stdout.flush()
    except OSError:
        with suppress(OSError):
            print(
                "The reset was committed but the recovery code could not be written to stdout. "
                "Run reset_account again to issue another.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
