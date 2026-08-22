from datetime import timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import config
from database import async_session
from handlers.buy import _approve_order
from models import Order, Subscription
from vpn_service import VPNPanelService


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /approve <order_id>"""
    if update.effective_user.id not in config.admin_ids:
        await update.message.reply_text("⛔ Access denied.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /approve <order_id>")
        return

    try:
        order_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid order ID.")
        return

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()

        if not order:
            await update.message.reply_text(f"❌ Order #{order_id} not found.")
            return

        if order.status == "approved":
            await update.message.reply_text(f"⚠️ Order #{order_id} is already approved.")
            return

        order.status = "approved"
        subscription = await _approve_order(session, order)

        await update.message.reply_text(
            f"✅ Order #{order_id} approved.\n"
            f"📦 {order.package_label} | {order.duration_days} days\n"
            f"🔗 Config: <code>{subscription.vpn_config}</code>",
            parse_mode="HTML",
        )

        # Notify user
        try:
            await context.bot.send_message(
                chat_id=order.user.telegram_id,
                text=(
                    f"🎉 <b>Your order #{order_id} has been approved!</b>\n\n"
                    f"📦 Package: {order.package_label}\n"
                    f"🔗 Your VPN config:\n<code>{subscription.vpn_config}</code>\n\n"
                    "Import this link into your V2Ray/Nekoray/Streisand app."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /pending — list all pending orders"""
    if update.effective_user.id not in config.admin_ids:
        await update.message.reply_text("⛔ Access denied.")
        return

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(Order).where(Order.status == "pending").order_by(Order.created_at.desc())
        )
        orders = result.scalars().all()

    if not orders:
        await update.message.reply_text("📋 No pending orders.")
        return

    lines = ["📋 <b>Pending Orders:</b>\n"]
    for o in orders:
        lines.append(
            f"#{o.id} | User: <code>{o.user.telegram_id}</code> | "
            f"{o.package_label} | ${o.amount_usd} | {o.created_at.strftime('%Y-%m-%d %H:%M')}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /stats — show server stats"""
    if update.effective_user.id not in config.admin_ids:
        await update.message.reply_text("⛔ Access denied.")
        return

    status = VPNPanelService.get_server_status()
    text = (
        "📊 <b>Server Stats</b>\n\n"
        f"🖥 Server: {status['server']}\n"
        f"🟢 Status: {status['status']}\n"
        f"👥 Active: {status['active_users']}/{status['max_users']}"
    )
    await update.message.reply_text(text, parse_mode="HTML")
