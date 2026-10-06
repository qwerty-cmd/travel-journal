"""
One-time seed of the single trip row — the record every endpoint is scoped by.

    cd backend && uv run python -m app.data.seed_trip --name "Alps 2026" --start-date 2026-06-01

Why a script rather than an endpoint: there is exactly one trip and no admin UI
for v1 (spec Section 13). A "create trip" flow would be an unauthenticated
write path into the one table the entire authorization model rests on, built
for a row that gets inserted once.

**What this prints is the only copy of the slugs that will ever exist.** The two
slugs *are* the access model (``docs/api-contract.md``, "Access: two slugs, no
accounts"): no accounts, no passwords, no sessions, no rotation and no recovery
flow. They are permanent credentials, and after this run they live in exactly
one place — the ``trips`` row. Three rules follow, and they are the reason this
module looks more defensive than a seed script normally would:

1. **No log records.** This module imports no log machinery and never calls a
   logger — ``tests/test_seed_trip.py`` asserts a seed emits no record at all,
   at any level. A slug in a log line is a credential in a log aggregator, a
   terminal scrollback and a support ticket, forever.
2. **No files.** It opens nothing for writing. Redirecting stdout is the
   operator's decision to make, not this script's.
3. **No ``str()`` of a SQLAlchemy error, ever.** ``StatementError.__str__``
   appends ``[SQL: ...] [parameters: ...]`` — the bound parameters of the
   failing statement, which on the INSERT below *are the slugs*. Printing or
   re-raising such an error, or letting one escape into a traceback, would put a
   live slug on the terminal from the one code path guaranteed to be noisy. So
   every database error out of the INSERT is converted to a ``SeedTripError``
   carrying only the constraint name and SQLSTATE, raised ``from None`` so that
   neither ``__cause__`` nor ``__context__`` can print the original.

``_print_result`` — one write to stdout, flushed — is the single deliberate exit
for a slug value.

**It refuses to run twice.** If ``trips`` holds any row, the insert does not
happen. There is one trip; a second run would either mean the first one's
output was lost — which this script cannot reprint, but which a
``SELECT rider_slug, viewer_slug FROM trips`` recovers, and the refusal says so
rather than leaving the operator to conclude the only fix is to delete the row —
or that someone is re-running a command they already ran. The existing trip is reported
by id/name/start_date so the operator can see *what* is there, and deliberately
without its slugs: the guard query does not even select those columns, so they
are never read into this process.

**Two constraint failures, handled in opposite directions.** Both are bug
signals rather than routine conditions (``docs/decision-log.md`` Entry 3), but
they signal different bugs:

- ``trips_rider_slug_key`` / ``trips_viewer_slug_key`` (UNIQUE) — a freshly
  generated 256-bit token collided with a stored one. Regenerate **both** slugs
  and retry, at most ``_MAX_INSERT_ATTEMPTS`` times. Repeated collisions are not
  a transient condition to keep retrying through; they mean the RNG is not
  producing what it claims to, and the script stops and says so.
- ``trips_slugs_differ_check`` (CHECK, ``migrations/0002_*.sql``) — the same
  value was written to both columns. Nothing random caused that: it means *this
  script* generated one slug and assigned it twice (``docs/decision-log.md``
  Entry 8, Amendment, which names this file as exactly where that mistake gets
  made). Retrying would re-run the defect, so this does **not** retry — it fails
  immediately, naming the constraint. The row it would have written is a
  privilege escalation: a link handed out as read-only resolves to RIDER.

**No ``ON CONFLICT``, in any form, on any column** (``docs/decision-log.md``
Entry 3). ``DO UPDATE`` would silently overwrite an existing trip's slug —
precisely the authorization leak the UNIQUE constraints exist to prevent — and
``DO NOTHING`` would report success having inserted nothing. With
cryptographically random tokens, a conflict is a bug to surface, not a
condition to absorb. Let the insert fail.

SQLAlchemy Core against ``app/data/tables.py``, per that module's "Core, not
ORM" note.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.sql.dml import Insert

from app.core.config import get_settings
from app.data.db import normalize_database_url
from app.data.tables import trip_members, trips, users
from app.models.member import MemberRole

# Bounded because a UNIQUE collision on a 256-bit token is not a condition that
# clears itself — see the module docstring. Three attempts distinguishes "an
# astronomically improbable thing happened once" from "the RNG is broken"
# without ever becoming a retry loop.
_MAX_INSERT_ATTEMPTS = 3

# Postgres SQLSTATEs, used to classify a failure without reading its message.
_UNIQUE_VIOLATION = "23505"
_CHECK_VIOLATION = "23514"
_UNDEFINED_TABLE = "42P01"

# Printed whenever the operator is left without the slugs in front of them: the
# already-exists refusal, and a stdout write that failed after the commit.
#
# It exists because "the slugs exist nowhere but the database" is true and
# dangerously incomplete. An operator who reads only that concludes they are
# gone, and the obvious next move - DELETE FROM trips and seed again - destroys
# the trip (and, via ON DELETE CASCADE, every stop, photo and bike scoped to it)
# to recover something a plain SELECT already returns. There is no in-app read
# path to point at instead: ``TripOut`` has no slug field and
# ``app/data/repositories/trips.py`` only looks a trip up *by* a slug you must
# already have. So the recovery path is the query itself, spelled out.
_SLUG_RECOVERY_INSTRUCTIONS = (
    "To recover them, read them straight out of the row - do NOT delete the trip\n"
    "and seed again, which would destroy the trip and everything scoped to it:\n"
    "\n"
    "    SELECT rider_slug, viewer_slug FROM trips;\n"
    "\n"
    "  local:  psql \"$DATABASE_URL\" -c 'SELECT rider_slug, viewer_slug FROM trips;'\n"
    "  prod:   run the same query in the Neon console's SQL editor.\n"
    "\n"
    "Nothing in the application will show them: TripOut has no slug field, and the\n"
    "lookup in app/data/repositories/trips.py takes a slug as input rather than\n"
    "returning one. Postgres is the only reader."
)

# Constraint names from migrations/0001_initial_schema.sql and
# migrations/0002_trips_slugs_differ_check.sql.
_SLUGS_DIFFER_CONSTRAINT = "trips_slugs_differ_check"
_SLUG_UNIQUE_CONSTRAINTS = frozenset({"trips_rider_slug_key", "trips_viewer_slug_key"})


class SeedTripError(Exception):
    """
    Base for every way seeding can refuse or fail.

    Its message is always built from values this module chose — constraint
    names, SQLSTATEs, the existing trip's id — and never from a database
    exception's text, which would carry the bound slug parameters (module
    docstring, rule 3). ``main()`` prints these messages directly.
    """


class TripAlreadyExistsError(SeedTripError):
    """
    ``trips`` already holds a row, so nothing was inserted.

    Carries the existing trip's identity but **not its slugs** — they are not
    selected by the guard query at all, so there is no attribute on this object
    that could leak one into a printed message or a traceback.
    """

    def __init__(self, *, trip_id: str, name: str, start_date: date) -> None:
        self.trip_id = trip_id
        self.name = name
        self.start_date = start_date
        super().__init__(
            f"trips already holds a row (id {trip_id}) - refusing to seed a second trip."
        )


class DatabaseNotReadyError(SeedTripError):
    """
    The already-exists guard query itself failed, so nothing was even attempted.

    Overwhelmingly this is SQLSTATE ``42P01`` — ``trips`` does not exist because
    the migrations have not been applied — which is the single most likely way a
    real run of this script fails: someone runs it before ``app.data.migrate``.
    Left unhandled that produced a raw ``ProgrammingError`` traceback, which is
    the least legible response this module gives to its most common failure.

    No slug leak is possible here regardless of what went wrong: this is raised
    from *before* the generation loop, so no slug exists yet, and the message is
    still built only from the exception's class name and SQLSTATE attribute
    (module docstring, rule 3) rather than its text.
    """

    def __init__(self, *, error_type: str, sqlstate: str | None) -> None:
        self.error_type = error_type
        self.sqlstate = sqlstate
        if sqlstate == _UNDEFINED_TABLE:
            detail = (
                f"the trips table does not exist (SQLSTATE {_UNDEFINED_TABLE}). The migrations "
                "have not been applied to this database - run `uv run python -m app.data.migrate` "
                "from backend/, then run this again."
            )
        else:
            detail = (
                f"the trips table could not be read ({error_type}, SQLSTATE "
                f"{sqlstate or 'unknown'}). The error text is deliberately not reproduced here."
            )
        super().__init__(
            f"{detail} Nothing was inserted and no slugs were generated, so running this again "
            "once the database is reachable costs nothing."
        )


class UnknownLeaderError(SeedTripError):
    """``--leader-username`` names no account, so nothing was inserted."""

    def __init__(self, username: str) -> None:
        self.username = username
        super().__init__(
            f"no account with username {username!r} for --leader-username. Nothing was "
            "inserted and no slugs were generated."
        )


class SlugsNotDistinctError(SeedTripError):
    """
    The CHECK constraint rejected the insert: both slug columns got one value.

    A defect in this script, not a random event, and therefore never retried —
    see the module docstring.
    """

    def __init__(self, constraint: str) -> None:
        self.constraint = constraint
        super().__init__(
            f"{constraint} rejected the insert: rider_slug and viewer_slug were the same "
            "string. That is a bug in this script - one generated slug assigned to both "
            "columns - not a random collision, so it is not retried. Such a row would make "
            "the read-only viewer link grant rider (write) access. Nothing was inserted."
        )


class SlugCollisionError(SeedTripError):
    """
    Every attempt hit a UNIQUE violation on a slug column.

    ``_MAX_INSERT_ATTEMPTS`` independent 256-bit tokens colliding with stored
    values is not a transient condition — it means the RNG is not random.
    """

    def __init__(self, attempts: int, constraint: str | None) -> None:
        self.attempts = attempts
        self.constraint = constraint
        named = constraint or "a UNIQUE constraint on a slug column"
        super().__init__(
            f"{named} rejected {attempts} independently generated slug pairs. Random "
            "256-bit tokens do not collide; this indicates a broken or reseeded RNG, not "
            "a transient condition that retrying will clear. Nothing was inserted - "
            "investigate the random source before running this again."
        )


@dataclass(frozen=True, slots=True)
class SeededTripResult:
    """
    The trip row this script just wrote, including both slugs.

    Frozen because it is a record of what landed in the database: a caller that
    could edit a slug here would be holding a value the database never had.
    Deliberately not a Pydantic model — ``TripOut`` has no slug fields and these
    values must never be serialisable into an API response.
    """

    id: str
    name: str
    start_date: date
    rider_slug: str
    viewer_slug: str


def format_slug_output(result: SeededTripResult) -> str:
    """
    The block of text printed after a successful seed.

    Pure: no database, no I/O, no clock, no randomness. It only formats what it
    is given, which is what makes it testable without a live trip — and keeps
    the decision of *when* a slug reaches a terminal in exactly one place
    (``main()``).
    """
    return "\n".join(
        [
            "Trip seeded. Save both links now - this output is the only copy.",
            "",
            f"  trip id:     {result.id}",
            f"  name:        {result.name}",
            f"  start date:  {result.start_date.isoformat()}",
            "",
            f"  rider slug:  {result.rider_slug}    (read + write)",
            f"  viewer slug: {result.viewer_slug}    (read only)",
            "",
            "This is the only copy of these slugs. They are stored only in Postgres,",
            "nothing logs them, no endpoint returns them, and there is no rotation or",
            "recovery flow - if this output is lost the trips row is all that is left.",
        ]
    )


def _classify(exc: SQLAlchemyError) -> tuple[str | None, str | None]:
    """
    ``(constraint name, SQLSTATE)`` for a failed statement, read off exception
    *attributes* — never parsed out of an exception's text.

    asyncpg's errors carry ``constraint_name`` and ``sqlstate`` as attributes;
    SQLAlchemy's asyncpg dialect re-raises a DBAPI-shaped error ``from`` the
    original (copying ``sqlstate``/``pgcode`` onto it), so the asyncpg exception
    is reachable through ``__cause__``. Walking that chain for attributes is
    what keeps this module away from ``str(exc)``, which renders the INSERT's
    bound parameters — the slugs — into text (module docstring, rule 3).

    Either half may be ``None`` if the driver did not supply it; callers must
    treat that as "unknown", never as a match.

    Takes any ``SQLAlchemyError``, not just an ``IntegrityError``: the guard
    query and the non-integrity INSERT failures want the same leak-free
    SQLSTATE, and ``orig`` is read with ``getattr`` because only
    ``StatementError`` and its subclasses carry one.
    """
    constraint: str | None = None
    sqlstate: str | None = None
    candidate: BaseException | None = getattr(exc, "orig", None)
    while candidate is not None:
        if constraint is None:
            value = getattr(candidate, "constraint_name", None)
            if isinstance(value, str) and value:
                constraint = value
        if sqlstate is None:
            for attribute in ("sqlstate", "pgcode"):
                value = getattr(candidate, attribute, None)
                if isinstance(value, str) and value:
                    sqlstate = value
                    break
        candidate = candidate.__cause__
    return constraint, sqlstate


def _trip_insert(
    *,
    trip_id: str,
    name: str,
    start_date: date,
    rider_slug: str,
    viewer_slug: str,
) -> Insert:
    """
    The INSERT this script runs: a **plain** one, with no ``ON CONFLICT``.

    Split out as its own function so the statement can be inspected without a
    database. A conflict is unreachable in a test — the already-exists guard
    means the insert only ever runs against an empty table — so "there is no
    ``ON CONFLICT`` clause here" is not a property any behavioural test can
    observe, and this is the shape that lets ``tests/test_seed_trip.py`` assert
    it directly instead. ``docs/decision-log.md`` Entry 3 forbids the clause
    outright; without this, nothing in the suite would notice it appearing.
    """
    return trips.insert().values(
        id=trip_id,
        name=name,
        rider_slug=rider_slug,
        viewer_slug=viewer_slug,
        start_date=start_date,
    )


async def _existing_trip(conn: AsyncConnection) -> tuple[str, str, date] | None:
    """
    The trip already in the table, as ``(id, name, start_date)``, or ``None``.

    The slug columns are **not** selected. This is the single reason it is safe
    for ``main()`` to report the existing trip at all: a value that never enters
    the process cannot be printed by mistake later.
    """
    row = (
        await conn.execute(select(trips.c.id, trips.c.name, trips.c.start_date).limit(1))
    ).first()
    if row is None:
        return None
    return row.id, row.name, row.start_date


async def seed_trip(
    conn: AsyncConnection,
    *,
    name: str,
    start_date: date,
    leader_username: str | None = None,
) -> SeededTripResult:
    """
    Insert the one trip row with two freshly generated slugs and return it.

    With ``leader_username``, the named account also gets an active leader
    membership, inserted in the same transaction as the trip, so neither exists
    without the other. The account is looked up before any slug is generated;
    an unknown one raises ``UnknownLeaderError`` with nothing inserted.

    Takes an **injected connection** and never builds an engine: the caller owns
    the transaction, so the test suite can run a real seed and roll it back, and
    ``main()`` can commit exactly once. A function that made its own engine could
    only ever be run for real.

    Raises ``TripAlreadyExistsError`` (inserting nothing) if the table is not
    empty, ``SlugsNotDistinctError`` on the CHECK constraint, and
    ``SlugCollisionError`` after ``_MAX_INSERT_ATTEMPTS`` UNIQUE violations —
    see the module docstring for why those two are handled in opposite
    directions.
    """
    # The SELECT-then-INSERT below is not atomic, and at READ COMMITTED two
    # overlapping runs can both pass this guard and both insert. Accepted
    # deliberately, not overlooked: this is a one-shot script run by one human
    # from one terminal, racing it takes effort, and the real protection is the
    # UNIQUE/CHECK constraints plus the operator seeing two slug blocks. No
    # locking here on purpose - please do not add `FOR UPDATE`/advisory locks to
    # "fix" it.
    try:
        existing = await _existing_trip(conn)
    except SQLAlchemyError as exc:
        # Before any slug is generated, so nothing sensitive exists to leak -
        # but still no str(exc), because rule 3 is a property of this module and
        # not a judgement call made per call site.
        _, sqlstate = _classify(exc)
        raise DatabaseNotReadyError(error_type=type(exc).__name__, sqlstate=sqlstate) from None

    if existing is not None:
        trip_id, existing_name, existing_start_date = existing
        raise TripAlreadyExistsError(
            trip_id=trip_id,
            name=existing_name,
            start_date=existing_start_date,
        )

    leader_id = None
    if leader_username is not None:
        leader_id = await _leader_id(conn, leader_username)

    last_unique_constraint: str | None = None

    for _attempt in range(_MAX_INSERT_ATTEMPTS):
        # Server-side id. Client-generated ids are an offline-queue rule for
        # stops, photos and bikes (docs/api-contract.md, "Idempotency"): those
        # are written from a phone that may replay a queued request and needs
        # the replay to be recognisable. A trip has no client and no queue - it
        # is created here, once - so there is nothing to make idempotent and no
        # reason to accept an id from outside.
        trip_id = str(uuid.uuid4())

        # Two independent calls, never one value used twice - the CHECK
        # constraint exists because that specific mistake lands here.
        #
        # 32 bytes = 256 bits, giving a 43-character URL-safe token. Longer than
        # the 16 the test fixture uses, on purpose: these are permanent
        # credentials. They are shared in a URL, they never expire, there is no
        # rotation, no account to lock and no rate limiting in front of the
        # endpoints they open. A fixture slug lives for one test and guards
        # nothing.
        rider_slug = secrets.token_urlsafe(32)
        viewer_slug = secrets.token_urlsafe(32)

        try:
            # A SAVEPOINT, so a rejected attempt does not poison the caller's
            # transaction: after any error Postgres refuses every further
            # statement until the failed one is rolled back, and rolling back
            # the whole transaction would throw away the caller's context and
            # make retrying impossible.
            async with conn.begin_nested():
                # A plain INSERT. No ON CONFLICT of any kind - see _trip_insert,
                # the module docstring and docs/decision-log.md Entry 3.
                await conn.execute(
                    _trip_insert(
                        trip_id=trip_id,
                        name=name,
                        start_date=start_date,
                        rider_slug=rider_slug,
                        viewer_slug=viewer_slug,
                    )
                )
        except IntegrityError as exc:
            constraint, sqlstate = _classify(exc)

            # `from None` on every raise below: chaining would keep the
            # IntegrityError in `__cause__`/`__context__`, and printing a
            # traceback calls str() on it - which renders the bound parameters,
            # i.e. the slugs (module docstring, rule 3).
            if constraint == _SLUGS_DIFFER_CONSTRAINT or sqlstate == _CHECK_VIOLATION:
                raise SlugsNotDistinctError(constraint or _SLUGS_DIFFER_CONSTRAINT) from None

            if sqlstate == _UNIQUE_VIOLATION and (
                constraint is None or constraint in _SLUG_UNIQUE_CONSTRAINTS
            ):
                last_unique_constraint = constraint
                continue

            raise SeedTripError(
                f"the INSERT into trips was rejected by {constraint or 'an unknown constraint'}"
                f" (SQLSTATE {sqlstate or 'unknown'}). Nothing was inserted."
            ) from None
        except SQLAlchemyError as exc:
            # Same leak boundary, wider net: any StatementError renders the
            # bound parameters when it is printed, so no database exception is
            # allowed to escape this function. Only the class name and the
            # SQLSTATE survive - and the SQLSTATE is what makes the message
            # actionable, since `DataError`/`ProgrammingError` alone cannot tell
            # "the migrations were never applied" (42P01) from "the schema
            # drifted" (42703) from "the driver broke". It is read off an
            # exception *attribute*, exactly as _classify does for the
            # IntegrityError branch, so it costs nothing and leaks nothing.
            _, sqlstate = _classify(exc)
            raise SeedTripError(
                f"the INSERT into trips failed with {type(exc).__name__} (SQLSTATE "
                f"{sqlstate or 'unknown'}). The error text is deliberately not reproduced "
                "here because it would contain the generated slugs. Nothing was inserted."
            ) from None

        if leader_id is not None:
            await _insert_leader(conn, trip_id=trip_id, user_id=leader_id)

        return SeededTripResult(
            id=trip_id,
            name=name,
            start_date=start_date,
            rider_slug=rider_slug,
            viewer_slug=viewer_slug,
        )

    raise SlugCollisionError(_MAX_INSERT_ATTEMPTS, last_unique_constraint)


async def _leader_id(conn: AsyncConnection, username: str) -> str:
    """The id of the ``--leader-username`` account; ``UnknownLeaderError`` if there is none."""
    # Stored usernames are lowercase ASCII; non-ASCII input names no account.
    try:
        user_id = (
            None
            if not username.isascii()
            else await conn.scalar(select(users.c.id).where(users.c.username == username.lower()))
        )
    except SQLAlchemyError as exc:
        # No slug exists yet, but rule 3 holds module-wide: no str(exc).
        _, sqlstate = _classify(exc)
        raise SeedTripError(
            f"the leader account could not be looked up ({type(exc).__name__}, SQLSTATE "
            f"{sqlstate or 'unknown'}). Nothing was inserted."
        ) from None
    if user_id is None:
        raise UnknownLeaderError(username)
    return user_id


async def _insert_leader(conn: AsyncConnection, *, trip_id: str, user_id: str) -> None:
    """Insert the new trip's active leader membership, in the caller's transaction."""
    try:
        await conn.execute(
            trip_members.insert().values(
                id=str(uuid.uuid4()),
                trip_id=trip_id,
                user_id=user_id,
                role=MemberRole.LEADER.value,
            )
        )
    except SQLAlchemyError as exc:
        # Rule 3 again: the caller's transaction holds the slugs, and this
        # failure aborts it, so the trip is not inserted either.
        _, sqlstate = _classify(exc)
        raise SeedTripError(
            f"the leader membership insert failed with {type(exc).__name__} (SQLSTATE "
            f"{sqlstate or 'unknown'}). Nothing was inserted."
        ) from None


def _print_result(result: SeededTripResult) -> int:
    """
    Write the slug block to stdout and report whether it actually landed.

    Returns the process exit code: ``0`` if the operator now has the slugs,
    ``1`` if the write failed. This is the only place a slug is ever written
    anywhere; ``main()`` chooses it and passes it in, so the decision of *when*
    a slug reaches a terminal is still made in exactly one place.

    Two failures are handled here, both of which happen **after the commit**,
    which is the worst position this script can be in — the trip exists and its
    only credentials are about to be lost:

    - **The write itself can fail.** A full disk (``ENOSPC``), a closed pipe
      (``| head``), a detached terminal. Unhandled that is a traceback with an
      exit code and no slugs. Caught, it can at least answer with the one thing
      that still helps: the SELECT that reads the slugs back out of the row.
      That fallback goes to **stderr**, a different file descriptor, which is
      routinely still writable when stdout is not (``> full-disk/slugs.txt``,
      or a redirect the operator aimed somewhere unfortunate).
    - **A successful write is not a durable one.** At a terminal stdout is
      line-buffered, but under a redirect ``line_buffering`` is False and
      ``print()`` returning only means the text reached a buffer inside this
      process. The explicit ``flush()`` is what makes "the call returned" mean
      "the bytes left this process", so a kill between here and interpreter exit
      cannot swallow them.
    """
    try:
        sys.stdout.write(format_slug_output(result) + "\n")
        sys.stdout.flush()
    except OSError as exc:
        # `exc` is an OS error about a stream, not a SQLAlchemy one - it has no
        # bound parameters and cannot carry a slug, so rule 3 does not apply to
        # it. Only `strerror` is used regardless, which is the part an operator
        # needs ("No space left on device").
        with suppress(OSError):
            print(
                "The trip was seeded and committed, but writing its slugs to stdout failed "
                f"({exc.strerror or type(exc).__name__}). They were not printed.",
                file=sys.stderr,
            )
            print(_SLUG_RECOVERY_INSTRUCTIONS, file=sys.stderr)
            sys.stderr.flush()
        return 1
    return 0


async def _seed_with_new_engine(
    *,
    name: str,
    start_date: date,
    emit: Callable[[SeededTripResult], int],
    leader_username: str | None = None,
) -> int:
    """
    Build an engine for this one call, seed in a single transaction, commit,
    hand the result to ``emit``, and only then dispose. Returns ``emit``'s exit
    code.

    Its own engine rather than ``app.data.db.engine`` because that one is
    created at import time and its pooled asyncpg connections belong to whatever
    event loop first checks one out (see the comment in ``app/data/db.py``).
    ``asyncio.run()`` here makes a fresh loop, so the engine is built inside it
    and disposed before it closes — the same pattern ``app/data/migrate.py`` and
    ``tests/conftest.py`` use.

    **Why ``emit`` is called in here rather than by ``main()`` afterwards.**
    Between the commit and the print is the only window in which the slugs exist
    solely in this process's memory, and it used to contain ``engine.dispose()``
    — several milliseconds of real network I/O, on connections that may already
    be dead, idle-timed-out by Neon, or on a dropped socket. A ``dispose()``
    that raised would propagate *instead of* the returned result: the trip
    committed, the slugs unrecoverable, and the operator shown an ``OSError``
    about a connection. Printing first removes the I/O from that window
    entirely, and the ``finally`` below removes its ability to fail the run.
    """
    engine = create_async_engine(normalize_database_url(get_settings().database_url))
    try:
        async with engine.begin() as conn:
            result = await seed_trip(
                conn, name=name, start_date=start_date, leader_username=leader_username
            )

        # Committed by the context manager above, and only then printed: slugs
        # that reached the terminal from a transaction that then rolled back
        # would be credentials for a trip that does not exist. Commit-before-
        # print is the correct order; the fix for the window is to put nothing
        # else in it, not to print first.
        return emit(result)
    finally:
        # Never allowed to fail the run. By the time this executes the work is
        # either committed and printed, or already propagating a better error,
        # so a dispose failure could only replace a message that matters with
        # one that does not — and in the committed case it would be destroying
        # the only copy of the slugs to report a closed socket. Whatever is
        # still open is closed by the process exiting anyway.
        with suppress(Exception):
            await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """
    CLI entry point. Returns 0 on a successful seed, 1 on a refusal or failure.

    Arguments are parsed before anything touches the database, so ``--help`` and
    a bad argument cost nothing and connect to nothing.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.data.seed_trip",
        description=(
            "Seed the single trip row and print its two slugs once. Run this from backend/ "
            "after the migrations have been applied. It refuses to run if a trip already "
            "exists."
        ),
        epilog=(
            "The slugs printed are the entire access model for the trip: the rider slug "
            "grants read and write, the viewer slug read only. They are shown exactly once "
            "and cannot be rotated. Save both before closing the terminal - if that output "
            "is lost, the only way to read them back is "
            "SELECT rider_slug, viewer_slug FROM trips."
        ),
    )
    parser.add_argument(
        "--name",
        required=True,
        help='The trip\'s display name, e.g. "Alps 2026". Shown in the app as TripOut.name.',
    )
    parser.add_argument(
        "--start-date",
        required=True,
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="The day the trip starts, as an ISO date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--leader-username",
        default=None,
        help=(
            "Optional. An existing account to make the trip's first active leader, "
            "in the same transaction as the trip. Without it no membership is created."
        ),
    )
    args = parser.parse_args(argv)

    try:
        return asyncio.run(
            _seed_with_new_engine(
                name=args.name,
                start_date=args.start_date,
                emit=_print_result,
                leader_username=args.leader_username,
            )
        )
    except TripAlreadyExistsError as exc:
        print("Refusing to seed: the trips table already holds a row. Nothing was inserted.")
        print(f"  trip id:     {exc.trip_id}")
        print(f"  name:        {exc.name}")
        print(f"  start date:  {exc.start_date.isoformat()}")
        # The recovery path is spelled out rather than left implied. An operator
        # who lost the original output is exactly the operator most likely to be
        # re-running this command, and "they exist nowhere but the database"
        # reads to them as "gone" - whose obvious next move is to delete the row
        # and seed again, destroying the trip to recover what a SELECT returns.
        print(
            "That trip's slugs are not shown here - they were printed once when it was "
            "seeded, and Postgres is now the only place they exist."
        )
        print(_SLUG_RECOVERY_INSTRUCTIONS)
        return 1
    except SeedTripError as exc:
        print(f"Seeding failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
