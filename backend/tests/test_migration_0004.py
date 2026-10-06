"""
Migration 0004 (per-session confirmation counter, decision-log Entry 33).

Written from ``docs/api-contract.md`` "Migration 0004" and "Per-session
confirmation limit": the migration adds ``sessions.failed_confirmations``
(``integer NOT NULL DEFAULT 0``) and the CHECK
``sessions_failed_confirmations_check`` (``>= 0``); existing rows read 0; it is
additive, so nothing already in ``sessions`` changes; the runner records it once.

A throwaway database is migrated through 0003 only, seeded with sessions the
way the pre-0004 app would have written them (plain SQL against the 0003
columns, not ``tables.py``, which already describes 0004), then migrated to
0004. Needs a Postgres role that may ``CREATE DATABASE``; see tests/conftest.py.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import exc, make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.data.migrate import MIGRATIONS_DIR, run_migrations
from tests.test_migration_0003 import MIGRATION_0003, migrations_through

MIGRATION_0004 = "0004_session_confirm_failures"
CHECK_NAME = "sessions_failed_confirmations_check"
SESSION_COLUMNS = "token_hash, user_id, created_at, last_used_at, absolute_expires_at"
_T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


@pytest.fixture
async def pre_0004_database(database_url: str, tmp_path: Path) -> AsyncIterator[str]:
    """A fresh database at 0003 with one user and two sessions; dropped on teardown."""
    name = f"bike_trip_m4_{secrets.token_hex(6)}"
    url = make_url(database_url).set(database=name).render_as_string(hide_password=False)

    admin = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))

        applied = await run_migrations(url, migrations_through(tmp_path / "m", MIGRATION_0003))
        assert applied[-1] == MIGRATION_0003

        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO users (id, username, display_name, password_hash) "
                        "VALUES ('u-0004', 'pre0004', 'Pre 0004', 'argon2id-placeholder')"
                    )
                )
                for n in range(2):
                    await conn.execute(
                        text(
                            f"INSERT INTO sessions ({SESSION_COLUMNS}) "
                            "VALUES (:h, 'u-0004', :c, :u, :a)"
                        ),
                        {
                            "h": hashlib.sha256(f"token-{n}".encode()).digest(),
                            "c": _T0,
                            "u": _T0 + timedelta(hours=n),
                            "a": _T0 + timedelta(days=365),
                        },
                    )
        finally:
            await engine.dispose()

        yield url
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def _sessions(url: str) -> list[tuple[Any, ...]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(f"SELECT {SESSION_COLUMNS} FROM sessions ORDER BY token_hash")
            )
            return [tuple(row) for row in rows]
    finally:
        await engine.dispose()


async def test_existing_sessions_survive_and_read_zero(
    pre_0004_database: str, tmp_path: Path
) -> None:
    before = await _sessions(pre_0004_database)
    assert len(before) == 2

    through_0004 = migrations_through(tmp_path / "through_0004", MIGRATION_0004)
    assert await run_migrations(pre_0004_database, through_0004) == [MIGRATION_0004]

    assert await _sessions(pre_0004_database) == before
    engine = create_async_engine(pre_0004_database)
    try:
        async with engine.connect() as conn:
            counters = (
                await conn.execute(text("SELECT failed_confirmations FROM sessions"))
            ).scalars()
            assert list(counters) == [0, 0]
            column = (
                await conn.execute(
                    text(
                        "SELECT data_type, is_nullable, column_default "
                        "FROM information_schema.columns WHERE table_schema = current_schema() "
                        "AND table_name = 'sessions' AND column_name = 'failed_confirmations'"
                    )
                )
            ).one()
            assert (column.data_type, column.is_nullable) == ("integer", "NO")
            assert column.column_default == "0"
    finally:
        await engine.dispose()


async def test_check_rejects_negative_by_name_and_allows_zero(
    pre_0004_database: str, tmp_path: Path
) -> None:
    assert await run_migrations(
        pre_0004_database, migrations_through(tmp_path / "through_0004", MIGRATION_0004)
    ) == [MIGRATION_0004]

    engine = create_async_engine(pre_0004_database)
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                with pytest.raises(exc.IntegrityError) as caught:
                    async with conn.begin_nested():
                        await conn.execute(text("UPDATE sessions SET failed_confirmations = -1"))
                assert CHECK_NAME in str(caught.value), caught.value

                # 0 is allowed, and so is a value past the threshold: 10 lives in code.
                for allowed in (0, 10):
                    await conn.execute(
                        text("UPDATE sessions SET failed_confirmations = :v"), {"v": allowed}
                    )
                await conn.execute(
                    text(
                        f"INSERT INTO sessions ({SESSION_COLUMNS}) VALUES (:h, 'u-0004', :t, :t, :a)"
                    ),
                    {"h": b"\x01" * 32, "t": _T0, "a": _T0 + timedelta(days=1)},
                )
                inserted = (
                    await conn.execute(
                        text("SELECT failed_confirmations FROM sessions WHERE token_hash = :h"),
                        {"h": b"\x01" * 32},
                    )
                ).scalar_one()
                assert inserted == 0, "an insert that omits the column must default to 0"
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


async def test_second_migrate_applies_nothing(pre_0004_database: str, tmp_path: Path) -> None:
    through_0004 = migrations_through(tmp_path / "through_0004", MIGRATION_0004)
    assert await run_migrations(pre_0004_database, through_0004) == [MIGRATION_0004]
    assert await run_migrations(pre_0004_database, through_0004) == []
    later = [p.stem for p in sorted(MIGRATIONS_DIR.glob("*.sql")) if p.stem > MIGRATION_0004]
    assert await run_migrations(pre_0004_database) == later
    assert await run_migrations(pre_0004_database) == []
