from telegram import ReplyKeyboardMarkup

MAIN_MENU = [
    ["🛒 Buy Subscription"],
    ["👤 My Profile / Subscriptions"],
    ["ℹ️ Help / Support"],
]

PACKAGES = [
    ["10 GB", "20 GB"],
]

DURATIONS = [
    ["1 Month", "2 Months", "3 Months"],
]

PAYMENT = [
    ["✅ I have paid"],
    ["❌ Cancel"],
]

BACK = [
    ["🔙 Main Menu"],
]

BACK_TO_PACKAGES = [
    ["🔙 Back to Packages"],
]


def main_menu_keyboard():
    return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)


def packages_keyboard():
    return ReplyKeyboardMarkup(PACKAGES, resize_keyboard=True)


def durations_keyboard():
    return ReplyKeyboardMarkup(DURATIONS + BACK_TO_PACKAGES, resize_keyboard=True)


def payment_keyboard():
    return ReplyKeyboardMarkup(PAYMENT, resize_keyboard=True)


def back_keyboard():
    return ReplyKeyboardMarkup(BACK, resize_keyboard=True)
