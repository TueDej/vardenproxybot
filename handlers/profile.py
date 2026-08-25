import logging
from datetime import datetime, timezone
from html import escape

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from config import config
from database import async_session
from keyboards import back_keyboard, main_menu_keyboard
from models import User
from vpn_service import VPNPanelError, VPNPanelService

log = logging.getLogger(__name__)


def _expiry_dt(ms: int) -> datetime | None:
    """Panel expiry in ms since epoch; 0 means 'never expires'."""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _data_label(total_gb: int) -> str:
    return "Unlimited" if total_gb == 0 else f"{total_gb // (1024**3)}GB"


def _format_links_block(links: list[str], sub_url: str) -> str:
    lines = []
    if sub_url:
        lines.append("📡 <b>Subscription URL:</b>")
        lines.append(f"<pre><code>{escape(sub_url)}</code></pre>")
    if links:
        if lines:
            lines.append("")
        lines.append("🔗 <b>Config links:</b>")
        for link in links:
            lines.append(f"<pre><code>{escape(link)}</code></pre>")
    return "\n".join(lines)


def _format_product_message(c: dict, expiry_dt: datetime | None, online_emails: set) -> str:
    if expiry_dt is None:
        expiry_line = "⏳ Expires: never"
    else:
        remaining = (expiry_dt - datetime.now(timezone.utc)).days
        expiry_line = f"⏳ Expires: {expiry_dt.strftime('%Y-%m-%d')} ({remaining}d left)"
    online_tag = " 🟢 <i>online</i>" if c["email"] in online_emails else ""
    return (
        f"📦 {_data_label(c['total_gb'])} | {c['limit_ip']} device(s){online_tag}\n"
        f"{expiry_line}\n"
        f"{_format_links_block(c['links'], c['subscription_url'])}"
    )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()

        if not user:
            await update.message.reply_text(
                "🛒 You don't have an active subscription yet.\n\n"
                "Use <b>🛒 Buy Subscription</b> to get started!",
                reply_markup=main_menu_keyboard(),
                parse_mode="HTML",
            )
            return

        text = (
            f"👤 <b>Profile</b>\n\n"
            f"🆔 User ID: <code>{user.telegram_id}</code>\n"
            f"📛 Name: {escape(user.first_name)}\n"
            f"📅 Member since: {user.created_at.strftime('%Y-%m-%d')}\n\n"
        )

    if not config.panel_configured:
        text += "<i>Panel not configured.</i>"
        await update.message.reply_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
        return

    try:
        clients = await VPNPanelService.get_clients_by_telegram_id(telegram_id)
    except VPNPanelError as exc:
        text += f"<i>Panel error: {escape(str(exc))}</i>"
        await update.message.reply_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
        return

    now = datetime.now(timezone.utc)
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

    summary = f"📦 <b>Active Subscriptions:</b> {len(active)}"
    if expired:
        summary += f"\n⌛ Expired: {len(expired)}"
    if disabled:
        summary += f"\n🚫 Disabled: {len(disabled)}"

    await update.message.reply_text(
        text + summary, reply_markup=back_keyboard(), parse_mode="HTML"
    )

    for c, expiry_dt in active:
        await update.message.reply_text(
            _format_product_message(c, expiry_dt, online_emails),
            parse_mode="HTML",
        )

    for c, expiry_dt in expired:
        when = "unknown" if expiry_dt is None else expiry_dt.strftime("%Y-%m-%d")
        await update.message.reply_text(
            f"⌛ <b>Expired</b> — {_data_label(c['total_gb'])}, ended {when}\n"
            "Use 🛒 Buy Subscription to renew.",
            parse_mode="HTML",
        )
