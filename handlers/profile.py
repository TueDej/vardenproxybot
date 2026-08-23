from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from database import async_session
from handlers.buy import format_vpn_config as config_block
from keyboards import back_keyboard, main_menu_keyboard
from models import Subscription, User
from vpn_service import VPNPanelService


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(User).where(User.telegram_id == update.effective_user.id)
        )
        user = result.scalar_one_or_none()

        if not user:
            await update.message.reply_text(
                "🛒 You don't have an active subscription yet.\n\n"
                "Use <b>🛒 Buy Subscription</b> to get started!",
                reply_markup=main_menu_keyboard(),
                parse_mode="HTML",
            )
            return

        now = datetime.now(timezone.utc)
        active_subs: list[Subscription] = []
        expired_subs: list[Subscription] = []
        for s in user.subscriptions:
            if not s.is_active:
                continue
            expires = s.expires_at if s.expires_at.tzinfo else s.expires_at.replace(tzinfo=timezone.utc)
            (active_subs if expires > now else expired_subs).append(s)

        # Lazily flag panel-expired subscriptions as inactive
        if expired_subs:
            for s in expired_subs:
                s.is_active = False
            await session.commit()

        text = (
            f"👤 <b>Profile</b>\n\n"
            f"🆔 User ID: <code>{user.telegram_id}</code>\n"
            f"📛 Name: {user.first_name}\n"
            f"📅 Member since: {user.created_at.strftime('%Y-%m-%d')}\n\n"
            f"📦 <b>Active Subscriptions:</b> {len(active_subs)}\n"
        )

        if active_subs:
            for i, sub in enumerate(active_subs, 1):
                expires = sub.expires_at if sub.expires_at.tzinfo else sub.expires_at.replace(tzinfo=timezone.utc)
                remaining = (expires - now).days
                sub_url = await VPNPanelService.subscription_url(sub.sub_id)
                text += (
                    f"\n─── #{i} ───\n"
                    f"📦 {sub.package_label} | {sub.data_gb}GB | {sub.duration_days}d\n"
                    f"⏳ Expires: {expires.strftime('%Y-%m-%d')} ({remaining}d left)\n"
                    f"{config_block(sub, sub_url)}"
                )
        else:
            text += "\n<i>No active subscriptions.</i>"

    await update.message.reply_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
