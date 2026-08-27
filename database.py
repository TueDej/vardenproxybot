import contextlib
import logging
import os

from sqlalchemy import inspect
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config import config
from models import Base

log = logging.getLogger(__name__)

# Engine creation — NullPool + check_same_thread only for sqlite to allow
# concurrent_updates=True without pooling; postgres uses default AsyncAdaptedQueuePool
_kwargs: dict = {"echo": False}
if config.database_url.startswith("sqlite"):
    _kwargs["poolclass"] = NullPool
    _kwargs["connect_args"] = {"check_same_thread": False}
elif config.database_url.startswith("postgresql"):
    # Postgres: sane pool defaults for 256 concurrent telegram updates
    # SQLAlchemy's default pool_size=5 is fine for low concurrency; bump for prod.
    _kwargs["pool_size"] = 10
    _kwargs["max_overflow"] = 20
    _kwargs["pool_pre_ping"] = True
    _kwargs["pool_recycle"] = 3600

engine = create_async_engine(config.database_url, **_kwargs)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        try:
            await conn.run_sync(_migrate_sync)
        except Exception:
            log.warning("Migration failed; continuing", exc_info=True)

    # sqlite file perms: ensure 600 (no-op for postgres)
    if config.database_url.startswith("sqlite"):
        with contextlib.suppress(Exception):
            try:
                url = make_url(config.database_url)
                db_path = url.database or ""
                if db_path == ":memory:":
                    db_path = ""
            except Exception:
                db_path = config.database_url.split(":///")[-1].split("?")[0]

            if db_path and not db_path.startswith(":memory:"):
                if os.path.exists(db_path):
                    with contextlib.suppress(Exception):
                        os.chmod(db_path, 0o600)
                for suffix in ("-wal", "-shm"):
                    p = db_path + suffix
                    if os.path.exists(p):
                        with contextlib.suppress(Exception):
                            os.chmod(p, 0o600)


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

    # Telegram user IDs now exceed PostgreSQL INTEGER range (~2.1e9). Promote the
    # telegram_id columns to BIGINT so lookups/inserts for real users don't fail
    # with "integer out of range". SQLite stores integers as 64-bit already, so
    # this is Postgres-only (and the USING clause keeps existing rows intact).
    if config.database_url.startswith("postgresql"):
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
