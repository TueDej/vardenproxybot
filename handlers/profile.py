import logging

try:
    from datetime import UTC
except ImportError:  # Python <3.11
    from datetime import timezone

    UTC = timezone.utc  # type: ignore[no-redef]  # noqa: UP017
from datetime import datetime
from html import escape

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import config
from database import async_session
from handlers.rate_limit import check_cooldown
from keyboards import home_keyboard, main_menu_keyboard
from message_render import (
    expiry_dt as _expiry_dt,
)
from message_render import (
    format_disabled_message,
    format_expired_message,
    format_product_message as _format_product_message,
)
from models import Order, User
from rtl import btn as _btn
from rtl import rtl
from rtl import user as _rtl_user
from vpn_service import VPNPanelError, VPNPanelService

log = logging.getLogger(__name__)


def _renew_keyboard(email: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(_btn("🔄 تمدید اشتراک"), callback_data=f"renew|{email}")]]
    )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_cooldown(update.effective_user.id, "profile", 5):
        await update.message.reply_text(rtl("⏳ لطفاً کمی صبر کنید و دوباره تلاش کنید."))
        return
    telegram_id = update.effective_user.id
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()

        if not user:
            await update.message.reply_text(
                rtl(
                    "🛒 هنوز اشتراک فعالی ندارید.\n\n"
                    "برای شروع، گزینه <b>🛒 خرید اشتراک</b> را انتخاب کنید."
                ),
                reply_markup=main_menu_keyboard(),
                parse_mode="HTML",
            )
            return

        text = rtl(
            f"👤 <b>پروفایل</b>\n\n"
            f"🆔 شناسه کاربری: <code>{user.telegram_id}</code>\n"
            f"📛 نام: {_rtl_user(escape(user.first_name or ''))}\n"
            f"📅 عضو از: {user.created_at.strftime('%Y-%m-%d')}\n\n"
        )

    if not config.panel_configured:
        text += rtl("<i>اطلاعات سرور هنوز پیکربندی نشده است.</i>")
        await update.message.reply_text(text, reply_markup=home_keyboard(), parse_mode="HTML")
        return

    try:
        clients = await VPNPanelService.get_clients_by_telegram_id(telegram_id)
    except VPNPanelError as exc:
        # Don't leak raw panel errors to the user — log details, show a clean message
        log.warning("Profile panel error for %s: %s", telegram_id, exc)
        text += rtl("<i>❌ خطا در دریافت اطلاعات از سرور؛ لطفاً کمی بعد دوباره تلاش کنید.</i>")
        await update.message.reply_text(text, reply_markup=home_keyboard(), parse_mode="HTML")
        return

    now = datetime.now(UTC)
    online_emails = await VPNPanelService.get_online_emails() if clients else set()

    # Gifts are not renewable — collect gift panel_emails for this user
    gift_emails: set[str] = set()
    try:
        async with async_session() as _s:
            res = await _s.execute(
                select(Order.panel_email).where(
                    Order.user_id == user.id,  # type: ignore[attr-defined]
                    Order.is_gift == True,  # type: ignore[attr-defined]
                    Order.panel_email.is_not(None),
                )
            )
            gift_emails = {r[0] for r in res.all() if r[0]}
    except Exception as e:
        if "is_gift" not in str(e) and "UndefinedColumn" not in type(e).__name__:
            log.warning("Gift emails fetch failed for %s: %s", telegram_id, e)

    # Split subscriptions by state so expired ones stay visible.
    active, expired, disabled = [], [], []
    for c in clients:
        expiry_dt = _expiry_dt(c["expiry_time"])
        if not c["enable"]:
            disabled.append((c, expiry_dt))
        elif expiry_dt is None or expiry_dt > now:
            active.append((c, expiry_dt))
        else:
            expired.append((c, expiry_dt))

    summary = rtl(f"📦 <b>اشتراک‌های فعال:</b> {len(active)}")
    if expired:
        summary += rtl(f"\n⌛ منقضی‌شده: {len(expired)}")
    if disabled:
        summary += rtl(f"\n🚫 غیرفعال: {len(disabled)}")

    await update.message.reply_text(text + summary, reply_markup=home_keyboard(), parse_mode="HTML")

    # Paginate to avoid flood: max 10 detailed messages, rest truncated
    max_details = 10
    sent = 0
    truncated = False

    for c, expiry_dt in active:
        if sent >= max_details:
            truncated = True
            break
        msg = _format_product_message(c, expiry_dt, online_emails, now)
        if c["email"] in gift_emails:
            msg += rtl("\n\n🎁 <i>این اشتراک هدیه است — قابل تمدید نیست.</i>")
        if len(msg) > 4000:
            msg = msg[:3990] + "…"
        kb = None if c["email"] in gift_emails else _renew_keyboard(c["email"])
        await update.message.reply_text(msg, reply_markup=kb, parse_mode="HTML")
        sent += 1

    for c, expiry_dt in expired:
        if sent >= max_details:
            truncated = True
            break
        exp_msg = format_expired_message(c, expiry_dt)
        if c["email"] in gift_emails:
            exp_msg += rtl("\n\n🎁 <i>این اشتراک هدیه است — قابل تمدید نیست.</i>")
            await update.message.reply_text(exp_msg, parse_mode="HTML")
        else:
            await update.message.reply_text(
                exp_msg,
                reply_markup=_renew_keyboard(c["email"]),
                parse_mode="HTML",
            )
        sent += 1

    for c, expiry_dt in disabled:
        if sent >= max_details:
            truncated = True
            break
        await update.message.reply_text(
            format_disabled_message(c, expiry_dt),
            parse_mode="HTML",
        )
        sent += 1

    if truncated:
        await update.message.reply_text(
            rtl(
                f"ℹ️ تعداد اشتراک‌ها زیاد است؛ فقط {max_details} مورد نمایش داده شد. "
                "برای مشاهده بقیه با پشتیبانی تماس بگیرید."
            ),
            parse_mode="HTML",
        )
