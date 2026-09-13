"""
Tests for the one-time trip seed (``app/data/seed_trip.py``).

Two things shape every test in this file.

**The subject is a credential, so nothing here may name one.** The slugs this
script generates are the whole access model (``docs/api-contract.md``, "Access:
two slugs, no accounts") and they exist only in Postgres. There is therefore no
literal slug value anywhere below, and no assertion of the form
``result.rider_slug == "..."``. Every check is a **property**: the two slugs
differ, they are long enough, they use the URL-safe alphabet, the formatted
output contains the values the call returned. A test that pinned a slug to a
constant would either be asserting against a value the script did not generate,
or publishing one it did.

**The seed is one-shot, so the suite must not consume it.** The real run of this
script inserts the single trip the whole app is scoped by, and its slugs cannot
be regenerated. So the happy-path tests run inside ``migrated_engine.begin()``,
``DELETE FROM trips`` to establish the empty precondition the script requires,
seed, assert, and then **roll back**. Nothing is committed: the table is left
exactly as it was found, zero rows added, and these tests stay green after the
user really seeds the live trip into this same database — including the
already-exists tests, which are the only ones that need a row and get it from
the ``seeded_trips`` fixture rather than from the live data.

What is deliberately *not* tested, because it cannot be: the UNIQUE-collision
retry path. The already-exists guard means the INSERT only ever runs against an
empty table, so no conflict is reachable from a test without mutating the code
under test. ``test_the_insert_has_no_on_conflict_clause`` asserts the closest
observable property instead — that the statement carries no ``ON CONFLICT``
clause, which ``docs/decision-log.md`` Entry 3 forbids outright and which no
behavioural test in this file could otherwise notice appearing.

**The post-commit window has its own tests, and they get the same treatment.**
Between the commit and the print, the slugs exist only in this process's memory;
anything that can fail there destroys them. Two such failures are reachable —
``engine.dispose()`` raising, and the stdout write raising — and both are
exercised below against a real seed, with the engine and the stream faked rather
than the database. ``_NonCommittingEngine`` is what makes that safe: it is the
one place ``_seed_with_new_engine``'s commit is intercepted, and it cannot
commit, so the one-shot seed survives these tests exactly as it survives the
others.

The two failure branches that need a *broken* database — an un-migrated one, and
a schema without the slug columns — get a scratch schema created inside the same
rolled-back transaction and reached via ``SET LOCAL search_path``. Nothing is
dropped, altered or renamed: the real ``trips`` table is simply not on the path
for the duration of one transaction that is then thrown away.
"""

from __future__ import annotations

import errno
import io
import logging
import re
import secrets
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date

import pytest
from conftest import SeededTrip
from sqlalchemy import func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.data import seed_trip as seed_trip_module
from app.data import tables
from app.data.seed_trip import (
    DatabaseNotReadyError,
    SeededTripResult,
    SeedTripError,
    TripAlreadyExistsError,
    _print_result,
    _seed_with_new_engine,
    _trip_insert,
    format_slug_output,
    main,
    seed_trip,
)

# The URL-safe base64 alphabet, which is all `secrets.token_urlsafe` can emit.
# A slug appears in a URL path with no encoding, so a character outside this set
# would be a slug that cannot be handed out as a link.
SLUG_ALPHABET = re.compile(r"[A-Za-z0-9_-]+")

# 32 random bytes, base64url-encoded without padding, is 43 characters. Asserted
# as a minimum rather than an equality: the property that matters is "at least
# this much entropy", and a future increase in the token size should not need a
# test edit to stay true.
MINIMUM_SLUG_LENGTH = 43


async def _trip_count(conn: AsyncConnection) -> int:
    """Rows in ``trips``, on the caller's connection (so inside its transaction)."""
    return (await conn.execute(select(func.count()).select_from(tables.trips))).scalar_one()


