from telegram import Update
from telegram.ext import ContextTypes

from keyboards import main_menu_keyboard


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    welcome_text = (
        f"👋 Welcome, {user.first_name}!\n\n"
        "Welcome to <b>VardenProxy</b> — your gateway to a free and secure internet.\n\n"
        "🛒 <b>Buy Subscription</b> — choose a package and get instant access.\n"
        "👤 <b>My Profile</b> — view your active subscriptions and config links.\n"
        "ℹ️ <b>Help / Support</b> — get assistance from our team.\n\n"
        "Select an option below to get started:"
    )
    await update.message.reply_text(welcome_text, reply_markup=main_menu_keyboard(), parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ <b>Help & Support</b>\n\n"
        "• To purchase a VPN subscription, click <b>🛒 Buy Subscription</b>.\n"
        "• After payment, your config will be issued automatically or after admin approval.\n"
        "• Use <b>👤 My Profile</b> to view your active subscriptions.\n\n"
        "📩 For support, contact: @VardenProxySupport"
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="HTML")
