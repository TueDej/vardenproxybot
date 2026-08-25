import logging

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config import config
from models import Base

log = logging.getLogger(__name__)

engine = create_async_engine(config.database_url, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_sync)


def _migrate_sync(sync_conn) -> None:
    """Lightweight startup migrations for databases created by older versions."""
    inspector = inspect(sync_conn)

    if inspector.has_table("orders"):
        cols = {c["name"] for c in inspector.get_columns("orders")}
        if "amount_usd" in cols and "amount_toomans" not in cols:
            sync_conn.exec_driver_sql(
                "ALTER TABLE orders RENAME COLUMN amount_usd TO amount_toomans"
            )
            log.info("Migrated orders.amount_usd -> orders.amount_toomans")
        if "panel_email" not in cols:
            sync_conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN panel_email VARCHAR(128)")
            log.info("Added orders.panel_email")
        if "sub_id" not in cols:
            sync_conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN sub_id VARCHAR(64)")
            log.info("Added orders.sub_id")
