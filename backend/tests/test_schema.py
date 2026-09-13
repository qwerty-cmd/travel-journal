"""
Schema tests — does the live Postgres schema actually match what the data
layer thinks it is?

Two things are being protected here:

1. **Drift between ``migrations/*.sql`` and ``app/data/tables.py``.** Those are
   two independent descriptions of the same tables. If a future migration adds
   a column and nobody updates the Core metadata (or vice versa), every query
   still compiles and the failure only surfaces as a runtime error from a real
   request. Reflecting the live database and comparing it to the metadata turns
   that into a test failure at the point the mistake is made.
2. **The location_source CHECK constraint.** The Pydantic ``LocationSource``
   enum guards the API edge, but the database is reachable from seed scripts
   and repositories that don't go through it. The constraint is what makes
   "every stop is either a GPS fix or a manual tap" true of the data rather
   than merely of the happy path.

These tests need a real Postgres (docker-compose service ``postgres``, or any
DATABASE_URL) — see tests/conftest.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import DateTime, MetaData, exc, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.data import tables

EXPECTED_TABLES = ["trips", "stops", "photos", "bikes"]

# asyncpg binds typed parameters, so date/timestamptz columns need real
# date/datetime objects rather than ISO strings.
_START_DATE = date(2026, 1, 1)
_ARRIVED_AT = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)

_INSERT_TRIP = (
    "INSERT INTO trips (id, name, rider_slug, viewer_slug, start_date) "
    "VALUES (:id, :name, :rider, :viewer, :start_date)"
)
_INSERT_STOP = (
    "INSERT INTO stops (id, trip_id, name, lat, lng, location_source, arrived_at) "
    "VALUES (:id, :trip_id, :name, :lat, :lng, :source, :arrived_at)"
)


async def _reflect(engine: AsyncEngine) -> MetaData:
    reflected = MetaData()
    async with engine.connect() as conn:
        await conn.run_sync(reflected.reflect)
    return reflected


async def test_all_declared_tables_exist(migrated_engine: AsyncEngine) -> None:
    reflected = await _reflect(migrated_engine)
    for name in EXPECTED_TABLES:
        assert name in tables.metadata.tables, f"{name} missing from app/data/tables.py"
        assert name in reflected.tables, f"{name} missing from the migrated database"


async def test_schema_migrations_ledger_exists(migrated_engine: AsyncEngine) -> None:
    """The runner's own bookkeeping table, and the initial migration recorded in it."""
    reflected = await _reflect(migrated_engine)
    assert "schema_migrations" in reflected.tables
    async with migrated_engine.connect() as conn:
        rows = await conn.execute(text("SELECT version FROM schema_migrations"))
        versions = {row[0] for row in rows}
    assert "0001_initial_schema" in versions


@pytest.mark.parametrize("table_name", EXPECTED_TABLES)
async def test_columns_match_metadata(migrated_engine: AsyncEngine, table_name: str) -> None:
    """Every column in tables.py exists in the database with the same type and nullability."""
    reflected = await _reflect(migrated_engine)
    declared = tables.metadata.tables[table_name]
    live = reflected.tables[table_name]

    assert set(declared.columns.keys()) == set(live.columns.keys()), (
        f"{table_name}: column names differ between tables.py and the database"
    )

    for column in declared.columns:
        live_column = live.columns[column.name]
        qualified = f"{table_name}.{column.name}"

        assert column.type.python_type is live_column.type.python_type, (
            f"{qualified}: declared {column.type!r}, database has {live_column.type!r}"
        )
        assert column.nullable == live_column.nullable, (
            f"{qualified}: declared nullable={column.nullable}, "
            f"database has nullable={live_column.nullable}"
        )
        # timestamptz vs timestamp is invisible to python_type (both datetime)
        # and is exactly the mistake that makes arrival times ambiguous once
        # the trip crosses a time zone, so it gets its own assertion.
        if isinstance(column.type, DateTime):
            assert column.type.timezone is True, f"{qualified}: declared without timezone"
            assert live_column.type.timezone is True, f"{qualified}: database column is not timestamptz"


async def test_primary_keys_are_text_not_uuid(migrated_engine: AsyncEngine) -> None:
    """
    Ids are client-generated and the contract does not enforce UUID4 format
    (docs/api-contract.md, "Idempotency"). A uuid column would turn a malformed
    id into a raw driver error instead of something a route can map to a 4xx.
    """
    reflected = await _reflect(migrated_engine)
    for name in EXPECTED_TABLES:
        pk_columns = list(reflected.tables[name].primary_key.columns)
        assert [c.name for c in pk_columns] == ["id"], f"{name}: primary key is not (id)"
        assert pk_columns[0].type.python_type is str, f"{name}.id is not a text column"


