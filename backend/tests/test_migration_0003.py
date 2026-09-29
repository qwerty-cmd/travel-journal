"""
Migration 0003 (accounts and membership, decision-log Entry 29) — what it does
to data that already exists, and the constraints it adds.

Two groups:

1. **Obligation 13: existing data survives unchanged.** A throwaway database is
   migrated to 0002 only, seeded the way the pre-0003 app would have left it
   (two trips with slugs, stops, photos, bikes), and then migrated to 0003.
   Every existing trip must come out private with a 24h delay, every slug
   byte-identical, every row count and every pre-existing value unchanged, and
   no membership or join request invented. The same database then proves a
   second migrate run applies nothing.
2. **The new constraints behave as specified**, on the shared migrated
   database, inside transactions that are always rolled back. Each rejection
   is asserted by constraint *name*, so an insert failing for some other reason
   (a missing FK parent, say) cannot pass for the rule under test, and each is
   paired with the insert the rule must still allow — ``CHECK (false)`` would
   satisfy every rejection test on its own.

These need a real Postgres whose role may ``CREATE DATABASE`` (the
docker-compose ``postgres`` service is one); see tests/conftest.py.
"""

from __future__ import annotations

import secrets
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import exc, make_url, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.data.migrate import MIGRATIONS_DIR, run_migrations

MIGRATION_0003 = "0003_accounts_membership"
PRE_0003_FILES = ("0001_initial_schema.sql", "0002_trips_slugs_differ_check.sql")

# Tables that exist before 0003, and the columns they had then. The snapshot
# compares exactly these, so a migration that rewrote an existing value (not
# just added a column) fails.
LEGACY_COLUMNS = {
    "trips": "id, name, rider_slug, viewer_slug, start_date",
    "stops": "id, trip_id, name, lat, lng, location_source, arrived_at, notes",
    "photos": "id, stop_id, object_key, one_drive_file_id, uploaded_by, taken_at",
    "bikes": "id, trip_id, rider_name, make, model, year, specs",
}
NEW_TABLES = ("users", "sessions", "trip_members", "join_requests")

_ARRIVED_AT = datetime(2026, 5, 1, 9, 30, tzinfo=UTC)


# --------------------------------------------------------------------------
# 1. Obligation 13 — a database seeded before 0003, then migrated
# --------------------------------------------------------------------------


@pytest.fixture
async def pre_0003_database(database_url: str, tmp_path: Path) -> AsyncIterator[str]:
    """
    A fresh database at migration 0002, seeded with pre-0003 data; dropped on teardown.

    0001–0002 are applied by the real runner from a directory holding only
    those two files, so the ledger it leaves is exactly what a pre-0003
    deployment has. Seeded with plain SQL against the pre-0003 columns rather
    than ``tables.py``, which already describes the post-0003 schema.
    """
    name = f"bike_trip_m3_{secrets.token_hex(6)}"
    url = make_url(database_url).set(database=name).render_as_string(hide_password=False)

    admin = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))

        pre_dir = tmp_path / "migrations"
        pre_dir.mkdir()
        for filename in PRE_0003_FILES:
            shutil.copy(MIGRATIONS_DIR / filename, pre_dir / filename)
        assert await run_migrations(url, pre_dir) == [Path(f).stem for f in PRE_0003_FILES]

        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await _seed_pre_0003(conn)
        finally:
            await engine.dispose()

        yield url
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def _seed_pre_0003(conn: AsyncConnection) -> None:
    """Two trips with slugs, each with stops, photos and bikes."""
    for t in range(2):
        trip_id = f"trip-{t}"
        await conn.execute(
            text(
                "INSERT INTO trips (id, name, rider_slug, viewer_slug, start_date) "
                "VALUES (:id, :name, :rider, :viewer, :start)"
            ),
            {
                "id": trip_id,
                "name": f"Pre-0003 trip {t}",
                # token_urlsafe, the way real slugs are issued: '-' and '_'
                # included, so a normalisation of either would show.
                "rider": secrets.token_urlsafe(32),
                "viewer": secrets.token_urlsafe(32),
                "start": date(2026, 4, 1 + t),
            },
        )
        for s in range(2):
            stop_id = f"stop-{t}-{s}"
            await conn.execute(
                text(
                    "INSERT INTO stops "
                    "(id, trip_id, name, lat, lng, location_source, arrived_at, notes) "
                    "VALUES (:id, :trip_id, :name, :lat, :lng, :source, :arrived_at, :notes)"
                ),
                {
                    "id": stop_id,
                    "trip_id": trip_id,
                    "name": f"Stop {t}.{s}",
                    "lat": -33.8 + t + s / 10,
                    "lng": 151.2 - t - s / 10,
                    "source": "gps" if s == 0 else "manual",
                    "arrived_at": _ARRIVED_AT.replace(day=1 + s, hour=8 + t),
                    "notes": None if s == 0 else "Fuel and coffee",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO photos "
                    "(id, stop_id, object_key, one_drive_file_id, uploaded_by, taken_at) "
                    "VALUES (:id, :stop_id, :key, :od, :by, :taken_at)"
                ),
                {
                    "id": f"photo-{t}-{s}",
                    "stop_id": stop_id,
                    "key": f"trips/{trip_id}/photo-{t}-{s}.jpg",
                    "od": None if s == 0 else f"graph-{t}-{s}",
                    "by": "Free-text rider name",
                    "taken_at": _ARRIVED_AT,
                },
            )
        await conn.execute(
            text(
                "INSERT INTO bikes (id, trip_id, rider_name, make, model, year) "
                "VALUES (:id, :trip_id, :rider, :make, :model, :year)"
            ),
            {
                "id": f"bike-{t}",
                "trip_id": trip_id,
                "rider": f"Rider {t}",
                "make": "Honda",
                "model": "Africa Twin",
                "year": 2020 + t,
            },
        )


