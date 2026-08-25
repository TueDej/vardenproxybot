"""Internal HTTP server receiving Zarinpal payment callbacks + admin panel.

Binds to a loopback address only; a reverse proxy (Caddy/nginx) terminates
TLS on the public domain and forwards here.
"""

import base64
import hmac
import logging
import pathlib
from datetime import UTC, datetime
from html import escape

from aiohttp import web
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

from config import config
from database import async_session
from handlers.buy import OrderAlreadyApproved, OrderNotApprovable, verify_and_fulfill_order
from keyboards import main_menu_keyboard
from models import Order, User
from vpn_service import VPNPanelError
from zarinpal import ZarinpalError, reverse_payment, verify_payment

log = logging.getLogger(__name__)

CALLBACK_PATH = "/zarinpal/callback"
ADMIN_PREFIX = "/admin"

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


# ── BasicAuth helpers ───────────────────────────────────────────────


def _is_admin_authenticated(request: web.Request) -> bool:
    if not config.admin_panel_enabled:
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:].strip()).decode("utf-8", errors="strict")
    except Exception:
        return False
    if ":" not in decoded:
        return False
    user, pwd = decoded.split(":", 1)
    # constant-time compare
    user_ok = hmac.compare_digest(user, config.admin_panel_user)
    pass_ok = hmac.compare_digest(pwd, config.admin_panel_pass)
    return user_ok and pass_ok


def _admin_auth_required_response() -> web.Response:
    headers = {"WWW-Authenticate": 'Basic realm="Varden Admin"'}
    return web.Response(status=401, text="Authentication required", headers=headers)


@web.middleware
async def admin_auth_middleware(request: web.Request, handler):
    if request.path.startswith(ADMIN_PREFIX):
        if not config.admin_panel_enabled:
            return web.Response(
                status=503, text="Admin panel not configured (ADMIN_PANEL_USER/PASS missing)"
            )
        if not _is_admin_authenticated(request):
            return _admin_auth_required_response()
    return await handler(request)


# ── Admin API ─────────────────────────────────────────────────────────


async def handle_admin_stats(request: web.Request) -> web.Response:
    async with async_session() as session:
        # total orders
        total_orders = (await session.execute(select(func.count(Order.id)))).scalar() or 0
        # by status
        status_rows = (
            await session.execute(select(Order.status, func.count(Order.id)).group_by(Order.status))
        ).all()
        by_status = {row[0]: row[1] for row in status_rows}
        # total revenue approved only
        total_revenue = (
            await session.execute(
                select(func.coalesce(func.sum(Order.amount_toomans), 0)).where(
                    Order.status == "approved"
                )
            )
        ).scalar() or 0
        # total users
        total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
        # today revenue / pending
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        today_orders = (
            await session.execute(
                select(func.count(Order.id)).where(Order.created_at >= today_start)
            )
        ).scalar() or 0
        pending = by_status.get("pending", 0)
    return web.json_response(
        {
            "total_orders": total_orders,
            "by_status": by_status,
            "total_revenue": total_revenue,
            "total_users": total_users,
            "today_orders": today_orders,
            "pending": pending,
        }
    )


def _parse_pagination(request: web.Request) -> tuple[int, int]:
    try:
        page = max(1, int(request.rel_url.query.get("page", "1")))
    except ValueError:
        page = 1
    try:
        limit = int(request.rel_url.query.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 100))
    return page, limit


