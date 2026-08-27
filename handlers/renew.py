import logging
from html import escape

from sqlalchemy import select
from sqlalchemy import update as sa_update
from telegram import Update
from telegram.ext import ContextTypes

from config import config
from database import async_session
from handlers.buy import get_or_create_user, purchase_blocked_reason
from handlers.rate_limit import check_cooldown
from keyboards import cancel_keyboard, payment_keyboard
from models import Order
from packages import DURATION_DAYS, load_packages
from vpn_service import VPNPanelError, VPNPanelService
from zarinpal import ZarinpalError, request_payment

log = logging.getLogger(__name__)


async def _resolve_renewal_terms(email: str) -> tuple[str, int, int, int]:
    """Return (package_label, duration_days, data_gb, amount_toomans) for a renewal.

    Prefers the user's last approved order for this client (keeps the price they
    paid). Falls back to the package list priced by the client's current data.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Order)
            .where(Order.panel_email == email, Order.status == "approved")
            .order_by(Order.created_at.desc())
            .limit(1)
        )
        orig = result.scalar_one_or_none()
    if orig is not None:
        return orig.package_label, orig.duration_days, orig.data_gb, orig.amount_toomans

    # Fallback: price by the client's current data quota.
    client = await VPNPanelService.get_client(email)
    data_gb = 0
    if client:
        data_gb = (int(client.get("totalGB") or 0)) // (1024**3)
    pkgs, _, _ = load_packages()
    match = next((p for p in pkgs if p["data_gb"] == data_gb), None)
    if match:
        return match["label"], DURATION_DAYS, data_gb, match["price"]
    # Last resort: 30 days at the largest package price.
    price = max((p["price"] for p in pkgs), default=0)
    return "Renewal", DURATION_DAYS, data_gb, price


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

    try:
        package_label, duration_days, data_gb, amount = await _resolve_renewal_terms(email)
    except VPNPanelError as exc:
        log.warning("Could not resolve renewal terms for %s: %s", email, exc)
        await query.message.reply_text(
            "❌ خطای سرور — دریافت اطلاعات اشتراک ممکن نشد. لطفاً بعداً تلاش کنید.",
            parse_mode="HTML",
        )
        return

    async with async_session() as session:
        user_obj = await get_or_create_user(session, user)
        # Supersede any pending renewal for the same client.
        await session.execute(
            sa_update(Order)
            .where(
                Order.user_id == user_obj.id,
                Order.status == "pending",
                Order.renew_email == email,
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

    context.user_data["order_id"] = order.id

    # Build the payment prompt. Non-admins must pay via Zarinpal; admins always
    # get a free-confirm button (see payment_keyboard) and may also pay normally.
    is_admin = user.id in config.admin_ids
    public_url = None
    if not is_admin:
        try:
            pay = await request_payment(
                order.id, amount, f"VardenProxy renewal — {package_label} (order #{order.id})"
            )
        except ZarinpalError as exc:
            log.warning("Renewal payment request for order #%s failed: %s", order.id, exc)
            async with async_session() as session:
                await session.execute(
                    sa_update(Order).where(Order.id == order.id).values(status="cancelled")
                )
                await session.commit()
            context.user_data.pop("order_id", None)
            await query.message.reply_text(
                "❌ <b>خطا در ایجاد پرداخت</b>\nلطفاً چند دقیقه بعد دوباره تلاش کنید.",
                parse_mode="HTML",
            )
            return
        async with async_session() as session:
            await session.execute(
                sa_update(Order).where(Order.id == order.id).values(payment_authority=pay["authority"])
            )
            await session.commit()
        public_url = config.zarinpal_public_start_url(pay["authority"])
    else:
        # Admin: try Zarinpal too, but never block on its failure — the
        # free-confirm button is always offered so they can renew for free.
        try:
            pay = await request_payment(
                order.id, amount, f"VardenProxy renewal — {package_label} (order #{order.id})"
            )
            async with async_session() as session:
                await session.execute(
                    sa_update(Order).where(Order.id == order.id).values(payment_authority=pay["authority"])
                )
                await session.commit()
            public_url = config.zarinpal_public_start_url(pay["authority"])
        except ZarinpalError as exc:
            log.warning("Admin renewal payment request failed (offering free): %s", exc)

    separator = "─" * 20
    renew_text = (
        f"💳 <b>تمدید اشتراک</b>\n\n"
        f"📦 پکیج: {escape(package_label)}\n"
        f"⏳ مدت: {duration_days} روز\n"
        f"💰 مبلغ: <b>{amount:,} تومان</b>\n\n"
        f"{separator}\n"
        "پس از پرداخت، زمان اشتراک فعلی شما تمدید می‌شود (همان کانفیگ قبلی)."
    )
    if is_admin:
        renew_text += "\n\n🔧 <i>ادمین:</i> می‌توانید بدون پرداخت، تمدید را به‌صورت رایگان تأیید کنید."
    pay_keyboard = payment_keyboard(public_url, order.id, is_admin)
    await query.message.reply_text(renew_text, reply_markup=pay_keyboard, parse_mode="HTML")
    await query.message.reply_text(
        "⏳ در انتظار پرداخت شما هستیم؛ پرداخت به‌صورت خودکار تشخیص داده می‌شود.",
        reply_markup=cancel_keyboard(),
        parse_mode="HTML",
    )
