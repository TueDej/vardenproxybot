import logging
from html import escape

from sqlalchemy import select, update as sa_update
from telegram import Update
from telegram.ext import ContextTypes

from config import config
from database import async_session
from handlers.discount import (
    calc_discounted_amount,
    consume_discount_code,
    release_discount_code_by_order,
    validate_discount_code,
)
from keyboards import cancel_keyboard, discount_prompt_keyboard, payment_keyboard
from models import Order
from vpn_service import VPNPanelError
from zarinpal import ZarinpalError, request_payment

log = logging.getLogger(__name__)


async def send_discount_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, order: Order) -> None:
    """Ask the user if they have a discount code, before generating the gateway.

    Stores enough state in user_data so the callback / text handlers can resume
    the exact same order (buy or renew).
    """
    context.user_data["pending_order_id"] = order.id
    context.user_data["awaiting_discount_code"] = False
    is_renew = bool(order.renew_email)
    base_text = (
        "🎟️ <b>کد تخفیف</b>\n\n"
        "اگر کد تخفیف دارید، قبل از ورود به درگاه پرداخت می‌توانید استفاده کنید.\n"
        "هر کد فقط یک بار قابل استفاده است.\n\n"
    )
    if is_renew:
        base_text += "قصد تمدید اشتراک خود را دارید. آیا کد تخفیف دارید؟"
    else:
        base_text += "قصد خرید اشتراک جدید را دارید. آیا کد تخفیف دارید؟"
    await update.effective_message.reply_text(
        base_text, reply_markup=discount_prompt_keyboard(), parse_mode="HTML"
    )


async def handle_disc_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle disc|yes and disc|no inline buttons."""
    query = update.callback_query
    await query.answer()
    if query.message is None:
        return
    parts = (query.data or "").split("|", 1)
    choice = parts[1] if len(parts) == 2 else ""
    order_id = context.user_data.get("pending_order_id")
    if not order_id:
        await query.message.reply_text("⚠️ سفارشی در انتظار نیست؛ لطفاً از منوی اصلی شروع کنید.")
        return

    if choice == "yes":
        context.user_data["awaiting_discount_code"] = True
        await query.message.reply_text(
            "✏️ لطفاً <b>کد تخفیف</b> خود را ارسال کنید.\n"
            "اگر کد ندارید، کلمه <code>skip</code> را بفرستید تا بدون تخفیف ادامه دهید.\n"
            "برای لغو، «❌ انصراف» را بزنید.",
            parse_mode="HTML",
        )
        return
    if choice == "no":
        context.user_data["awaiting_discount_code"] = False
        await _resume_payment_for_order(update, context, order_id)
        return

    # Unknown — fall back to resuming without discount
    await _resume_payment_for_order(update, context, order_id)


async def discount_code_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Text handler invoked while awaiting a discount code (intercepts menu_router)."""
    text = (update.effective_message.text or "").strip()
    order_id = context.user_data.get("pending_order_id")
    if not order_id:
        context.user_data["awaiting_discount_code"] = False
        await update.effective_message.reply_text(
            "⚠️ سفارشی در انتظار نیست؛ لطفاً از منوی اصلی شروع کنید.",
            reply_markup=None,
        )
        return

    # skip keyword → continue without discount
    if text.lower() in ("skip", "رد", "بدون", "skip."):
        context.user_data["awaiting_discount_code"] = False
        await _resume_payment_for_order(update, context, order_id)
        return

    code_str = text.upper().replace("-", "").replace(" ", "")
    async with async_session() as session:
        order_obj = (
            await session.execute(select(Order).where(Order.id == order_id))
        ).scalar_one_or_none()
        if order_obj is None or order_obj.status != "pending":
            context.user_data.pop("awaiting_discount_code", None)
            context.user_data.pop("pending_order_id", None)
            await update.effective_message.reply_text(
                "⚠️ سفارش منقضی یا لغو شده است؛ لطفاً از منوی اصلی دوباره شروع کنید.",
                reply_markup=None,
            )
            return
        dc = await validate_discount_code(session, code_str)
        if dc is None:
            await update.effective_message.reply_text(
                "❌ کد تخفیف نامعتبر است یا قبلاً استفاده شده.\n"
                "می‌توانید کد دیگری بفرستید یا <code>skip</code> بنویسید تا بدون تخفیف ادامه دهید.",
                parse_mode="HTML",
            )
            return
        # Apply discount
        original = order_obj.amount_toomans
        new_amount = calc_discounted_amount(original, dc.discount_percent)
        order_obj.original_amount_toomans = original
        order_obj.discount_code = dc.code
        order_obj.discount_percent = dc.discount_percent
        order_obj.discount_code_id = dc.id
        order_obj.amount_toomans = new_amount
        await session.commit()
        # Consume (one-time); released if order later cancelled/expired
        await consume_discount_code(session, dc, update.effective_user.id, order_obj.id)
        context.user_data["awaiting_discount_code"] = False
        await update.effective_message.reply_text(
            f"✅ کد تخفیف {escape(dc.code)} اعمال شد — <b>{dc.discount_percent}%</b> تخفیف.\n"
            f"💰 مبلغ نهایی: <b>{new_amount:,} تومان</b> (از {original:,} تومان)",
            parse_mode="HTML",
        )
    # Resume payment with discounted amount
    await _resume_payment_for_order(update, context, order_id)


