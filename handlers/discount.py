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
CODE_LEN = 8  # e.g. 8 chars like 1A2B3C4D

def _generate_code() -> str:
    # Avoid confusing chars: remove O,0,I,1? keep simple for now
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(CODE_LEN))

async def generate_unique_code(session, discount_percent: int) -> DiscountCode:
    for _ in range(20):
        code = _generate_code()
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
    dc.is_used = True
    from datetime import datetime
    try:
        from datetime import UTC
    except ImportError:
        from datetime import timezone
        UTC = timezone.utc
    dc.used_at = datetime.now(UTC)
    dc.used_by_telegram_id = telegram_id
    if order_id is not None:
        dc.used_order_id = order_id
    await session.commit()

async def release_discount_code_by_order(session, order: Order) -> None:
    """If order had a discount code and is cancelled/expired, free the code for reuse."""
    if not order.discount_code_id and not order.discount_code:
        return
    dc = None
    if order.discount_code_id:
        result = await session.execute(select(DiscountCode).where(DiscountCode.id == order.discount_code_id))
        dc = result.scalar_one_or_none()
    elif order.discount_code:
        code = (order.discount_code or "").strip().upper()
        result = await session.execute(select(DiscountCode).where(DiscountCode.code == code))
        dc = result.scalar_one_or_none()
    if dc and dc.is_used and dc.id == order.discount_code_id:
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
