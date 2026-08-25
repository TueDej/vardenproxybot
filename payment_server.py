"""Internal HTTP server receiving Zarinpal payment callbacks.

Binds to a loopback address only; a reverse proxy (Caddy/nginx) terminates
TLS on the public domain and forwards here.
"""

import logging
from html import escape

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from config import config
from database import async_session
from handlers.buy import OrderAlreadyApproved, verify_and_fulfill_order
from models import Order
from vpn_service import VPNPanelError
from zarinpal import ZarinpalError

log = logging.getLogger(__name__)

CALLBACK_PATH = "/zarinpal/callback"

_PAGE = """<!DOCTYPE html>
<html lang="fa"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VardenProxy</title>
<style>body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.box{{text-align:center;padding:2rem}}h1{{font-size:1.4rem}}</style></head>
<body><div class="box"><h1>{title}</h1><p>{body}</p></div></body></html>"""


def _page(title: str, body: str) -> web.Response:
    return web.Response(
        text=_PAGE.format(title=escape(title), body=escape(body)),
        content_type="text/html",
    )


async def handle_zarinpal_callback(request: web.Request) -> web.Response:
    application = request.app["ptb_application"]
    authority = request.rel_url.query.get("Authority", "")
    status = request.rel_url.query.get("Status", "")

    if status.upper() != "OK" or not authority:
        log.info("Callback with non-success status (Status=%r)", status)
        return _page("پرداخت انجام نشد", "پرداخت لغو شد یا ناموفق بود. می‌توانید دوباره تلاش کنید.")

    async with async_session() as session:
        result = await session.execute(
            select(Order)
            .options(selectinload(Order.user))
            .where(Order.payment_authority == authority)
        )
        order = result.scalar_one_or_none()
        if order is None:
            log.warning("Callback for unknown authority %s…", authority[:10])
            return _page("سفارش یافت نشد", "سفارشی برای این پرداخت پیدا نشد. با پشتیبانی تماس بگیرید.")

        chat_id = order.user.telegram_id
        oid = order.id  # snapshot — failures below may expire ORM attributes

        if order.status == "approved":
            return _page("قبلاً تأیید شده", "این سفارش قبلاً تأیید و فعال شده است. به ربات برگردید. ✅")

        try:
            outcome = await verify_and_fulfill_order(session, order)
        except ZarinpalError as exc:
            log.info("Order #%s not verified yet: %s", oid, exc)
            return _page("تأیید نشد", "پرداختی برای این تراکنش ثبت نشده است. اگر مبلغ کسر شده، کمی بعد دوباره تلاش کنید.")
        except VPNPanelError as exc:
            log.error("Order #%s PAID but provisioning failed: %s", oid, exc)
            await _notify(application, chat_id,
                          "✅ Your payment was received, but setting up your config hit a delay. "
                          "We're on it — check My Profile shortly or contact support.")
            return _page("در حال بررسی", "پرداخت شما ثبت شد؛ فعال‌سازی چند دقیقه طول خواهد کشید.")
        except OrderAlreadyApproved:
            return _page("قبلاً تأیید شده", "این سفارش قبلاً تأیید و فعال شده است. به ربات برگردید. ✅")

    ref = outcome["ref_id"]
    await _notify(
        application, chat_id,
        f"🎉 <b>Your payment was confirmed!</b> (order #{oid})\n\n"
        "Your VPN config is ready — check <b>👤 My Profile</b> in the bot.",
    )
    body = f"پرداخت با موفقیت تأیید شد (کد پیگیری: {ref}). به ربات برگردید و کانفیگ خود را دریافت کنید."
    return _page("پرداخت موفق ✅", body)


async def _notify(application, chat_id: int, text: str | None) -> None:
    """Best-effort Telegram notification about the successful payment."""
    try:
        if text is None:
            text = (
                "🎉 <b>Your payment was confirmed!</b>\n\n"
                "Your VPN config is ready — check <b>👤 My Profile</b> in the bot."
            )
        await application.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception:
        log.warning("Could not notify user %s about completed payment", chat_id, exc_info=True)


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def build_app(application) -> web.Application:
    app = web.Application()
    app["ptb_application"] = application
    app.router.add_get(CALLBACK_PATH, handle_zarinpal_callback)
    app.router.add_get("/healthz", handle_health)
    return app


async def start_payment_server(application) -> web.AppRunner:
    """Run the callback listener until the returned runner is cleaned up."""
    runner = web.AppRunner(build_app(application))
    await runner.setup()
    site = web.TCPSite(runner, config.zarinpal_bind_host, config.zarinpal_bind_port)
    await site.start()
    log.info(
        "Payment callback listening on http://%s:%s%s",
        config.zarinpal_bind_host, config.zarinpal_bind_port, CALLBACK_PATH,
    )
    return runner


async def stop_payment_server(runner: web.AppRunner) -> None:
    await runner.cleanup()
