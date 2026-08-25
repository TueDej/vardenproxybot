from html import escape

from telegram import Update
from telegram.ext import ContextTypes

from keyboards import main_menu_keyboard


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    welcome_text = (
        f"👋 سلام {escape(user.first_name)} عزیز، خوش آمدید!\n\n"
        "<b>واردن‌پروکسی</b> — دروازه شما به اینترنتی آزاد و امن.\n\n"
        "🛒 <b>خرید اشتراک</b> — انتخاب پلن و تحویل فوری کانفیگ.\n"
        "👤 <b>پروفایل من</b> — مشاهده اشتراک‌های فعال و کانفیگ‌ها.\n"
        "ℹ️ <b>راهنما و پشتیبانی</b> — پاسخ پرسش‌های متداول و ارتباط با ما.\n\n"
        "برای شروع، یکی از گزینه‌های زیر را انتخاب کنید:"
    )
    await update.message.reply_text(welcome_text, reply_markup=main_menu_keyboard(), parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ <b>راهنما و پشتیبانی</b>\n\n"
        "• برای تهیه اشتراک، گزینه <b>🛒 خرید اشتراک</b> را انتخاب کنید.\n"
        "• پس از پرداخت از طریق درگاه زرین‌پال، اشتراک شما به‌صورت خودکار فعال و کانفیگ آن تحویل داده می‌شود.\n"
        "• اشتراک‌ها و کانفیگ‌های فعال خود را در بخش <b>👤 پروفایل من</b> مشاهده کنید.\n\n"
        "📩 پشتیبانی: @VardenProxySupport"
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="HTML")
