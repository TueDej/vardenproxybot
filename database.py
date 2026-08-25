import logging

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config import config
from models import Base

log = logging.getLogger(__name__)

# Engine creation — use NullPool for sqlite to allow concurrent_updates=True
_kwargs: dict = {"echo": False}
if config.database_url.startswith("sqlite"):
    _kwargs["poolclass"] = NullPool
    _kwargs["connect_args"] = {"check_same_thread": False}

engine = create_async_engine(config.database_url, **_kwargs)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        try:
            await conn.run_sync(_migrate_sync)
        except Exception:
            log.warning("Migration failed; continuing", exc_info=True)


async def dispose_engine():
    await engine.dispose()


def _migrate_sync(sync_conn) -> None:
    """Lightweight startup migrations for databases created by older versions."""
    inspector = inspect(sync_conn)

    if inspector.has_table("orders"):
        cols = {c["name"] for c in inspector.get_columns("orders")}

        def _add_col(sql, msg):
            try:
                sync_conn.exec_driver_sql(sql)
                log.info(msg)
            except Exception:
                log.warning("Migration step failed: %s", msg, exc_info=True)

        if "amount_usd" in cols and "amount_toomans" not in cols:
            try:
                sync_conn.exec_driver_sql(
                    "ALTER TABLE orders RENAME COLUMN amount_usd TO amount_toomans"
                )
                log.info("Migrated orders.amount_usd -> orders.amount_toomans")
            except Exception:
                log.warning("Migration amount_usd->amount_toomans failed", exc_info=True)
        if "panel_email" not in cols:
            _add_col("ALTER TABLE orders ADD COLUMN panel_email VARCHAR(128)", "Added orders.panel_email")
        if "sub_id" not in cols:
            _add_col("ALTER TABLE orders ADD COLUMN sub_id VARCHAR(64)", "Added orders.sub_id")
        if "payment_authority" not in cols:
            _add_col("ALTER TABLE orders ADD COLUMN payment_authority VARCHAR(64)", "Added orders.payment_authority")
        if "payment_ref_id" not in cols:
            _add_col("ALTER TABLE orders ADD COLUMN payment_ref_id VARCHAR(32)", "Added orders.payment_ref_id")
