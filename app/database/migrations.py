from app.database.session import initialize_database


async def ensure_schema(engine):
    await initialize_database(engine)