async def _public_tables(conn: AsyncConnection) -> set[str]:
    rows = await conn.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        )
    )
    return {row[0] for row in rows}


async def _counts(conn: AsyncConnection, table_names: set[str]) -> dict[str, int]:
    return {
        name: (await conn.execute(text(f'SELECT count(*) FROM "{name}"'))).scalar_one()
        for name in sorted(table_names)
    }


async def _legacy_snapshot(conn: AsyncConnection) -> dict[str, list[tuple[Any, ...]]]:
    return {
        name: [
            tuple(row)
            for row in await conn.execute(text(f"SELECT {columns} FROM {name} ORDER BY id"))
        ]
        for name, columns in LEGACY_COLUMNS.items()
    }


async def _slug_bytes(conn: AsyncConnection) -> dict[str, tuple[bytes, bytes]]:
    """Slugs as the bytes Postgres stores, not as decoded strings."""
    rows = await conn.execute(
        text(
            "SELECT id, convert_to(rider_slug, 'UTF8'), convert_to(viewer_slug, 'UTF8') FROM trips"
        )
    )
    return {row[0]: (bytes(row[1]), bytes(row[2])) for row in rows}


async def test_obligation_13_existing_data_survives_0003(pre_0003_database: str) -> None:
    engine = create_async_engine(pre_0003_database)
    try:
        async with engine.connect() as conn:
            tables_before = await _public_tables(conn)
            counts_before = await _counts(conn, tables_before)
            legacy_before = await _legacy_snapshot(conn)
            slugs_before = await _slug_bytes(conn)

        # The seed is what it claims to be, so "unchanged" below is not vacuous.
        assert counts_before["trips"] == 2
        assert counts_before["stops"] == counts_before["photos"] == 4
        assert counts_before["bikes"] == 2
        assert not tables_before & set(NEW_TABLES)

        assert await run_migrations(pre_0003_database) == [MIGRATION_0003]

        async with engine.connect() as conn:
            trips = (
                await conn.execute(
                    text(
                        "SELECT id, visibility, public_delay_hours, created_by, created_at "
                        "FROM trips ORDER BY id"
                    )
                )
            ).all()
            assert [tuple(row[1:]) for row in trips] == [("private", 24, None, None)] * 2

            assert await _slug_bytes(conn) == slugs_before
            assert await _legacy_snapshot(conn) == legacy_before

            counts_after = await _counts(conn, await _public_tables(conn))
            for name in tables_before - {"schema_migrations"}:
                assert counts_after[name] == counts_before[name], f"{name} row count changed"
            assert counts_after["schema_migrations"] == counts_before["schema_migrations"] + 1
            for name in NEW_TABLES:
                assert counts_after[name] == 0, f"0003 created rows in {name}"

            for name in ("stops", "photos", "bikes"):
                non_null = (
                    await conn.execute(
                        text(f"SELECT count(*) FROM {name} WHERE created_by IS NOT NULL")
                    )
                ).scalar_one()
                assert non_null == 0, f"{name}.created_by backfilled on pre-0003 rows"
    finally:
        await engine.dispose()


async def test_second_migrate_applies_nothing(pre_0003_database: str) -> None:
    assert await run_migrations(pre_0003_database) == [MIGRATION_0003]
    assert await run_migrations(pre_0003_database) == []


# --------------------------------------------------------------------------
# 2. The new constraints
# --------------------------------------------------------------------------


