"""
Accounts: signup, signin, recovery and the caller's own identity (decision-log Entry 29).

Request models validate the contract's field rules at the edge, so a malformed
username or a short password is a 422 before any route logic runs. The rules
live here once; `AccountCreate`, `AccountRecover` and `PasswordChange` share the
password rule rather than each restating it. See docs/api-contract.md,
"Notes per endpoint (v2)" → Signup.

Credentials a caller merely *presents* (signin, current password, recovery code)
are deliberately **not** format-checked: a wrong one is a 401/403 from the route,
and a 422 there would answer "is this a plausible credential?" for free. Their one
constraint is a generous length cap (`PRESENTED_CREDENTIAL_MAX_LENGTH`, 1024).
Presented passwords are NFKC-normalised by `app.core.passwords` before they are
verified, the same as new passwords are before they are hashed.
"""

import re
import unicodedata
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, StringConstraints

# The stored form of a username (after lowercasing). Mirrors migration 0003's
# `users_username_format_check`, so a value that passes here cannot fail the CHECK.
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")

PASSWORD_MIN_LENGTH = 15  # NIST SP 800-63B-4: single-factor passwords need at least 15
PASSWORD_MAX_LENGTH = 128  # the NIST floor for the maximum is 64; 128 leaves headroom

# The one rule on a credential a caller *presents* (signin, current and rotation
# password, recovery code): a bound on how much input reaches argon2 or a hash. It
# is far above any real value (a new password is at most 128 code points), so a
# 422 here says nothing about whether a value is plausible. It applies to the raw
# input, before NFKC, which can shorten a value but never lengthen it past this.
PRESENTED_CREDENTIAL_MAX_LENGTH = 1024


def _has_control_character(value: str) -> bool:
    """True if any code point is Unicode category `Cc` (C0/C1 controls, DEL)."""
    return any(unicodedata.category(char) == "Cc" for char in value)


def _validate_username(value: str) -> str:
    """Lowercase, then require the stored format. Returns the lowercased username."""
    # ASCII first: `str.lower()` maps some non-ASCII letters onto ASCII ones (the
    # Kelvin sign U+212A lowercases to `k`), which would let a look-alike input
    # pass the ASCII-only pattern below. Rejecting it is the conservative reading
    # of "lowercased, then must match".
    if not value.isascii():
        raise ValueError("Username may only use the letters a-z, digits, '_', '.' and '-'.")
    lowered = value.lower()
    if not USERNAME_PATTERN.fullmatch(lowered):
        raise ValueError(
            "Username must be 3-32 characters of a-z, digits, '_', '.' or '-', "
            "starting with a letter or digit."
        )
    return lowered


def _validate_new_password(value: str) -> str:
    """NFKC-normalise, then 15-128 code points with no `Cc`. Returns the normalised form."""
    # Not trimmed: leading and trailing spaces are part of the password.
    normalised = unicodedata.normalize("NFKC", value)
    if not PASSWORD_MIN_LENGTH <= len(normalised) <= PASSWORD_MAX_LENGTH:
        raise ValueError(
            f"Password must be {PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} characters long."
        )
    if _has_control_character(normalised):
        raise ValueError("Password must not contain control characters.")
    return normalised


def _validate_display_name(value: str) -> str:
    """Reject control characters. Trimming and the 1-40 length run first, in the type."""
    if _has_control_character(value):
        raise ValueError("Display name must not contain control characters.")
    return value


Username = Annotated[str, AfterValidator(_validate_username)]
NewPassword = Annotated[str, AfterValidator(_validate_new_password)]
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=40),
    AfterValidator(_validate_display_name),
]

_USERNAME_RULE = (
    "Lowercased, then it must match `^[a-z0-9][a-z0-9_.-]{2,31}$` (3-32 characters), "
    "or the request is rejected with 422 / VALIDATION_ERROR. Only ASCII is accepted."
)
_NEW_PASSWORD_RULE = (
    "Any printable text: NFKC-normalised, then 15-128 characters (code points) with no "
    "control characters (Unicode category Cc). Not trimmed — leading and trailing spaces "
    "count. Anything else is rejected with 422 / VALIDATION_ERROR."
)
_PRESENTED_PASSWORD_RULE = (
    " NFKC-normalised before it is checked, the same as a new password. At most "
    f"{PRESENTED_CREDENTIAL_MAX_LENGTH} characters, or the request is rejected with 422 / "
    "VALIDATION_ERROR."
)
_DISPLAY_NAME_RULE = (
    "Trimmed, then 1-40 characters with no control characters, or the request is "
    "rejected with 422 / VALIDATION_ERROR."
)


