import atexit
import contextlib
import fcntl
import logging
import os

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import payment_server
from config import config
from database import dispose_engine, init_db
from handlers.admin import approve, pending, stats
from handlers.buy import (
    buy_start,
    cancel_order,
    package_selected,
)
from handlers.free import free_confirm_callback
from handlers.profile import profile
from handlers.renew import renew_callback
from handlers.start import help_command, start
from keyboards import main_menu_keyboard
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
    # Single-loop init: runs on the same event loop as polling (fixes double asyncio.run)
    await init_db()
    if config.zarinpal_configured:
        runner = await payment_server.start_payment_server(application)
        application.bot_data["payment_runner"] = runner


async def _post_shutdown(application: Application) -> None:
    runner = application.bot_data.get("payment_runner")
    if runner:
        await payment_server.stop_payment_server(runner)
    # Release DB engine connections cleanly (important for SQLite WAL)
    with contextlib.suppress(Exception):
        await dispose_engine()


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route text messages based on button presses.

    Awaiting-payment guard: while a pending order exists (DB status pending),
    any navigation / text other than explicit ❌ انصراف auto-cancels the
    pending order(s) and informs the user. This prevents orphan pendings
    when the user leaves the payment screen. Cancel is DB-driven (survives
    restart) and the payment callback will auto-reverse late payments.
    """
    msg = update.effective_message
    if msg is None or not getattr(msg, "text", None):
        return
    # Avoid reacting to messages that are actually commands (safety if filter changes)
    text = (msg.text or "").strip()

    # Explicit cancel — let cancel_order handle its own UX
    if text == "❌ انصراف" or text == "❌ Cancel":
        await cancel_order(update, context)
        return

    # ── Awaiting-payment guard: any other input while pending → auto-cancel ──
    telegram_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    if telegram_id is not None:
        try:
            from handlers.buy import cancel_all_pending_for_user

            cancelled = await cancel_all_pending_for_user(telegram_id, context, chat_id)
            if cancelled:
                ids_str = ", #".join(str(i) for i in cancelled)
                await update.effective_message.reply_text(
                    f"❌ سفارش #{ids_str} به‌صورت خودکار <b>لغو</b> شد چون به بخش دیگری رفتید.\n"
                    "💡 اگر مبلغی پرداخت کرده‌اید، به‌صورت خودکار به حساب شما بازگردانده می‌شود.",
                    parse_mode="HTML",
                    reply_markup=main_menu_keyboard(),
                )
                # fall through to handle the new request normally — now on main menu
        except Exception:
            log.warning("pending guard failed", exc_info=True)

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
                "❌ پکیج نامعتبر است؛ لطفاً از دکمه‌های زیر استفاده کنید.",
                reply_markup=main_menu_keyboard(),
            )
            return

    # Legacy labels still sitting on old clients' keyboards
    elif text == "🔙 Main Menu" or text == "🏠 Home":
        await start(update, context)

    # Unknown — return to home so user is not stuck on cancel-only keyboard
    else:
        await update.effective_message.reply_text(
            "❓ گزینه نامعتبر است؛ لطفاً از دکمه‌های زیر استفاده کنید.",
            reply_markup=main_menu_keyboard(),
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

    # Prevent multiple instances using a file lock — secure against symlink races
    # Prefer /run/lock (1777 with sticky, safer than /tmp) fallback to /tmp.
    # Use O_CREAT|O_EXCL|O_NOFOLLOW to avoid following attacker symlinks.
    lock_file = None
    lock_path = None
    for cand in ("/run/lock/vardenproxybot.lock", "/tmp/vardenproxybot.lock"):
        try:
            parent = os.path.dirname(cand)
            if not os.path.isdir(parent):
                continue
            # Try atomic create — fails if file/symlink exists
            try:
                fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                lock_file = os.fdopen(fd, "w")
                lock_path = cand
                break
            except FileExistsError:
                # Exists — open without EXCL but with NOFOLLOW to avoid symlink
                try:
                    fd = os.open(cand, os.O_RDWR | os.O_NOFOLLOW)
                    # Verify not a symlink via lstat (defense in depth)
                    st = os.lstat(cand)
                    import stat as _stat

                    if _stat.S_ISLNK(st.st_mode):
                        raise OSError("lock is symlink")
                    # Ensure 600 perms
                    with contextlib.suppress(Exception):
                        os.fchmod(fd, 0o600)
                    lock_file = os.fdopen(fd, "w")
                    lock_path = cand
                    break
                except OSError as e:
                    # Symlink or not accessible — remove stale symlink if we can and retry once
                    if "symlink" in str(e).lower() or "loop" in str(e).lower():
                        with contextlib.suppress(Exception):
                            os.unlink(cand)
                        try:
                            fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                            lock_file = os.fdopen(fd, "w")
                            lock_path = cand
                            break
                        except OSError:
                            continue
                    continue
            except OSError:
                continue
        except OSError:
            continue
    if lock_file is None:
        # Last resort fallback (should not happen) — unsafe but better than no lock
        log.warning("Secure lock creation failed, falling back to /tmp")
        lock_file = open("/tmp/vardenproxybot.lock", "w")
        lock_path = "/tmp/vardenproxybot.lock"
        with contextlib.suppress(Exception):
            os.fchmod(lock_file.fileno(), 0o600)
    else:
        with contextlib.suppress(Exception):
            os.fchmod(lock_file.fileno(), 0o600)
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("Another instance is already running. Exiting.")
        with contextlib.suppress(Exception):
            lock_file.close()
        return

    # Ensure lock is released on exit
    def _release_lock():
        with contextlib.suppress(Exception):
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        with contextlib.suppress(Exception):
            lock_file.close()

    atexit.register(_release_lock)

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

    # Inline button callbacks (e.g. renew subscription, admin free confirm)
    app.add_handler(CallbackQueryHandler(renew_callback, pattern=r"^renew\|"))
    app.add_handler(CallbackQueryHandler(free_confirm_callback, pattern=r"^free\|"))

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
    try:
        app.run_polling()
    finally:
        # Ensure lock released; DB engine is disposed in _post_shutdown (same loop)
        with contextlib.suppress(Exception):
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        with contextlib.suppress(Exception):
            lock_file.close()
        # Do not unlink lock file — flock is released, file stays for next run
        # (unlinking while another waiter holds flock would be racy)


if __name__ == "__main__":
    main()