async def _seed_and_roll_back(
    engine: AsyncEngine,
    *,
    name: str,
    start_date: date,
) -> SeededTripResult:
    """
    Run a real seed against the real database and undo it.

    The ``DELETE`` establishes the empty table ``seed_trip`` requires, and the
    explicit ``rollback()`` is what keeps this test suite from consuming the
    one-time seed or destroying live data — both statements are discarded
    together. It is the last statement in the block on purpose: SQLAlchemy
    refuses further commands on a connection whose transaction was ended inside
    its own context manager.

    Returns the result object for assertions that do not need a live
    transaction, which is safe because ``SeededTripResult`` is a plain frozen
    value with no connection attached.
    """
    async with engine.begin() as conn:
        await conn.execute(tables.trips.delete())
        result = await seed_trip(conn, name=name, start_date=start_date)
        await conn.rollback()
    return result


async def test_seeds_exactly_one_trip_and_rolls_back(migrated_engine: AsyncEngine) -> None:
    """
    The happy path: one row, matching the arguments, with two usable slugs.

    Every assertion about the slugs is a property rather than a value — see the
    module docstring — and the row count is checked on both sides of the call so
    that "it inserted the trip" is distinguishable from "it inserted something".
    """
    name = "Seed script test trip"
    start_date = date(2026, 6, 1)

    async with migrated_engine.begin() as conn:
        rows_before_delete = await _trip_count(conn)

        await conn.execute(tables.trips.delete())
        assert await _trip_count(conn) == 0

        result = await seed_trip(conn, name=name, start_date=start_date)

        # Exactly one row added, not "at least one".
        assert await _trip_count(conn) == 1

        assert result.name == name
        assert result.start_date == start_date

        # The one property the CHECK constraint in migration 0002 exists to
        # guarantee: equal slugs would make the read-only link grant writes.
        assert result.rider_slug != result.viewer_slug

        for slug in (result.rider_slug, result.viewer_slug):
            assert len(slug) >= MINIMUM_SLUG_LENGTH
            assert SLUG_ALPHABET.fullmatch(slug) is not None

        # Server-generated UUID4 (docs/api-contract.md's client-generated ids are
        # an offline-queue rule for stops/photos/bikes; a trip has neither).
        assert uuid.UUID(result.id).version == 4

        # The row in the table is the row that was returned - a result object
        # describing something the database does not contain would satisfy every
        # assertion above.
        stored = (
            await conn.execute(
                select(
                    tables.trips.c.id,
                    tables.trips.c.name,
                    tables.trips.c.start_date,
                    tables.trips.c.rider_slug,
                    tables.trips.c.viewer_slug,
                )
            )
        ).one()
        assert stored.id == result.id
        assert stored.name == result.name
        assert stored.start_date == result.start_date
        assert stored.rider_slug == result.rider_slug
        assert stored.viewer_slug == result.viewer_slug

        await conn.rollback()

    # Nothing was committed: the table is exactly as it was found, whether that
    # was empty or holding the real trip.
    async with migrated_engine.connect() as conn:
        assert await _trip_count(conn) == rows_before_delete


async def test_format_slug_output_carries_both_slugs_and_says_it_is_the_only_copy(
    migrated_engine: AsyncEngine,
) -> None:
    """
    The printed block is the script's only egress for a slug, so it has to carry
    both of them, labelled, plus the warning that they are not recoverable.

    Asserted against the object ``seed_trip`` returned, never against a literal:
    a hard-coded expected string here would be a slug in the repository.
    """
    result = await _seed_and_roll_back(
        migrated_engine,
        name="Seed script output test trip",
        start_date=date(2026, 6, 2),
    )

    output = format_slug_output(result)

    assert result.rider_slug in output
    assert result.viewer_slug in output
    assert "rider" in output.lower()
    assert "viewer" in output.lower()
    assert "only copy" in output.lower()


