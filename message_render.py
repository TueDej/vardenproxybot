"""Shared renderer for VPN config messages — used by profile and admin forward.

Matches the exact format shown in the user's profile (`handlers/profile.py`)
so that admin-forwarded configs are indistinguishable from the profile view.
"""

try:
    from datetime import UTC
except ImportError:  # Python <3.11
    from datetime import timezone

    UTC = timezone.utc  # type: ignore[no-redef]  # noqa: UP017
from datetime import datetime
from html import escape

from rtl import rtl as _rtl


def expiry_dt(ms: int) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def data_label(total_gb: int) -> str:
    return "Unlimited" if total_gb == 0 else f"{total_gb // (1024 ** 3)}GB"


def format_links_block(links: list[str]) -> str:
    if not links:
        return ""
    lines = ["🔗 <b>کانفیگ‌ها:</b>"]
    for link in links:
        lines.append(f"<pre><code>{escape(link)}</code></pre>")
    return _rtl("\n".join(lines))


def format_product_message(
    c: dict, expiry: datetime | None, online_emails: set[str], now: datetime
) -> str:
    if expiry is None:
        expiry_line = "⏳ انقضا: ندارد"
    else:
        remaining = (expiry - now).days
        expiry_line = f"⏳ انقضا: {expiry.strftime('%Y-%m-%d')} ({remaining} روز باقی‌مانده)"
    online_tag = " 🟢 <i>آنلاین</i>" if c["email"] in online_emails else ""
    links_block = format_links_block(c["links"])
    suffix = f"\n{links_block}" if links_block else ""
    return _rtl(
        f"📦 {data_label(c['total_gb'])} | {c['limit_ip']} دستگاه{online_tag}\n"
        f"{expiry_line}"
        f"{suffix}"
    )


def format_expired_message(c: dict, expiry: datetime | None) -> str:
    when = expiry.strftime("%Y-%m-%d") if expiry else "نامشخص"
    return _rtl(
        f"⌛ <b>منقضی‌شده</b> — {data_label(c['total_gb'])}، پایان: {when}\n"
        "برای تمدید روی دکمه زیر بزنید یا گزینه 🛒 خرید اشتراک را انتخاب کنید."
    )


def format_disabled_message(c: dict, expiry: datetime | None) -> str:
    return _rtl(
        f"🚫 <b>غیرفعال</b> — {data_label(c['total_gb'])} | {c['limit_ip']} دستگاه\n"
        f"⏳ انقضا: {expiry.strftime('%Y-%m-%d') if expiry else 'ندارد'}\n"
        "با پشتیبانی تماس بگیرید."
    )
