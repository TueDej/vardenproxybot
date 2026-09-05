"""Internal HTTP server receiving Zarinpal payment callbacks + admin panel.

Binds to a loopback address only; a reverse proxy (Caddy/nginx) terminates
TLS on the public domain and forwards here.
"""

import asyncio
import base64
import collections
import contextlib
import hmac
import ipaddress
import json
import logging
import pathlib
import re
import secrets
import time

try:
    from datetime import UTC
except ImportError:  # Python <3.11
    from datetime import timezone

    UTC = timezone.utc  # type: ignore[no-redef]  # noqa: UP017
from datetime import datetime, timedelta
from html import escape

from aiohttp import web
from sqlalchemy import and_, func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import selectinload

from config import config
from database import async_session
from handlers.buy import (
    OrderAlreadyApproved,
    OrderNotApprovable,
    RenewalLimitExceeded,
    expire_pending_orders,
    is_order_expired,
    verify_and_fulfill_order,
)
from keyboards import main_menu_keyboard
from message_render import (
    data_label as _msg_data_label,
)
from message_render import (
    expiry_dt as _msg_expiry_dt,
)
from message_render import (
    format_disabled_message as _msg_format_disabled,
)
from message_render import (
    format_expired_message as _msg_format_expired,
)
from message_render import (
    format_links_block as _msg_format_links,
)
from message_render import (
    format_product_message as _msg_format_product,
)
from message_render import (
    subscription_card as _msg_subscription_card,
)
from models import DiscountCode, MessageLog, Order, User
from rtl import rtl as _rtl
from vpn_service import (
    PanelRenewalLimitError,
    VPNPanelError,
    VPNPanelService,
    client_used_bytes,
    set_link_remark,
)
from zarinpal import ZarinpalError, reverse_payment, verify_payment

log = logging.getLogger(__name__)

CALLBACK_PATH = "/zarinpal/callback"
ADMIN_PREFIX = "/admin"

