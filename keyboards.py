from telegram import ReplyKeyboardMarkup

from packages import PACKAGES

MAIN_MENU = [
    ["🛒 خرید اشتراک"],
    ["👤 پروفایل و اشتراک‌های من"],
    ["ℹ️ راهنما و پشتیبانی"],
]

HOME_BUTTON = ("🏠 خانه",)  # tuple to prevent accidental mutation
_HOME_ROW = list(HOME_BUTTON)


def _package_buttons():
    rows = [[f"{p['label']} - {p['price']:,} تومان"] for p in PACKAGES]
    rows.append(_HOME_ROW.copy())
    return rows


def main_menu_keyboard():
    return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)


def packages_keyboard():
    return ReplyKeyboardMarkup(_package_buttons(), resize_keyboard=True)


def cancel_keyboard():
    """Gateway flow: payment is detected automatically, so only cancel/home."""
    return ReplyKeyboardMarkup(
        [
            ["❌ انصراف"],
            _HOME_ROW.copy(),
        ],
        resize_keyboard=True,
    )


def home_keyboard():
    return ReplyKeyboardMarkup([_HOME_ROW.copy()], resize_keyboard=True)
