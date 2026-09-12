# Postgres access — the *only* module in this app that knows a database
# exists. Route handlers depend on repositories in data/repositories/, never
# on SQLAlchemy or a connection string directly (spec Section 4, "Portability
# principle"). Same DATABASE_URL interface locally (docker-compose postgres)
# and in prod (Neon) — nothing here is provider-specific.

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_settings = get_settings()
engine = create_async_engine(_settings.database_url, pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        yield session