_PAGE = """<!DOCTYPE html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
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


# ── Session-based admin auth (replaces BasicAuth) ──────────────────

# In-memory session store: token -> expiry monotonic
_admin_sessions: dict[str, float] = {}
_admin_sessions_lock = asyncio.Lock()
_ADMIN_SESSION_TTL = 7 * 24 * 3600  # 7 days
_ADMIN_COOKIE_NAME = "admin_session"


def _is_trusted_proxy(remote: str | None) -> bool:
    if not remote:
        return False
    try:
        ip = ipaddress.ip_address(remote)
        return ip.is_loopback
    except ValueError:
        return False


def _check_admin_credentials(user: str, pwd: str) -> bool:
    if not config.admin_panel_enabled:
        return False
    # compare_digest raises TypeError on non-ASCII str — compare bytes.
    user_ok = hmac.compare_digest(user.encode("utf-8"), config.admin_panel_user.encode("utf-8"))
    pass_ok = hmac.compare_digest(pwd.encode("utf-8"), config.admin_panel_pass.encode("utf-8"))
    return user_ok and pass_ok


async def _create_admin_session() -> str:
    token = secrets.token_urlsafe(32)
    expiry = time.monotonic() + _ADMIN_SESSION_TTL
    async with _admin_sessions_lock:
        _admin_sessions[token] = expiry
        # periodic cleanup: drop expired if table grows
        if len(_admin_sessions) > 500:
            now = time.monotonic()
            for k, exp in list(_admin_sessions.items()):
                if exp <= now:
                    _admin_sessions.pop(k, None)
    return token


async def _is_admin_authenticated(request: web.Request) -> bool:
    if not config.admin_panel_enabled:
        return False
    token = request.cookies.get(_ADMIN_COOKIE_NAME)
    if not token:
        return False
    async with _admin_sessions_lock:
        exp = _admin_sessions.get(token)
        if exp is None:
            return False
        if exp <= time.monotonic():
            _admin_sessions.pop(token, None)
            return False
        # sliding expiry: refresh on activity
        _admin_sessions[token] = time.monotonic() + _ADMIN_SESSION_TTL
        return True


async def _destroy_admin_session(request: web.Request) -> None:
    token = request.cookies.get(_ADMIN_COOKIE_NAME)
    if token:
        async with _admin_sessions_lock:
            _admin_sessions.pop(token, None)


def _admin_login_page(error: str | None = None) -> web.Response:
    err_html = f'<div class="error">{escape(error)}</div>' if error else ""
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Varden Admin — Login</title>
<link rel="stylesheet" href="/admin/static/style.css?v=10">
</head>
<body><div class="login-wrap"><form class="login-card" method="POST" action="/admin/login">
<div class="login-brand">
  <div class="brand-logo"><span data-icon="shield-for-security"></span></div>
  <h1>Varden<span>Admin</span></h1>
</div>
<div class="muted">Sign in to continue</div>
{err_html}
<label class="field">Username<input name="username" autocomplete="username" required></label>
<label class="field">Password<input name="password" type="password" autocomplete="current-password" required></label>
<button class="btn primary block" type="submit">Sign in</button>
</form></div>
<script src="/admin/static/icons.js"></script>
</body></html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_admin_login_get(request: web.Request) -> web.Response:
    if not config.admin_panel_enabled:
        return web.Response(status=503, text="Admin panel not configured")
    if await _is_admin_authenticated(request):
        raise web.HTTPFound("/admin/")
    return _admin_login_page()


async def handle_admin_login_post(request: web.Request) -> web.Response:
    if not config.admin_panel_enabled:
        return web.Response(status=503, text="Admin panel not configured")
    # Rate-limit is already applied via middleware (60/min per IP on /admin)
    # Parse form data (supports both x-www-form-urlencoded and json)
    username = ""
    password = ""
    ctype = request.headers.get("Content-Type", "")
    if "application/json" in ctype:
        try:
            data = await request.json()
            username = str(data.get("username", "")).strip()
            password = str(data.get("password", ""))
        except Exception:
            pass
    else:
        try:
            data = await request.post()
            username = str(data.get("username", "")).strip()
            password = str(data.get("password", ""))
        except Exception:
            pass
    if not _check_admin_credentials(username, password):
        # Generic error to avoid user enumeration
        return _admin_login_page(error="Invalid username or password")
    token = await _create_admin_session()
    # Determine Secure flag: proxy terminates TLS, check X-Forwarded-Proto
    secure = False
    try:
        xf_proto = request.headers.get("X-Forwarded-Proto", "")
        if xf_proto == "https" or request.scheme == "https":
            secure = True
    except Exception:
        pass
    # Set cookie — SameSite=Lax blocks most CSRF, HttpOnly prevents JS theft
    resp = web.HTTPFound("/admin/")
    # Build cookie header manually to support SameSite attribute reliably
    cookie_val = f"{_ADMIN_COOKIE_NAME}={token}; Path=/admin; HttpOnly; SameSite=Lax"
    if secure:
        cookie_val += "; Secure"
    # 7 days
    cookie_val += f"; Max-Age={_ADMIN_SESSION_TTL}"
    resp.headers.add("Set-Cookie", cookie_val)
    # Also set for API compatibility: allow /admin/api with same cookie path
    return resp


async def handle_admin_logout(request: web.Request) -> web.Response:
    await _destroy_admin_session(request)
    resp = web.HTTPFound("/admin/login")
    # Clear cookie
    resp.headers.add(
        "Set-Cookie",
        f"{_ADMIN_COOKIE_NAME}=; Path=/admin; HttpOnly; SameSite=Lax; Max-Age=0",
    )
    return resp


# ── Auto-expire pending orders (15-min payment window) ────────────

_expire_task: asyncio.Task | None = None
_reconcile_task: asyncio.Task | None = None


async def _auto_expire_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            async with async_session() as session:
                count = await expire_pending_orders(session)
                if count:
                    log.info("Auto-expired %s pending orders (15-min window)", count)
        except asyncio.CancelledError:
            break
        except Exception:
            log.warning("Auto-expire tick failed", exc_info=True)


# ── Rate limit (in-memory, per-IP sliding window) ─────────────────

# path prefix -> (max_requests, window_seconds)
_RATE_LIMITS: dict[str, tuple[int, int]] = {
    CALLBACK_PATH: (20, 60),  # Zarinpal callback: 20/min per IP+authority
    "/zarinpal/start": (30, 60),  # start page: 30/min per IP
    ADMIN_PREFIX: (180, 60),  # admin API/UI: 180/min per IP (was 60 — too tight for refreshes; 6 req/load → 30 refreshes/min)
    "/zub": (60, 60),  # lite sub server: sub clients poll every few hours; 60/min/IP is plenty
    "/healthz": (120, 60),
}

_rate_hits: dict[str, collections.deque] = {}
_rate_lock = asyncio.Lock()

# ── Admin messaging (broadcast) ─────────────────────────────────
_broadcast_lock = asyncio.Lock()
_MSG_SEND_SEM = asyncio.Semaphore(3)  # telegram 30 msg/sec -> 3 concurrent + sleep
_PANEL_HYDRATE_SEM = asyncio.Semaphore(5)  # mirrors vpn_service.py:370
_MAX_BROADCAST = 5000
_MAX_BROADCAST_WITH_CONFIGS = 200
_MAX_CONFIG_MESSAGES = 10
_ALLOWED_HTML_TAGS = {"b", "strong", "i", "em", "u", "s", "strike", "del", "code", "pre", "a"}
_msg_send_hits: dict[str, collections.deque] = {}
_msg_send_lock = asyncio.Lock()
_gift_lock = asyncio.Lock()


def _client_ip(request: web.Request) -> str:
    # Only trust X-Forwarded-For when request comes from a trusted proxy (loopback).
    # The server binds to 127.0.0.1 behind Caddy/nginx; direct public access has
    # remote == client IP and must not be spoofable via XFF header.
    # Use the *last* XFF entry when trusted: proxy appends client IP, first entry
    # is attacker-controllable, last is proxy-added real IP.
    remote = request.remote or "unknown"
    if _is_trusted_proxy(remote):
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            # Take last entry (rightmost) for spoof resistance with appending proxies
            candidate = xff.split(",")[-1].strip()
            if candidate:
                try:
                    ipaddress.ip_address(candidate)
                    return candidate
                except ValueError:
                    # Invalid XFF entry — fall back to remote (proxy IP)
                    pass
    return remote


def _rate_key(request: web.Request) -> str | None:
    path = request.path
    # exact callback, prefix for start/admin
    for prefix, (_limit, _window) in _RATE_LIMITS.items():
        if path == prefix or path.startswith(prefix + "/"):
            # per-authority sub-key for callback to prevent enumeration flood
            if prefix == CALLBACK_PATH:
                auth = (request.rel_url.query.get("Authority") or "")[:16]
                return f"{_client_ip(request)}:{prefix}:{auth}"
            return f"{_client_ip(request)}:{prefix}"
    return None


# Aggregate per-IP cap for the callback path: the per-Authority sub-key is
# client-controlled, so without an aggregate cap a flood of unique Authority
# values would churn the map and reset other clients' buckets.
_CALLBACK_AGG_LIMIT = 120
_RATE_MAP_SOFT_CAP = 2000


@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    key = _rate_key(request)
    if key is not None:
        # find matching limit
        path = request.path
        limit = window = 60
        for prefix, (lim, win) in _RATE_LIMITS.items():
            if path == prefix or path.startswith(prefix + "/"):
                limit, window = lim, win
                break
        now = time.monotonic()
        # Callback path: enforce an additional per-IP aggregate bucket.
        keys: list[tuple[str, int]] = [(key, limit)]
        if path == CALLBACK_PATH:
            keys.append((f"{_client_ip(request)}:{path}:#agg", _CALLBACK_AGG_LIMIT))
        async with _rate_lock:
            retry = 0
            for k, lim in keys:
                dq = _rate_hits.get(k)
                if dq is None:
                    dq = collections.deque()
                    _rate_hits[k] = dq
                # prune old
                while dq and dq[0] <= now - window:
                    dq.popleft()
                if len(dq) >= lim:
                    retry = max(retry, int(dq[0] + window - now) + 1)
                    continue
                dq.append(now)
            # cap memory: evict expired keys first — never reset live buckets
            if len(_rate_hits) > _RATE_MAP_SOFT_CAP:
                stale = now - 120  # > max window (60s) with margin
                for k, d in list(_rate_hits.items()):
                    if not d or d[0] <= stale:
                        _rate_hits.pop(k, None)
            if retry:
                return web.Response(
                    status=429,
                    text="Too Many Requests",
                    headers={"Retry-After": str(max(1, retry))},
                )
    return await handler(request)


@web.middleware
async def admin_no_cache_middleware(request: web.Request, handler):
    """Force revalidation of admin pages and static assets.

    aiohttp static responses carry ETag/Last-Modified but no Cache-Control,
    so browsers apply heuristic freshness and serve stale app.js after a
    deploy — old JS against a new index.html throws and leaves tabs stuck
    on "Loading…". no-cache keeps 304 revalidation (cheap) but guarantees
    the browser asks on every load.
    """
    resp = await handler(request)
    if request.path.startswith(ADMIN_PREFIX):
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


@web.middleware
async def admin_auth_middleware(request: web.Request, handler):
    path = request.path
    # Public endpoints: login page and static assets must be reachable without auth
    if path == "/admin/login" or path.startswith("/admin/login/"):
        return await handler(request)
    if path.startswith("/admin/static/"):
        return await handler(request)
    if path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/"):
        if not config.admin_panel_enabled:
            return web.Response(
                status=503, text="Admin panel not configured (ADMIN_PANEL_USER/PASS missing)"
            )
        # CSRF protection for state-changing admin API calls
        if request.method == "POST" and path.startswith(ADMIN_PREFIX + "/api/"):
            # Require AJAX marker header (fetch) — browser form POST from attacker origin cannot set this
            # Also enforce SameSite via cookie, but extra header blocks simple CSRF
            xrw = request.headers.get("X-Requested-With", "")
            sec_fetch_site = request.headers.get("Sec-Fetch-Site", "")
            origin = request.headers.get("Origin", "")
            # Allow if X-Requested-With == XMLHttpRequest, or Sec-Fetch-Site == same-origin, or Origin matches host
            allowed_csrf = False
            if xrw == "XMLHttpRequest":
                allowed_csrf = True
            elif sec_fetch_site == "same-origin":
                allowed_csrf = True
            elif origin:
                # Validate Origin host matches request host
                try:
                    from urllib.parse import urlsplit

                    o_host = urlsplit(origin).netloc
                    req_host = request.headers.get("Host", "")
                    if o_host and req_host and o_host == req_host:
                        allowed_csrf = True
                except Exception:
                    pass
            if not allowed_csrf:
                # For fetch-based API, missing header means likely CSRF — block
                # Still allow if Referer is same-origin as fallback
                referer = request.headers.get("Referer", "")
                if referer:
                    try:
                        from urllib.parse import urlsplit

                        r_host = urlsplit(referer).netloc
                        req_host = request.headers.get("Host", "")
                        if r_host and req_host and r_host == req_host:
                            allowed_csrf = True
                    except Exception:
                        pass
            if not allowed_csrf:
                return web.json_response({"error": "CSRF check failed"}, status=403)
        if not await _is_admin_authenticated(request):
            # For API callers, return JSON 401 so fetch can redirect to login
            if path.startswith(ADMIN_PREFIX + "/api/"):
                return web.json_response({"error": "Authentication required"}, status=401)
            # For browser navigation, redirect to login page
            raise web.HTTPFound("/admin/login")
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
        # Total revenue: approved + paid only. payment_ref_id is set from the
        # gateway verify response, so manual approvals (/approve, free-confirm)
        # and gifts never count — only real money. "provisioning" also counts:
        # money was captured, provisioning just hasn't finished.
        # Gracefully handle DBs where is_gift column hasn't been migrated yet (UndefinedColumn)
        try:
            total_revenue = (
                await session.execute(
                    select(func.coalesce(func.sum(Order.amount_toomans), 0)).where(
                        and_(
                            Order.status == "approved",
                            Order.amount_toomans > 0,
                            Order.payment_ref_id.is_not(None),
                            or_(Order.is_gift == False, Order.is_gift.is_(None)),  # type: ignore[comparison-overlap]
                        )
                    )
                )
            ).scalar() or 0
        except Exception as e:
            # Fallback for DBs without is_gift column yet
            if "is_gift" in str(e) or "UndefinedColumn" in type(e).__name__:
                log.warning("is_gift column missing, falling back to amount>0 only for revenue")
                # Ensure migration is attempted for next request
                try:
                    await session.rollback()
                except Exception:
                    pass
                # Try to create column inline (best-effort, ignore if fails due to race)
                try:
                    async with async_session() as s2:
                        async with s2.begin():
                            await s2.execute(__import__("sqlalchemy").text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS is_gift BOOLEAN DEFAULT FALSE"))
                except Exception:
                    pass
                total_revenue = (
                    await session.execute(
                        select(func.coalesce(func.sum(Order.amount_toomans), 0)).where(
                            and_(
                                Order.status == "approved",
                                Order.amount_toomans > 0,
                                Order.payment_ref_id.is_not(None),
                            )
                        )
                    )
                ).scalar() or 0
            else:
                raise
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
            # Escape LIKE wildcards so %/_ in query don't broaden search
            q_escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{q_escaped}%"
            # For telegram_id numeric exact match
            or_parts = [
                Order.payment_ref_id.ilike(like, escape="\\"),
                Order.payment_authority.ilike(like, escape="\\"),
                Order.panel_email.ilike(like, escape="\\"),
                Order.sub_id.ilike(like, escape="\\"),
                Order.package_label.ilike(like, escape="\\"),
            ]
            if need_user_join:
                or_parts.extend(
                    [
                        User.username.ilike(like, escape="\\"),
                        User.first_name.ilike(like, escape="\\"),
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
            q_escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{q_escaped}%"
            or_parts = [
                User.username.ilike(like, escape="\\"),
                User.first_name.ilike(like, escape="\\"),
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


async def handle_admin_packages_get(request: web.Request) -> web.Response:
    import packages as pkg

    packages, base, paused, discount_max = pkg.load_packages()
    return web.json_response(
        {
            "base_price_per_gb": base,
            "packages": packages,
            "payments_paused": paused,
            "discount_max_pct": discount_max,
        }
    )


async def handle_admin_discounts_get(request: web.Request) -> web.Response:
    async with async_session() as session:
        result = await session.execute(select(DiscountCode).order_by(DiscountCode.created_at.desc()))
        codes = result.scalars().all()
        items = []
        for c in codes:
            items.append(
                {
                    "id": c.id,
                    "code": c.code,
                    "discount_percent": c.discount_percent,
                    "is_used": bool(c.is_used),
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "used_at": c.used_at.isoformat() if c.used_at else None,
                    "used_by_telegram_id": c.used_by_telegram_id,
                    "used_order_id": c.used_order_id,
                }
            )
        return web.json_response({"items": items})


async def handle_admin_discounts_create(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    pct = data.get("discount_percent")
    if pct is None:
        pct = data.get("discount_pct")
    try:
        pct = int(pct)
    except (ValueError, TypeError):
        return web.json_response({"error": "discount_percent must be integer 1-100"}, status=400)
    if not (1 <= pct <= 99):
        # 100% would create a 0-amount order the payment gateway rejects
        return web.json_response({"error": "discount_percent out of range 1..99"}, status=400)
    # Optional admin-supplied prefix (alnum, truncated in generator)
    prefix = data.get("prefix")
    if prefix is not None and not str(prefix).strip():
        prefix = None
    # generate
    from handlers.discount import generate_unique_code

    async with async_session() as session:
        try:
            dc = await generate_unique_code(session, pct, prefix)
        except Exception as e:
            log.error("Failed to generate discount code: %s", e, exc_info=True)
            return web.json_response({"error": "Could not generate code"}, status=500)
        return web.json_response(
            {
                "id": dc.id,
                "code": dc.code,
                "discount_percent": dc.discount_percent,
                "is_used": bool(dc.is_used),
                "created_at": dc.created_at.isoformat() if dc.created_at else None,
            }
        )


async def handle_admin_discounts_delete(request: web.Request) -> web.Response:
    code_id = request.match_info.get("id")
    try:
        code_id = int(code_id)
    except (ValueError, TypeError):
        return web.json_response({"error": "invalid id"}, status=400)
    async with async_session() as session:
        result = await session.execute(select(DiscountCode).where(DiscountCode.id == code_id))
        dc = result.scalar_one_or_none()
        if dc is None:
            return web.json_response({"error": "not found"}, status=404)
        if dc.is_used:
            return web.json_response({"error": "cannot delete used code"}, status=400)
        await session.delete(dc)
        await session.commit()
        return web.json_response({"ok": True})


async def handle_admin_packages_save(request: web.Request) -> web.Response:
    import packages as pkg

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    base = data.get("base_price_per_gb")
    packages_in = data.get("packages")
    paused = data.get("payments_paused")
    discount_max = data.get("discount_max_pct")

    # Validate base
    if base is not None:
        try:
            base = int(base)
        except (ValueError, TypeError):
            return web.json_response({"error": "base_price_per_gb must be integer"}, status=400)
        if not (1000 <= base <= 100000):
            return web.json_response(
                {"error": "base_price_per_gb out of range 1000..100000"}, status=400
            )

    if discount_max is not None:
        try:
            discount_max = int(discount_max)
        except (ValueError, TypeError):
            return web.json_response({"error": "discount_max_pct must be integer"}, status=400)
        if not (0 <= discount_max <= 60):
            return web.json_response({"error": "discount_max_pct out of range 0..60"}, status=400)

    # Validate packages if provided
    if packages_in is not None:
        if not isinstance(packages_in, list) or not packages_in:
            return web.json_response({"error": "packages must be non-empty list"}, status=400)
        for p in packages_in:
            if not isinstance(p, dict):
                return web.json_response({"error": "each package must be object"}, status=400)
            if not str(p.get("label", "")).strip():
                return web.json_response({"error": "label required"}, status=400)
            try:
                gb = int(p.get("data_gb", 0))
            except (ValueError, TypeError):
                return web.json_response({"error": "data_gb must be int"}, status=400)
            if gb < 0 or gb > 10000:
                return web.json_response({"error": "data_gb out of range"}, status=400)
            # Unlimited must have price
            if gb == 0:
                try:
                    price = int(p.get("price", 0))
                except (ValueError, TypeError):
                    return web.json_response({"error": "Unlimited price must be int"}, status=400)
                if not (1000 <= price <= 10_000_000):
                    return web.json_response({"error": "Unlimited price out of range"}, status=400)

    saved_pkgs, saved_base, saved_paused, saved_discount_max = pkg.save_packages(
        packages=packages_in,
        base_price_per_gb=base,
        payments_paused=paused,
        discount_max_pct=discount_max,
    )
    return web.json_response(
        {
            "base_price_per_gb": saved_base,
            "packages": saved_pkgs,
            "payments_paused": saved_paused,
            "discount_max_pct": saved_discount_max,
        }
    )


# ── Admin messaging ────────────────────────────────────────────────


def _sanitize_html(text: str) -> str:
    """Allow only Telegram-supported HTML tags; escape everything else."""
    if not text:
        return ""
    # Escape all then re-allow whitelisted tags
    text = text[:4000]
    # Build regex for allowed tags: <b>, </b>, <a href="..."> etc
    # First escape, then unescape allowed
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Re-allow simple tags without attributes: b, strong, i, em, u, s, strike, del, code, pre
    for tag in ("b", "strong", "i", "em", "u", "s", "strike", "del", "code", "pre"):
        escaped = escaped.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
        escaped = escaped.replace(f"&lt;{tag.upper()}&gt;", f"<{tag}>").replace(f"&lt;/{tag.upper()}&gt;", f"</{tag}>")
    # Allow <a href="..."> with href validation
    def _allow_a(m):
        inner = m.group(1) or ""
        href = ""
        # extract href="..." if present
        hm = re.search(r'href\s*=\s*"([^"]+)"', inner, flags=re.IGNORECASE)
        if hm:
            url = hm.group(1).strip()
            if url.startswith("http://") or url.startswith("https://") or url.startswith("tg://"):
                href = f' href="{escape(url)}"'
            else:
                return escape(m.group(0))
        return f"<a{href}>"

    escaped = re.sub(r"&lt;a(.*?)&gt;", _allow_a, escaped, flags=re.IGNORECASE)
    escaped = escaped.replace("&lt;/a&gt;", "</a>").replace("&lt;/A&gt;", "</a>")
    # Allow <br>, <br/>, <br />
    escaped = re.sub(r"&lt;br\s*/?&gt;", "<br>", escaped, flags=re.IGNORECASE)
    # Allow span with limited? strip
    return escaped


def _validate_text_html(text: str | None, require_non_empty: bool = True) -> str | None:
    if text is None:
        text = ""
    text = text.strip()
    if not text:
        if require_non_empty:
            return None
        return ""
    if len(text) > 4000:
        raise ValueError("Message too long (max 4000 characters)")
    # Basic check for broken tags count
    if text.count("<") != text.count(">"):
        raise ValueError("Malformed HTML tags")
    return _sanitize_html(text)


def _get_admin_identity(request: web.Request) -> str:
    # Use configured admin user as identity; if multiple admins later, extend via session map
    try:
        return config.admin_panel_user or "admin"
    except Exception:
        return "admin"


async def _check_msg_send_ratelimit(request: web.Request) -> web.Response | None:
    ip = _client_ip(request)
    key = f"msg_send:{ip}"
    now = time.monotonic()
    async with _msg_send_lock:
        dq = _msg_send_hits.get(key)
        if dq is None:
            dq = collections.deque()
            _msg_send_hits[key] = dq
        while dq and dq[0] <= now - 60:
            dq.popleft()
        # 5 sends per minute per IP
        if len(dq) >= 5:
            retry = int(dq[0] + 60 - now) + 1
            return web.json_response(
                {"error": "Too many messaging requests, retry in a few seconds"},
                status=429,
                headers={"Retry-After": str(max(1, retry))},
            )
        dq.append(now)
        if len(_msg_send_hits) > 2000:
            for k in list(_msg_send_hits.keys())[:400]:
                _msg_send_hits.pop(k, None)
    return None


async def _resolve_broadcast_targets(
    session, flt: dict
) -> list[User]:
    base = select(User)
    conditions = []
    q = (flt.get("q") or "").strip()
    from_s = _parse_date_param(flt.get("from"))
    to_s = _parse_date_param(flt.get("to"))
    has_approved = flt.get("has_approved")
    # has_approved can be bool or string "true"
    if isinstance(has_approved, str):
        has_approved = has_approved.lower() in ("1", "true", "yes", "on")
    if from_s:
        conditions.append(User.created_at >= from_s)
    if to_s:
        conditions.append(User.created_at <= to_s)
    if q:
        q_escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{q_escaped}%"
        or_parts = [
            User.username.ilike(like, escape="\\"),
            User.first_name.ilike(like, escape="\\"),
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
    # has_approved filter via exists subquery
    if has_approved:
        base = base.where(
            select(Order.id).where(and_(Order.user_id == User.id, Order.status == "approved")).exists()
        )
    base = base.order_by(User.created_at.desc()).limit(_MAX_BROADCAST + 1)
    result = await session.execute(base)
    users = result.scalars().all()
    if len(users) > _MAX_BROADCAST:
        raise ValueError(f"Broadcast too large (>{_MAX_BROADCAST}), narrow filter")
    return users


async def _hydrate_configs_for_user(telegram_id: int, panel_email: str | None) -> list[dict]:
    """Hydrate and bucket panel configs for a user, filtered by panel_email if set."""
    if not config.panel_configured:
        return []
    try:
        async with _PANEL_HYDRATE_SEM:
            clients = await VPNPanelService.get_clients_by_telegram_id(telegram_id)
    except VPNPanelError:
        return []
    if panel_email and panel_email != "all":
        clients = [c for c in clients if c.get("email") == panel_email]
    # sort active first like profile
    now = datetime.now(UTC)
    online = set()
    try:
        if clients:
            online = await VPNPanelService.get_online_emails()
    except Exception:
        online = set()
    bucket = []
    for c in clients:
        expiry = _msg_expiry_dt(c.get("expiryTime", 0))
        status = "disabled" if not c.get("enable") else ("active" if expiry is None or expiry > now else "expired")
        bucket.append((c, expiry, online, status))
    # order active, expired, disabled
    bucket.sort(key=lambda x: {"active": 0, "expired": 1, "disabled": 2}.get(x[3], 3))
    return bucket[:_MAX_CONFIG_MESSAGES]


async def handle_admin_messages_emails(request: web.Request) -> web.Response:
    tid_s = (request.rel_url.query.get("telegram_id") or "").strip()
    if not tid_s or not tid_s.lstrip("-").isdigit():
        return web.json_response({"error": "telegram_id required (numeric)"}, status=400)
    try:
        telegram_id = int(tid_s)
    except ValueError:
        return web.json_response({"error": "invalid telegram_id"}, status=400)
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            return web.json_response({"error": "user not found"}, status=404)
    if not config.panel_configured:
        return web.json_response({"items": [], "panel_configured": False})
    try:
        bucket = await _hydrate_configs_for_user(telegram_id, None)
    except Exception as e:
        log.warning("Failed to hydrate configs for %s: %s", telegram_id, e)
        return web.json_response({"items": [], "error": str(e)})
    items = []
    for c, expiry, _online, status in bucket:
        items.append(
            {
                "email": c.get("email"),
                "sub_id": c.get("sub_id"),
                "total_gb": c.get("total_gb", 0),
                "data_label": _msg_data_label(c.get("total_gb", 0)),
                "limit_ip": c.get("limit_ip", 0),
                "expiry_ms": c.get("expiry_time", 0),
                "expiry_iso": expiry.isoformat() if expiry else None,
                "status": status,
                "links_count": len(c.get("links", [])),
            }
        )
    return web.json_response({"items": items, "panel_configured": True})


async def handle_admin_messages_preview(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    text_raw = data.get("text_html") if "text_html" in data else data.get("text", "")
    panel_email = (data.get("panel_email") or "").strip() or None
    telegram_id = data.get("telegram_id")
    include_configs = bool(data.get("include_configs"))
    if panel_email and panel_email != "all" and not re.match(r"^vp\d+-\d+-[0-9a-f]{2,}$", panel_email):
        # allow any vp* pattern loosely
        if not panel_email.startswith("vp"):
            return web.json_response({"error": "invalid panel_email format"}, status=400)
    text_html = ""
    if text_raw:
        try:
            text_html = _validate_text_html(str(text_raw), require_non_empty=False) or ""
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
    if not text_html and not include_configs:
        return web.json_response({"error": "Provide text or enable include_configs"}, status=400)
    if include_configs and not config.panel_configured:
        return web.json_response({"error": "Panel not configured"}, status=400)
    rendered_parts = []
    if text_html:
        rendered_parts.append(text_html)
    if include_configs:
        if telegram_id is None:
            return web.json_response({"error": "telegram_id required for config preview"}, status=400)
        try:
            telegram_id = int(telegram_id)
        except (ValueError, TypeError):
            return web.json_response({"error": "invalid telegram_id"}, status=400)
        bucket = await _hydrate_configs_for_user(telegram_id, panel_email)
        if not bucket:
            rendered_parts.append("<i>No active configs found for this user.</i>")
        else:
            now = datetime.now(UTC)
            for c, expiry, online, status in bucket:
                if status == "active":
                    rendered_parts.append(_msg_format_product(c, expiry, online, now))
                elif status == "expired":
                    rendered_parts.append(_msg_format_expired(c, expiry))
                else:
                    rendered_parts.append(_msg_format_disabled(c, expiry))
    combined = "\n\n".join(rendered_parts)
    if len(combined) > 4000 and include_configs:
        # will be split into multiple messages; preview truncates first
        combined = combined[:3990] + "…"
    return web.json_response({"rendered_html": combined, "parts": rendered_parts})


async def handle_admin_messages_log(request: web.Request) -> web.Response:
    try:
        limit = int(request.rel_url.query.get("limit", "20"))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 100))
    page, _ = _parse_pagination(request)
    # pagination uses page/limit, offset = (page-1)*limit
    async with async_session() as session:
        base = select(MessageLog).order_by(MessageLog.created_at.desc())
        count_q = select(func.count()).select_from(base.subquery())
        total = (await session.execute(count_q)).scalar() or 0
        base = base.offset((page - 1) * limit).limit(limit)
        result = await session.execute(base)
        logs = result.scalars().all()
        items = []
        for l in logs:
            items.append(
                {
                    "id": l.id,
                    "created_at": l.created_at.isoformat() if l.created_at else None,
                    "admin_user": l.admin_user,
                    "kind": l.kind,
                    "status": l.status,
                    "filter_json": l.filter_json,
                    "text_html": (l.text_html[:200] + "…") if l.text_html and len(l.text_html) > 200 else l.text_html,
                    "panel_email": l.panel_email,
                    "total": l.total,
                    "sent": l.sent,
                    "failed": l.failed,
                    "error_summary": l.error_summary,
                }
            )
        return web.json_response({"total": total, "page": page, "limit": limit, "items": items})


async def handle_admin_generate_subscription(request: web.Request) -> web.Response:
    # Rate limit gift generation: 10/min per IP
    ip = _client_ip(request)
    key = f"gift_gen:{ip}"
    now = time.monotonic()
    async with _msg_send_lock:
        dq = _msg_send_hits.get(key)
        if dq is None:
            dq = collections.deque()
            _msg_send_hits[key] = dq
        while dq and dq[0] <= now - 60:
            dq.popleft()
        if len(dq) >= 10:
            retry = int(dq[0] + 60 - now) + 1
            return web.json_response({"error": "Too many gift generations, retry later"}, status=429, headers={"Retry-After": str(max(1, retry))})
        dq.append(now)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    # Validate telegram_id
    tid_raw = data.get("telegram_id") or data.get("target")
    if tid_raw is None or str(tid_raw).strip() == "":
        return web.json_response({"error": "telegram_id required"}, status=400)
    try:
        telegram_id = int(str(tid_raw).strip())
    except (ValueError, TypeError):
        return web.json_response({"error": "invalid telegram_id"}, status=400)
    if telegram_id <= 0 or telegram_id > 10**15:
        return web.json_response({"error": "telegram_id out of range"}, status=400)
    # data_gb and duration
    try:
        data_gb = int(data.get("data_gb", 0))
    except (ValueError, TypeError):
        return web.json_response({"error": "data_gb must be integer"}, status=400)
    if data_gb < 0 or data_gb > 10000:
        return web.json_response({"error": "data_gb out of range 0..10000"}, status=400)
    try:
        duration_days = int(data.get("duration_days") or data.get("duration") or 30)
    except (ValueError, TypeError):
        return web.json_response({"error": "duration_days must be integer"}, status=400)
    if duration_days < 1 or duration_days > 365:
        return web.json_response({"error": "duration_days out of range 1..365"}, status=400)
    label = str(data.get("package_label") or data.get("label") or "").strip()
    if not label:
        label = "Unlimited" if data_gb == 0 else f"{data_gb}GB"
        if data.get("is_gift") is not False:
            label = f"Gift {label}"
    if len(label) > 64:
        label = label[:64]
    if not config.panel_configured:
        return web.json_response({"error": "Panel not configured"}, status=500)
    # Serialize with global gift lock to avoid races on same user
    async with _gift_lock:
        # Get or create user
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()
            if user is None:
                # Auto-create minimal user (admin gift for new user)
                user = User(telegram_id=telegram_id, username=None, first_name=f"User {telegram_id}")
                session.add(user)
                try:
                    await session.commit()
                    await session.refresh(user)
                except Exception as e:
                    await session.rollback()
                    return web.json_response({"error": f"Could not create user: {e}"}, status=500)
            # Create pending gift order (handle DBs without is_gift column yet)
            try:
                order = Order(
                    user_id=user.id,
                    package_label=label,
                    duration_days=duration_days,
                    data_gb=data_gb,
                    amount_toomans=0,
                    status="pending",
                    is_gift=True,
                )
                session.add(order)
                await session.commit()
                await session.refresh(order)
            except Exception as e:
                if "is_gift" in str(e) or "UndefinedColumn" in type(e).__name__:
                    await session.rollback()
                    log.warning("is_gift column missing during gift create, attempting migration fallback")
                    # Try to add column then retry
                    try:
                        # Use a separate connection to avoid transaction conflict
                        async with async_session() as s2:
                            async with s2.begin():
                                await s2.execute(__import__("sqlalchemy").text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS is_gift BOOLEAN DEFAULT FALSE"))
                            await s2.commit()
                    except Exception:
                        pass
                    # Retry with is_gift after ensuring column exists
                    try:
                        order = Order(
                            user_id=user.id,
                            package_label=label,
                            duration_days=duration_days,
                            data_gb=data_gb,
                            amount_toomans=0,
                            status="pending",
                            is_gift=True,
                        )
                        session.add(order)
                        await session.commit()
                        await session.refresh(order)
                    except Exception as e2:
                        if "is_gift" in str(e2):
                            await session.rollback()
                            # Last resort: create without is_gift column (amount 0 ensures revenue exclusion)
                            from sqlalchemy import text as sa_text

                            result = await session.execute(
                                sa_text(
                                    "INSERT INTO orders (user_id, package_label, duration_days, data_gb, amount_toomans, status, created_at) "
                                    "VALUES (:uid, :label, :dur, :gb, 0, 'pending', NOW()) RETURNING id"
                                ),
                                {"uid": user.id, "label": label, "dur": duration_days, "gb": data_gb},
                            )
                            await session.commit()
                            # Fetch the newly created order
                            new_id = result.scalar() if hasattr(result, "scalar") else None
                            if new_id is None:
                                # Fallback query
                                res2 = await session.execute(
                                    select(Order).where(Order.user_id == user.id, Order.status == "pending").order_by(Order.id.desc()).limit(1)
                                )
                                order = res2.scalar_one()
                            else:
                                res2 = await session.execute(select(Order).where(Order.id == new_id))
                                order = res2.scalar_one()
                        else:
                            raise
                else:
                    raise
            order_id = order.id
            user_id = user.id
        # Provision via approve_order (handles panel create + atomic claim)
        from handlers.buy import approve_order

        async with async_session() as session:
            result = await session.execute(select(Order).where(Order.id == order_id))
            order = result.scalar_one_or_none()
            if order is None:
                return web.json_response({"error": "order not found after create"}, status=500)
            try:
                panel = await approve_order(session, order)
            except Exception as exc:
                # Mark cancelled on failure
                try:
                    async with async_session() as s2:
                        await s2.execute(sa_update(Order).where(Order.id == order_id).values(status="cancelled"))
                        await s2.commit()
                except Exception:
                    pass
                log.warning("Gift generation failed for %s: %s", telegram_id, exc)
                return web.json_response({"error": f"Panel provisioning failed: {exc}"}, status=500)
            # panel now has email, sub_id, links
            email = panel.get("email")
            links = panel.get("links", [])
        # Notify user via Telegram (best-effort)
        application = request.app.get("ptb_application")
        notify_ok = False
        notify_error = None
        if application is not None and hasattr(application, "bot") and application.bot is not None:
            try:
                # Same card the profile shows — one shared renderer for every path.
                try:
                    card = await _msg_subscription_card(email or "", data_gb, duration_days, links)
                except Exception:
                    log.warning("Gift card render failed for %s", email, exc_info=True)
                    card = ""
                if card:
                    text = (
                        f"🎁 <b>اشتراک هدیه برای شما فعال شد!</b>\n\n"
                        f"{card}"
                        "\n\n🎁 <i>این اشتراک هدیه است — قابل تمدید نیست.</i>"
                    )
                elif links:
                    config_block = "\n".join(f"🔗 <code>{escape(l)}</code>" for l in links)
                    text = (
                        f"🎁 <b>اشتراک هدیه برای شما فعال شد!</b>\n\n"
                        f"{config_block}"
                        "\n\n🎁 <i>این اشتراک هدیه است — قابل تمدید نیست.</i>"
                    )
                else:
                    text = f"🎁 <b>اشتراک هدیه</b> {escape(label)} فعال شد."
                await application.bot.send_message(
                    chat_id=telegram_id,
                    text=_rtl(text),
                    parse_mode="HTML",
                    reply_markup=main_menu_keyboard(),
                )
                notify_ok = True
            except Exception as e:
                notify_error = str(e)
                log.warning("Could not notify user %s about gift: %s", telegram_id, e)
        # Also create a MessageLog-like entry for audit? Use MessageLog with kind=gift
        try:
            async with async_session() as session:
                ml = MessageLog(
                    admin_user=_get_admin_identity(request),
                    kind="gift",
                    status="done" if notify_ok else "done",
                    filter_json={"telegram_id": telegram_id, "order_id": order_id, "label": label},
                    text_html=f"Gift {label} {data_gb}GB {duration_days}d",
                    panel_email=email,
                    total=1,
                    sent=1 if notify_ok else 0,
                    failed=0 if notify_ok else 1,
                    error_summary=notify_error,
                )
                session.add(ml)
                await session.commit()
        except Exception:
            pass
        return web.json_response(
            {
                "ok": True,
                "order_id": order_id,
                "user_id": user_id,
                "panel_email": email,
                "links": links,
                "notified": notify_ok,
                "notify_error": notify_error,
            }
        )


async def handle_admin_messages_send(request: web.Request) -> web.Response:
    # Extra per-IP messaging rate limit
    rl = await _check_msg_send_ratelimit(request)
    if rl is not None:
        return rl
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    mode = (data.get("mode") or data.get("type") or "single").strip().lower()
    if mode not in ("single", "broadcast"):
        return web.json_response({"error": "mode must be single or broadcast"}, status=400)
    text_raw = data.get("text_html") if "text_html" in data else data.get("text", "")
    panel_email = (data.get("panel_email") or "").strip() or None
    include_configs = bool(data.get("include_configs"))
    # Allow "all" sentinel
    if panel_email == "all":
        panel_email = "all"
    elif panel_email and not re.match(r"^vp", panel_email):
        # generic check: allow any non-empty panel email with vp prefix; otherwise reject
        if panel_email and not panel_email.startswith("vp"):
            return web.json_response({"error": "invalid panel_email"}, status=400)
    text_html = ""
    if text_raw is not None and str(text_raw).strip():
        try:
            text_html = _validate_text_html(str(text_raw), require_non_empty=False) or ""
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
    else:
        text_html = ""
    if not text_html and not include_configs:
        return web.json_response({"error": "Provide message text or enable configs"}, status=400)
    if len(text_html) > 4000:
        return web.json_response({"error": "Message too long (max 4000)"}, status=400)
    if include_configs and not config.panel_configured:
        return web.json_response({"error": "Panel not configured, cannot forward configs"}, status=400)

    # Resolve targets
    targets: list[User] = []
    filter_json: dict | None = None
    kind = "custom"
    if include_configs:
        kind = "config_forward" if mode == "single" else "broadcast"
    elif mode == "broadcast":
        kind = "broadcast"

    # Resolve targets (outside lock to keep lock time short)
    async with async_session() as session:
        if mode == "single":
            tid_raw = data.get("telegram_id") or data.get("target") or data.get("user_id")
            if tid_raw is None or str(tid_raw).strip() == "":
                return web.json_response({"error": "telegram_id required for single mode"}, status=400)
            try:
                telegram_id = int(str(tid_raw).strip())
            except (ValueError, TypeError):
                return web.json_response({"error": "invalid telegram_id"}, status=400)
            if telegram_id <= 0 or telegram_id > 10**15:
                return web.json_response({"error": "telegram_id out of range"}, status=400)
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()
            if user is None:
                return web.json_response({"error": "user not found"}, status=404)
            targets = [user]
            filter_json = {"telegram_id": telegram_id, "panel_email": panel_email}
        else:
            flt = data.get("filter") or {}
            if not isinstance(flt, dict):
                return web.json_response({"error": "filter must be object"}, status=400)
            # also allow top-level q/from/to for convenience
            for k in ("q", "from", "to", "has_approved"):
                if k in data and k not in flt:
                    flt[k] = data[k]
            filter_json = dict(flt)
            if panel_email:
                filter_json["panel_email"] = panel_email
            try:
                targets = await _resolve_broadcast_targets(session, flt)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)
            if not targets:
                return web.json_response({"error": "No users match filter"}, status=400)
            if include_configs and len(targets) > _MAX_BROADCAST_WITH_CONFIGS:
                return web.json_response(
                    {"error": f"Broadcast with configs limited to {_MAX_BROADCAST_WITH_CONFIGS} users (got {len(targets)}), narrow filter"},
                    status=400,
                )

    # Fail fast instead of queueing behind a long-running broadcast: the send
    # loop currently runs under the global lock, so a second concurrent send
    # would otherwise hang the admin's browser for minutes.
    if _broadcast_lock.locked():
        return web.json_response({"error": "Another broadcast is in progress"}, status=429)
    # Acquire global lock before dedup + log creation to make them atomic (race-safe)
    async with _broadcast_lock:
        # Dedup + log creation (atomic under lock)
        async with async_session() as session:
            try:
                recent = (await session.execute(select(MessageLog).order_by(MessageLog.created_at.desc()).limit(5))).scalars().all()
                for r in recent:
                    try:
                        rf = r.filter_json or {}
                        # Normalize JSON for sqlite vs postgres (dict vs string)
                        rf_s = json.dumps(rf, sort_keys=True) if isinstance(rf, dict) else str(rf)
                        cur_s = json.dumps(filter_json or {}, sort_keys=True) if isinstance(filter_json, dict) else str(filter_json)
                        same_filter = rf_s == cur_s
                    except Exception:
                        same_filter = r.filter_json == filter_json
                    if same_filter and r.text_html == text_html and r.status in ("pending", "sending"):
                        return web.json_response({"error": "Duplicate send in progress, wait a moment"}, status=429)
                    if same_filter and r.text_html == text_html:
                        try:
                            rc = r.created_at
                            if rc is not None and rc.tzinfo is None:
                                rc = rc.replace(tzinfo=UTC)
                            age = (datetime.now(UTC) - rc).total_seconds() if rc else 999
                        except Exception:
                            age = 999
                        if age < 10:
                            return web.json_response({"error": "Duplicate message sent just now, wait 10s"}, status=429)
            except Exception as e:
                log.warning("Dedup check failed: %s", e)
            admin_identity = _get_admin_identity(request)
            log_entry = MessageLog(
                admin_user=admin_identity,
                kind=kind,
                status="pending",
                filter_json=filter_json,
                text_html=text_html,
                panel_email=panel_email,
                total=len(targets),
                sent=0,
                failed=0,
            )
            session.add(log_entry)
            await session.commit()
            await session.refresh(log_entry)
            log_id = log_entry.id
            # Update to sending while still under outer lock (so second waiter sees 'sending')
            log_entry.status = "sending"
            await session.commit()

        application = request.app.get("ptb_application")
        if application is None or not hasattr(application, "bot"):
            async with async_session() as session:
                result = await session.execute(select(MessageLog).where(MessageLog.id == log_id))
                entry = result.scalar_one_or_none()
                if entry:
                    entry.status = "failed"
                    entry.error_summary = "Bot not available"
                    await session.commit()
            return web.json_response({"error": "Bot not available"}, status=500)

        bot = application.bot
        sent = 0
        failed = 0
        errors: list[str] = []
        # Semaphore for panel hydration already global; TG send semaphore per batch
        for idx, user in enumerate(targets):
            telegram_id = user.telegram_id
            try:
                # Build messages for this user
                messages: list[str] = []
                if text_html:
                    messages.append(text_html)
                if include_configs:
                    bucket = await _hydrate_configs_for_user(telegram_id, panel_email)
                    if not bucket:
                        if not text_html:
                            messages.append("<i>اشتراک فعالی برای شما یافت نشد.</i>")
                    else:
                        now = datetime.now(UTC)
                        for c, expiry, online, status in bucket:
                            if status == "active":
                                messages.append(_msg_format_product(c, expiry, online, now))
                            elif status == "expired":
                                messages.append(_msg_format_expired(c, expiry))
                            else:
                                messages.append(_msg_format_disabled(c, expiry))
                    # Truncate to max
                    if len(messages) > _MAX_CONFIG_MESSAGES + (1 if text_html else 0):
                        messages = messages[: _MAX_CONFIG_MESSAGES + (1 if text_html else 0)]
                # Send each message with rate limiting
                for msg in messages:
                    if len(msg) > 4000:
                        msg = msg[:3990] + "…"
                    msg = _rtl(msg)
                    async with _MSG_SEND_SEM:
                        try:
                            await bot.send_message(chat_id=telegram_id, text=msg, parse_mode="HTML")
                        except Exception as e:
                            # Handle Telegram RetryAfter
                            err_s = str(e).lower()
                            if "retry after" in err_s or "flood" in err_s or "too many requests" in err_s:
                                # extract seconds
                                m = re.search(r"retry after (\d+)", err_s)
                                wait = int(m.group(1)) if m else 2
                                wait = min(wait, 10)
                                await asyncio.sleep(wait)
                                # retry once
                                try:
                                    await bot.send_message(chat_id=telegram_id, text=msg, parse_mode="HTML")
                                except Exception as e2:
                                    raise e2
                            else:
                                raise
                    # per-message throttle
                    await asyncio.sleep(0.04)
                sent += 1
            except Exception as e:
                failed += 1
                err_msg = f"{telegram_id}: {type(e).__name__}: {str(e)[:120]}"
                errors.append(err_msg)
                log.warning("Messaging failed for %s: %s", telegram_id, e)
            # Batch throttle every 20 users
            if (idx + 1) % 20 == 0:
                await asyncio.sleep(0.5)
            if (idx + 1) % 100 == 0:
                # incremental DB update to survive crashes
                async with async_session() as session:
                    result = await session.execute(select(MessageLog).where(MessageLog.id == log_id))
                    entry = result.scalar_one_or_none()
                    if entry:
                        entry.sent = sent
                        entry.failed = failed
                        await session.commit()

        # Final DB update
        async with async_session() as session:
            result = await session.execute(select(MessageLog).where(MessageLog.id == log_id))
            entry = result.scalar_one_or_none()
            if entry:
                entry.sent = sent
                entry.failed = failed
                entry.status = "done" if failed == 0 else ("failed" if sent == 0 else "done")
                if errors:
                    entry.error_summary = "\n".join(errors[:20])
                await session.commit()

        return web.json_response(
            {
                "ok": True,
                "log_id": log_id,
                "total": len(targets),
                "sent": sent,
                "failed": failed,
                "errors": errors[:10],
            }
        )


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
            f"⚠️ <b>پرداخت سفارش لغو شده!</b> سفارش #{escape(str(oid))} (کد پیگیری "
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
        f"ℹ️ سفارش لغو شده #{escape(str(oid))} پرداخت شد — مبلغ به‌صورت خودکار مسترد شد "
        f"(کد پیگیری <code>{escape(str(outcome['ref_id']))}</code>).",
    )
    await _notify(
        application,
        order.user.telegram_id,
        f"💳 سفارش #{escape(str(oid))} قبلاً لغو شده بود؛ به همین دلیل مبلغ پرداختی به‌صورت خودکار به کارت شما بازگشت داده شد.",
    )
    return _page(
        "مبلغ مسترد شد ✅",
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

        # Sandbox and maintenance mode: only admins may pay — defense in
        # depth even if order was created before the flags were enabled.
        if config.zarinpal_sandbox and chat_id not in config.admin_ids:
            log.warning("Sandbox mode: rejecting callback for non-admin order #%s user %s", oid, chat_id)
            return _page(
                "حالت آزمایشی",
                "در حالت آزمایشی فقط مدیران امکان پرداخت دارند.",
            )
        try:
            import packages as _pkg

            _, _, _paused = _pkg.load_packages()[:3]
            if _paused and chat_id not in config.admin_ids:
                log.warning("Paused mode: rejecting callback for non-admin order #%s user %s", oid, chat_id)
                return _page(
                    "سرویس در حال به‌روزرسانی",
                    "سرویس در حال به‌روزرسانی است — لطفاً چند دقیقه بعد دوباره تلاش کنید.",
                )
        except Exception:
            pass

        if order.status == "approved":
            return _page(
                "قبلاً تأیید شده", "این سفارش قبلاً تأیید و فعال شده است. به ربات برگردید. ✅"
            )

        # 15-min window: expired pending orders are treated as cancelled
        # and any late payment is reversed asap.
        if order.status == "pending" and is_order_expired(order):
            log.info("Order #%s expired (15-min window)", oid)
            try:
                order.status = "cancelled"
                await session.commit()
            except Exception:
                pass
            try:
                outcome = await verify_payment(order.payment_authority, order.amount_toomans)
            except ZarinpalError:
                return _page(
                    "لینک منقضی شده ⏰",
                    "این لینک پرداخت پس از 15 دقیقه منقضی شده است. لطفاً سفارش جدیدی ثبت کنید.",
                )
            return await _paid_cancelled_flow(application, session, order, authority, outcome)

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
            # Panel-side over-limit guard (vpn_service.extend_client raises
            # PanelRenewalLimitError): refund the verified payment, same as the
            # in-renew_order pre-check's RenewalLimitExceeded.
            if isinstance(exc, PanelRenewalLimitError):
                log.info("Order #%s renewal exceeds 60 days (panel): %s", oid, exc)
                try:
                    outcome = await verify_payment(order.payment_authority, order.amount_toomans)
                except ZarinpalError:
                    return _page(
                        "تمدید ممکن نیست",
                        "مجموع زمان اشتراک پس از تمدید بیش از 60 روز می‌شود — تمدید انجام نشد.",
                    )
                return await _paid_cancelled_flow(application, session, order, authority, outcome)
            # Provisioning failed after money was verified. Keep the order
            # pending (a retry can complete it) and alert both the user and
            # the admins — without an admin ping a stranded paid order is
            # invisible until it expires. Note: over-limit renewals raise
            # OrderNotApprovable (RenewalLimitExceeded), never VPNPanelError.
            log.error("Order #%s PAID but provisioning failed: %s", oid, exc)
            await _notify(
                application,
                chat_id,
                "✅ پرداخت شما دریافت شد، اما آماده‌سازی کانفیگ کمی زمان می‌برد. "
                "به‌زودی از بخش 👤 پروفایل و اشتراک‌های من بررسی کنید یا با پشتیبانی تماس بگیرید.",
            )
            await _notify_admins(
                application,
                f"🚨 <b>پرداخت بدون فعال‌سازی!</b> سفارش #{escape(str(oid))} پرداخت و تأیید شد، اما "
                f"آماده‌سازی روی پنل شکست خورد و سفارش در حالت pending ماند:\n"
                f"<code>{escape(str(exc))}</code>\n"
                "لطفاً دستی رسیدگی کنید (تأیید مجدد سفارش یا استرداد وجه).",
            )
            # Mark the order so the 15-min auto-expire does NOT cancel it:
            # money is captured and an admin retry (/approve or free-confirm)
            # can still complete provisioning on the still-pending order.
            try:
                await session.execute(
                    sa_update(Order).where(Order.id == oid).values(payment_ref_id="provisioning")
                )
                await session.commit()
            except Exception:
                log.warning("Could not mark order #%s as provisioning-pending", oid, exc_info=True)
            return _page("در حال بررسی", "پرداخت شما ثبت شد؛ فعال‌سازی چند دقیقه طول خواهد کشید.")
        except OrderAlreadyApproved:
            return _page(
                "قبلاً تأیید شده", "این سفارش قبلاً تأیید و فعال شده است. به ربات برگردید. ✅"
            )
        except OrderNotApprovable as exc:
            # 60-day limit: inform user and auto-refund if money moved.
            # RenewalLimitExceeded is the structured marker (no substring
            # matching — panel emails/order ids can contain those by chance).
            if isinstance(exc, RenewalLimitExceeded):
                log.info("Order #%s renewal exceeds 60 days: %s", oid, exc)
                try:
                    outcome = await verify_payment(order.payment_authority, order.amount_toomans)
                except ZarinpalError:
                    return _page(
                        "تمدید ممکن نیست",
                        "مجموع زمان اشتراک پس از تمدید بیش از 60 روز می‌شود — تمدید انجام نشد.",
                    )
                return await _paid_cancelled_flow(application, session, order, authority, outcome)
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
    # Same card the profile shows — one shared renderer for every path.
    try:
        _card_email = order.panel_email or order.renew_email or ""
        _card = await _msg_subscription_card(
            _card_email, order.data_gb, order.duration_days, None
        )
    except Exception:
        log.warning("Success card render failed for order #%s", oid, exc_info=True)
        _card = ""
    _card_suffix = f"\n\n{_card}" if _card else ""
    if order.renew_email:
        await _notify(
            application,
            chat_id,
            f"🎉 <b>تمدید اشتراک شما با موفقیت انجام شد!</b> (سفارش #{escape(str(oid))})"
            f"{_card_suffix}",
        )
        body = (
            f"تمدید با موفقیت انجام شد (کد پیگیری: {escape(str(ref))}). "
            "اشتراک شما تمدید شد و کانفیگ قبلی فعال است."
        )
    else:
        await _notify(
            application,
            chat_id,
            f"🎉 <b>پرداخت شما با موفقیت تأیید شد!</b> (سفارش #{escape(str(oid))})"
            f"{_card_suffix}",
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
                "کانفیگ شما آماده است؛ آن را از بخش 👤 پروفایل و اشتراک‌های من دریافت کنید."
            )
        await application.bot.send_message(
            chat_id=chat_id,
            text=_rtl(text),
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        log.warning("Could not notify user %s about completed payment", chat_id, exc_info=True)


async def _notify_admins(application, text: str) -> None:
    """Best-effort alert to every configured admin."""
    text = _rtl(text)
    for admin_id in config.admin_ids:
        try:
            await application.bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
        except Exception:
            log.warning("Could not alert admin %s", admin_id, exc_info=True)


# ── Startup reconciliation for stranded orders ─────────────────────


async def _reconcile_stranded_orders(application) -> None:
    """Alert admins about orders claimed 'approved' but never provisioned.

    A crash between the atomic status claim and panel provisioning strands
    such an order: paid money, no client, stuck at status 'approved' with
    neither panel_email (set only by successful new-purchase provisioning)
    nor payment_ref_id (set only after successful fulfillment). Auto-revert
    is unsafe — the panel client may already exist — so surface them for
    manual action instead.

    Scoped to new purchases only: renewals keep panel_email NULL even when
    healthy, so they cannot be distinguished this way (in-process renewal
    failures are already reported by the callback's admin notification).
    """
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Order.id, Order.amount_toomans)
                .where(
                    Order.status == "approved",
                    Order.payment_ref_id.is_(None),
                    Order.panel_email.is_(None),
                    Order.renew_email.is_(None),
                )
                .order_by(Order.created_at.desc())
                .limit(20)
            )
            rows = result.all()
    except Exception:
        log.warning("Stranded-order reconciliation query failed", exc_info=True)
        return
    if not rows:
        return
    detail = "\n".join(
        f"• سفارش #{oid} — {amount:,} تومان (تأیید شده، بدون کانفیگ)" for oid, amount in rows
    )
    log.warning(
        "Reconciliation: %d stranded approved order(s) (approved, never provisioned)", len(rows)
    )
    try:
        await _notify_admins(
            application,
            f"🚨 <b>سفارش‌های نیمه‌کاره پس از راه‌اندازی مجدد:</b>\n{escape(detail)}\n"
            "این سفارش‌ها تأیید شده‌اند اما هیچ کانفیگی برایشان ساخته نشده است "
            "(احتمالاً توقف ناگهانی بین ثبت تأیید و ساخت کانفیگ روی پنل). لطفاً دستی بررسی کنید.",
        )
    except Exception:
        log.warning("Could not notify admins about stranded orders", exc_info=True)


async def handle_zarinpal_start(request: web.Request) -> web.Response:
    """Intermediate website page that then redirects to Zarinpal.

    Telegram buttons now point here (https://pay.example.com/zarinpal/start/{authority})
    so the Referer to Zarinpal is our website, not t.me — this lets Zarinpal
    treat it as a website checkout and skip the checkout.toodej.shop owner-details
    interstitial. We serve a small HTML with meta-refresh + JS redirect, falling
    back to a manual link.
    """
    authority = (request.match_info.get("authority") or "").strip()
    # Validate like callback
    if not authority or len(authority) > 64 or not authority.replace("_", "").replace("-", "").isalnum():
        return _page("پرداخت نامعتبر", "شناسه پرداخت نامعتبر است.")
    # Optional: verify order exists and is pending to avoid open-redirect abuse
    async with async_session() as session:
        result = await session.execute(
            select(Order).options(selectinload(Order.user)).where(Order.payment_authority == authority)
        )
        order = result.scalar_one_or_none()
        if order is None:
            log.warning("Start page for unknown authority (len=%d)", len(authority))
            return _page("سفارش یافت نشد", "سفارشی برای این پرداخت پیدا نشد.")
        # Sandbox and maintenance mode: only admins may access the payment start page
        if config.zarinpal_sandbox and order.user.telegram_id not in config.admin_ids:
            log.warning(
                "Sandbox mode: rejecting start page for non-admin order #%s user %s",
                order.id,
                order.user.telegram_id,
            )
            return _page(
                "حالت آزمایشی",
                "در حالت آزمایشی فقط مدیران امکان پرداخت دارند.",
            )
        try:
            import packages as _pkg

            _, _, _paused = _pkg.load_packages()[:3]
            if _paused and order.user.telegram_id not in config.admin_ids:
                log.warning(
                    "Paused mode: rejecting start page for non-admin order #%s user %s",
                    order.id,
                    order.user.telegram_id,
                )
                return _page(
                    "سرویس در حال به‌روزرسانی",
                    "سرویس در حال به‌روزرسانی است — لطفاً چند دقیقه بعد دوباره تلاش کنید.",
                )
        except Exception:
            pass
        if order.status == "pending" and is_order_expired(order):
            try:
                order.status = "cancelled"
                await session.commit()
            except Exception:
                pass
            return _page(
                "لینک منقضی شده ⏰",
                "این لینک پرداخت پس از 15 دقیقه منقضی شده است. لطفاً سفارش جدیدی ثبت کنید.",
            )
        if order.status != "pending":
            if order.status == "approved":
                return _page("قبلاً پرداخت شده", "این سفارش قبلاً پرداخت و فعال شده است.")
            return _page("سفارش نامعتبر", f"وضعیت سفارش: {escape(order.status)}")
    # Build direct Zarinpal URL (with ZarinGate if enabled)
    target = config.zarinpal_startpay_url(authority)
    # Small HTML that auto-redirects — Referer will be this page (our website)
    html = f"""<!DOCTYPE html><html lang="fa"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="0; url={escape(target)}">
<title>انتقال به درگاه پرداخت</title>
<style>body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}} .box{{text-align:center;padding:2rem}} .spinner{{width:36px;height:36px;border:3px solid #334155;border-top-color:#38bdf8;border-radius:50%;animation:spin 1s linear infinite;margin:0 auto 16px}} @keyframes spin{{to{{transform:rotate(360deg)}}}} a{{color:#38bdf8}}</style>
<script>window.location.replace("{escape(target)}");</script>
</head><body><div class="box"><div class="spinner"></div><h1>در حال انتقال به درگاه پرداخت</h1><p>لطفاً چند لحظه صبر کنید…</p><p><a href="{escape(target)}">اگر منتقل نشدید، اینجا کلیک کنید</a></p></div></body></html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# ── Lite subscription server (/zub/{sub_id}) ──────────────────────
# The panel's own /sub/{subId} URLs point at the panel backend, which is only
# reachable via the reverse proxy that fronts this bot server — so we serve
# subscriptions ourselves: panel config links, rewritten for the public entry
# (rewrite_vless_link), with a remark carrying the remaining traffic/days.

_SUB_ID_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
_SUB_REMARK_PREFIX = "Vard3n"
_SUB_CACHE_TTL = 60.0  # seconds — survives multi-device refresh storms
_sub_cache: dict[str, tuple[float, bytes, dict[str, str]]] = {}


def _sub_remark(total_bytes: int, used_bytes: int | None, expiry_ms: int) -> str:
    """Profile name shown in client apps: remaining data + days left."""
    if total_bytes <= 0:
        label = "Unlimited"
    elif used_bytes is None:
        label = "?GB"
    else:
        label = f"{round(max(0, total_bytes - used_bytes) / 1024**3, 1):g}GB"
    days = ""
    if expiry_ms > 0:
        expires = datetime.fromtimestamp(expiry_ms / 1000, tz=UTC)
        days = f" | {max(0, (expires - datetime.now(UTC)).days)}d"
    return f"{_SUB_REMARK_PREFIX} {label}{days}"


async def handle_sub(request: web.Request) -> web.Response:
    sub_id = request.match_info.get("sub_id", "")
    if not _SUB_ID_RE.fullmatch(sub_id):
        return web.Response(status=404, text="Not found")

    cached = _sub_cache.get(sub_id)
    if cached and time.monotonic() - cached[0] < _SUB_CACHE_TTL:
        _, body, headers = cached
        return web.Response(body=body, headers=headers)

    try:
        client = await VPNPanelService.get_client_by_sub_id(sub_id)
    except VPNPanelError as exc:
        log.warning("Sub server panel error for %s...: %s", sub_id[:8], exc)
        return web.Response(status=503, text="Panel unavailable, try again later")
    if not isinstance(client, dict):
        return web.Response(status=404, text="Not found")

    email = str(client.get("email") or "")
    try:
        links = await VPNPanelService.get_client_links(email)
    except VPNPanelError as exc:
        log.warning("Sub server link fetch failed for %s: %s", email, exc)
        links = []
    if not links:
        return web.Response(status=404, text="Not found")

    total_bytes = max(0, int(client.get("totalGB") or 0))
    used_bytes = client_used_bytes(client)
    expiry_ms = int(client.get("expiryTime") or 0)
    traffic = client.get("traffic") if isinstance(client.get("traffic"), dict) else client
    try:
        up = max(0, int(traffic.get("up") or 0))
        down = max(0, int(traffic.get("down") or 0))
    except (TypeError, ValueError):
        up = down = 0

    remark = _sub_remark(total_bytes, used_bytes, expiry_ms)
    links = [set_link_remark(link, remark) for link in links]
    body = base64.b64encode("\n".join(links).encode("utf-8"))
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store",
        "Profile-Title": "base64:" + base64.b64encode(remark.encode("utf-8")).decode("ascii"),
        "Profile-Update-Interval": "12",
        "Subscription-Userinfo": (
            f"upload={up}; download={down}; total={total_bytes}; expire={expiry_ms // 1000}"
        ),
        "Content-Disposition": f'attachment; filename="{_SUB_REMARK_PREFIX}"',
    }
    if len(_sub_cache) > 2000:
        _sub_cache.clear()
    _sub_cache[sub_id] = (time.monotonic(), body, headers)
    log.info("Sub served: %s (%s) %s", email, remark, _client_ip(request))
    return web.Response(body=body, headers=headers)


def build_app(application) -> web.Application:
    # rate_limit first so 429s are cheap, then auth
    app = web.Application(
        middlewares=[rate_limit_middleware, admin_no_cache_middleware, admin_auth_middleware]
    )
    app["ptb_application"] = application
    app.router.add_get(CALLBACK_PATH, handle_zarinpal_callback)
    app.router.add_get("/zarinpal/start/{authority}", handle_zarinpal_start)
    app.router.add_get("/zub/{sub_id}", handle_sub)
    app.router.add_get("/healthz", handle_health)
    # Admin panel — login is public, rest is session-protected
    app.router.add_get("/admin/login", handle_admin_login_get)
    app.router.add_post("/admin/login", handle_admin_login_post)
    app.router.add_get("/admin/logout", handle_admin_logout)
    app.router.add_post("/admin/logout", handle_admin_logout)
    app.router.add_get(ADMIN_PREFIX, handle_admin_index)
    app.router.add_get(ADMIN_PREFIX + "/", handle_admin_index)
    app.router.add_get(ADMIN_PREFIX + "/api/stats", handle_admin_stats)
    app.router.add_get(ADMIN_PREFIX + "/api/orders", handle_admin_orders)
    app.router.add_get(ADMIN_PREFIX + "/api/users", handle_admin_users)
    app.router.add_get(ADMIN_PREFIX + "/api/packages", handle_admin_packages_get)
    app.router.add_post(ADMIN_PREFIX + "/api/packages", handle_admin_packages_save)
    app.router.add_get(ADMIN_PREFIX + "/api/discounts", handle_admin_discounts_get)
    app.router.add_post(ADMIN_PREFIX + "/api/discounts", handle_admin_discounts_create)
    app.router.add_delete(ADMIN_PREFIX + "/api/discounts/{id}", handle_admin_discounts_delete)
    app.router.add_get(ADMIN_PREFIX + "/api/messages/emails", handle_admin_messages_emails)
    app.router.add_post(ADMIN_PREFIX + "/api/messages/preview", handle_admin_messages_preview)
    app.router.add_post(ADMIN_PREFIX + "/api/messages/send", handle_admin_messages_send)
    app.router.add_get(ADMIN_PREFIX + "/api/messages/log", handle_admin_messages_log)
    app.router.add_post(ADMIN_PREFIX + "/api/messages/generate-subscription", handle_admin_generate_subscription)
    # Static files for admin UI (public for login page styling)
    static_dir = _admin_static_dir()
    if static_dir.exists():
        app.router.add_static(ADMIN_PREFIX + "/static/", path=static_dir, name="admin_static")
    return app


async def start_payment_server(application) -> web.AppRunner:
    """Run the callback listener until the returned runner is cleaned up."""
    global _expire_task
    runner = web.AppRunner(build_app(application))
    await runner.setup()
    site = web.TCPSite(runner, config.zarinpal_bind_host, config.zarinpal_bind_port)
    await site.start()
    # Start 15-min expiry background loop (runs every 60s)
    if _expire_task is None or _expire_task.done():
        _expire_task = asyncio.create_task(_auto_expire_loop())
    # One-shot startup reconciliation: flag paid-but-never-provisioned orders
    global _reconcile_task
    if _reconcile_task is None or _reconcile_task.done():
        _reconcile_task = asyncio.create_task(_reconcile_stranded_orders(application))
    log.info(
        "Payment callback listening on http://%s:%s%s",
        config.zarinpal_bind_host,
        config.zarinpal_bind_port,
        CALLBACK_PATH,
    )
    log.info("Payment expiry: pending orders auto-cancel after 15 minutes")
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
    global _expire_task
    if _expire_task is not None:
        _expire_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _expire_task
        _expire_task = None
    await runner.cleanup()
