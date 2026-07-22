from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base


def create_database(database_url: str | None) -> tuple[AsyncEngine | None, async_sessionmaker[AsyncSession] | None]:
    if not database_url:
        return None, None
    if database_url.startswith("postgres://"):
        database_url = "postgresql+asyncpg://" + database_url[len("postgres://") :]
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+asyncpg://" + database_url[len("postgresql://") :]
    engine = create_async_engine(database_url, pool_pre_ping=True, pool_recycle=1800)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def initialize_database(engine: AsyncEngine | None) -> None:
    if engine is None:
        return
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
