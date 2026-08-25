from telegram import ReplyKeyboardMarkup

from packages import PACKAGES

MAIN_MENU = [
    ["🛒 Buy Subscription"],
    ["👤 My Profile / Subscriptions"],
    ["ℹ️ Help / Support"],
]

HOME_BUTTON = ["🏠 Home"]


def _package_buttons():
    rows = [[f"{p['label']} - {p['price']:,} Toomans"] for p in PACKAGES]
    rows.append(HOME_BUTTON)
    return rows


def main_menu_keyboard():
    return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)


def packages_keyboard():
    return ReplyKeyboardMarkup(_package_buttons(), resize_keyboard=True)


def cancel_keyboard():
    """Gateway flow: payment is detected automatically, so only cancel/home."""
    return ReplyKeyboardMarkup(
        [
            ["❌ Cancel"],
            HOME_BUTTON,
        ],
        resize_keyboard=True,
    )


def home_keyboard():
    return ReplyKeyboardMarkup([HOME_BUTTON], resize_keyboard=True)
