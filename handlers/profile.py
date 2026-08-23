from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from config import config
from database import async_session
from keyboards import back_keyboard, main_menu_keyboard
from models import User
from vpn_service import VPNPanelError, VPNPanelService


def _format_links_block(links: list[str], sub_url: str) -> str:
    lines = [f"🔗 <code>{link}</code>" for link in links if link]
    if sub_url:
        lines.append(f"📡 Subscription: <code>{sub_url}</code>")
    return "\n".join(lines)


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    async with async_session() as session:
        from sqlalchemy import select
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
            f"📛 Name: {user.first_name}\n"
            f"📅 Member since: {user.created_at.strftime('%Y-%m-%d')}\n\n"
        )

        if not config.panel_configured:
            text += "<i>Panel not configured.</i>"
            await update.message.reply_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
            return

        try:
            clients = await VPNPanelService.get_clients_by_telegram_id(telegram_id)
        except VPNPanelError as exc:
            text += f"<i>Panel error: {exc}</i>"
            await update.message.reply_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
            return

        now = datetime.now(timezone.utc)
        online_emails = await VPNPanelService.get_online_emails()

        # Filter to enabled + not expired
        active = []
        for c in clients:
            if not c["enable"]:
                continue
            expiry_dt = datetime.fromtimestamp(c["expiry_time"] / 1000, tz=timezone.utc)
            if expiry_dt > now:
                active.append((c, expiry_dt))

        text += f"📦 <b>Active Subscriptions:</b> {len(active)}\n"

        if active:
            for i, (c, expiry_dt) in enumerate(active, 1):
                remaining = (expiry_dt - now).days
                online_tag = " 🟢 <i>online</i>" if c["email"] in online_emails else ""
                data_label = "Unlimited" if c["total_gb"] == 0 else f"{c['total_gb'] // (1024**3)}GB"
                text += (
                    f"\n─── #{i} ───\n"
                    f"📦 {data_label} | {c['limit_ip']} device(s){online_tag}\n"
                    f"⏳ Expires: {expiry_dt.strftime('%Y-%m-%d')} ({remaining}d left)\n"
                    f"{_format_links_block(c['links'], c['subscription_url'])}"
                )
        else:
            text += "\n<i>No active subscriptions.</i>"

    await update.message.reply_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
