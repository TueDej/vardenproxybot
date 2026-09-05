import logging
from html import escape

from sqlalchemy import select
from sqlalchemy import update as sa_update
from telegram import Update
from telegram.ext import ContextTypes

from config import config
from database import async_session
from handlers.discount import (
    calc_discounted_amount,
    release_discount_code_by_order,
    validate_discount_code,
)
from keyboards import (
    CHOICE_HAVE_CODE,
    CHOICE_NO_CODE,
    cancel_keyboard,
    discount_choice_keyboard,
    discount_entry_keyboard,
    main_menu_keyboard,
    payment_keyboard,
)
from models import DiscountCode, Order, User
from rtl import rtl, strip_bidi
from zarinpal import ZarinpalError, request_payment

log = logging.getLogger(__name__)

# Shown while a discount code is awaited (sent with discount_entry_keyboard(),
# so the skip/cancel buttons referenced in the text are actually on screen).
ENTRY_PROMPT_TEXT = rtl(
    "✏️ لطفاً <b>کد تخفیف</b> خود را ارسال کنید.\n"
    "اگر کد ندارید، دکمه ⏭️ ادامه بدون تخفیف را بزنید.\n"
    "برای لغو، ❌ انصراف را بزنید."
)

INVALID_CODE_TEXT = rtl(
    "❌ کد تخفیف نامعتبر است یا قبلاً استفاده شده.\n"
    "می‌توانید کد دیگری بفرستید، دکمه ⏭️ ادامه بدون تخفیف را بزنید "
    "یا ❌ انصراف را انتخاب کنید."
)

# Texts accepted as "continue without a discount" while a code is awaited
# (compared after strip_bidi, so RLM-prefixed button echoes still match)
SKIP_KEYWORDS = ("skip", "skip.", "رد", "بدون", "⏭️ ادامه بدون تخفیف", "ادامه بدون تخفیف")


async def _latest_pending_order_id(telegram_id: int) -> int | None:
    """Fallback lookup when user_data was lost (bot restart mid-purchase)."""
    async with async_session() as session:
        res = await session.execute(
            select(Order.id)
            .join(User, Order.user_id == User.id)
            .where(User.telegram_id == telegram_id, Order.status == "pending")
            .order_by(Order.created_at.desc())
            .limit(1)
        )
        return res.scalar_one_or_none()


async def send_discount_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, order: Order) -> None:
    """Ask the user if they have a discount code, before generating the gateway.

    Stores enough state in user_data so the text router can resume the exact
    same order (buy or renew). Uses a reply keyboard so all three options —
    code entry, skip, cancel — are visible as buttons at the same time.
    """
    context.user_data["pending_order_id"] = order.id
    context.user_data["awaiting_discount_choice"] = True
    context.user_data["awaiting_discount_code"] = False
    is_renew = bool(order.renew_email)
    base_text = rtl(
        "🎟️ <b>کد تخفیف</b>\n"
        "هر کد فقط یک‌بار قابل استفاده است.\n\n"
        + (
            "قصد تمدید اشتراک خود را دارید؛ کد تخفیف دارید؟"
            if is_renew
            else "قصد خرید اشتراک جدید را دارید؛ کد تخفیف دارید؟"
        )
    )
    await update.effective_message.reply_text(
        base_text, reply_markup=discount_choice_keyboard(), parse_mode="HTML"
    )


