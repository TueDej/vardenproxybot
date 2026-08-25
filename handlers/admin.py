import logging
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import config
from database import async_session
from handlers.buy import OrderAlreadyApproved, approve_order, format_vpn_config
from models import Order
from vpn_service import VPNPanelError, VPNPanelService

log = logging.getLogger(__name__)


async def _is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id in config.admin_ids


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /approve <order_id>"""
    if not await _is_admin(update):
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
        result = await session.execute(
            select(Order).options(selectinload(Order.user)).where(Order.id == order_id)
        )
        order = result.scalar_one_or_none()

        if not order:
            await update.message.reply_text(f"❌ Order #{order_id} not found.")
            return

        if order.status == "approved":
            await update.message.reply_text(f"⚠️ Order #{order_id} is already approved.")
            return

        try:
            panel = await approve_order(session, order)
        except OrderAlreadyApproved:
            await update.message.reply_text(
                f"⚠️ Order #{order_id} was just approved by someone else."
            )
            return
        except VPNPanelError as exc:
            await update.message.reply_text(
                f"❌ Panel error — order #{order_id} was NOT approved.\n<code>{escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return

        package_label = escape(order.package_label)
        config_block = format_vpn_config(panel["links"])
        await update.message.reply_text(
            f"✅ Order #{order_id} approved.\n"
            f"📦 {package_label} | {order.duration_days} days\n"
            f"{config_block}",
            parse_mode="HTML",
        )

        # Notify the user (selectinload above makes .user safe to access here)
        try:
            await context.bot.send_message(
                chat_id=order.user.telegram_id,
                text=(
                    f"🎉 <b>Your order #{order_id} has been approved!</b>\n\n"
                    f"📦 Package: {package_label}\n"
                    f"{config_block}\n\n"
                    "Import the vless:// link into your V2Ray/Nekoray/Streisand app "
                    "to connect."
                ),
                parse_mode="HTML",
            )
        except TelegramError as exc:
            log.warning(
                "Could not notify user %s about approval of order #%s: %s",
                order.user.telegram_id, order_id, exc,
            )


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /pending — list all pending orders"""
    if not await _is_admin(update):
        await update.message.reply_text("⛔ Access denied.")
        return

    async with async_session() as session:
        result = await session.execute(
            select(Order)
            .options(selectinload(Order.user))
            .where(Order.status == "pending")
            .order_by(Order.created_at.desc())
        )
        orders = result.scalars().all()

    if not orders:
        await update.message.reply_text("📋 No pending orders.")
        return

    lines = ["📋 <b>Pending Orders:</b>\n"]
    for o in orders:
        lines.append(
            f"#{o.id} | User: <code>{o.user.telegram_id}</code> | "
            f"{escape(o.package_label)} | {o.amount_toomans:,} Toomans | "
            f"{o.created_at.strftime('%Y-%m-%d %H:%M')}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /stats — show server stats"""
    if not await _is_admin(update):
        await update.message.reply_text("⛔ Access denied.")
        return

    status = await VPNPanelService.get_server_status()
    text = (
        "📊 <b>Server Stats</b>\n\n"
        f"🖥 Panel: {escape(status['server'])}\n"
        f"🟢 Status: {escape(str(status['status']))}\n"
        f"🟢 Online now: {status['online_users']}\n"
        f"👥 Inbound clients: {status['inbound_clients']}"
    )
    await update.message.reply_text(text, parse_mode="HTML")