class AccountCreate(BaseModel):
    """POST /api/v2/auth/signup body."""

    username: Username = Field(
        description="The private login handle. Unique, stored lowercase, and never shown to "
        "anyone but its owner. " + _USERNAME_RULE
    )
    displayName: DisplayName = Field(
        description="The public name shown on the trips, stops and photos this account "
        "contributes to. " + _DISPLAY_NAME_RULE
    )
    password: NewPassword = Field(description="The account's password. " + _NEW_PASSWORD_RULE)


class SessionCreate(BaseModel):
    """POST /api/v2/auth/signin body."""

    username: str = Field(
        description="The login handle chosen at signup, compared case-insensitively. Not "
        "format-checked: an unknown or malformed username gets the same 401 as a wrong "
        "password."
    )
    password: str = Field(
        max_length=PRESENTED_CREDENTIAL_MAX_LENGTH,
        description="The account's password. Not format-checked: a wrong password is a 401 "
        "with the same message as an unknown username." + _PRESENTED_PASSWORD_RULE,
    )


class AccountRecover(BaseModel):
    """POST /api/v2/auth/recover body."""

    username: str = Field(
        description="The login handle chosen at signup, compared case-insensitively. Not "
        "format-checked: an unknown username is the same 401 as a wrong code."
    )
    recoveryCode: str = Field(
        description="The one-time recovery code shown at signup, after the last recovery or "
        "rotation, or handed over by the operator. Case-insensitive; hyphens and spaces are "
        "ignored, and O, I and L are read as 0, 1 and 1. A wrong code is a 401 and counts "
        f"against the account lockout. At most {PRESENTED_CREDENTIAL_MAX_LENGTH} characters, "
        "or the request is rejected with 422 / VALIDATION_ERROR.",
        max_length=PRESENTED_CREDENTIAL_MAX_LENGTH,
    )
    newPassword: NewPassword = Field(
        description="The password to set on success. " + _NEW_PASSWORD_RULE
    )


class PasswordChange(BaseModel):
    """POST /api/v2/auth/password body."""

    currentPassword: str = Field(
        max_length=PRESENTED_CREDENTIAL_MAX_LENGTH,
        description="The account's current password. Not format-checked: a wrong one is a "
        "403 and counts against the account lockout." + _PRESENTED_PASSWORD_RULE,
    )
    newPassword: NewPassword = Field(
        description="The password to set. Equal to the current one is a 422. " + _NEW_PASSWORD_RULE
    )


class RecoveryCodeCreate(BaseModel):
    """POST /api/v2/auth/recovery-code body — rotates the recovery code."""

    password: str = Field(
        description="The account's current password, re-confirmed before a new recovery code "
        "is issued. Not format-checked: a wrong one is a 403 and counts against the account "
        "lockout." + _PRESENTED_PASSWORD_RULE,
        max_length=PRESENTED_CREDENTIAL_MAX_LENGTH,
    )


class MeOut(BaseModel):
    """The signed-in account. The only response that ever contains a username."""

    id: str = Field(
        description="The account's id, a server-generated UUID4. Private: it is never in a "
        "response a non-member of a trip can receive."
    )
    username: str = Field(
        description="The private login handle, lowercase. Only ever returned to its owner."
    )
    displayName: str = Field(
        description="The public name shown on the trips, stops and photos this account "
        "contributes to."
    )
    createdAt: datetime = Field(
        description="When the account was created, as a timezone-aware ISO 8601 instant."
    )


class RecoveryCodeIssuedOut(BaseModel):
    """Signup, recover and rotation response: the account plus a freshly issued recovery code."""

    account: MeOut = Field(description="The account the recovery code belongs to.")
    recoveryCode: str = Field(
        description="The new one-time recovery code: 26 Crockford base32 characters. This is "
        "the **only** time it is shown — only its hash is stored, and any earlier code has "
        "stopped working. Show it to the user to write down."
    )
