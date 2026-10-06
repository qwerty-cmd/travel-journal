"""
Password primitives: argon2id parameters, NFKC, the dummy verify, and the
concurrency bound (task t-am-identity-core; docs/api-contract.md, "Sessions").

Written from the contract and the task AC, not from the implementation:

- argon2id at the OWASP minimum (m=19456 KiB, t=2, p=1);
- hashed and verified in a worker thread behind ``asyncio.Semaphore(2)``;
- a dummy-hash verify for unknown users;
- NFKC before hashing, and NFKC on *presented* passwords before verifying.

The concurrency test swaps ``app.core.passwords._hasher`` for a slow fake (the
real ``PasswordHasher`` uses ``__slots__``, so its methods can't be patched one
at a time) and measures the peak number of argon2 calls running at once. It is
the only test here that makes the module semaphore *wait*, which binds it to
that test's event loop; a second test that did the same from another loop
would hit asyncio's "bound to a different event loop" error, so keep it single.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
import unicodedata

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core import passwords

# The PHC prefix the contract's parameters produce. v=19 is argon2 1.3.
PHC_PREFIX = "$argon2id$v=19$m=19456,t=2,p=1$"


def _phc_params(phc: str) -> dict[str, str]:
    """The ``m``/``t``/``p`` fields of an argon2 PHC string."""
    match = re.match(r"^\$(argon2(?:id|i|d))\$v=(\d+)\$([^$]+)\$[^$]+\$[^$]+$", phc)
    assert match is not None, "not a PHC string"
    params = dict(item.split("=", 1) for item in match.group(3).split(","))
    return {"type": match.group(1), "v": match.group(2), **params}


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------


async def test_hash_is_argon2id_at_owasp_minimum() -> None:
    phc = await passwords.hash_password("correct horse battery staple")

    assert phc.startswith(PHC_PREFIX)
    assert _phc_params(phc) == {"type": "argon2id", "v": "19", "m": "19456", "t": "2", "p": "1"}


async def test_hash_is_salted() -> None:
    """Two hashes of one password differ: a per-hash random salt."""
    first = await passwords.hash_password("correct horse battery staple")
    second = await passwords.hash_password("correct horse battery staple")

    assert first != second


async def test_hash_verifies_with_an_independent_argon2_verifier() -> None:
    """The stored value is a standard PHC string any argon2 library can check."""
    phc = await passwords.hash_password("correct horse battery staple")

    assert PasswordHasher().verify(phc, "correct horse battery staple") is True


async def test_verify_roundtrip_and_mismatch() -> None:
    phc = await passwords.hash_password("correct horse battery staple")

    assert await passwords.verify_password(phc, "correct horse battery staple") is True
    assert await passwords.verify_password(phc, "correct horse battery stapler") is False
    assert await passwords.verify_password(phc, "") is False


@pytest.mark.parametrize(
    "stored",
    ["", "not-a-hash", "$argon2id$v=19$m=19456,t=2,p=1$garbage", "$2b$12$bcryptlooking"],
)
async def test_verify_against_malformed_hash_is_false_not_an_exception(stored: str) -> None:
    assert await passwords.verify_password(stored, "anything at all here") is False


# --------------------------------------------------------------------------
# NFKC — on the new password before hashing, and on a presented one before verify
# --------------------------------------------------------------------------

COMPOSED = "café au lait à midi"  # é, à precomposed (NFC == NFKC)
DECOMPOSED = "café au lait à midi"  # e + U+0301, a + U+0300
PLAIN_ASCII = "password horse staple"
FULLWIDTH = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else "　" for c in PLAIN_ASCII)
LIGATURE = "ﬁne ﬂow of ﬀort"  # ﬁ, ﬂ, ﬀ
LIGATURE_EXPANDED = "fine flow of ffort"


def test_fixture_strings_are_what_they_claim() -> None:
    """Guard: each variant really differs from, and NFKC-folds onto, its target."""
    for variant, target in [
        (DECOMPOSED, COMPOSED),
        (FULLWIDTH, PLAIN_ASCII),
        (LIGATURE, LIGATURE_EXPANDED),
    ]:
        assert variant != target
        assert unicodedata.normalize("NFKC", variant) == target
        assert unicodedata.normalize("NFKC", target) == target


@pytest.mark.parametrize(
    ("stored_as", "presented"),
    [
        (COMPOSED, DECOMPOSED),
        (DECOMPOSED, COMPOSED),
        (PLAIN_ASCII, FULLWIDTH),
        (FULLWIDTH, PLAIN_ASCII),
        (LIGATURE_EXPANDED, LIGATURE),
        (LIGATURE, LIGATURE_EXPANDED),
    ],
    ids=[
        "composed-set/decomposed-typed",
        "decomposed-set/composed-typed",
        "ascii-set/fullwidth-typed",
        "fullwidth-set/ascii-typed",
        "expanded-set/ligature-typed",
        "ligature-set/expanded-typed",
    ],
)
async def test_presented_password_is_nfkc_normalised_before_verify(
    stored_as: str, presented: str
) -> None:
    phc = await passwords.hash_password(stored_as)

    assert await passwords.verify_password(phc, presented) is True


@pytest.mark.parametrize("raw", [DECOMPOSED, FULLWIDTH, LIGATURE])
async def test_new_password_is_nfkc_normalised_before_hashing(raw: str) -> None:
    """
    What is hashed is the NFKC form, not the raw input — checked with an
    independent verifier that applies no normalisation of its own.
    """
    phc = await passwords.hash_password(raw)
    independent = PasswordHasher()

    assert independent.verify(phc, unicodedata.normalize("NFKC", raw)) is True
    with pytest.raises(VerifyMismatchError):
        independent.verify(phc, raw)


# --------------------------------------------------------------------------
# Dummy verify for unknown users
# --------------------------------------------------------------------------


class _RecordingHasher:
    """Wraps a real hasher and records the stored hash each verify was run against."""

    def __init__(self, real: PasswordHasher) -> None:
        self._real = real
        self.verified_against: list[str] = []

    def hash(self, password: str) -> str:
        return self._real.hash(password)

    def verify(self, phc: str, password: str) -> bool:
        self.verified_against.append(phc)
        return self._real.verify(phc, password)


async def test_dummy_verify_exists_runs_full_argon2id_and_never_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RecordingHasher(passwords._hasher)  # type: ignore[arg-type]
    monkeypatch.setattr(passwords, "_hasher", recorder)

    result = await passwords.verify_dummy("whatever the unknown user typed")

    # It spent one real verification, against a hash with the same cost
    # parameters as a real account's, so its timing matches a wrong password.
    assert len(recorder.verified_against) == 1
    assert _phc_params(recorder.verified_against[0]) == {
        "type": "argon2id",
        "v": "19",
        "m": "19456",
        "t": "2",
        "p": "1",
    }
    # It never signals success.
    assert result is None or result is False


async def test_dummy_verify_accepts_any_input_without_raising() -> None:
    for presented in ["", "x" * 1024, FULLWIDTH, LIGATURE, "\u0000"]:
        await passwords.verify_dummy(presented)


# --------------------------------------------------------------------------
# Concurrency: at most two at once, off the event loop
# --------------------------------------------------------------------------


class _SlowFakeHasher:
    """
    Stands in for ``PasswordHasher``: each call blocks its thread for a while and
    records how many calls were running at the same moment, and on which thread.
    """

    def __init__(self, hold_seconds: float) -> None:
        self._hold = hold_seconds
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0
        self.calls = 0
        self.thread_ids: set[int] = set()

    def _run(self) -> None:
        with self._lock:
            self._active += 1
            self.calls += 1
            self.peak = max(self.peak, self._active)
            self.thread_ids.add(threading.get_ident())
        try:
            time.sleep(self._hold)  # blocking, like argon2's C code
        finally:
            with self._lock:
                self._active -= 1

    def hash(self, password: str) -> str:
        self._run()
        return PHC_PREFIX + "c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNo"

    def verify(self, phc: str, password: str) -> bool:
        self._run()
        return True


def test_semaphore_is_module_level_with_two_permits() -> None:
    assert isinstance(passwords._semaphore, asyncio.Semaphore)
    assert passwords.MAX_CONCURRENT_HASHES == 2
    # Nothing holds it between tests: all permits free.
    assert passwords._semaphore._value == 2


async def test_at_most_two_argon2_runs_at_once_and_none_on_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _SlowFakeHasher(hold_seconds=0.15)
    monkeypatch.setattr(passwords, "_hasher", fake)

    loop_thread = threading.get_ident()
    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        # Advances only if the loop is free while the fake blocks its thread.
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    ticker_task = asyncio.create_task(ticker())
    work = [passwords.hash_password(f"password number {i:02d}") for i in range(4)]
    work += [passwords.verify_password(PHC_PREFIX + "x$y", f"presented {i:02d}") for i in range(3)]
    work.append(passwords.verify_dummy("unknown user attempt"))

    started = time.monotonic()
    await asyncio.gather(*work)
    elapsed = time.monotonic() - started
    stop.set()
    await ticker_task

    assert fake.calls == 8
    # Bounded at two, and actually reaching two (it is a limit, not serialisation).
    assert fake.peak == 2
    # Eight 0.15 s calls, two at a time, can't finish in under ~0.6 s.
    assert elapsed >= 0.55
    # Every call ran in a worker thread, never on the event loop's thread...
    assert loop_thread not in fake.thread_ids
    # ...and the loop kept running other work meanwhile.
    assert ticks >= 20
    # Every permit came back.
    assert passwords._semaphore._value == 2


# --------------------------------------------------------------------------
# Presented credentials: max_length 1024 on the raw input, nothing else
# --------------------------------------------------------------------------

_NEW_PASSWORD = "a perfectly fine new password"


def _presented_bodies(value: str) -> list[tuple[type, dict[str, str], str]]:
    from app.models.account import (
        AccountRecover,
        PasswordChange,
        RecoveryCodeCreate,
        SessionCreate,
    )

    return [
        (SessionCreate, {"username": "rider", "password": value}, "password"),
        (
            PasswordChange,
            {"currentPassword": value, "newPassword": _NEW_PASSWORD},
            "currentPassword",
        ),
        (RecoveryCodeCreate, {"password": value}, "password"),
        (
            AccountRecover,
            {"username": "rider", "recoveryCode": value, "newPassword": _NEW_PASSWORD},
            "recoveryCode",
        ),
    ]


@pytest.mark.parametrize("char", ["x", "ｐ", "ﬁ"], ids=["ascii", "fullwidth", "ligature"])
def test_presented_credentials_accept_1024_characters(char: str) -> None:
    # The ligature case: 1024 raw characters that NFKC expands to 2048 are still
    # accepted, because the cap is on the raw input.
    for model, body, _field in _presented_bodies(char * 1024):
        model.model_validate(body)


def test_presented_credentials_reject_1025_characters() -> None:
    from pydantic import ValidationError

    for model, body, field in _presented_bodies("x" * 1025):
        with pytest.raises(ValidationError) as caught:
            model.model_validate(body)
        errors = caught.value.errors()
        assert [e["loc"] for e in errors] == [(field,)]
        assert errors[0]["type"] == "string_too_long"


@pytest.mark.parametrize("value", ["", " ", "short", "\u0007bell", "  padded  "])
def test_presented_credentials_are_not_format_checked(value: str) -> None:
    """A 422 must not answer "is this plausible?": anything up to the cap passes."""
    for model, body, _field in _presented_bodies(value):
        model.model_validate(body)
