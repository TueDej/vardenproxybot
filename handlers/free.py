import contextlib
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
    RenewalLimitExceeded,
    approve_order,
    renew_order,
)
from keyboards import main_menu_keyboard
from message_render import subscription_card
from models import Order
from rtl import rtl
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
        await query.message.reply_text(rtl("⛔ دسترسی ندارید."), parse_mode="HTML")
        return

    async with async_session() as session:
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if order is None:
            await query.message.reply_text(rtl("❌ سفارش یافت نشد."), parse_mode="HTML")
            return
        try:
            if order.renew_email:
                panel_renew = await renew_order(session, order)
                card_email = panel_renew.get("email") or order.renew_email or ""
            else:
                panel_new = await approve_order(session, order)
                card_email = panel_new.get("email") or order.panel_email or ""
                panel_renew = panel_new
            # Same card the profile shows — one shared renderer for every path.
            card = await subscription_card(
                card_email, order.data_gb, order.duration_days, panel_renew.get("links")
            )
            gift_note = (
                rtl("\n\n🎁 <i>این اشتراک هدیه است — قابل تمدید نیست.</i>")
                if bool(getattr(order, "is_gift", False))
                else ""
            )
            if order.renew_email:
                await query.message.reply_text(
                    rtl(f"✅ <b>تمدید رایگان (ادمین)</b> انجام شد.\nسفارش #{order.id}\n\n{card}")
                    + gift_note,
                    parse_mode="HTML",
                    reply_markup=main_menu_keyboard(),
                )
            else:
                await query.message.reply_text(
                    rtl(f"✅ <b>اشتراک رایگان (ادمین)</b> ایجاد شد.\n\n{card}") + gift_note,
                    parse_mode="HTML",
                    reply_markup=main_menu_keyboard(),
                )
            # Order settled: drop the stale inline pay button (if this message
            # is the gateway prompt) and clear pending markers so the
            # awaiting-payment guard can't auto-cancel anything afterwards.
            # Main-menu keyboard above already replaced the cancel-only one.
            with contextlib.suppress(Exception):
                await query.message.edit_reply_markup(reply_markup=None)
            for _key in (
                "order_id",
                "pay_message_id",
                "pending_order_id",
                "awaiting_discount_code",
                "awaiting_discount_choice",
            ):
                context.user_data.pop(_key, None)
        except OrderAlreadyApproved:
            await query.message.reply_text(rtl("⚠️ این سفارش قبلاً تأیید شده است."), parse_mode="HTML")
            return
        except OrderNotApprovable as exc:
            # 60-day limit is surfaced as RenewalLimitExceeded (a subclass)
            if isinstance(exc, RenewalLimitExceeded):
                await query.message.reply_text(
                    rtl(
                        "⛔ تمدید ممکن نیست — مجموع زمان اشتراک پس از تمدید بیش از 60 روز می‌شود.\n"
                        f"<code>{escape(str(exc))}</code>"
                    ),
                    parse_mode="HTML",
                )
            else:
                await query.message.reply_text(rtl("⚠️ این سفارش قابل پرداخت نیست."), parse_mode="HTML")
            return
        except VPNPanelError as exc:
            log.warning("Admin free fulfillment failed for order #%s: %s", order_id, exc)
            await query.message.reply_text(
                rtl(
                    "❌ خطای سرور — تأیید رایگان انجام نشد.\n"
                    f"<code>{escape(str(exc))}</code>"
                ),
                parse_mode="HTML",
            )
            return
