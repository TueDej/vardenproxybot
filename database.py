from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config import config
from models import Base

engine = create_async_engine(config.database_url, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)

# Columns added after the initial schema: {table: {column: DDL type}}
_MIGRATIONS = {
    "subscriptions": {
        "xui_email": "VARCHAR(128)",
        "sub_id": "VARCHAR(64)",
    },
}


async def _migrate_sqlite(conn) -> None:
    for table, columns in _MIGRATIONS.items():
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        existing = {row[1] for row in result.fetchall()}
        if not existing:
            continue  # table doesn't exist yet; create_all will handle it
        for column, ddl in columns.items():
            if column not in existing:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


async def init_db():
    async with engine.begin() as conn:
        if config.database_url.startswith("sqlite"):
            await _migrate_sqlite(conn)
        await conn.run_sync(Base.metadata.create_all)


async def get_session():
    async with async_session() as session:
        yield session