async def handle_discount_choice_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Route the discount-choice reply-keyboard buttons.

    Returns True when the text was consumed here. False lets menu_router fall
    through to the pending-order auto-cancel guard, so any other text still
    cancels the pending order (documented behavior).
    """
    text = strip_bidi((update.effective_message.text or "").strip())
    if text == strip_bidi(CHOICE_HAVE_CODE):
        context.user_data["awaiting_discount_choice"] = False
        context.user_data["awaiting_discount_code"] = True
        await update.effective_message.reply_text(
            ENTRY_PROMPT_TEXT,
            reply_markup=discount_entry_keyboard(),
            parse_mode="HTML",
        )
        return True
    if text == strip_bidi(CHOICE_NO_CODE):
        context.user_data["awaiting_discount_choice"] = False
        order_id = context.user_data.get("pending_order_id")
        if not order_id and update.effective_user:
            # user_data lost (restart) — recover the pending order from the DB
            order_id = await _latest_pending_order_id(update.effective_user.id)
        if order_id:
            await _resume_payment_for_order(update, context, order_id)
        else:
            context.user_data.pop("pending_order_id", None)
            await update.effective_message.reply_text(
                rtl("⚠️ سفارش فعالی برای ادامه وجود ندارد؛ لطفاً از منوی اصلی دوباره شروع کنید."),
                reply_markup=main_menu_keyboard(),
            )
        return True
    return False


async def handle_disc_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy inline-buttons path (disc|yes / disc|no) for older messages."""
    query = update.callback_query
    await query.answer()
    if query.message is None:
        return
    parts = (query.data or "").split("|", 1)
    choice = parts[1] if len(parts) == 2 else ""
    order_id = context.user_data.get("pending_order_id")
    if not order_id and update.effective_user:
        order_id = await _latest_pending_order_id(update.effective_user.id)
    if not order_id:
        context.user_data.pop("awaiting_discount_choice", None)
        await query.message.reply_text(
            rtl("⚠️ سفارشی در انتظار نیست؛ لطفاً از منوی اصلی شروع کنید."),
            reply_markup=main_menu_keyboard(),
        )
        return

    if choice == "yes":
        context.user_data["awaiting_discount_choice"] = False
        context.user_data["awaiting_discount_code"] = True
        context.user_data["pending_order_id"] = order_id
        await query.message.reply_text(
            ENTRY_PROMPT_TEXT,
            reply_markup=discount_entry_keyboard(),
            parse_mode="HTML",
        )
        return
    if choice == "no":
        context.user_data["awaiting_discount_choice"] = False
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
        context.user_data.pop("awaiting_discount_choice", None)
        await update.effective_message.reply_text(
            rtl("⚠️ سفارشی در انتظار نیست؛ لطفاً از منوی اصلی شروع کنید."),
            reply_markup=main_menu_keyboard(),
        )
        return

    # skip keyword / button → continue without discount
    text_norm = strip_bidi(text)
    if text_norm.lower() in SKIP_KEYWORDS or text_norm in SKIP_KEYWORDS:
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
            context.user_data.pop("awaiting_discount_choice", None)
            context.user_data.pop("pending_order_id", None)
            await update.effective_message.reply_text(
                rtl("⚠️ سفارش منقضی یا لغو شده است؛ لطفاً از منوی اصلی دوباره شروع کنید."),
                reply_markup=main_menu_keyboard(),
            )
            return
        dc = await validate_discount_code(session, code_str)
        if dc is None:
            await update.effective_message.reply_text(
                INVALID_CODE_TEXT,
                parse_mode="HTML",
            )
            return
        # Atomic discount application: claim code + update order in one transaction
        # Prevents double-spend when two users (or two tabs) race on the same code,
        # and avoids the previous two-commit window where a crash could leave
        # the order discounted without marking the code used (or vice-versa).
        from datetime import datetime

        try:
            from datetime import UTC
        except ImportError:
            from datetime import timezone

            UTC = timezone.utc  # type: ignore[no-redef]  # noqa: UP017
        original = order_obj.amount_toomans
        new_amount = calc_discounted_amount(original, dc.discount_percent)
        claim = await session.execute(
            sa_update(DiscountCode)
            .where(DiscountCode.id == dc.id, DiscountCode.is_used == False)  # noqa: E712
            .values(
                is_used=True,
                used_at=datetime.now(UTC),
                used_by_telegram_id=update.effective_user.id,
                used_order_id=order_obj.id,
            )
        )
        if claim.rowcount == 0:
            await session.rollback()
            await update.effective_message.reply_text(
                INVALID_CODE_TEXT,
                parse_mode="HTML",
            )
            return
        order_obj.original_amount_toomans = original
        order_obj.discount_code = dc.code
        order_obj.discount_percent = dc.discount_percent
        order_obj.discount_code_id = dc.id
        order_obj.amount_toomans = new_amount
        await session.commit()
        # Keep ORM in sync for downstream handlers
        dc.is_used = True
        dc.used_at = datetime.now(UTC)
        dc.used_by_telegram_id = update.effective_user.id
        dc.used_order_id = order_obj.id
        context.user_data["awaiting_discount_code"] = False
        await update.effective_message.reply_text(
            rtl(
                f"✅ کد تخفیف {escape(dc.code)} اعمال شد — <b>{dc.discount_percent}%</b> تخفیف.\n"
                f"💰 مبلغ نهایی: <b>{new_amount:,} تومان</b> (از {original:,} تومان)"
            ),
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
            context.user_data.pop("awaiting_discount_code", None)
            context.user_data.pop("awaiting_discount_choice", None)
            # A code consumed moments ago must not stay bound to a dead order
            # (cancelled/expired between the two awaits) — free it for reuse.
            if order is not None:
                try:
                    await release_discount_code_by_order(session, order)
                except Exception:
                    log.warning(
                        "Failed to release discount code for dead order #%s", order.id, exc_info=True
                    )
            await update.effective_message.reply_text(
                rtl("⚠️ سفارش منقضی یا لغو شده است؛ لطفاً از منوی اصلی دوباره شروع کنید."),
                reply_markup=main_menu_keyboard(),
            )
            return

        context.user_data["order_id"] = order.id
        is_renew = bool(order.renew_email)
        amount = order.amount_toomans
        public_url = None
        if not is_admin:
            try:
                desc = f"Website admission request (order #{order.id})"
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
                context.user_data.pop("awaiting_discount_code", None)
                context.user_data.pop("awaiting_discount_choice", None)
                await update.effective_message.reply_text(
                    rtl("❌ <b>خطا در ایجاد پرداخت</b>\nلطفاً چند دقیقه بعد دوباره تلاش کنید."),
                    reply_markup=main_menu_keyboard(),
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
                desc = f"Website admission request (order #{order.id})"
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

    # Build gateway text — compact: summary block, then one line per key fact.
    from handlers.buy import PAYMENT_EXPIRY_SECONDS

    expiry_minutes = PAYMENT_EXPIRY_SECONDS // 60
    discounted_note = ""
    if order.discount_percent:
        discounted_note = (
            f"🎟️ تخفیف {order.discount_percent}% — کد <code>{escape(order.discount_code or '')}</code>\n"
        )
    if is_renew:
        gateway_text = rtl(
            f"💳 <b>تمدید اشتراک</b>\n\n"
            f"📦 پکیج: {escape(order.package_label)}\n"
            f"⏳ مدت: {order.duration_days} روز\n"
            f"💰 مبلغ: <b>{amount:,} تومان</b>\n"
            f"{discounted_note}\n"
            "پس از پرداخت، زمان اشتراک فعلی تمدید می‌شود (همان کانفیگ قبلی).\n"
            f"⏰ اعتبار لینک پرداخت: <b>{expiry_minutes} دقیقه</b>"
        )
    else:
        gateway_text = rtl(
            f"💳 <b>سفارش #{order.id}</b>\n\n"
            f"📦 پکیج: {escape(order.package_label)}\n"
            f"📅 مدت: یک ماه\n"
            f"💰 مبلغ: <b>{amount:,} تومان</b>\n"
            f"{discounted_note}\n"
            "✅ بلافاصله پس از پرداخت، اشتراک شما فعال می‌شود.\n"
            f"⏰ اعتبار لینک پرداخت: <b>{expiry_minutes} دقیقه</b>"
        )
    pay_keyboard = payment_keyboard(public_url, order.id, is_admin)
    sent = await update.effective_message.reply_text(
        gateway_text, reply_markup=pay_keyboard, parse_mode="HTML"
    )
    context.user_data["pay_message_id"] = sent.message_id
    context.user_data.pop("pending_order_id", None)
    await update.effective_message.reply_text(
        rtl(
            "⏳ منتظر پرداخت شما هستیم — به‌صورت خودکار تشخیص داده می‌شود.\n"
            "⚠️ خروج از این صفحه سفارش را <b>لغو</b> می‌کند؛ برای لغو دستی ❌ انصراف را بزنید."
        ),
        reply_markup=cancel_keyboard(),
        parse_mode="HTML",
    )
