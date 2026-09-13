# Postgres access — the *only* module in this app that knows a database
# exists. Route handlers depend on repositories in data/repositories/, never
# on SQLAlchemy or a connection string directly (spec Section 4, "Portability
# principle"). Same DATABASE_URL interface locally (docker-compose postgres)
# and in prod (Neon) — nothing here is provider-specific.

from collections.abc import AsyncIterator

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
engine = create_async_engine(normalize_database_url(_settings.database_url), pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        yield session
