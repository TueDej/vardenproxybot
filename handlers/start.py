from html import escape

from telegram import Update
from telegram.ext import ContextTypes

from keyboards import main_menu_keyboard


async def _abort_pending_if_any(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """If a pending order exists, auto-cancel it and inform the user.

    Used for command entry points (/start, /help) which bypass menu_router's
    text guard. Safe to call even when no pending exists.
    """
    telegram_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    if telegram_id is None:
        return
    # Don't interfere with explicit cancel flow
    try:
        txt = (update.effective_message.text or "").strip() if update.effective_message else ""
    except Exception:
        txt = ""
    if txt in ("❌ انصراف", "❌ Cancel"):
        return
    try:
        from handlers.buy import cancel_all_pending_for_user

        cancelled = await cancel_all_pending_for_user(telegram_id, context, chat_id)
        if cancelled:
            ids_str = ", #".join(str(i) for i in cancelled)
            await update.effective_message.reply_text(
                f"❌ سفارش #{ids_str} به‌صورت خودکار <b>لغو</b> شد چون به بخش دیگری رفتید.\n"
                "💡 اگر مبلغی پرداخت کرده‌اید، به‌صورت خودکار به حساب شما بازگردانده می‌شود.",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(),
            )
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _abort_pending_if_any(update, context)
    user = update.effective_user
    welcome_text = (
        f"👋 سلام {escape(user.first_name)} عزیز، خوش آمدید!\n\n"
        "<b>واردن‌پروکسی</b> — دروازه شما به اینترنتی آزاد و امن.\n\n"
        "🛒 <b>خرید اشتراک</b> — انتخاب پلن و تحویل فوری کانفیگ.\n"
        "👤 <b>پروفایل من</b> — مشاهده اشتراک‌های فعال و کانفیگ‌ها.\n"
        "ℹ️ <b>راهنما و پشتیبانی</b> — پاسخ پرسش‌های متداول و ارتباط با ما.\n\n"
        "برای شروع، یکی از گزینه‌های زیر را انتخاب کنید:"
    )
    await update.message.reply_text(
        welcome_text, reply_markup=main_menu_keyboard(), parse_mode="HTML"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _abort_pending_if_any(update, context)
    text = (
        "ℹ️ <b>راهنما و پشتیبانی</b>\n\n"
        "• برای تهیه اشتراک، گزینه <b>🛒 خرید اشتراک</b> را انتخاب کنید.\n"
        "• پس از پرداخت از طریق درگاه زرین‌پال، اشتراک شما به‌صورت خودکار فعال و کانفیگ آن تحویل داده می‌شود.\n"
        "• اشتراک‌ها و کانفیگ‌های فعال خود را در بخش <b>👤 پروفایل من</b> مشاهده کنید.\n\n"
        "📩 پشتیبانی: https://t.me/vardenERR"
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="HTML")
