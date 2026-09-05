import logging
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import config
from database import async_session
from handlers.buy import (
    OrderAlreadyApproved,
    OrderNotApprovable,
    approve_order,
    format_vpn_config,
    renew_order,
)
from models import Order
from rtl import rtl
from vpn_service import VPNPanelError, VPNPanelService

log = logging.getLogger(__name__)


def _is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id in config.admin_ids


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /approve <order_id>"""
    if not _is_admin(update):
        await update.message.reply_text(rtl("⛔ دسترسی ندارید."))
        return

    if not context.args:
        await update.message.reply_text(rtl("استفاده: /approve <شماره سفارش>"))
        return

    try:
        order_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(rtl("❌ شماره سفارش نامعتبر است."))
        return

    async with async_session() as session:
        result = await session.execute(
            select(Order).options(selectinload(Order.user)).where(Order.id == order_id)
        )
        order = result.scalar_one_or_none()

        if not order:
            await update.message.reply_text(rtl(f"❌ سفارش #{order_id} یافت نشد."))
            return

        if order.status == "approved":
            await update.message.reply_text(rtl(f"⚠️ سفارش #{order_id} قبلاً تأیید شده است."))
            return

        try:
            if order.renew_email:
                panel = await renew_order(session, order)
            else:
                panel = await approve_order(session, order)
        except OrderAlreadyApproved:
            await update.message.reply_text(
                rtl(f"⚠️ سفارش #{order_id} هم‌اکنون توسط شخص دیگری تأیید شد.")
            )
            return
        except OrderNotApprovable as exc:
            await update.message.reply_text(
                rtl(f"⚠️ سفارش #{order_id} قابل تأیید نیست.\n<code>{escape(str(exc))}</code>"),
                parse_mode="HTML",
            )
            return
        except VPNPanelError as exc:
            await update.message.reply_text(
                rtl(f"❌ خطای سرور — سفارش #{order_id} تأیید نشد.\n<code>{escape(str(exc))}</code>"),
                parse_mode="HTML",
            )
            return

        package_label = escape(order.package_label)
        config_block = format_vpn_config(panel["links"])
        await update.message.reply_text(
            rtl(
                f"✅ سفارش #{order_id} تأیید شد.\n"
                f"📦 {package_label} | {order.duration_days} روز\n"
                f"{config_block}"
            ),
            parse_mode="HTML",
        )

        # Notify the user (selectinload above makes .user safe to access here)
        try:
            await context.bot.send_message(
                chat_id=order.user.telegram_id,
                text=rtl(
                    f"🎉 <b>سفارش #{order_id} شما تأیید شد!</b>\n\n"
                    f"📦 پکیج: {package_label}\n"
                    f"{config_block}\n\n"
                    "لینک vless را در برنامه V2Ray/Nekoray/Streisand وارد کنید تا متصل شوید."
                ),
                parse_mode="HTML",
            )
        except TelegramError as exc:
            log.warning(
                "Could not notify user %s about approval of order #%s: %s",
                order.user.telegram_id,
                order_id,
                exc,
            )


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /pending — list all pending orders"""
    if not _is_admin(update):
        await update.message.reply_text(rtl("⛔ دسترسی ندارید."))
        return

    async with async_session() as session:
        result = await session.execute(
            select(Order)
            .options(selectinload(Order.user))
            .where(Order.status == "pending")
            .order_by(Order.created_at.desc())
            .limit(50)
        )
        orders = result.scalars().all()

    if not orders:
        await update.message.reply_text(rtl("📋 سفارش در انتظار تأییدی وجود ندارد."))
        return

    lines = ["📋 <b>سفارش‌های در انتظار تأیید:</b>\n"]
    for o in orders:
        lines.append(
            f"#{o.id} | کاربر: <code>{o.user.telegram_id}</code> | "
            f"{escape(o.package_label)} | {o.amount_toomans:,} تومان | "
            f"{o.created_at.strftime('%Y-%m-%d %H:%M')}"
        )
    text = rtl("\n".join(lines))
    if len(text) > 4000:
        text = text[:3990] + "\n… (truncated, 50 shown)"
    await update.message.reply_text(text, parse_mode="HTML")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /stats — show server stats"""
    if not _is_admin(update):
        await update.message.reply_text(rtl("⛔ دسترسی ندارید."))
        return

    status = await VPNPanelService.get_server_status()
    text = rtl(
        "📊 <b>وضعیت سرور</b>\n\n"
        f"🖥 پنل: {escape(status['server'])}\n"
        f"🟢 وضعیت: {escape(str(status['status']))}\n"
        f"🟢 آنلاین در لحظه: {status['online_users']}\n"
        f"👥 کلاینت‌های اینباند: {status['inbound_clients']}"
    )
    await update.message.reply_text(text, parse_mode="HTML")
