import logging
from datetime import UTC, datetime
from html import escape

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from config import config
from database import async_session
from keyboards import home_keyboard, main_menu_keyboard
from models import User
from vpn_service import VPNPanelError, VPNPanelService

log = logging.getLogger(__name__)


def _expiry_dt(ms: int) -> datetime | None:
    """Panel expiry in ms since epoch; 0 means 'never expires'."""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _data_label(total_gb: int) -> str:
    return "Unlimited" if total_gb == 0 else f"{total_gb // (1024 ** 3)}GB"


def _format_links_block(links: list[str]) -> str:
    if not links:
        return ""
    lines = ["🔗 <b>کانفیگ‌ها:</b>"]
    for link in links:
        lines.append(f"<pre><code>{escape(link)}</code></pre>")
    return "\n".join(lines)


def _format_product_message(
    c: dict, expiry_dt: datetime | None, online_emails: set, now: datetime
) -> str:
    if expiry_dt is None:
        expiry_line = "⏳ انقضا: ندارد"
    else:
        remaining = (expiry_dt - now).days
        expiry_line = f"⏳ انقضا: {expiry_dt.strftime('%Y-%m-%d')} ({remaining} روز باقی‌مانده)"
    online_tag = " 🟢 <i>آنلاین</i>" if c["email"] in online_emails else ""
    links_block = _format_links_block(c["links"])
    suffix = f"\n{links_block}" if links_block else ""
    return (
        f"📦 {_data_label(c['total_gb'])} | {c['limit_ip']} دستگاه{online_tag}\n"
        f"{expiry_line}"
        f"{suffix}"
    )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await update.message.reply_text(msg, parse_mode="HTML")
        sent += 1

    for c, expiry_dt in expired:
        if sent >= max_details:
            truncated = True
            break
        when = expiry_dt.strftime("%Y-%m-%d") if expiry_dt else "نامشخص"
        await update.message.reply_text(
            f"⌛ <b>منقضی‌شده</b> — {_data_label(c['total_gb'])}، پایان: {when}\n"
            "برای تمدید، گزینه 🛒 خرید اشتراک را انتخاب کنید.",
            parse_mode="HTML",
        )
        sent += 1

    for c, expiry_dt in disabled:
        if sent >= max_details:
            truncated = True
            break
        await update.message.reply_text(
            f"🚫 <b>غیرفعال</b> — {_data_label(c['total_gb'])} | {c['limit_ip']} دستگاه\n"
            f"⏳ انقضا: {expiry_dt.strftime('%Y-%m-%d') if expiry_dt else 'ندارد'}\n"
            "با پشتیبانی تماس بگیرید.",
            parse_mode="HTML",
        )
        sent += 1

    if truncated:
        await update.message.reply_text(
            f"ℹ️ تعداد اشتراک‌ها زیاد است؛ فقط {max_details} مورد نمایش داده شد. برای مشاهده بقیه با پشتیبانی تماس بگیرید.",
            parse_mode="HTML",
        )
