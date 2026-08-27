import logging

from sqlalchemy import select
from sqlalchemy import update as sa_update
from telegram import Update
from telegram.ext import ContextTypes

from database import async_session
from handlers.buy import get_or_create_user, purchase_blocked_reason
from handlers.rate_limit import check_cooldown
from models import Order, User
from packages import DURATION_DAYS, MAX_SUBSCRIPTION_DAYS, load_packages
from vpn_service import VPNPanelError, VPNPanelService
from zarinpal import ZarinpalError

log = logging.getLogger(__name__)

# Maximum total subscription length after renewal
MAX_TOTAL_DAYS = MAX_SUBSCRIPTION_DAYS


async def _resolve_renewal_terms(email: str, telegram_id: int | None = None) -> tuple[str, int, int, int]:
    """Return (package_label, duration_days, data_gb, amount_toomans) for a renewal.

    Prefers the user's last approved order for this client (keeps the price they
    paid). Falls back to the package list priced by the client's current data.
    When telegram_id is supplied the DB lookup is scoped to that user to avoid
    leaking another user's pricing.
    """
    async with async_session() as session:
        q = select(Order).where(Order.panel_email == email, Order.status == "approved")
        if telegram_id is not None:
            q = q.join(User, Order.user_id == User.id).where(User.telegram_id == telegram_id)
        q = q.order_by(Order.created_at.desc()).limit(1)
        result = await session.execute(q)
        orig = result.scalar_one_or_none()
    if orig is not None:
        return orig.package_label, orig.duration_days, orig.data_gb, orig.amount_toomans

    # Fallback: price by the client's current data quota.
    client = await VPNPanelService.get_client(email)
    data_gb = 0
    if client:
        # get_client may return {"client": {...}} wrapper (see vpn_service.extend_client)
        inner = client.get("client") if isinstance(client.get("client"), dict) else client
        # totalGB may be on wrapper or inner
        raw = None
        if isinstance(inner, dict):
            raw = inner.get("totalGB")
        if raw is None and isinstance(client, dict):
            raw = client.get("totalGB")
        try:
            data_gb = int(raw or 0) // (1024**3)
        except (TypeError, ValueError):
            data_gb = 0
    pkgs = load_packages()[0]
    match = next((p for p in pkgs if p["data_gb"] == data_gb), None)
    if match:
        return match["label"], DURATION_DAYS, data_gb, match["price"]
    # Last resort: 30 days at the largest package price.
    price = max((p["price"] for p in pkgs), default=0)
    return "Renewal", DURATION_DAYS, data_gb, price


async def _is_renewal_owned(email: str, telegram_id: int) -> bool:
    """Verify that panel client `email` belongs to `telegram_id`.

    Checks DB history first (cheap), then panel tgId. Returns False if
    ownership cannot be confirmed.
    """
    # 1. DB history: any approved order with panel_email == email owned by user
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Order.id)
                .join(User, Order.user_id == User.id)
                .where(Order.panel_email == email, User.telegram_id == telegram_id)
                .limit(1)
            )
            if result.scalar_one_or_none() is not None:
                return True
    except Exception:
        pass

    # 2. Panel: check tgId on the client object
    try:
        client = await VPNPanelService.get_client(email)
        if client is not None:
            inner = client.get("client") if isinstance(client.get("client"), dict) else client
            tg = None
            if isinstance(inner, dict):
                tg = inner.get("tgId")
            if tg is None and isinstance(client, dict):
                tg = client.get("tgId")
            if tg is not None:
                return str(tg) == str(telegram_id)
            # tg missing — fallback to list check (hydrate not needed for ownership,
            # but reuses existing method)
            try:
                clients = await VPNPanelService.get_clients_by_telegram_id(telegram_id)
                return any(c.get("email") == email for c in clients)
            except VPNPanelError:
                return False
    except VPNPanelError:
        return False
    except Exception:
        return False
    return False


