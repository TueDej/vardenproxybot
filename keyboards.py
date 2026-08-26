from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

import packages as _pkg_mod

MAIN_MENU = [
    ["🛒 خرید اشتراک"],
    ["👤 پروفایل و اشتراک‌های من"],
    ["ℹ️ راهنما و پشتیبانی"],
]

HOME_BUTTON = ("🏠 خانه",)  # tuple to prevent accidental mutation
_HOME_ROW = list(HOME_BUTTON)


def _package_buttons():
    try:
        pkgs, _, _ = _pkg_mod.load_packages()
    except Exception:
        pkgs = _pkg_mod.PACKAGES
    rows = [[f"{p['label']} - {p['price']:,} تومان"] for p in pkgs]
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


def payment_keyboard(public_url: str | None, order_id: int, is_admin: bool) -> InlineKeyboardMarkup:
    """Build the payment prompt buttons.

    Non-admins get only the ZarinPal pay button. Admins always get a
    free-confirm button too (so they can provision without paying), plus the
    ZarinPal button when a payment URL is available. Use this in every payment
    prompt (new purchase, renewal, ...) for a single extensible entry point.
    """
    buttons: list[list[InlineKeyboardButton]] = []
    if public_url:
        buttons.append([InlineKeyboardButton("💳 پرداخت با زرین‌پال", url=public_url)])
    if is_admin:
        buttons.append(
            [InlineKeyboardButton("✅ تایید رایگان (ادمین)", callback_data=f"free|{order_id}")]
        )
    return InlineKeyboardMarkup(buttons)