async def _resume_payment_for_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    """Create the Zarinpal request for an existing pending order and show the gateway."""
    user = update.effective_user
    is_admin = user.id in config.admin_ids
    async with async_session() as session:
        order = (
            await session.execute(select(Order).where(Order.id == order_id))
        ).scalar_one_or_none()
        if order is None or order.status != "pending":
            context.user_data.pop("pending_order_id", None)
            await update.effective_message.reply_text(
                "⚠️ سفارش منقضی یا لغو شده است؛ لطفاً از منوی اصلی دوباره شروع کنید.",
                reply_markup=None,
            )
            return

        context.user_data["order_id"] = order.id
        is_renew = bool(order.renew_email)
        amount = order.amount_toomans
        public_url = None
        if not is_admin:
            try:
                desc = (
                    f"VardenProxy renewal — {order.package_label} (order #{order.id})"
                    if is_renew
                    else f"VardenProxy subscription — {order.package_label} (order #{order.id})"
                )
                pay = await request_payment(order.id, amount, desc)
            except ZarinpalError as exc:
                log.warning("Discount-flow payment request for order #%s failed: %s", order.id, exc)
                await session.execute(
                    sa_update(Order).where(Order.id == order.id).values(status="cancelled")
                )
                await session.commit()
                # Payment failed before gateway — free the (already consumed) code
                try:
                    await release_discount_code_by_order(session, order)
                except Exception:
                    log.warning("Failed to release discount code for failed order #%s", order.id, exc_info=True)
                context.user_data.pop("order_id", None)
                context.user_data.pop("pending_order_id", None)
                await update.effective_message.reply_text(
                    "❌ <b>خطا در ایجاد پرداخت</b>\nلطفاً چند دقیقه بعد دوباره تلاش کنید.",
                    parse_mode="HTML",
                )
                return
            await session.execute(
                sa_update(Order)
                .where(Order.id == order.id)
                .values(payment_authority=pay["authority"])
            )
            await session.commit()
            public_url = config.zarinpal_public_start_url(pay["authority"])
        else:
            try:
                desc = (
                    f"VardenProxy renewal — {order.package_label} (order #{order.id})"
                    if is_renew
                    else f"VardenProxy subscription — {order.package_label} (order #{order.id})"
                )
                pay = await request_payment(order.id, amount, desc)
                await session.execute(
                    sa_update(Order)
                    .where(Order.id == order.id)
                    .values(payment_authority=pay["authority"])
                )
                await session.commit()
                public_url = config.zarinpal_public_start_url(pay["authority"])
            except ZarinpalError as exc:
                log.warning("Admin discount-flow payment request failed (offering free): %s", exc)

    # Build gateway text
    separator = "─" * 20
    discounted_note = ""
    if order.discount_percent:
        discounted_note = f"🎟️ تخفیف {order.discount_percent}% (کد {escape(order.discount_code or '')}) اعمال شد\n"
    if is_renew:
        gateway_text = (
            f"💳 <b>تمدید اشتراک</b>\n\n"
            f"📦 پکیج: {escape(order.package_label)}\n"
            f"⏳ مدت: {order.duration_days} روز\n"
            f"💰 مبلغ: <b>{amount:,} تومان</b>\n"
            f"{discounted_note}"
            f"{separator}\n"
            "پس از پرداخت، زمان اشتراک فعلی شما تمدید می‌شود (همان کانفیگ قبلی).\n"
            "⏰ این لینک پرداخت فقط <b>15 دقیقه</b> معتبر است؛ پس از آن سفارش به‌صورت خودکار لغو می‌شود."
        )
    else:
        gateway_text = (
            f"💳 <b>سفارش #{order.id}</b>\n\n"
            f"📦 پکیج: {escape(order.package_label)}\n"
            f"📅 مدت: یک ماه\n"
            f"💰 مبلغ: <b>{amount:,} تومان</b>\n"
            f"{discounted_note}"
            f"{separator}\n"
            "برای پرداخت امن، روی دکمه زیر بزنید و پرداخت را در <b>درگاه زرین‌پال</b> انجام دهید.\n"
            "✅ بلافاصله پس از پرداخت، اشتراک شما به‌صورت خودکار فعال می‌شود.\n"
            "⏰ این لینک پرداخت فقط <b>15 دقیقه</b> معتبر است؛ پس از آن سفارش به‌صورت خودکار لغو می‌شود."
        )
    if is_admin:
        gateway_text += "\n\n🔧 <i>ادمین:</i> می‌توانید بدون پرداخت، اشتراک را به‌صورت رایگان تأیید کنید."
    pay_keyboard = payment_keyboard(public_url, order.id, is_admin)
    sent = await update.effective_message.reply_text(
        gateway_text, reply_markup=pay_keyboard, parse_mode="HTML"
    )
    context.user_data["pay_message_id"] = sent.message_id
    context.user_data.pop("pending_order_id", None)
    await update.effective_message.reply_text(
        "⏳ در انتظار پرداخت شما هستیم؛ پرداخت به‌صورت خودکار تشخیص داده می‌شود.\n"
        "⚠️ تا تکمیل پرداخت از این صفحه خارج نشوید — با انتخاب هر گزینه‌ی دیگر یا ارسال هر پیامی، سفارش فعلی به‌صورت خودکار <b>لغو</b> می‌شود.\n"
        "برای لغو دستی، «❌ انصراف» را بزنید:",
        reply_markup=cancel_keyboard(),
        parse_mode="HTML",
    )