async def _check_renewal_limit(email: str, duration_days: int) -> tuple[bool, int]:
    """Check if renewal would exceed MAX_TOTAL_DAYS.

    Returns (allowed, remaining_days). If not allowed, caller should block
    renewal and inform the user. Fetches panel client to read expiryTime.
    """
    try:
        client = await VPNPanelService.get_client(email)
    except VPNPanelError:
        # If panel unavailable, allow renewal to proceed — it will fail later with proper error
        return True, 0
    if not client:
        return True, 0
    # Unwrap possible {"client": {...}} wrapper
    inner = client.get("client") if isinstance(client.get("client"), dict) else client
    expiry_ms = 0
    if isinstance(inner, dict):
        expiry_ms = int(inner.get("expiryTime") or 0)
    if not expiry_ms and isinstance(client, dict):
        try:
            expiry_ms = int(client.get("expiryTime") or 0)
        except Exception:
            expiry_ms = 0
    if not expiry_ms:
        # No expiry (unlimited) — allow renewal, it will set to now+duration
        return True, 0
    try:
        from datetime import UTC, datetime

        now_ms = int(datetime.now(UTC).timestamp() * 1000)
    except Exception:
        import time

        now_ms = int(time.time() * 1000)
    remaining_ms = expiry_ms - now_ms
    if remaining_ms <= 0:
        return True, 0
    remaining_days = (remaining_ms + 86400000 - 1) // 86400000  # ceil
    total_after = remaining_days + duration_days
    if total_after > MAX_TOTAL_DAYS:
        return False, int(remaining_days)
    return True, int(remaining_days)


async def renew_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.message is None:
        return

    if not await check_cooldown(update.effective_user.id, "renew", 8):
        if query.message:
            await query.message.reply_text("⏳ لطفاً کمی صبر کنید و دوباره تلاش کنید.")
        return

    email = ""
    if query.data:
        parts = query.data.split("|", 1)
        if len(parts) == 2:
            email = parts[1].strip()
    if not email:
        return

    user = update.effective_user
    blocked = purchase_blocked_reason(user.id)
    if blocked:
        await query.message.reply_text(blocked, parse_mode="HTML")
        return

    # --- IDOR fix: ensure email belongs to caller before any renewal logic ---
    if not await _is_renewal_owned(email, user.id):
        log.warning("Renewal IDOR blocked: user %s tried to renew %s", user.id, email)
        await query.message.reply_text("⛔ این اشتراک متعلق به شما نیست.", parse_mode="HTML")
        return

    try:
        package_label, duration_days, data_gb, amount = await _resolve_renewal_terms(email, user.id)
    except VPNPanelError as exc:
        log.warning("Could not resolve renewal terms for %s: %s", email, exc)
        await query.message.reply_text(
            "❌ خطای سرور — دریافت اطلاعات اشتراک ممکن نشد. لطفاً بعداً تلاش کنید.",
            parse_mode="HTML",
        )
        return

    # 60-day limit: renewal must not push total subscription beyond MAX_TOTAL_DAYS
    try:
        allowed, remaining_days = await _check_renewal_limit(email, duration_days)
        if not allowed:
            await query.message.reply_text(
                f"⛔ تمدید ممکن نیست — مجموع زمان اشتراک پس از تمدید بیش از {MAX_TOTAL_DAYS} روز می‌شود.\n"
                f"⏳ باقی‌مانده فعلی: {remaining_days} روز — لطفاً پس از نزدیک شدن به انقضا دوباره تلاش کنید.",
                parse_mode="HTML",
            )
            return
    except Exception as exc:
        log.warning("Renewal limit check failed for %s: %s", email, exc)

    async with async_session() as session:
        user_obj = await get_or_create_user(session, user)
        # Single pending invariant: creating a new renewal supersedes *any*
        # pending order (buy or other renew) so the guard's auto-cancel
        # and this creation stay consistent. Previously only same-email was
        # cancelled, leaving orphan buy pendings.
        await session.execute(
            sa_update(Order)
            .where(
                Order.user_id == user_obj.id,
                Order.status == "pending",
            )
            .values(status="cancelled")
        )
        order = Order(
            user_id=user_obj.id,
            package_label=package_label,
            duration_days=duration_days,
            data_gb=data_gb,
            amount_toomans=amount,
            status="pending",
            renew_email=email,
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)

    # Ask about discount code BEFORE generating the gateway (resume on choice)
    from handlers.discount_flow import send_discount_prompt

    await send_discount_prompt(update, context, order)
