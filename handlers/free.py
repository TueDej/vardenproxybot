import logging
from html import escape

from sqlalchemy import select
from telegram import Update
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
from vpn_service import VPNPanelError

log = logging.getLogger(__name__)


async def free_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: fulfill a pending order without going through Zarinpal.

    Works for both new purchases and renewals (detected via order.renew_email).
    """
    query = update.callback_query
    await query.answer()
    if query.message is None or not query.data:
        return

    try:
        order_id = int(query.data.split("|", 1)[1])
    except (ValueError, IndexError):
        return

    user = update.effective_user
    if user is None or user.id not in config.admin_ids:
        await query.message.reply_text("⛔ دسترسی ندارید.", parse_mode="HTML")
        return

    async with async_session() as session:
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if order is None:
            await query.message.reply_text("❌ سفارش یافت نشد.", parse_mode="HTML")
            return
        try:
            if order.renew_email:
                await renew_order(session, order)
                await query.message.reply_text(
                    f"✅ <b>تمدید رایگان (ادمین)</b> انجام شد.\nسفارش #{order.id}",
                    parse_mode="HTML",
                )
            else:
                panel = await approve_order(session, order)
                config_block = format_vpn_config(panel["links"])
                await query.message.reply_text(
                    f"✅ <b>اشتراک رایگان (ادمین)</b> ایجاد شد.\n\n"
                    f"📦 {escape(order.package_label)} | {order.duration_days} روز\n\n"
                    f"{config_block}",
                    parse_mode="HTML",
                )
        except OrderAlreadyApproved:
            await query.message.reply_text("⚠️ این سفارش قبلاً تأیید شده است.", parse_mode="HTML")
            return
        except OrderNotApprovable as exc:
            # 60-day limit is surfaced as OrderNotApprovable with "exceed" in status
            msg = str(exc) or ""
            if "exceed" in msg.lower() or "60" in msg:
                await query.message.reply_text(
                    f"⛔ تمدید ممکن نیست — مجموع زمان اشتراک پس از تمدید بیش از 60 روز می‌شود.\n<code>{escape(msg)}</code>",
                    parse_mode="HTML",
                )
            else:
                await query.message.reply_text("⚠️ این سفارش قابل پرداخت نیست.", parse_mode="HTML")
            return
        except VPNPanelError as exc:
            log.warning("Admin free fulfillment failed for order #%s: %s", order_id, exc)
            await query.message.reply_text(
                "❌ خطای سرور — تایید رایگان انجام نشد.\n"
                f"<code>{escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return
