from telegram import ReplyKeyboardMarkup

from packages import PACKAGES

MAIN_MENU = [
    ["🛒 Buy Subscription"],
    ["👤 My Profile / Subscriptions"],
    ["ℹ️ Help / Support"],
]

PAYMENT = [
    ["✅ I have paid"],
    ["❌ Cancel"],
]

BACK = [
    ["🔙 Main Menu"],
]


def _package_buttons():
    return [[f"{p['label']} - {p['price']:,} Toomans"] for p in PACKAGES]


def main_menu_keyboard():
    return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)


def packages_keyboard():
    return ReplyKeyboardMarkup(_package_buttons(), resize_keyboard=True)


def payment_keyboard():
    return ReplyKeyboardMarkup(PAYMENT, resize_keyboard=True)


def back_keyboard():
    return ReplyKeyboardMarkup(BACK, resize_keyboard=True)
