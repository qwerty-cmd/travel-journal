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
2. **The CHECK constraints.** The Pydantic models guard the API edge, but the
   database is reachable from seed scripts and repositories that don't go
   through it. A constraint is what makes a rule true of the *data* rather than
   merely of the happy path. Two are asserted here: ``location_source``, so
   every stop is a GPS fix or a manual tap; and ``trips_slugs_differ_check``, so
   no trip can carry the same string as both its rider and its viewer slug —
   which would silently turn a read-only link into a writable one.

These tests need a real Postgres (docker-compose service ``postgres``, or any
DATABASE_URL) — see tests/conftest.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Double,
    Integer,
    MetaData,
    Text,
    exc,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.types import REAL, VARCHAR, TypeEngine

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


def _postgres_ddl(column_type: TypeEngine[Any]) -> str:
    """
    The column type as Postgres DDL — ``TEXT``, ``VARCHAR(20)``, ``DOUBLE PRECISION``.

    Compiled against the Postgres dialect so a declared generic type (``Text``,
    ``Double``) and the dialect-specific type reflection hands back (``TEXT``,
    ``DOUBLE_PRECISION``) render to the same string exactly when the database
    would store the same thing, and to different strings when it would not —
    length, precision and width included.
    """
    return column_type.compile(dialect=postgresql.dialect())


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

        # Compared as the DDL Postgres itself would emit, not as `python_type`.
        # `python_type` collapses Text/VARCHAR(20) to str, Double/REAL to float
        # and Integer/BigInteger to int, so a migration that narrowed a column
        # tables.py still declares wide passed this check (t-schema-type-drift-check).
        declared_ddl = _postgres_ddl(column.type)
        live_ddl = _postgres_ddl(live_column.type)
        assert declared_ddl == live_ddl, (
            f"{qualified}: declared {declared_ddl} ({column.type!r}), "
            f"database has {live_ddl} ({live_column.type!r})"
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
            assert live_column.type.timezone is True, (
                f"{qualified}: database column is not timestamptz"
            )


@pytest.mark.parametrize(
    ("declared", "narrower"),
    [(Text(), VARCHAR(20)), (Double(), REAL()), (BigInteger(), Integer())],
    ids=["text-vs-varchar20", "double-vs-real", "bigint-vs-integer"],
)
def test_type_comparison_sees_what_python_type_cannot(
    declared: TypeEngine[Any], narrower: TypeEngine[Any]
) -> None:
    """
    The comparator's own guard: the three pairs QA found ``python_type`` equating.

    Each pair has the same ``python_type`` (asserted, so the case keeps meaning
    what it says) and must still compare *different* under the check
    ``test_columns_match_metadata`` uses.
    """
    assert declared.python_type is narrower.python_type
    assert _postgres_ddl(declared) != _postgres_ddl(narrower)


def test_type_comparison_equates_generic_and_reflected_spellings() -> None:
    """...and does not over-report: ``Text`` declared and ``TEXT`` reflected are one type."""
    assert _postgres_ddl(Text()) == _postgres_ddl(postgresql.TEXT())
    assert _postgres_ddl(Double()) == _postgres_ddl(postgresql.DOUBLE_PRECISION())
    assert _postgres_ddl(DateTime(timezone=True)) == _postgres_ddl(
        postgresql.TIMESTAMP(timezone=True)
    )


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


# --------------------------------------------------------------------------
# trips_slugs_differ_check — rider_slug <> viewer_slug
# --------------------------------------------------------------------------
# Per-column UNIQUE says two *trips* cannot share a slug. It says nothing about
# one trip carrying the same string in both columns, and that row is a silent
# privilege escalation: `security.py` derives access as "RIDER if the presented
# slug equals rider_slug, else VIEWER", so on such a row the rider branch always
# wins and a link handed out as read-only writes. No code can tell the two links
# apart, because there is only one link — which is why this is enforced in the
# schema rather than by a check somewhere in the request path.
#
# Distinct from the cross-trip collision decision-log entry 8 dismissed: there
# the open question was "which trip did you mean", and every answer was
# self-consistent. Here it is "what may you do", and the answer is wrong.

_SLUGS_DIFFER_CONSTRAINT = "trips_slugs_differ_check"

# One row per CHECK constraint on the table, with its definition. Asked of
# pg_constraint per named constraint rather than by scanning a dump of the DDL
# for "<>", per decision-log entry 2: a keyword search over concatenated
# introspection output passes on the strength of some unrelated object.
_CHECK_CONSTRAINTS_SQL = """
SELECT c.conname, pg_get_constraintdef(c.oid)
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE t.relname = :table
  AND n.nspname = current_schema()
  AND c.contype = 'c'
"""


async def test_slugs_differ_constraint_exists(migrated_engine: AsyncEngine) -> None:
    """The CHECK is present in the live database as a check constraint by name."""
    async with migrated_engine.connect() as conn:
        rows = await conn.execute(text(_CHECK_CONSTRAINTS_SQL), {"table": "trips"})
        checks = {row[0]: row[1] for row in rows}

    assert _SLUGS_DIFFER_CONSTRAINT in checks, (
        f"trips has no CHECK named {_SLUGS_DIFFER_CONSTRAINT} "
        f"(CHECK constraints on trips: {sorted(checks) or 'none'})"
    )
    definition = checks[_SLUGS_DIFFER_CONSTRAINT]
    assert "rider_slug" in definition and "viewer_slug" in definition, (
        f"{_SLUGS_DIFFER_CONSTRAINT} does not compare the two slug columns: {definition}"
    )


def test_slugs_differ_constraint_is_declared_in_core_metadata() -> None:
    """
    ...and in ``app/data/tables.py``, which the migrations README requires in the
    same patch.

    ``test_columns_match_metadata`` compares columns only, so a migration that
    added this constraint without the matching Core declaration would pass every
    other test in this file while leaving the two descriptions of ``trips``
    disagreeing.
    """
    declared = {
        constraint.name: str(constraint.sqltext)
        for constraint in tables.trips.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert _SLUGS_DIFFER_CONSTRAINT in declared, (
        f"tables.trips declares no CheckConstraint named {_SLUGS_DIFFER_CONSTRAINT} "
        f"(declared: {sorted(declared) or 'none'})"
    )
    assert "rider_slug" in declared[_SLUGS_DIFFER_CONSTRAINT]
    assert "viewer_slug" in declared[_SLUGS_DIFFER_CONSTRAINT]


async def test_equal_slugs_are_rejected(migrated_engine: AsyncEngine) -> None:
    """
    The insert that would grant write access through a read-only link fails.

    This is the concrete thing the constraint buys, and it is due now rather
    than later: ``s-seed-trip-record`` is the next story that inserts a trip,
    and a copy-paste of ``rider_slug`` into ``viewer_slug`` there looks exactly
    like a working seed.
    """
    async with migrated_engine.connect() as conn:
        trans = await conn.begin()
        try:
            with pytest.raises(exc.IntegrityError):
                await conn.execute(
                    text(_INSERT_TRIP),
                    {
                        "id": "test-trip-equal-slugs",
                        "name": "Equal slugs fixture",
                        "rider": "test-slug-used-for-both",
                        "viewer": "test-slug-used-for-both",
                        "start_date": _START_DATE,
                    },
                )
        finally:
            await trans.rollback()


async def test_distinct_slugs_are_accepted(migrated_engine: AsyncEngine) -> None:
    """
    ...and the normal case is untouched.

    Paired with the test above deliberately: a constraint written as
    ``CHECK (false)`` would satisfy the rejection test perfectly and break every
    trip in the system.
    """
    async with migrated_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(_INSERT_TRIP),
                {
                    "id": "test-trip-distinct-slugs",
                    "name": "Distinct slugs fixture",
                    "rider": "test-rider-distinct-slugs",
                    "viewer": "test-viewer-distinct-slugs",
                    "start_date": _START_DATE,
                },
            )
        finally:
            # Nothing this test wrote survives it.
            await trans.rollback()


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
