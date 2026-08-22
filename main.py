from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config import config
from database import init_db
from handlers.admin import approve, pending, stats
from handlers.buy import (
    buy_start,
    cancel_order,
    duration_selected,
    package_selected,
    payment_confirmed,
)
from handlers.profile import profile
from handlers.start import help_command, start
from keyboards import get_main_menu_keyboard


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup(
        [InlineKeyboardButton(text=btn, callback_data=data) for btn, data in row]
        for row in get_main_menu_keyboard()
    )
    await query.edit_message_text(
        "🏠 <b>Main Menu</b>\n\nSelect an option:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


def main():
    if not config.bot_token:
        print("ERROR: BOT_TOKEN is not set in .env")
        return

    app = Application.builder().token(config.bot_token).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("stats", stats))

    # Callback queries
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(buy_start, pattern="^buy$"))
    app.add_handler(CallbackQueryHandler(package_selected, pattern="^pkg_"))
    app.add_handler(CallbackQueryHandler(duration_selected, pattern="^dur_"))
    app.add_handler(CallbackQueryHandler(payment_confirmed, pattern="^paid_"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_"))
    app.add_handler(CallbackQueryHandler(profile, pattern="^profile$"))
    app.add_handler(CallbackQueryHandler(help_command, pattern="^help$"))

    print("🤖 Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    import asyncio
    asyncio.run(init_db())
    main()
