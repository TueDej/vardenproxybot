"""Internal HTTP server receiving Zarinpal payment callbacks + admin panel.

Binds to a loopback address only; a reverse proxy (Caddy/nginx) terminates
TLS on the public domain and forwards here.
"""

import asyncio
import collections
import hmac
import ipaddress
import logging
import pathlib
import secrets
import time

try:
    from datetime import UTC
except ImportError:  # Python <3.11
    from datetime import timezone

    UTC = timezone.utc  # type: ignore[no-redef]  # noqa: UP017
from datetime import datetime
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
    user_ok = hmac.compare_digest(user, config.admin_panel_user)
    pass_ok = hmac.compare_digest(pwd, config.admin_panel_pass)
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
<link rel="stylesheet" href="/admin/static/style.css">
<style>
.login-wrap{{min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0e1411;padding:20px}}
.login-card{{background:#16201b;border:1px solid #25352c;border-radius:16px;padding:28px;width:100%;max-width:380px;box-shadow:0 4px 16px rgba(0,0,0,0.35)}}
.login-card h1{{margin:0 0 6px;font-size:22px;color:#dff0e3}}
.login-card .muted{{margin-bottom:18px}}
.login-card label{{display:block;margin:12px 0 4px;color:#8aa098;font-size:13px}}
.login-card input{{width:100%;padding:10px 12px;background:#0f1f18;border:1px solid #25352c;border-radius:10px;color:#dff0e3;font-size:14px;outline:none;box-sizing:border-box}}
.login-card input:focus{{border-color:#478061}}
.login-card .btn{{width:100%;margin-top:18px}}
.error{{background:rgba(220,90,90,0.14);border:1px solid rgba(220,90,90,0.22);color:#e07a7a;padding:8px 12px;border-radius:10px;margin-bottom:12px;font-size:13px}}
</style></head>
<body><div class="login-wrap"><form class="login-card" method="POST" action="/admin/login">
<h1>Varden<span style="color:#5fb68a">Admin</span></h1>
<div class="muted">Sign in to continue</div>
{err_html}
<label>Username</label><input name="username" autocomplete="username" required>
<label>Password</label><input name="password" type="password" autocomplete="current-password" required>
<button class="btn primary" type="submit">Sign in</button>
</form></div></body></html>"""
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
    cookie_val = f"{_ADMIN_COOKIE_NAME}={token}; Path=/admin/; HttpOnly; SameSite=Lax"
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
        f"{_ADMIN_COOKIE_NAME}=; Path=/admin/; HttpOnly; SameSite=Lax; Max-Age=0",
    )
    return resp


# ── Rate limit (in-memory, per-IP sliding window) ─────────────────

# path prefix -> (max_requests, window_seconds)
_RATE_LIMITS: dict[str, tuple[int, int]] = {
    CALLBACK_PATH: (20, 60),  # Zarinpal callback: 20/min per IP+authority
    "/zarinpal/start": (30, 60),  # start page: 30/min per IP
    ADMIN_PREFIX: (60, 60),  # admin API/UI: 60/min per IP
    "/healthz": (120, 60),
}

_rate_hits: dict[str, collections.deque] = {}
_rate_lock = asyncio.Lock()


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
        async with _rate_lock:
            dq = _rate_hits.get(key)
            if dq is None:
                dq = collections.deque()
                _rate_hits[key] = dq
            # prune old
            while dq and dq[0] <= now - window:
                dq.popleft()
            if len(dq) >= limit:
                retry = int(dq[0] + window - now) + 1
                return web.Response(
                    status=429,
                    text="Too Many Requests",
                    headers={"Retry-After": str(max(1, retry))},
                )
            dq.append(now)
            # cap memory
            if len(_rate_hits) > 2000:
                # drop oldest 20% keys
                for k in list(_rate_hits.keys())[:400]:
                    _rate_hits.pop(k, None)
    return await handler(request)


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

    packages, base, paused = pkg.load_packages()
    return web.json_response(
        {"base_price_per_gb": base, "packages": packages, "payments_paused": paused}
    )


async def handle_admin_packages_save(request: web.Request) -> web.Response:
    import packages as pkg

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    base = data.get("base_price_per_gb")
    packages_in = data.get("packages")
    paused = data.get("payments_paused")

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

    saved_pkgs, saved_base, saved_paused = pkg.save_packages(
        packages=packages_in, base_price_per_gb=base, payments_paused=paused
    )
    return web.json_response(
        {"base_price_per_gb": saved_base, "packages": saved_pkgs, "payments_paused": saved_paused}
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
    if order.renew_email:
        await _notify(
            application,
            chat_id,
            f"🎉 <b>تمدید اشتراک شما با موفقیت انجام شد!</b> (سفارش #{escape(str(oid))})\n\n"
            "زمان اشتراک فعلی شما تمدید شد؛ همان کانفیگ قبلی همچنان معتبر است.\n"
            "وضعیت را از بخش «👤 پروفایل من» بررسی کنید.",
        )
        body = (
            f"تمدید با موفقیت انجام شد (کد پیگیری: {escape(str(ref))}). "
            "اشتراک شما تمدید شد و کانفیگ قبلی فعال است."
        )
    else:
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
        result = await session.execute(select(Order).where(Order.payment_authority == authority))
        order = result.scalar_one_or_none()
        if order is None:
            log.warning("Start page for unknown authority (len=%d)", len(authority))
            return _page("سفارش یافت نشد", "سفارشی برای این پرداخت پیدا نشد.")
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


def build_app(application) -> web.Application:
    # rate_limit first so 429s are cheap, then auth
    app = web.Application(middlewares=[rate_limit_middleware, admin_auth_middleware])
    app["ptb_application"] = application
    app.router.add_get(CALLBACK_PATH, handle_zarinpal_callback)
    app.router.add_get("/zarinpal/start/{authority}", handle_zarinpal_start)
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
    # Static files for admin UI (public for login page styling)
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
