import asyncio
import fcntl
import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import payment_server
from config import config
from database import init_db
from handlers.admin import approve, pending, stats
from handlers.buy import (
    buy_start,
    cancel_order,
    package_selected,
)
from handlers.profile import profile
from handlers.start import help_command, start
from vpn_service import VPNPanelService

log = logging.getLogger(__name__)


def _build_request() -> HTTPXRequest:
    """Create HTTPXRequest configured with SOCKS5 proxy and timeouts."""
    kwargs: dict = {
        "connect_timeout": 15.0,
        "read_timeout": 20.0,
        "write_timeout": 20.0,
        "pool_timeout": 10.0,
    }
    proxy_url = config.proxy_url
    if proxy_url:
        kwargs["proxy"] = proxy_url
    # Prevent httpx from inheriting HTTP_PROXY env vars when proxy is explicit.
    kwargs["httpx_kwargs"] = {"trust_env": False}
    return HTTPXRequest(**kwargs)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled handler exceptions and tell the user something went wrong."""
    log.error("Unhandled error processing update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ خطایی رخ داد؛ لطفاً کمی بعد دوباره تلاش کنید."
            )
        except TelegramError:
            log.warning("Could not deliver error notice to the user.")


async def _post_init(application: Application) -> None:
    if config.zarinpal_configured:
        runner = await payment_server.start_payment_server(application)
        application.bot_data["payment_runner"] = runner


async def _post_shutdown(application: Application) -> None:
    runner = application.bot_data.get("payment_runner")
    if runner:
        await payment_server.stop_payment_server(runner)


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route text messages based on button presses."""
    msg = update.effective_message
    if msg is None or not getattr(msg, "text", None):
        return
    # Avoid reacting to messages that are actually commands (safety if filter changes)
    text = (msg.text or "").strip()

    # Main menu buttons
    if text == "🛒 خرید اشتراک":
        await buy_start(update, context)
    elif text == "👤 پروفایل و اشتراک‌های من":
        await profile(update, context)
    elif text == "ℹ️ راهنما و پشتیبانی":
        await help_command(update, context)

    # Navigation
    elif text == "🏠 خانه":
        await start(update, context)

    # Buy flow — only known package labels (prevents arbitrary "تومان" texts from creating orders)
    elif text.endswith("تومان"):
        from handlers.buy import _get_package_map as _gpm

        pm = _gpm()
        if text in pm:
            await package_selected(update, context)
        else:
            await update.effective_message.reply_text(
                "❌ پکیج نامعتبر است؛ لطفاً از دکمه‌های زیر استفاده کنید."
            )
            return
    elif text == "❌ انصراف":
        await cancel_order(update, context)

    # Legacy labels still sitting on old clients' keyboards
    elif text == "🔙 Main Menu" or text == "🏠 Home":
        await start(update, context)
    elif text == "❌ Cancel":
        await cancel_order(update, context)

    # Unknown
    else:
        await update.effective_message.reply_text(
            "❓ گزینه نامعتبر است؛ لطفاً از دکمه‌های زیر استفاده کنید."
        )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not config.bot_token:
        log.error("BOT_TOKEN is not set in .env")
        return

    # Prevent multiple instances using a file lock (must stay open to hold lock)
    lock_file = open("/tmp/vardenproxybot.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("Another instance is already running. Exiting.")
        with __import__("contextlib").suppress(Exception):
            lock_file.close()
        return

    request = _build_request()

    app = (
        Application.builder()
        .token(config.bot_token)
        .request(request)
        .get_updates_request(request)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_error_handler(on_error)

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("stats", stats))

    # Text messages (reply keyboard)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    log.info("Bot is running...")
    if config.proxy_url_redacted:
        log.info("Proxy: %s (enabled)", config.proxy_url_redacted)
    else:
        log.info("Proxy: disabled")
    if config.zarinpal_access_token and config.zarinpal_callback_url:
        _zg = "via ZarinGate (direct bank)" if config.zarinpal_zaringate and not config.zarinpal_sandbox else "via checkout"
        log.info(
            "Payments: Zarinpal (%s) | callback %s | %s",
            "sandbox" if config.zarinpal_sandbox else "LIVE",
            config.zarinpal_callback_url,
            _zg,
        )
    elif config.zarinpal_access_token:
        log.warning(
            "Payments: mock/manual mode — ZARINPAL_ACCESS_TOKEN is set but ZARINPAL_CALLBACK_URL is missing"
        )
    elif config.zarinpal_callback_url:
        log.warning(
            "Payments: mock/manual mode — ZARINPAL_CALLBACK_URL is set but ZARINPAL_ACCESS_TOKEN is missing"
        )
    else:
        log.info("Payments: mock/manual mode (no ZARINPAL_* configured)")
    if not VPNPanelService.is_configured():
        log.warning("3x-ui panel not configured; approvals will fail until PANEL_* vars are set.")
    else:
        log.info("Panel: %s | inbound #%s", config.panel_url, config.xui_inbound_id)
    app.run_polling()


if __name__ == "__main__":
    asyncio.run(init_db())
    main()
