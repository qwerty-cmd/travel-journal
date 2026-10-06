"""
Password hashing and verification: argon2id, off the event loop, two at a time.

**Context.** Accounts sign in with a username and password (decision-log Entry 29
§3; ``docs/api-contract.md``, "Sessions" → Identity). This module is the only
place a password is hashed or checked. Signup, signin, recovery, password change
and recovery-code rotation all go through it, so the parameters, the
normalisation and the concurrency cap are decided once.

**How it works.**

- **argon2id at the OWASP minimum**: m=19456 KiB (19 MiB), t=2, p=1, through
  ``argon2-cffi``. The hash is a PHC string (``$argon2id$v=19$m=19456,t=2,p=1$...``),
  stored as ``users.password_hash``. The parameters travel inside it, so a later
  change to them does not break verification of hashes made with these.
- **NFKC first, always.** A password is normalised before it is hashed *and*
  before it is verified. The request models already normalise a *new* password,
  but a *presented* one (signin, current password, rotation) arrives raw.
  Without normalising it here, a password set as ``ﬁ`` (U+FB01, stored as
  ``fi``) or typed on a keyboard that emits a decomposed ``é`` could never sign
  in again. NFKC is idempotent, so normalising an already-normalised value
  changes nothing.
- **Off the event loop, at most two at once.** One argon2id run is tens of
  milliseconds of CPU and 19 MiB of memory. It runs in ``asyncio.to_thread`` so
  it doesn't block other requests, behind a module-level ``Semaphore(2)`` so a
  burst of signins can't claim ``N × 19 MiB`` on a free-tier container.
- **A dummy verify for unknown users.** ``verify_dummy`` runs a full argon2id
  verification against a hash of a random secret made at import time with the
  same parameters. Signin calls it when the username doesn't exist, so the
  response takes as long as a wrong password and its timing doesn't reveal
  whether the account exists (contract, "Rate limits and lockout").
- **Nothing is logged.** No function here logs, and none puts a password or a
  hash into an exception message: a verification failure is ``False``, never an
  exception carrying the input.

**Semaphore and event loops.** The semaphore binds to the first event loop that
has to *wait* on it, and it then raises ``RuntimeError`` if another loop waits on
it. Production has one loop per process, so this never fires there. A test that
drives more than two concurrent hashes from a second event loop in the same
process would trip it.

**Related.** ``app/models/account.py`` (the password rules and the 1024-character
cap on presented credentials), ``app/core/sessions.py`` (what a successful
signin issues), ``app/data/repositories/users.py`` (where the hash is stored).
"""

from __future__ import annotations

import asyncio
import secrets
import unicodedata

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# OWASP Password Storage Cheat Sheet, argon2id minimum: m=19 MiB, t=2, p=1.
ARGON2_MEMORY_COST_KIB = 19456
ARGON2_TIME_COST = 2
ARGON2_PARALLELISM = 1

# At most this many argon2 runs at once, process-wide.
MAX_CONCURRENT_HASHES = 2

_hasher = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_COST_KIB,
    parallelism=ARGON2_PARALLELISM,
    type=Type.ID,
)

_semaphore = asyncio.Semaphore(MAX_CONCURRENT_HASHES)

# A hash of a random secret nobody knows, made once with the same parameters as
# every real hash so a dummy verify costs what a real one does. Made at import so
# the first unknown-username signin isn't slower (hash + verify) than the rest.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))


def normalise(password: str) -> str:
    """NFKC-normalise a password. Applied before every hash and every verify."""
    return unicodedata.normalize("NFKC", password)


def _verify_sync(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def hash_password(password: str) -> str:
    """
    NFKC-normalise ``password`` and return its argon2id PHC string.

    Runs in a worker thread behind the module semaphore.
    """
    normalised = normalise(password)
    async with _semaphore:
        return await asyncio.to_thread(_hasher.hash, normalised)


async def verify_password(password_hash: str, password: str) -> bool:
    """
    True if ``password``, NFKC-normalised, matches ``password_hash``.

    A mismatch, or a stored value that isn't a valid argon2 hash, is ``False``.
    It never raises for a wrong password. Runs in a worker thread behind the
    module semaphore.
    """
    normalised = normalise(password)
    async with _semaphore:
        return await asyncio.to_thread(_verify_sync, password_hash, normalised)


async def verify_dummy(password: str) -> None:
    """
    Spend one full argon2id verification on ``password``, and discard the answer.

    For a signin or recovery whose username doesn't exist: the caller answers
    401 either way, but only after the same work a real account would cost.
    """
    await verify_password(_DUMMY_HASH, password)
