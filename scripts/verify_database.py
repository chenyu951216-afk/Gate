import asyncio

from app.config import get_settings
from app.database.session import create_database, initialize_database


async def main() -> None:
    engine, _ = create_database(get_settings().database_url)
    if engine is None:
        print({"mode": "memory", "persistent": False})
        return
    await initialize_database(engine)
    await engine.dispose()
    print({"mode": "postgresql", "persistent": True})


if __name__ == "__main__":
    asyncio.run(main())

