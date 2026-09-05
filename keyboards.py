from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

import packages as _pkg_mod
from rtl import btn as _btn

MAIN_MENU = [
    ["🛒 خرید اشتراک"],
    ["👤 پروفایل و اشتراک‌های من"],
    ["ℹ️ راهنما و پشتیبانی"],
]

HOME_BUTTON = ("🏠 خانه",)  # tuple to prevent accidental mutation
_HOME_ROW = list(HOME_BUTTON)


def _package_buttons():
    try:
        pkgs = _pkg_mod.load_packages()[0]
    except Exception:
        pkgs = _pkg_mod.PACKAGES
    rows = [[_btn(f"{p['label']} - {p['price']:,} تومان")] for p in pkgs]
    rows.append([_btn(c) for c in _HOME_ROW])
    return rows


CANCEL_BUTTON = "❌ انصراف"
CHOICE_HAVE_CODE = "🎟️ دارم کد تخفیف"
CHOICE_NO_CODE = "⏭️ نه، ادامه"


def main_menu_keyboard():
    return ReplyKeyboardMarkup([[_btn(c) for c in row] for row in MAIN_MENU], resize_keyboard=True)


def packages_keyboard():
    return ReplyKeyboardMarkup(_package_buttons(), resize_keyboard=True)


def cancel_keyboard():
    """Awaiting-payment: only explicit cancel.

    Home is intentionally omitted — any navigation away from this state
    auto-cancels the pending order (see menu_router guard). This makes
    the destructive action explicit and prevents orphan pending orders.
    """
    return ReplyKeyboardMarkup(
        [
            [_btn("❌ انصراف")],
        ],
        resize_keyboard=True,
    )


def home_keyboard():
    return ReplyKeyboardMarkup([[_btn(c) for c in _HOME_ROW]], resize_keyboard=True)


def discount_prompt_keyboard() -> InlineKeyboardMarkup:
    """Legacy inline discount prompt (kept for older messages still in chats).

    New prompts use the reply-keyboard `discount_choice_keyboard()` instead,
    which keeps ❌ انصراف visible and needs no callback state.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(_btn("🎟️ بله، کد دارم"), callback_data="disc|yes"),
                InlineKeyboardButton(_btn("⏭️ نه، ادامه بدون تخفیف"), callback_data="disc|no"),
            ]
        ]
    )


def discount_choice_keyboard() -> ReplyKeyboardMarkup:
    """Discount prompt: enter a code, skip, or cancel — all visible at once.

    Reply keyboard (not inline) so the routing stays in menu_router and
    survives bot restarts; ❌ انصراف is always on screen.
    """
    return ReplyKeyboardMarkup(
        [
            [_btn(CHOICE_HAVE_CODE), _btn(CHOICE_NO_CODE)],
            [_btn(CANCEL_BUTTON)],
        ],
        resize_keyboard=True,
    )


def discount_entry_keyboard() -> ReplyKeyboardMarkup:
    """Shown while a discount code is awaited: explicit skip and cancel rows."""
    return ReplyKeyboardMarkup(
        [
            [_btn("⏭️ ادامه بدون تخفیف")],
            [_btn(CANCEL_BUTTON)],
        ],
        resize_keyboard=True,
    )


def payment_keyboard(public_url: str | None, order_id: int, is_admin: bool) -> InlineKeyboardMarkup:
    """Build the payment prompt buttons.

    Non-admins get only the ZarinPal pay button. Admins always get a
    free-confirm button too (so they can provision without paying), plus the
    ZarinPal button when a payment URL is available. Use this in every payment
    prompt (new purchase, renewal, ...) for a single extensible entry point.
    """
    buttons: list[list[InlineKeyboardButton]] = []
    if public_url:
        buttons.append([InlineKeyboardButton(_btn("💳 پرداخت با زرین‌پال"), url=public_url)])
    if is_admin:
        buttons.append(
            [InlineKeyboardButton(_btn("✅ تایید رایگان (ادمین)"), callback_data=f"free|{order_id}")]
        )
    return InlineKeyboardMarkup(buttons)
