from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from database import async_session
from keyboards import back_keyboard, main_menu_keyboard
from models import Subscription, User


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(User).where(User.telegram_id == update.effective_user.id)
        )
        user = result.scalar_one_or_none()

        if not user:
            await update.message.reply_text(
                "❌ You don't have an account yet. Use /start first.",
                reply_markup=main_menu_keyboard(),
            )
            return

        now = datetime.now(timezone.utc)
        active_subs: list[Subscription] = [
            s for s in user.subscriptions
            if s.is_active and (s.expires_at if s.expires_at.tzinfo else s.expires_at.replace(tzinfo=timezone.utc)) > now
        ]

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
                text += (
                    f"\n─── #{i} ───\n"
                    f"📦 {sub.package_label} | {sub.data_gb}GB\n"
                    f"⏳ Expires: {expires.strftime('%Y-%m-%d')} ({remaining}d left)\n"
                    f"🔗 <code>{sub.vpn_config}</code>\n"
                )
        else:
            text += "\n<i>No active subscriptions.</i>"

    await update.message.reply_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
