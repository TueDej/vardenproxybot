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
    data_label as _data_label,
)
from message_render import (
    expiry_dt as _expiry_dt,
)
from message_render import (
    format_disabled_message,
    format_expired_message,
    format_product_message as _format_product_message,
)
from models import User
from vpn_service import VPNPanelError, VPNPanelService

log = logging.getLogger(__name__)


def _renew_keyboard(email: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 تمدید اشتراک", callback_data=f"renew|{email}")]]
    )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_cooldown(update.effective_user.id, "profile", 5):
        await update.message.reply_text("⏳ لطفاً کمی صبر کنید و دوباره تلاش کنید.")
        return
    telegram_id = update.effective_user.id
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()

        if not user:
            await update.message.reply_text(
                "🛒 هنوز اشتراک فعالی ندارید.\n\n"
                "برای شروع، گزینه <b>🛒 خرید اشتراک</b> را انتخاب کنید.",
                reply_markup=main_menu_keyboard(),
                parse_mode="HTML",
            )
            return

        text = (
            f"👤 <b>پروفایل</b>\n\n"
            f"🆔 شناسه کاربری: <code>{user.telegram_id}</code>\n"
            f"📛 نام: {escape(user.first_name or '')}\n"
            f"📅 عضو از: {user.created_at.strftime('%Y-%m-%d')}\n\n"
        )

    if not config.panel_configured:
        text += "<i>اطلاعات سرور هنوز پیکربندی نشده است.</i>"
        await update.message.reply_text(text, reply_markup=home_keyboard(), parse_mode="HTML")
        return

    try:
        clients = await VPNPanelService.get_clients_by_telegram_id(telegram_id)
    except VPNPanelError as exc:
        text += f"<i>خطای سرور: {escape(str(exc))}</i>"
        await update.message.reply_text(text, reply_markup=home_keyboard(), parse_mode="HTML")
        return

    now = datetime.now(UTC)
    online_emails = await VPNPanelService.get_online_emails() if clients else set()

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

    summary = f"📦 <b>اشتراک‌های فعال:</b> {len(active)}"
    if expired:
        summary += f"\n⌛ منقضی‌شده: {len(expired)}"
    if disabled:
        summary += f"\n🚫 غیرفعال: {len(disabled)}"

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
        if len(msg) > 4000:
            msg = msg[:3990] + "…"
        await update.message.reply_text(
            msg, reply_markup=_renew_keyboard(c["email"]), parse_mode="HTML"
        )
        sent += 1

    for c, expiry_dt in expired:
        if sent >= max_details:
            truncated = True
            break
        await update.message.reply_text(
            format_expired_message(c, expiry_dt),
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
            f"ℹ️ تعداد اشتراک‌ها زیاد است؛ فقط {max_details} مورد نمایش داده شد. برای مشاهده بقیه با پشتیبانی تماس بگیرید.",
            parse_mode="HTML",
        )
