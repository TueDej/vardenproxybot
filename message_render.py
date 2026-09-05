"""Shared renderer for VPN config messages — used by profile and admin forward.

Matches the exact format shown in the user's profile (`handlers/profile.py`)
so that admin-forwarded configs are indistinguishable from the profile view.

`subscription_card()` is the single entry point every fulfillment path
(purchase, renewal, gift, admin approve, payment callback) must use, so a
delivered config always looks exactly like the profile card.
"""

try:
    from datetime import UTC
except ImportError:  # Python <3.11
    from datetime import timezone

    UTC = timezone.utc  # type: ignore[no-redef]  # noqa: UP017
import contextlib
from datetime import datetime
from html import escape

from config import config as _config
from rtl import rtl as _rtl
from vpn_service import VPNPanelService, build_expiry_ms, client_used_bytes


def expiry_dt(ms: int) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def data_label(total_gb: int) -> str:
    return "Unlimited" if total_gb == 0 else f"{total_gb // (1024 ** 3)}GB"


def remaining_data_label(total_bytes: int, used_bytes: int | None) -> str:
    """Remaining traffic label: unlimited plans show نامحدود, unknown usage نامشخص."""
    total = max(0, int(total_bytes or 0))
    if total == 0:
        return "نامحدود"
    if used_bytes is None:
        return "نامشخص"
    remaining_gb = max(0, total - max(0, int(used_bytes))) / 1024**3
    return f"{round(remaining_gb, 1):g}GB"


def format_links_block(links: list[str]) -> str:
    if not links:
        return ""
    lines = ["🔗 <b>کانفیگ‌ها:</b>"]
    for link in links:
        lines.append(f"<pre><code>{escape(link)}</code></pre>")
    return _rtl("\n".join(lines))


def format_sub_block(sub_url: str) -> str:
    """Public subscription URL block — import once, auto-refreshes quota/expiry."""
    if not sub_url:
        return ""
    return _rtl(f"🌐 <b>لینک اشتراک (آپدیت خودکار):</b>\n<pre><code>{escape(sub_url)}</code></pre>")


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
    sub_block = format_sub_block(_config.sub_url(str(c.get("sub_id") or "")))
    if sub_block:
        suffix += f"\n\n{sub_block}"
    # One fact per line: mixing LTR ("20GB"), digits and Persian on a single
    # line lets the bidi algorithm reorder them (📦 20GB | 2 دستگاه bug).
    return _rtl(
        f"📦 {data_label(c['total_gb'])}\n"
        f"📱 تعداد دستگاه مجاز: {c['limit_ip']}{online_tag}\n"
        f"📊 حجم باقی‌مانده: {remaining_data_label(c['total_gb'], c.get('used_bytes'))}\n"
        f"{expiry_line}"
        f"{suffix}"
    )


def format_expired_message(c: dict, expiry: datetime | None) -> str:
    when = expiry.strftime("%Y-%m-%d") if expiry else "نامشخص"
    return _rtl(
        f"⌛ <b>منقضی‌شده</b> — {data_label(c['total_gb'])}\n"
        f"📱 تعداد دستگاه مجاز: {c['limit_ip']}\n"
        f"📊 حجم باقی‌مانده: {remaining_data_label(c['total_gb'], c.get('used_bytes'))}\n"
        f"📅 پایان: {when}\n"
        "برای تمدید روی دکمه زیر بزنید یا گزینه 🛒 خرید اشتراک را انتخاب کنید."
    )


def format_disabled_message(c: dict, expiry: datetime | None) -> str:
    return _rtl(
        f"🚫 <b>غیرفعال</b> — {data_label(c['total_gb'])}\n"
        f"📱 تعداد دستگاه مجاز: {c['limit_ip']}\n"
        f"📊 حجم باقی‌مانده: {remaining_data_label(c['total_gb'], c.get('used_bytes'))}\n"
        f"⏳ انقضا: {expiry.strftime('%Y-%m-%d') if expiry else 'ندارد'}\n"
        "با پشتیبانی تماس بگیرید."
    )


def _unwrap_client(obj: dict | None) -> dict:
    """Accept both direct and {"client": {...}}-wrapped panel payloads."""
    if not isinstance(obj, dict):
        return {}
    inner = obj.get("client")
    if isinstance(inner, dict):
        return inner
    return obj


async def subscription_card(
    email: str,
    data_gb: int = 0,
    duration_days: int = 30,
    links: list[str] | None = None,
) -> str:
    """Render one subscription exactly like the profile's active card.

    Live panel data is preferred (quota, device limit, expiry, links,
    online flag); order-derived values are the fallback so the message
    still renders when the panel is unreachable.
    """
    total_bytes = max(0, int(data_gb or 0)) * 1024**3
    expiry_ms = build_expiry_ms(int(duration_days or 30))
    limit_ip = 0
    used_bytes: int | None = None
    sub_id = ""
    live_links: list[str] | None = None
    online = False
    if email:
        info: dict = {}
        with contextlib.suppress(Exception):
            info = _unwrap_client(await VPNPanelService.get_client(email))
        if info:
            with contextlib.suppress(TypeError, ValueError):
                total_bytes = int(info.get("totalGB", total_bytes) or 0)
            with contextlib.suppress(TypeError, ValueError):
                limit_ip = int(info.get("limitIp", 0) or 0)
            with contextlib.suppress(TypeError, ValueError):
                expiry_ms = int(info.get("expiryTime", expiry_ms) or expiry_ms)
            sub_id = str(info.get("subId") or "")
            used_bytes = client_used_bytes(info)
        with contextlib.suppress(Exception):
            live_links = await VPNPanelService.get_client_links(email)
        with contextlib.suppress(Exception):
            online = email in await VPNPanelService.get_online_emails()
    if not live_links:
        live_links = list(links or [])
    if not limit_ip:
        try:
            limit_ip = int(_config.vpn_limit_ip)
        except Exception:
            limit_ip = 0
    now = datetime.now(UTC)
    c = {
        "email": email or "",
        "sub_id": sub_id,
        "total_gb": total_bytes,
        "limit_ip": limit_ip,
        "used_bytes": used_bytes,
        "links": live_links,
    }
    return format_product_message(c, expiry_dt(expiry_ms), {email} if online and email else set(), now)