async def test_seeding_writes_nothing_to_any_log(
    migrated_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A slug in a log line is a permanent credential in a log aggregator, a
    terminal scrollback and any support ticket that quotes it.

    ``set_level(DEBUG)`` so this covers a debug-level record too: the failure
    being guarded against is someone adding ``logger.debug("seeded %s", result)``
    while chasing a bug, which a default-level capture would never see.
    """
    caplog.set_level(logging.DEBUG)

    await _seed_and_roll_back(
        migrated_engine,
        name="Seed script logging test trip",
        start_date=date(2026, 6, 3),
    )

    assert caplog.records == []


async def test_refuses_to_seed_when_a_trip_already_exists(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
) -> None:
    """
    The refusal, which is the whole "one-time" part of a one-time script.

    Uses the shared ``seeded_trips`` fixture (two rows, removed on teardown)
    rather than committing a trip of its own, so this test never depends on the
    live trip existing and never leaves one behind.

    Also asserts what the error is *not* allowed to carry: the existing trip's
    slugs. ``main()`` prints this error's details straight to stdout, so a slug
    reachable through it would be printed for a trip that was seeded long ago.
    """
    async with migrated_engine.begin() as conn:
        rows_before = await _trip_count(conn)

        with pytest.raises(TripAlreadyExistsError) as excinfo:
            await seed_trip(conn, name="A second trip", start_date=date(2026, 7, 1))

        assert await _trip_count(conn) == rows_before
        await conn.rollback()

    error = excinfo.value

    # It reports one of the trips that is actually there, in full.
    assert (error.trip_id, error.name, error.start_date) in {
        (trip.id, trip.name, trip.start_date) for trip in seeded_trips
    }

    # And neither its message nor any attribute on it carries a slug.
    message = str(error)
    attribute_values = {str(value) for value in vars(error).values()}
    for trip in seeded_trips:
        for slug in (trip.rider_slug, trip.viewer_slug):
            assert slug not in message
            assert slug not in attribute_values


def test_the_insert_has_no_on_conflict_clause() -> None:
    """
    ``docs/decision-log.md`` Entry 3: no ``ON CONFLICT`` on a slug column, in any
    form. ``DO UPDATE`` would silently overwrite an existing trip's slug — the
    authorization leak the UNIQUE constraints exist to prevent — and
    ``DO NOTHING`` would report success having written nothing.

    Structural because it cannot be behavioural: the already-exists guard means
    the INSERT only ever runs against an empty table, so no conflict is
    reachable and ``ON CONFLICT`` added to this statement would change no
    observable behaviour in any other test here.

    ``_post_values_clause`` is the attribute the postgresql dialect populates
    when ``on_conflict_do_update()`` / ``on_conflict_do_nothing()`` is called on
    an ``Insert``; ``None`` means the statement is a plain one. Asserted against
    that object attribute first and the compiled SQL second — and note the SQL
    check is a negative assertion over *this one statement's own* text, not the
    "dump introspection to a string and look for a keyword" shape that Entry 2
    rules out, where the keyword arrives from some unrelated object.

    The values below are placeholders, not slugs: the statement is compiled,
    never executed.
    """
    statement = _trip_insert(
        trip_id="placeholder-id",
        name="placeholder name",
        start_date=date(2026, 6, 1),
        rider_slug="placeholder-rider-value",
        viewer_slug="placeholder-viewer-value",
    )

    assert statement._post_values_clause is None
    assert "ON CONFLICT" not in str(statement.compile(dialect=postgresql.dialect())).upper()


# --------------------------------------------------------------------------
# The window between the commit and the print.
# --------------------------------------------------------------------------


class _NonCommittingEngine:
    """
    Stands in for the engine ``_seed_with_new_engine`` builds, and **cannot commit**.

    ``_seed_with_new_engine`` is the only function here that owns a transaction
    boundary: it seeds inside ``engine.begin()``, which commits on exit. Running
    it for real would consume the one-time seed. So the engine is replaced and
    the connection is supplied by the test, already inside a transaction the test
    rolls back — ``begin()`` below yields it and, on exit, does **nothing**. That
    single "no commit here" is the whole reason these tests are safe to run
    against the live database, which is why it is one obvious line rather than
    spread across each test.

    Everything either side of the boundary is the real thing: a real connection,
    a real INSERT, real generated slugs, the real ``_print_result``.

    ``dispose()`` is the subject. It is real network I/O against connections that
    may be dead or idle-timed-out by Neon, and it runs *after* the commit — so a
    raise from it, if ``_seed_with_new_engine`` let it propagate, would replace
    the returned exit code with an ``OSError`` and take the only copy of the
    slugs with it. ``dispose_error`` forces exactly that.

    ``events`` records call order so a test can assert the print happens before
    the dispose, not merely that both happened.
    """

    def __init__(
        self,
        conn: AsyncConnection,
        *,
        events: list[str],
        dispose_error: BaseException | None = None,
    ) -> None:
        self._conn = conn
        self._dispose_error = dispose_error
        self.events = events

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncConnection]:
        """Hand over the test's connection. No commit, no rollback — see the class docstring."""
        yield self._conn

    async def dispose(self) -> None:
        self.events.append("dispose")
        if self._dispose_error is not None:
            raise self._dispose_error


class _UnwritableStream:
    """
    A stdout that always fails, the way a full disk or a closed pipe does.

    Both methods raise, because handling only ``write`` would leave the flush —
    the part that makes "the call returned" mean "the bytes left this process" —
    as an unguarded second chance to lose everything.
    """

    def __init__(self, error: OSError) -> None:
        self._error = error

    def write(self, data: str) -> int:
        raise self._error

    def flush(self) -> None:
        raise self._error


async def test_a_dispose_failure_cannot_destroy_the_result(
    migrated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    ``engine.dispose()`` raising after the commit must not replace the result.

    This is the one failure in the script that cannot be recovered from by
    re-running it: the trip is committed, so a second run refuses, and the slugs
    were never printed. A ``dispose()`` inside a plain ``finally:`` on the value
    path discards ``return result`` and propagates the ``OSError`` instead — the
    operator gets a message about a closed socket and no credentials.

    Asserted on both halves of the fix: the slugs reach stdout and the exit code
    is still the success code (the dispose failure is non-fatal), and ``events``
    shows the print completed *before* dispose was called at all, so the I/O is
    not in the unrecoverable window in the first place.
    """
    events: list[str] = []
    seen: list[SeededTripResult] = []

    def emit(result: SeededTripResult) -> int:
        events.append("emit")
        seen.append(result)
        return _print_result(result)

    async with migrated_engine.connect() as conn:
        # The empty-table precondition seed_trip requires, inside the
        # transaction this block never commits.
        await conn.execute(tables.trips.delete())

        engine = _NonCommittingEngine(
            conn,
            events=events,
            dispose_error=OSError(errno.EPIPE, "Broken pipe"),
        )
        monkeypatch.setattr(seed_trip_module, "create_async_engine", lambda *a, **kw: engine)

        exit_code = await _seed_with_new_engine(
            name="Seed script dispose failure trip",
            start_date=date(2026, 6, 4),
            emit=emit,
        )

        await conn.rollback()

    captured = capsys.readouterr().out

    assert exit_code == 0
    (result,) = seen
    assert result.rider_slug in captured
    assert result.viewer_slug in captured

    # Ordering, not just survival: dispose ran, and it ran after the slugs were
    # already out. If this ever reads ["dispose", "emit"] the I/O is back inside
    # the window even if the suppression still holds.
    assert events == ["emit", "dispose"]


async def test_a_failed_stdout_write_is_reported_with_the_recovery_path(
    migrated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A stdout write that fails after the commit still has to leave the operator
    somewhere to go.

    Unhandled, ``ENOSPC`` on the one write this script makes is a traceback and
    no slugs for a trip that now exists. Handled, it can still name the only
    thing that helps: the SELECT that reads the slugs back out of the row. The
    fallback goes to stderr, a different descriptor, which is routinely still
    writable when stdout is not (``> /full-disk/slugs.txt``).

    The exit code has to be non-zero — "the slugs were printed" and "the slugs
    were lost" cannot both be success — and nothing on the fallback path may
    carry a slug, since stderr may be pointed somewhere stdout deliberately was
    not.
    """
    result = await _seed_and_roll_back(
        migrated_engine,
        name="Seed script stdout failure trip",
        start_date=date(2026, 6, 5),
    )

    stderr = io.StringIO()
    monkeypatch.setattr(
        sys, "stdout", _UnwritableStream(OSError(errno.ENOSPC, "No space left on device"))
    )
    monkeypatch.setattr(sys, "stderr", stderr)
    try:
        exit_code = _print_result(result)
    finally:
        # Restored before asserting, so a failure here can still be reported.
        monkeypatch.undo()

    reported = stderr.getvalue()

    assert exit_code == 1
    assert "No space left on device" in reported
    assert "SELECT rider_slug, viewer_slug FROM trips" in reported
    # The operator is told the trip *is* committed - otherwise the obvious read
    # of "writing the slugs failed" is "the seed failed, run it again".
    assert "committed" in reported
    assert result.rider_slug not in reported
    assert result.viewer_slug not in reported


def test_the_already_exists_refusal_names_the_recovery_select(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    The refusal has to say how to get the slugs back, not just that it will not
    reprint them.

    "They exist nowhere but the database" is true and, on its own, reads as
    *gone* — and the obvious next move for an operator who lost the original
    output is ``DELETE FROM trips`` and seed again, which destroys the trip and
    (via ``ON DELETE CASCADE``) every stop, photo and bike scoped to it, to
    recover something a plain ``SELECT`` already returns. There is no in-app read
    path to offer instead: ``TripOut`` has no slug field and the repository
    lookup takes a slug as input. So the query itself is the recovery path and
    the message must contain it.

    ``_seed_with_new_engine`` is replaced with one that only raises: this test is
    about what ``main()`` prints, and the real refusal is already proven against
    a real table by ``test_refuses_to_seed_when_a_trip_already_exists``. Nothing
    here opens a connection.
    """

    async def _refuse(*, name: str, start_date: date, emit: object) -> int:
        raise TripAlreadyExistsError(
            trip_id="existing-trip-id",
            name="An existing trip",
            start_date=date(2026, 6, 1),
        )

    monkeypatch.setattr(seed_trip_module, "_seed_with_new_engine", _refuse)

    exit_code = main(["--name", "A second trip", "--start-date", "2026-07-01"])

    printed = capsys.readouterr().out

    assert exit_code == 1

    # What is there, so the operator can tell "I already ran this" from "someone
    # else did".
    assert "existing-trip-id" in printed
    assert "An existing trip" in printed

    # And how to recover the slugs, in full: the query, where to run it, and the
    # warning against the destructive alternative.
    assert "SELECT rider_slug, viewer_slug FROM trips" in printed
    assert "psql" in printed
    assert "Neon" in printed
    assert "do NOT delete the trip" in printed


# --------------------------------------------------------------------------
# Failures that need a broken database.
# --------------------------------------------------------------------------

# A trips table with the columns the guard query reads and **not** the ones the
# INSERT writes. The guard therefore succeeds (empty table, seed proceeds) and
# the INSERT fails with 42703 undefined_column - a ProgrammingError, not an
# IntegrityError, so it lands in seed_trip's generic SQLAlchemyError branch,
# which is the branch under test. Shaped this way rather than by altering the
# real table, which is never touched.
_TRIPS_WITHOUT_SLUG_COLUMNS = """
    CREATE TABLE trips (
        id text PRIMARY KEY,
        name text NOT NULL,
        start_date date NOT NULL
    )
"""


@asynccontextmanager
async def _scratch_schema(
    engine: AsyncEngine,
    *,
    trips_ddl: str | None,
) -> AsyncIterator[AsyncConnection]:
    """
    A connection whose ``trips`` is a throwaway one (or is missing entirely).

    How the real table is protected, since this is the part worth checking
    before trusting these tests: nothing is dropped, altered or renamed. A
    schema with a random name is created inside a transaction, ``SET LOCAL``
    puts it in front of the search path *for that transaction only*, and the
    transaction is rolled back — taking the schema, the table and the search
    path change with it, because DDL in Postgres is transactional. The real
    ``public.trips`` is untouched throughout; it is simply not on the path.

    ``trips_ddl=None`` leaves no ``trips`` at all, which is what an un-migrated
    database looks like to this script.
    """
    async with engine.connect() as conn:
        await conn.begin()
        schema = f"seed_trip_scratch_{secrets.token_hex(6)}"
        try:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            # LOCAL: transaction-scoped, so the rollback below restores the path
            # even though this connection goes back to a shared pool.
            await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            if trips_ddl is not None:
                await conn.execute(text(trips_ddl))
            yield conn
        finally:
            await conn.rollback()


async def test_an_unmigrated_database_is_refused_rather_than_raising(
    migrated_engine: AsyncEngine,
) -> None:
    """
    Running this before ``app.data.migrate`` is the most likely way it ever
    fails, and it used to get the least legible answer: the guard SELECT sat
    outside the ``try``, so ``relation "trips" does not exist`` escaped as a raw
    traceback.

    No slug can leak on this path — it is reached before any is generated — but
    the message is still built from the exception's class name and SQLSTATE
    *attribute* rather than its text, because rule 3 is a property of the module
    and not a judgement made per call site. Asserted here as the absence of
    SQLAlchemy's ``[SQL: ...] [parameters: ...]`` rendering, which is the exact
    thing that would put a bound slug on the terminal on the INSERT path.

    The SQLSTATE is spelled ``42P01`` literally rather than imported from the
    module under test: a test that compares the code against its own constant
    passes whatever that constant is changed to.
    """
    async with _scratch_schema(migrated_engine, trips_ddl=None) as conn:
        with pytest.raises(DatabaseNotReadyError) as excinfo:
            await seed_trip(
                conn,
                name="Seed script unmigrated database trip",
                start_date=date(2026, 6, 6),
            )

    error = excinfo.value
    message = str(error)

    assert error.sqlstate == "42P01"
    assert "42P01" in message
    # It names the command that fixes it, and says the run cost nothing.
    assert "app.data.migrate" in message
    assert "Nothing was inserted" in message

    # main() prints this, so it must be one of the errors main() catches.
    assert isinstance(error, SeedTripError)

    # No StatementError text, and no chained exception a traceback could render.
    assert "[SQL:" not in message
    assert "[parameters:" not in message
    assert error.__cause__ is None


async def test_a_non_integrity_insert_failure_reports_sqlstate_without_a_slug(
    migrated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The generic ``SQLAlchemyError`` branch: the widest net in the module, and the
    one with the most to lose.

    It must report the SQLSTATE. ``ProgrammingError`` alone cannot distinguish
    "the migrations were never applied" (42P01) from "the schema drifted" (42703)
    from "the driver broke", and this is the branch an operator reaches when
    something genuinely unexpected happened — the moment a diagnostic is worth
    most. The SQLSTATE is read off an exception *attribute*, exactly as the
    IntegrityError branch does, so it costs nothing and leaks nothing.

    And it must not report anything else. This branch fires with real generated
    slugs bound into the failing statement, so ``str(exc)`` here would render
    them; the slugs are replaced with recognisable sentinels so their absence
    from the message is an assertion rather than a hope. The sentinels carry a
    random suffix so they cannot match by coincidence, and ``issued`` is checked
    to be non-empty — otherwise "no sentinel in the message" would pass on a run
    where no slug was ever generated.
    """
    issued: list[str] = []

    class _SentinelSlugs:
        """Stands in for the ``secrets`` module inside ``seed_trip``."""

        @staticmethod
        def token_urlsafe(nbytes: int | None = None) -> str:
            value = f"sentinel-slug-{len(issued)}-{secrets.token_urlsafe(8)}"
            issued.append(value)
            return value

    monkeypatch.setattr(seed_trip_module, "secrets", _SentinelSlugs)

    async with _scratch_schema(migrated_engine, trips_ddl=_TRIPS_WITHOUT_SLUG_COLUMNS) as conn:
        with pytest.raises(SeedTripError) as excinfo:
            await seed_trip(
                conn,
                name="Seed script schema drift trip",
                start_date=date(2026, 6, 7),
            )

    error = excinfo.value
    message = str(error)

    # Two slugs generated and bound into the statement that failed, so the
    # absence assertions below have something to be absent.
    assert len(issued) == 2
    assert len(set(issued)) == 2

    assert "42703" in message
    assert "ProgrammingError" in message
    assert "Nothing was inserted" in message

    for sentinel in issued:
        assert sentinel not in message

    assert "[SQL:" not in message
    assert "[parameters:" not in message
    assert error.__cause__ is None