def _parse_date_param(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    # Accept YYYY-MM-DD or ISO
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


async def handle_admin_orders(request: web.Request) -> web.Response:
    q = (request.rel_url.query.get("q") or "").strip()
    status = (request.rel_url.query.get("status") or "").strip()
    package = (request.rel_url.query.get("package") or "").strip()
    from_s = _parse_date_param(request.rel_url.query.get("from"))
    to_s = _parse_date_param(request.rel_url.query.get("to"))
    sort = (request.rel_url.query.get("sort") or "created_at.desc").strip()
    page, limit = _parse_pagination(request)

    async with async_session() as session:
        base = select(Order).options(selectinload(Order.user))
        # Join User for search on telegram_id/username - use outer join to keep orders even if user missing? inner is fine.
        # We'll build conditions including User fields via exists subquery to avoid join duplication complexity
        # Simpler: join
        need_user_join = bool(q)
        if need_user_join:
            base = base.join(User, Order.user_id == User.id)

        conditions = []
        if status and status in ("pending", "approved", "rejected", "cancelled"):
            conditions.append(Order.status == status)
        if package:
            conditions.append(Order.package_label == package)
        if from_s:
            conditions.append(Order.created_at >= from_s)
        if to_s:
            conditions.append(Order.created_at <= to_s)
        if q:
            like = f"%{q}%"
            # For telegram_id numeric exact match
            or_parts = [
                Order.payment_ref_id.ilike(like),
                Order.payment_authority.ilike(like),
                Order.panel_email.ilike(like),
                Order.sub_id.ilike(like),
                Order.package_label.ilike(like),
            ]
            if need_user_join:
                or_parts.extend(
                    [
                        User.username.ilike(like),
                        User.first_name.ilike(like),
                    ]
                )
                # telegram_id as string
                if q.isdigit():
                    try:
                        tid = int(q)
                        or_parts.append(User.telegram_id == tid)
                        or_parts.append(Order.id == tid)
                    except ValueError:
                        pass
            conditions.append(or_(*or_parts))

        if conditions:
            base = base.where(and_(*conditions))

        # sort
        if sort == "created_at.asc":
            base = base.order_by(Order.created_at.asc())
        elif sort == "amount.desc":
            base = base.order_by(Order.amount_toomans.desc())
        elif sort == "amount.asc":
            base = base.order_by(Order.amount_toomans.asc())
        else:
            base = base.order_by(Order.created_at.desc())

        # count
        count_q = select(func.count()).select_from(base.subquery())
        total = (await session.execute(count_q)).scalar() or 0

        # pagination
        base = base.offset((page - 1) * limit).limit(limit)
        result = await session.execute(base)
        orders = result.scalars().unique().all()

        items = []
        for o in orders:
            try:
                username = o.user.username if hasattr(o, "user") and o.user else None
                telegram_id = o.user.telegram_id if hasattr(o, "user") and o.user else None
                first_name = o.user.first_name if hasattr(o, "user") and o.user else None
            except Exception:
                username = None
                telegram_id = None
                first_name = None
            items.append(
                {
                    "id": o.id,
                    "user_id": o.user_id,
                    "telegram_id": telegram_id,
                    "username": username,
                    "first_name": first_name,
                    "package_label": o.package_label,
                    "duration_days": o.duration_days,
                    "data_gb": o.data_gb,
                    "amount_toomans": o.amount_toomans,
                    "status": o.status,
                    "panel_email": o.panel_email,
                    "sub_id": o.sub_id,
                    "payment_authority": o.payment_authority,
                    "payment_ref_id": o.payment_ref_id,
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                }
            )

        return web.json_response({"total": total, "page": page, "limit": limit, "items": items})


async def handle_admin_users(request: web.Request) -> web.Response:
    q = (request.rel_url.query.get("q") or "").strip()
    from_s = _parse_date_param(request.rel_url.query.get("from"))
    to_s = _parse_date_param(request.rel_url.query.get("to"))
    page, limit = _parse_pagination(request)

    async with async_session() as session:
        base = select(User)
        conditions = []
        if from_s:
            conditions.append(User.created_at >= from_s)
        if to_s:
            conditions.append(User.created_at <= to_s)
        if q:
            like = f"%{q}%"
            or_parts = [
                User.username.ilike(like),
                User.first_name.ilike(like),
            ]
            if q.isdigit():
                try:
                    tid = int(q)
                    or_parts.append(User.telegram_id == tid)
                    or_parts.append(User.id == tid)
                except ValueError:
                    pass
            conditions.append(or_(*or_parts))
        if conditions:
            base = base.where(and_(*conditions))
        base = base.order_by(User.created_at.desc())

        count_q = select(func.count()).select_from(base.subquery())
        total = (await session.execute(count_q)).scalar() or 0

        base = base.offset((page - 1) * limit).limit(limit)
        result = await session.execute(base)
        users = result.scalars().all()

        # For each user, count orders (could be optimized with subquery, but keep simple)
        items = []
        for u in users:
            order_count = (
                await session.execute(select(func.count(Order.id)).where(Order.user_id == u.id))
            ).scalar() or 0
            items.append(
                {
                    "id": u.id,
                    "telegram_id": u.telegram_id,
                    "username": u.username,
                    "first_name": u.first_name,
                    "created_at": u.created_at.isoformat() if u.created_at else None,
                    "order_count": order_count,
                }
            )
        return web.json_response({"total": total, "page": page, "limit": limit, "items": items})


# ── Admin static ────────────────────────────────────────────────────


def _admin_static_dir() -> pathlib.Path:
    # payment_server.py is in project root, admin_static is sibling dir
    return pathlib.Path(__file__).parent / "admin_static"


async def handle_admin_index(request: web.Request) -> web.Response:
    # Already authenticated via middleware
    static_dir = _admin_static_dir()
    index_path = static_dir / "index.html"
    if index_path.exists():
        return web.FileResponse(index_path)
    # Fallback inline if files not deployed yet
    return web.Response(
        text="<h1>Admin panel not deployed</h1><p>admin_static/index.html missing</p>",
        content_type="text/html",
    )


async def _paid_cancelled_flow(application, _session, order, authority, outcome) -> web.Response:
    """A payment succeeded for a cancelled/rejected order — auto-refund it.

    Uses Zarinpal's Reverse API (works fee-free within 30 minutes of the
    payment, provided the store's server IP is whitelisted in the terminal
    settings). Falls back to admin escalation when reversal isn't possible.
    """
    oid = order.id
    try:
        await reverse_payment(authority)
    except ZarinpalError as exc:
        log.error(
            "Order #%s was CANCELLED but payment succeeded (ref %s) and auto-refund failed: %s",
            oid,
            outcome["ref_id"],
            exc,
        )
        await _notify_admins(
            application,
            f"⚠️ <b>پرداخت سفارش لغوشده!</b> سفارش #{escape(str(oid))} (کد پیگیری "
            f"<code>{escape(str(outcome['ref_id']))}</code>) توسط کاربر لغو شده اما پرداخت آن انجام شد و استرداد خودکار ممکن نشد: {escape(str(exc))}. "
            "لطفاً دستی رسیدگی کنید.",
        )
        await _notify(
            application,
            order.user.telegram_id,
            f"ℹ️ سفارش #{escape(str(oid))} قبلاً لغو شده بود، اما پرداخت آن انجام شد؛ تیم پشتیبانی به‌زودی با شما تماس می‌گیرد.",
        )
        return _page(
            "نیاز به بررسی",
            "سفارش قبلاً لغو شده اما پرداخت انجام شده است؛ تیم پشتیبانی با شما تماس می‌گیرد.",
        )

    log.info(
        "Order #%s was paid after cancellation; auto-reversed (ref %s)", oid, outcome["ref_id"]
    )
    await _notify_admins(
        application,
        f"ℹ️ سفارش لغوشده #{escape(str(oid))} پرداخت شد — مبلغ به‌صورت خودکار مستردد شد "
        f"(کد پیگیری <code>{escape(str(outcome['ref_id']))}</code>).",
    )
    await _notify(
        application,
        order.user.telegram_id,
        f"💳 سفارش #{escape(str(oid))} قبلاً لغو شده بود؛ به همین دلیل مبلغ پرداختی به‌صورت خودکار به کارت شما بازگشت داده شد.",
    )
    return _page(
        "مبلغ مستردد شد ✅",
        "سفارش لغو شده بود؛ مبلغ پرداختی به‌صورت خودکار به کارت شما بازگشت داده شد.",
    )


async def handle_zarinpal_callback(request: web.Request) -> web.Response:
    application = request.app["ptb_application"]
    # Zarinpal docs: Authority + Status=OK/NOK via query string
    authority = (request.rel_url.query.get("Authority", "") or "").strip()
    status = (request.rel_url.query.get("Status", "") or "").strip()

    # Authority is a 6-36 char alphanumeric (sandbox starts with S, live with A); reject overly long/unsafe values.
    if len(authority) > 64:
        log.warning("Callback with overly long authority (%d chars); rejecting.", len(authority))
        return _page("پرداخت انجام نشد", "پرداخت لغو شد یا ناموفق بود. می‌توانید دوباره تلاش کنید.")
    if authority and (
        not authority.replace("_", "").replace("-", "").isalnum()
        or not all(c.isalnum() or c in "-_" for c in authority)
    ):
        log.warning("Callback with invalid authority characters.")
        return _page("سفارش یافت نشد", "سفارشی برای این پرداخت پیدا نشد. با پشتیبانی تماس بگیرید.")

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
            # Do not leak authority prefix; log length only to avoid enumeration.
            log.warning("Callback for unknown authority (len=%d)", len(authority))
            return _page(
                "سفارش یافت نشد", "سفارشی برای این پرداخت پیدا نشد. با پشتیبانی تماس بگیرید."
            )

        chat_id = order.user.telegram_id
        oid = order.id  # snapshot — failures below may expire ORM attributes

        if order.status == "approved":
            return _page(
                "قبلاً تأیید شده", "این سفارش قبلاً تأیید و فعال شده است. به ربات برگردید. ✅"
            )

        if order.status != "pending":
            # Cancelled/rejected before the money landed. The StartPay link
            # itself can't be revoked, so check whether the user paid anyway
            # and auto-refund via the Reverse API when they did.
            try:
                outcome = await verify_payment(order.payment_authority, order.amount_toomans)
            except ZarinpalError:
                return _page(
                    "سفارش لغو شده",
                    "این سفارش لغو شده و وجهی بابت آن پرداخت نشده است. در صورت نیاز دوباره خرید کنید.",
                )
            return await _paid_cancelled_flow(application, session, order, authority, outcome)

        try:
            outcome = await verify_and_fulfill_order(session, order)
        except ZarinpalError as exc:
            log.info("Order #%s not verified yet: %s", oid, exc)
            return _page(
                "تأیید نشد",
                "پرداختی برای این تراکنش ثبت نشده است. اگر مبلغ کسر شده، کمی بعد دوباره تلاش کنید.",
            )
        except VPNPanelError as exc:
            log.error("Order #%s PAID but provisioning failed: %s", oid, exc)
            await _notify(
                application,
                chat_id,
                "✅ پرداخت شما دریافت شد، اما آماده‌سازی کانفیگ اندکی طول کشیده است. "
                "به‌زودی از بخش «👤 پروفایل من» بررسی کنید یا با پشتیبانی تماس بگیرید.",
            )
            return _page("در حال بررسی", "پرداخت شما ثبت شد؛ فعال‌سازی چند دقیقه طول خواهد کشید.")
        except OrderAlreadyApproved:
            return _page(
                "قبلاً تأیید شده", "این سفارش قبلاً تأیید و فعال شده است. به ربات برگردید. ✅"
            )
        except OrderNotApprovable:
            # Order was cancelled while we were verifying — money may have
            # moved; verify explicitly and auto-refund.
            try:
                outcome = await verify_payment(order.payment_authority, order.amount_toomans)
            except ZarinpalError:
                return _page(
                    "سفارش لغو شده",
                    "این سفارش لغو شده و وجهی بابت آن پرداخت نشده است. در صورت نیاز دوباره خرید کنید.",
                )
            return await _paid_cancelled_flow(application, session, order, authority, outcome)

    ref = outcome["ref_id"]
    await _notify(
        application,
        chat_id,
        f"🎉 <b>پرداخت شما با موفقیت تأیید شد!</b> (سفارش #{escape(str(oid))})\n\n"
        "کانفیگ شما آماده است؛ آن را از بخش «👤 پروفایل من» دریافت کنید.",
    )
    body = f"پرداخت با موفقیت تأیید شد (کد پیگیری: {escape(str(ref))}). به ربات برگردید و کانفیگ خود را دریافت کنید."
    return _page("پرداخت موفق ✅", body)


async def _notify(application, chat_id: int, text: str | None) -> None:
    """Best-effort Telegram notification about the completed payment.

    Attaches the main-menu keyboard so leftover pre-payment buttons are
    replaced once payment is settled.
    """
    try:
        if text is None:
            text = (
                "🎉 <b>پرداخت شما با موفقیت تأیید شد!</b>\n\n"
                "کانفیگ شما آماده است؛ آن را از بخش «👤 پروفایل من» دریافت کنید."
            )
        await application.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        log.warning("Could not notify user %s about completed payment", chat_id, exc_info=True)


async def _notify_admins(application, text: str) -> None:
    """Best-effort alert to every configured admin."""
    for admin_id in config.admin_ids:
        try:
            await application.bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
        except Exception:
            log.warning("Could not alert admin %s", admin_id, exc_info=True)


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def build_app(application) -> web.Application:
    app = web.Application(middlewares=[admin_auth_middleware])
    app["ptb_application"] = application
    app.router.add_get(CALLBACK_PATH, handle_zarinpal_callback)
    app.router.add_get("/healthz", handle_health)
    # Admin panel (protected by BasicAuth middleware)
    app.router.add_get(ADMIN_PREFIX, handle_admin_index)
    app.router.add_get(ADMIN_PREFIX + "/", handle_admin_index)
    app.router.add_get(ADMIN_PREFIX + "/api/stats", handle_admin_stats)
    app.router.add_get(ADMIN_PREFIX + "/api/orders", handle_admin_orders)
    app.router.add_get(ADMIN_PREFIX + "/api/users", handle_admin_users)
    # Static files for admin UI
    static_dir = _admin_static_dir()
    if static_dir.exists():
        app.router.add_static(ADMIN_PREFIX + "/static/", path=static_dir, name="admin_static")
    return app


async def start_payment_server(application) -> web.AppRunner:
    """Run the callback listener until the returned runner is cleaned up."""
    runner = web.AppRunner(build_app(application))
    await runner.setup()
    site = web.TCPSite(runner, config.zarinpal_bind_host, config.zarinpal_bind_port)
    await site.start()
    log.info(
        "Payment callback listening on http://%s:%s%s",
        config.zarinpal_bind_host,
        config.zarinpal_bind_port,
        CALLBACK_PATH,
    )
    if config.admin_panel_enabled:
        log.info(
            "Admin panel at http://%s:%s%s (user=%s)",
            config.zarinpal_bind_host,
            config.zarinpal_bind_port,
            ADMIN_PREFIX,
            config.admin_panel_user,
        )
    else:
        log.warning("Admin panel disabled: set ADMIN_PANEL_USER and ADMIN_PANEL_PASS")
    return runner


async def stop_payment_server(runner: web.AppRunner) -> None:
    await runner.cleanup()
