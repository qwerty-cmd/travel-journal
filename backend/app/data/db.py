# Postgres access — the *only* module in this app that knows a database
# exists. Route handlers depend on repositories in data/repositories/, never
# on SQLAlchemy or a connection string directly (spec Section 4, "Portability
# principle"). Same DATABASE_URL interface locally (docker-compose postgres)
# and in prod (Neon) — nothing here is provider-specific.

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


def normalize_database_url(url: str) -> str:
    """
    Force a DATABASE_URL onto the asyncpg driver.

    Both the local docker-compose Postgres and Neon hand out (and .env.example
    carries) a plain ``postgresql://`` URL — the standard libpq form that every
    psql/pgAdmin/Neon console copy-paste produces. SQLAlchemy reads the scheme
    as the driver to use, so ``create_async_engine`` raises
    InvalidRequestError ("The asyncio extension requires an async driver") on
    that URL and nothing can reach the database at all.

    Rewriting it here rather than in the environment keeps the committed
    ``.env.example`` value working unmodified, and keeps the async-driver
    requirement an implementation detail of data/ — which is the only module
    allowed to know what it's talking to (spec Section 4, portability).

    A URL that already names a driver (``postgresql+asyncpg://``) is returned
    untouched, so an explicit choice is never overridden.
    """
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        # Some providers still emit the legacy `postgres://` alias.
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


_settings = get_settings()

# ---------------------------------------------------------------------------
# This engine belongs to ONE event loop — the loop that first checks a
# connection out of its pool.
#
# Creating it at import time opens no connections (verified: a real uvicorn
# process starts and serves fine), so the object itself is loop-agnostic. The
# pooled asyncpg connections underneath it are not: each is bound to the loop
# that created it, and the pool will happily hand one back to a caller running
# on a different loop. The failure is `AttributeError: 'NoneType' object has no
# attribute 'send'`, and it is *intermittent* — the botched checkout invalidates
# and replaces the connection, so the next attempt may succeed and the bug looks
# like a flake rather than a rule.
#
# What is safe, and why nothing today trips it:
#   * uvicorn                — one long-lived loop for the process. Safe.
#   * uvicorn --workers N    — each child process imports this module and builds
#                              its own engine, with an empty pool. Safe.
#   * uvicorn --reload       — the reloader replaces the process. Safe.
#
# What is not safe: touching `async_session` (or `engine`) from a *second* loop
# in the same process. The concrete way that gets introduced here is the
# write-only OneDrive archive sync (`s-photo-upload-onedrive-sync`) if it is
# started as `threading.Thread(target=lambda: asyncio.run(sync()))`, or from a
# sync scheduler that makes its own loop per tick, and then reads the database
# through this sessionmaker. Same for a script that calls `asyncio.run()` twice.
#
# If you need database access from another loop, build a separate engine inside
# that loop and dispose of it before the loop closes (`tests/conftest.py` and
# `app/data/migrate.py` both already do exactly that) — do not share this one.
# ---------------------------------------------------------------------------
#
# `hide_parameters=True`: a trip slug is the credential, and it travels as a
# bound parameter — without this, `StatementError.__str__` appends
# `[parameters: ('<live-slug>', ...)]` and the 500 handler's traceback writes it
# to the log (decision-log Entry 13, `core/errors.py` sink A). Removing it
# re-opens that leak. The exception class, driver message, `[SQL: ...]` and
# traceback all still render, so a 500 stays diagnosable.
# ponytail: engine-wide, so `echo=`/`echo_pool=` debugging loses parameters too;
# upgrade path is a setting if that ever bites — not a config knob today for a
# value that never changes.
engine = create_async_engine(
    normalize_database_url(_settings.database_url), pool_pre_ping=True, hide_parameters=True
)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        yield session


# What this buys, stated honestly: it hides the *import*, not the coupling. The
# value a handler receives is still a SQLAlchemy `AsyncSession` and it is still
# handed straight to a repository — this is not a portability improvement, and
# swapping the database would touch exactly as much code with it as without it.
#
# What it does buy is greppability — one place wires the session callable, so no
# route can quietly depend on a different one — and it makes this module's own
# header true: that header says route handlers never depend on SQLAlchemy
# directly, and until this alias existed `api/routes/trips.py` disproved it by
# importing `AsyncSession` for its dependency annotation.
#
# Only for FastAPI dependency injection. `Depends(...)` means nothing outside a
# request, so a plain function parameter (`_resolve_trip`, and every repository
# function) keeps its bare `AsyncSession` annotation and stays callable from a
# background job or a seed script.
SessionDep = Annotated[AsyncSession, Depends(get_session)]
