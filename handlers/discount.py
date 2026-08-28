import logging
import secrets
import string

from sqlalchemy import select
from sqlalchemy import update as sa_update
from telegram import Update
from telegram.ext import ContextTypes

from database import async_session
from models import DiscountCode, Order

log = logging.getLogger(__name__)

CODE_ALPHABET = string.ascii_uppercase + string.digits
CODE_LEN = 8  # fallback length when no prefix is supplied
CODE_SUFFIX_LEN = 6  # random part appended after an optional admin prefix
PREFIX_MAX_LEN = 12  # admin-supplied prefix is truncated to this

def _sanitize_prefix(prefix: str | None) -> str:
    """Normalize an admin prefix: uppercase alnum + underscore, truncated."""
    if not prefix:
        return ""
    out = "".join(ch for ch in str(prefix).strip().upper() if ch.isalnum() or ch == "_")
    return out[:PREFIX_MAX_LEN]

def _generate_code() -> str:
    # Legacy 8-char format used when no prefix is supplied
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(CODE_LEN))

def _generate_suffix() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(CODE_SUFFIX_LEN))

async def generate_unique_code(session, discount_percent: int, prefix: str | None = None) -> DiscountCode:
    norm_prefix = _sanitize_prefix(prefix)
    for _ in range(30):
        suffix = _generate_suffix()
        # Without a prefix we keep the legacy 8-char format for familiarity.
        code = (norm_prefix + suffix) if norm_prefix else _generate_code()
        # check collision
        exists = (await session.execute(select(DiscountCode).where(DiscountCode.code == code))).scalar_one_or_none()
        if exists is None:
            dc = DiscountCode(code=code, discount_percent=discount_percent)
            session.add(dc)
            await session.commit()
            await session.refresh(dc)
            return dc
    raise RuntimeError("Could not generate unique discount code after retries")

def calc_discounted_amount(original: int, pct: int) -> int:
    try:
        pct = int(pct)
    except Exception:
        pct = 0
    pct = max(0, min(100, pct))
    if pct == 0:
        return int(original)
    discounted = int(original) * (100 - pct) // 100
    # round down to 0 if 100%, otherwise at least 1? allow 0 for free
    if pct == 100:
        return 0
    return max(1, discounted)

async def validate_discount_code(session, code_str: str) -> DiscountCode | None:
    code_str = (code_str or "").strip().upper()
    if not code_str:
        return None
    # allow with or without dash? normalize
    code_str = code_str.replace("-", "").replace(" ", "")
    result = await session.execute(select(DiscountCode).where(DiscountCode.code == code_str))
    dc = result.scalar_one_or_none()
    if dc is None:
        return None
    if dc.is_used:
        return None
    return dc

async def consume_discount_code(session, dc: DiscountCode, telegram_id: int, order_id: int | None = None) -> None:
    """Atomically claim a discount code (fails if already used)."""
    from datetime import datetime

    try:
        from datetime import UTC
    except ImportError:
        from datetime import timezone

        UTC = timezone.utc  # type: ignore[no-redef]  # noqa: UP017
    # Atomic UPDATE ... WHERE is_used = false — prevents double-spend under concurrent use
    result = await session.execute(
        sa_update(DiscountCode)
        .where(DiscountCode.id == dc.id, DiscountCode.is_used == False)  # noqa: E712
        .values(
            is_used=True,
            used_at=datetime.now(UTC),
            used_by_telegram_id=telegram_id,
            used_order_id=order_id,
        )
    )
    code_str = dc.code  # capture before possible expire on rollback
    if result.rowcount == 0:
        # Already claimed by another transaction — refresh state for caller
        await session.rollback()
        raise RuntimeError(f"Discount code {code_str} already used")
    await session.commit()
    # Keep ORM object in sync (session is expire_on_commit=False, but be defensive)
    try:
        dc.is_used = True
        dc.used_at = datetime.now(UTC)
        dc.used_by_telegram_id = telegram_id
        if order_id is not None:
            dc.used_order_id = order_id
    except Exception:
        pass

async def release_discount_code_by_order(session, order: Order) -> None:
    """If order had a discount code and is cancelled/expired, free the code for reuse."""
    if not order.discount_code_id and not order.discount_code:
        return
    dc = None
    if order.discount_code_id:
        result = await session.execute(select(DiscountCode).where(DiscountCode.id == order.discount_code_id))
        dc = result.scalar_one_or_none()
    elif order.discount_code:
        code = (order.discount_code or "").strip().upper().replace("-", "").replace(" ", "")
        result = await session.execute(select(DiscountCode).where(DiscountCode.code == code))
        dc = result.scalar_one_or_none()
    # Relaxed guard: allow release when id matches, or when code is bound to this order,
    # or for legacy rows where discount_code_id is NULL but code string matches.
    should_release = False
    if dc and dc.is_used:
        if dc.id == order.discount_code_id:
            should_release = True
        elif dc.used_order_id == order.id:
            should_release = True
        elif order.discount_code and dc.code == (order.discount_code or "").strip().upper().replace("-", "").replace(" ", ""):
            should_release = True
    if should_release:
        dc.is_used = False
        dc.used_at = None
        dc.used_by_telegram_id = None
        dc.used_order_id = None
        try:
            await session.commit()
            log.info("Released discount code %s from cancelled order #%s", dc.code, order.id)
        except Exception:
            log.warning("Failed to release discount code for order #%s", order.id, exc_info=True)
            try:
                await session.rollback()
            except Exception:
                pass

async def release_discount_codes_for_cancelled_orders(session, cancelled_ids: list[int]) -> None:
    if not cancelled_ids:
        return
    # Find orders with discount
    result = await session.execute(select(Order).where(Order.id.in_(cancelled_ids)))
    orders = result.scalars().all()
    for o in orders:
        await release_discount_code_by_order(session, o)