async def test_photos_has_no_url_or_archived_column(migrated_engine: AsyncEngine) -> None:
    """
    PhotoOut.url is presigned at read time from object_key, and PhotoOut.archived
    is derived from one_drive_file_id IS NOT NULL. Persisting either would be a
    second source of truth (docs/api-contract.md, "Photo serving").
    """
    reflected = await _reflect(migrated_engine)
    columns = set(reflected.tables["photos"].columns.keys())
    assert "url" not in columns
    assert "archived" not in columns
    assert {"object_key", "one_drive_file_id"} <= columns


async def test_foreign_key_lookup_indexes_exist(migrated_engine: AsyncEngine) -> None:
    """Every read path is "all children of this parent"; Postgres does not index FKs for us."""
    reflected = await _reflect(migrated_engine)
    for table_name, column_name in (
        ("stops", "trip_id"),
        ("photos", "stop_id"),
        ("bikes", "trip_id"),
    ):
        indexed = {
            tuple(c.name for c in index.columns) for index in reflected.tables[table_name].indexes
        }
        assert (column_name,) in indexed, f"{table_name}({column_name}) is not indexed"


# Every column that carries a unique constraint or a unique index on `trips`,
# one row per (constraint/index, column). Asking pg_index rather than
# pg_indexes.indexdef means the answer is "is *this column* unique" rather
# than a substring match against a blob of DDL in which trips_pkey alone would
# make the word UNIQUE appear. Single-column only: a composite unique index on
# (rider_slug, viewer_slug) would not make either slug unique on its own, and
# `array_length(i.indkey ...) = 1` is what excludes it.
_UNIQUE_COLUMNS_SQL = """
SELECT a.attname
FROM pg_index i
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0]
WHERE t.relname = :table
  AND n.nspname = current_schema()
  AND i.indisunique
  AND array_length(i.indkey::int2[], 1) = 1
"""


@pytest.mark.parametrize("column_name", ["rider_slug", "viewer_slug"])
async def test_slugs_are_unique(migrated_engine: AsyncEngine, column_name: str) -> None:
    """
    Slug uniqueness is an access-control property, not a performance one: two
    trips sharing a slug would hand one trip's write access to the other.

    Asserted per column against the catalog, because an index merely *existing*
    on a slug column proves nothing — a plain non-unique index would let two
    trips share a slug just as happily as no index at all.
    """
    async with migrated_engine.connect() as conn:
        rows = await conn.execute(text(_UNIQUE_COLUMNS_SQL), {"table": "trips"})
        unique_columns = {row[0] for row in rows}

    assert column_name in unique_columns, (
        f"trips.{column_name} has no single-column UNIQUE constraint or index "
        f"(uniquely-constrained columns on trips: {sorted(unique_columns) or 'none'})"
    )


async def test_location_source_check_rejects_unknown_value(migrated_engine: AsyncEngine) -> None:
    """A stop must be a 'gps' fix or a 'manual' tap — 'satellite' is not a thing."""
    async with migrated_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(_INSERT_TRIP),
                {
                    "id": "test-trip-check-constraint",
                    "name": "Check constraint fixture",
                    "rider": "test-rider-check-constraint",
                    "viewer": "test-viewer-check-constraint",
                    "start_date": _START_DATE,
                },
            )
            with pytest.raises(exc.IntegrityError):
                await conn.execute(
                    text(_INSERT_STOP),
                    {
                        "id": "test-stop-check-constraint",
                        "trip_id": "test-trip-check-constraint",
                        "name": "Nowhere",
                        "lat": -23.7,
                        "lng": 133.88,
                        "source": "satellite",
                        "arrived_at": _ARRIVED_AT,
                    },
                )
        finally:
            # Nothing this test wrote survives it.
            await trans.rollback()


async def test_location_source_accepts_contract_values(migrated_engine: AsyncEngine) -> None:
    """The same constraint must not reject the two values the contract defines."""
    async with migrated_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(_INSERT_TRIP),
                {
                    "id": "test-trip-valid-sources",
                    "name": "Valid sources fixture",
                    "rider": "test-rider-valid-sources",
                    "viewer": "test-viewer-valid-sources",
                    "start_date": _START_DATE,
                },
            )
            for index, source in enumerate(("gps", "manual")):
                await conn.execute(
                    text(_INSERT_STOP),
                    {
                        "id": f"test-stop-valid-{source}",
                        "trip_id": "test-trip-valid-sources",
                        "name": f"Stop {index}",
                        "lat": -23.7,
                        "lng": 133.88,
                        "source": source,
                        "arrived_at": _ARRIVED_AT,
                    },
                )
        finally:
            await trans.rollback()
