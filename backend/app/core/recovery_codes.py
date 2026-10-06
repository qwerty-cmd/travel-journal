"""
One-time recovery codes: how a new one is made, and the only form that is stored.

**Context.** Accounts have no email, so a forgotten password is recovered with
a one-time code shown once at signup (decision-log Entry 29 §3 and its
defaults 9; ``docs/api-contract.md``, "Sessions" → Recovery code). The same
code is issued again after a recovery or a rotation, and the operator's
``reset_account`` writes the same column.

**How it works.**

- **128 random bits** from ``secrets``, written as **26 Crockford base32
  characters** (``0-9`` and ``A-Z`` without ``I``, ``L``, ``O`` and ``U``),
  most significant first and zero-padded. 26 characters hold 130 bits, so the
  first character carries only the top 3 bits and is always ``0``-``7``.
- **Stored as SHA-256 hex** (``users.recovery_code_hash``), not argon2: 128
  random bits can't be brute-forced from a fast hash, and the lockout covers
  online guessing.
- **Canonical form.** ``hash_recovery_code`` hashes exactly the string
  ``new_recovery_code`` returns (uppercase, no separators). Mapping what a user
  types (lowercase, hyphens, spaces, ``O``/``I``/``L``) onto that form belongs
  to the recover route and has to happen before hashing.
- **Nothing is logged.** No function here logs or puts a code into an
  exception message. The code leaves the server once, in the response that
  issues it.

**Related.** ``app/data/repositories/users.py`` (the column),
``app/api/routes/v2/auth.py`` (signup issues the first code).
"""

from __future__ import annotations

import hashlib
import secrets

# Crockford's base32 alphabet: digits, then letters without I, L, O and U.
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

RECOVERY_CODE_BITS = 128
RECOVERY_CODE_LENGTH = 26  # ceil(128 / 5)


def new_recovery_code() -> str:
    """A fresh recovery code: 128 random bits as 26 uppercase Crockford base32 characters."""
    value = secrets.randbits(RECOVERY_CODE_BITS)
    chars = []
    for _ in range(RECOVERY_CODE_LENGTH):
        value, digit = divmod(value, 32)
        chars.append(CROCKFORD_ALPHABET[digit])
    return "".join(reversed(chars))


def hash_recovery_code(code: str) -> str:
    """SHA-256 hex of a canonical code: the only form the database stores or is queried by."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()