@pytest.fixture
async def conn(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A connection inside a transaction that is always rolled back."""
    async with migrated_engine.connect() as connection:
        trans = await connection.begin()
        try:
            yield connection
        finally:
            await trans.rollback()


async def _insert_user(conn: AsyncConnection, user_id: str) -> None:
    await conn.execute(
        text(
            "INSERT INTO users (id, username, display_name, password_hash) "
            "VALUES (:id, :username, :display, 'argon2id-placeholder')"
        ),
        {"id": user_id, "username": f"u{secrets.token_hex(8)}", "display": "Test user"},
    )


async def _insert_trip(
    conn: AsyncConnection, trip_id: str, rider: str | None = None, viewer: str | None = None
) -> None:
    await conn.execute(
        text(
            "INSERT INTO trips (id, name, rider_slug, viewer_slug, start_date) "
            "VALUES (:id, 'Constraint fixture', :rider, :viewer, :start)"
        ),
        {"id": trip_id, "rider": rider, "viewer": viewer, "start": date(2026, 1, 1)},
    )


async def _expect_violation(
    conn: AsyncConnection, constraint: str, sql: str, params: dict[str, Any]
) -> None:
    """The statement fails, on ``constraint`` specifically; the outer transaction survives."""
    with pytest.raises(exc.IntegrityError) as caught:
        async with conn.begin_nested():
            await conn.execute(text(sql), params)
    assert constraint in str(caught.value), caught.value


@pytest.fixture
async def trip_and_user(conn: AsyncConnection) -> tuple[str, str]:
    trip_id = f"test-trip-{secrets.token_hex(6)}"
    user_id = f"test-user-{secrets.token_hex(6)}"
    await _insert_trip(conn, trip_id)
    await _insert_user(conn, user_id)
    return trip_id, user_id


_INSERT_JOIN_REQUEST = (
    "INSERT INTO join_requests (id, trip_id, user_id, state, decided_at) "
    "VALUES (:id, :trip_id, :user_id, :state, :decided_at)"
)


async def test_one_pending_join_request_per_trip_and_user(
    conn: AsyncConnection, trip_and_user: tuple[str, str]
) -> None:
    trip_id, user_id = trip_and_user
    base = {"trip_id": trip_id, "user_id": user_id}

    await conn.execute(
        text(_INSERT_JOIN_REQUEST), {**base, "id": "jr-1", "state": "pending", "decided_at": None}
    )
    await _expect_violation(
        conn,
        "ux_join_requests_one_pending",
        _INSERT_JOIN_REQUEST,
        {**base, "id": "jr-2", "state": "pending", "decided_at": None},
    )

    # Non-pending rows for the same (trip, user) are history, and there may be
    # any number of them alongside the one pending row.
    decided = datetime.now(UTC)
    await conn.execute(
        text(_INSERT_JOIN_REQUEST),
        {**base, "id": "jr-3", "state": "rejected", "decided_at": decided},
    )
    await conn.execute(
        text(_INSERT_JOIN_REQUEST),
        {**base, "id": "jr-4", "state": "cancelled", "decided_at": decided},
    )
    count = (
        await conn.execute(
            text("SELECT count(*) FROM join_requests WHERE trip_id = :t AND user_id = :u"),
            {"t": trip_id, "u": user_id},
        )
    ).scalar_one()
    assert count == 3


_INSERT_MEMBER = (
    "INSERT INTO trip_members (id, trip_id, user_id, role, revoked_at) "
    "VALUES (:id, :trip_id, :user_id, 'rider', :revoked_at)"
)


async def test_one_active_membership_per_trip_and_user(
    conn: AsyncConnection, trip_and_user: tuple[str, str]
) -> None:
    trip_id, user_id = trip_and_user
    base = {"trip_id": trip_id, "user_id": user_id}

    await conn.execute(text(_INSERT_MEMBER), {**base, "id": "tm-1", "revoked_at": None})
    await _expect_violation(
        conn, "ux_trip_members_active", _INSERT_MEMBER, {**base, "id": "tm-2", "revoked_at": None}
    )

    # A revoked row alongside the active one is history, not a second membership.
    await conn.execute(
        text(_INSERT_MEMBER), {**base, "id": "tm-3", "revoked_at": datetime.now(UTC)}
    )


_INSERT_TRIP_SLUGS = (
    "INSERT INTO trips (id, name, rider_slug, viewer_slug, start_date) "
    "VALUES (:id, 'Paired slugs fixture', :rider, :viewer, :start)"
)


@pytest.mark.parametrize(
    ("rider", "viewer"),
    [("test-rider-only", None), (None, "test-viewer-only")],
    ids=["viewer-null", "rider-null"],
)
async def test_slugs_paired_check_rejects_one_null_slug(
    conn: AsyncConnection, rider: str | None, viewer: str | None
) -> None:
    await _expect_violation(
        conn,
        "trips_slugs_paired_check",
        _INSERT_TRIP_SLUGS,
        {"id": "test-half-slugged", "rider": rider, "viewer": viewer, "start": date(2026, 1, 1)},
    )


async def test_slugs_paired_check_allows_both_or_neither(conn: AsyncConnection) -> None:
    """Two app-created (slugless) trips coexist: UNIQUE treats the NULLs as distinct."""
    await _insert_trip(conn, "test-slugless-1")
    await _insert_trip(conn, "test-slugless-2")
    await _insert_trip(conn, "test-slugged", rider="test-paired-rider", viewer="test-paired-viewer")
