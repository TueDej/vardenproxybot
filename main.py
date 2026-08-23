from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from config import config
from database import async_session, init_db
from handlers.admin import approve, pending, stats
from handlers.buy import (
    buy_start,
    cancel_order,
    package_selected,
    payment_confirmed,
)
from handlers.profile import profile
from handlers.start import help_command, start
from models import Subscription
from vpn_service import VPNPanelService


def _build_request() -> HTTPXRequest:
    """Create HTTPXRequest configured with SOCKS5 proxy and timeouts."""
    kwargs = {
        "connect_timeout": 15.0,
        "read_timeout": 20.0,
    }
    proxy_url = config.proxy_url
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return HTTPXRequest(**kwargs)


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route text messages based on button presses."""
    text = update.message.text

    # Main menu buttons
    if text == "🛒 Buy Subscription":
        await buy_start(update, context)
    elif text == "👤 My Profile / Subscriptions":
        await profile(update, context)
    elif text == "ℹ️ Help / Support":
        await help_command(update, context)

    # Navigation
    elif text == "🔙 Main Menu":
        await start(update, context)

    # Buy flow
    elif text.endswith("Toomans"):
        await package_selected(update, context)
    elif text == "✅ I have paid":
        await payment_confirmed(update, context)
    elif text == "❌ Cancel":
        await cancel_order(update, context)

    # Unknown
    else:
        await update.message.reply_text("❓ Unknown option. Use the keyboard buttons below.")


async def cleanup_expired_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    """Delete expired clients from the panel and flag their subscriptions inactive."""
    now = datetime.now(timezone.utc)
    async with async_session() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(Subscription).where(Subscription.is_active == True)  # noqa: E712
        )
        subs = result.scalars().all()
        removed = 0
        for sub in subs:
            expires = sub.expires_at if sub.expires_at.tzinfo else sub.expires_at.replace(tzinfo=timezone.utc)
            if expires > now:
                continue
            sub.is_active = False
            if sub.xui_email and await VPNPanelService.delete_client(sub.xui_email):
                removed += 1
        await session.commit()
    if removed:
        print(f"🧹 Removed {removed} expired panel client(s).")


def main():
    if not config.bot_token:
        print("ERROR: BOT_TOKEN is not set in .env")
        return

    # Prevent multiple instances using a file lock
    import fcntl
    lock_file = open("/tmp/vardenproxybot.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("ERROR: Another instance is already running. Exiting.")
        return

    request = _build_request()

    app = (
        Application.builder()
        .token(config.bot_token)
        .request(request)
        .get_updates_request(request)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("stats", stats))

    # Text messages (reply keyboard)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    # Hourly cleanup of expired panel clients
    if app.job_queue:
        app.job_queue.run_repeating(cleanup_expired_subscriptions, interval=3600, first=30)
    else:
        print("WARN: job-queue extra not installed; expired clients won't be auto-removed.")

    print("🤖 Bot is running...")
    if config.proxy_url:
        print(f"🧦 Proxy: {config.proxy_url}")
    if not VPNPanelService.is_configured():
        print("WARN: 3x-ui panel not configured; approvals will fail until PANEL_* vars are set.")
    else:
        print(f"🔒 Panel: {config.panel_url} | inbound #{config.xui_inbound_id}")
    app.run_polling()


if __name__ == "__main__":
    import asyncio
    asyncio.run(init_db())
    main()
