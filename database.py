import logging

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config import config
from models import Base

log = logging.getLogger(__name__)

# Postgres-only engine — prod is on postgresql+psycopg. SQLite leftovers
# (NullPool, file perms, check_same_thread) removed for simplicity.
_kwargs: dict = {
    "echo": False,
    "pool_size": 20,
    "max_overflow": 30,
    "pool_pre_ping": True,
    "pool_recycle": 3600,
}

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
            _add_col(
                "ALTER TABLE orders ADD COLUMN panel_email VARCHAR(128)", "Added orders.panel_email"
            )
        if "sub_id" not in cols:
            _add_col("ALTER TABLE orders ADD COLUMN sub_id VARCHAR(64)", "Added orders.sub_id")
        if "renew_email" not in cols:
            _add_col(
                "ALTER TABLE orders ADD COLUMN renew_email VARCHAR(128)", "Added orders.renew_email"
            )
        if "payment_authority" not in cols:
            _add_col(
                "ALTER TABLE orders ADD COLUMN payment_authority VARCHAR(64)",
                "Added orders.payment_authority",
            )
        if "payment_ref_id" not in cols:
            _add_col(
                "ALTER TABLE orders ADD COLUMN payment_ref_id VARCHAR(32)",
                "Added orders.payment_ref_id",
            )
        if "discount_code" not in cols:
            _add_col(
                "ALTER TABLE orders ADD COLUMN discount_code VARCHAR(32)",
                "Added orders.discount_code",
            )
        if "discount_percent" not in cols:
            _add_col(
                "ALTER TABLE orders ADD COLUMN discount_percent INTEGER",
                "Added orders.discount_percent",
            )
        if "original_amount_toomans" not in cols:
            _add_col(
                "ALTER TABLE orders ADD COLUMN original_amount_toomans INTEGER",
                "Added orders.original_amount_toomans",
            )
        if "discount_code_id" not in cols:
            _add_col(
                "ALTER TABLE orders ADD COLUMN discount_code_id INTEGER REFERENCES discount_codes(id) ON DELETE SET NULL",
                "Added orders.discount_code_id",
            )

    # Telegram user IDs exceed INTEGER range (~2.1e9) — ensure BIGINT.
    def _alter_bigint(sql, msg):
        try:
            sync_conn.exec_driver_sql(sql)
            log.info(msg)
        except Exception:
            log.warning("Migration step failed: %s", msg, exc_info=True)

    if inspector.has_table("users"):
        _alter_bigint(
            "ALTER TABLE users ALTER COLUMN telegram_id TYPE BIGINT USING telegram_id::bigint",
            "Migrated users.telegram_id -> BIGINT",
        )
    if inspector.has_table("discount_codes"):
        _alter_bigint(
            "ALTER TABLE discount_codes ALTER COLUMN used_by_telegram_id TYPE BIGINT USING used_by_telegram_id::bigint",
            "Migrated discount_codes.used_by_telegram_id -> BIGINT",
        )
