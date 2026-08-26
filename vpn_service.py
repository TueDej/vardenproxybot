import asyncio
import logging
import secrets
import time

try:
    from datetime import UTC
except ImportError:  # Python <3.11
    from datetime import timezone

    UTC = timezone.utc  # type: ignore[no-redef]  # noqa: UP017
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from config import config

log = logging.getLogger(__name__)

CLIENTS_API = "/panel/api/clients"
INBOUNDS_API = "/panel/api/inbounds"

# Entry point published to users (the reverse proxy in front of the panel).
PUBLIC_ENTRY_PORT = 443

# Preferred ordering of preserved transport parameters in rewritten URIs.
_TRANSPORT_KEY_ORDER = ("type", "host", "path", "serviceName", "mode", "headerType", "flow")


class VPNPanelError(Exception):
    """Raised when the 3x-ui panel API returns an error or is unreachable."""


def build_expiry_ms(duration_days: int) -> int:
    expires = datetime.now(UTC) + timedelta(days=duration_days)
    return int(expires.timestamp() * 1000)


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0", "::"}


def rewrite_vless_link(link: str) -> str:
    """Rewrite a panel vless URI so clients enter through the public reverse proxy.

    The panel emits links pointing at its raw backend address (often
    localhost:PORT) with security=none. Clients must instead connect to the
    public domain on the standard TLS entry. The public host is taken from the
    transport ``host`` parameter (set by the operator in the inbound stream
    settings); loopback values fall back to PANEL_URL's hostname. Port is
    forced to PUBLIC_ENTRY_PORT, TLS/SNI/fingerprint/ALPN are enforced, and
    remaining transport settings (type/path/...) are preserved.
    Non-vless or malformed URIs are returned unchanged.
    """
    try:
        parts = urlsplit(link)
    except ValueError:
        return link
    if parts.scheme != "vless" or not parts.hostname or not parts.username:
        return link

    params = dict(parse_qsl(parts.query, keep_blank_values=True))

    # Public domain: transport host param wins over the (backend) authority.
    raw_host = (params.get("host") or "").strip()
    # Strip port safely: handle IPv6 literals like [::1]:443
    public_host = raw_host
    if public_host.startswith("["):
        # IPv6 literal with brackets
        end = public_host.find("]")
        if end != -1:
            public_host = public_host[1:end].strip()
        else:
            public_host = public_host.strip("[]").strip()
    elif public_host.count(":") == 1 and public_host.count(".") == 0:
        # Likely host:port (single colon, no dots) — but avoid splitting IPv6 (multiple colons)
        public_host = public_host.split(":", 1)[0].strip()
    elif ":" in public_host and "." in public_host:
        # host:port with dot (e.g. example.com:443)
        public_host = public_host.split(":", 1)[0].strip()
    public_host = public_host.strip("[]").strip() or (parts.hostname or "")
    if not public_host:
        return link
    if public_host.lower() in _LOOPBACK_HOSTS and config.panel_url:
        fallback = urlsplit(config.panel_url).hostname
        if fallback:
            public_host = fallback

    rewritten = {
        "encryption": params.pop("encryption", "none"),
        "security": "tls",
        "sni": public_host,
        "fp": "chrome",
        "alpn": "h2",
        "insecure": "0",
        "allowInsecure": "0",
    }
    for key in _TRANSPORT_KEY_ORDER:
        if key in params:
            rewritten[key] = params.pop(key)
    for key, value in params.items():  # any unexpected extras keep their order
        rewritten.setdefault(key, value)

    # Bracket IPv6 literals for netloc
    host_for_netloc = public_host
    if ":" in public_host and not public_host.startswith("["):
        # Contains colon -> likely IPv6 without brackets
        host_for_netloc = f"[{public_host}]"
    netloc = f"{parts.username}@{host_for_netloc}:{PUBLIC_ENTRY_PORT}"
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(rewritten), parts.fragment))


def normalize_links(obj) -> list[str]:
    """Normalize the /links/{email} response payload to user-facing URI strings."""
    if obj is None:
        return []
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        for key in ("links", "urls", "obj"):
            if isinstance(obj.get(key), list):
                items = obj[key]
                break
        else:
            return []
    else:
        return []
    links = []
    for item in items:
        if isinstance(item, str) and "://" in item:
            links.append(rewrite_vless_link(item))
        elif isinstance(item, dict) and isinstance(item.get("url"), str):
            links.append(rewrite_vless_link(item["url"]))
    return links


class VPNPanelService:
    """Async client for the 3x-ui v3.x REST API (Bearer token auth)."""

    @staticmethod
    def is_configured() -> bool:
        return config.panel_configured

    @classmethod
    async def _request(
        cls,
        method: str,
        path: str,
        json_body: dict | None = None,
        retries: int = 3,
    ):
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=config.panel_url,
                    headers={"Authorization": f"Bearer {config.panel_api_token}"},
                    verify=config.panel_verify_ssl,
                    trust_env=False,
                    timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0),
                ) as client:
                    resp = await client.request(method, path, json=json_body)
            except httpx.HTTPError as exc:
                msg = str(exc)
                if "WRONG_VERSION_NUMBER" in msg or "wrong version number" in msg.lower():
                    reason = (
                        f"{exc} (hint: the panel port answered plain HTTP — "
                        f"use http:// instead of https:// in PANEL_URL)"
                    )
                elif "certificate verify" in msg.lower() or "self-signed" in msg.lower():
                    reason = f"{exc} (hint: self-signed cert? set PANEL_VERIFY_SSL=false)"
                else:
                    reason = msg
                last_error = VPNPanelError(f"Panel unreachable [{method} {path}]: {reason}")
            else:
                if resp.status_code in (401, 403):
                    raise VPNPanelError("Panel authentication failed — check PANEL_API_TOKEN.")
                try:
                    data = resp.json()
                except ValueError:
                    snippet = " ".join((resp.text or "").split())[:160]
                    suffix = f": {snippet}" if snippet else ""
                    last_error = VPNPanelError(
                        f"Non-JSON response (HTTP {resp.status_code}) for {method} {path}{suffix}"
                    )
                    data = None
                if isinstance(data, dict):
                    if not data.get("success"):
                        msg = data.get("msg") or f"HTTP {resp.status_code}"
                        # Retry on server errors (5xx), fail fast on client errors (4xx, auth)
                        if resp.status_code >= 500:
                            last_error = VPNPanelError(f"Panel error [{method} {path}]: {msg}")
                        else:
                            raise VPNPanelError(f"Panel error [{method} {path}]: {msg}")
                    else:
                        return data.get("obj")
                elif data is not None:
                    last_error = VPNPanelError(
                        f"Unexpected JSON payload for {method} {path}: expected an object"
                    )
            if attempt < retries:
                await asyncio.sleep(0.5 * attempt)
        raise last_error or VPNPanelError("Panel request failed.")

    @classmethod
    async def create_client(cls, telegram_id: int, duration_days: int, data_gb: int) -> dict:
        """Create a client on the configured inbound.

        Returns {"email", "sub_id", "links"} where links are ready-to-import URIs.
        """
        email = f"vp{telegram_id}-{int(time.time())}"
        sub_id = secrets.token_hex(8)
        payload = {
            "client": {
                "email": email,
                "subId": sub_id,
                "totalGB": max(0, data_gb) * 1024**3,
                "expiryTime": build_expiry_ms(duration_days),
                "limitIp": config.vpn_limit_ip,
                "tgId": telegram_id,
                "enable": True,
            },
            "inboundIds": [config.xui_inbound_id],
        }
        await cls._request("POST", f"{CLIENTS_API}/add", payload)

        # Re-read until the freshly created client shows up in link generation.
        try:
            for _ in range(5):
                try:
                    links = normalize_links(
                        await cls._request("GET", f"{CLIENTS_API}/links/{email}")
                    )
                except VPNPanelError:
                    await asyncio.sleep(0.4)
                    continue
                if links:
                    return {"email": email, "sub_id": sub_id, "links": links}
                await asyncio.sleep(0.4)
        except Exception:
            # Ensure orphan cleanup even if loop aborted
            await cls.delete_client(email)
            raise

        # No links appeared — remove the orphan so a retry won't create a duplicate.
        await cls.delete_client(email)
        raise VPNPanelError(f"Client {email} was created but no config links were returned.")

    @classmethod
    async def delete_client(cls, email: str) -> bool:
        """Delete a client by its panel email. Returns True on success."""
        try:
            await cls._request("POST", f"{CLIENTS_API}/del/{email}")
            return True
        except VPNPanelError as exc:
            log.warning("Failed to delete panel client %s: %s", email, exc)
            return False

    @classmethod
    async def get_client(cls, email: str) -> dict | None:
        """Fetch the full client payload by email (preserves uuid/password/flow).

        Returns the client dict on success, or None if the client is missing.
        """
        try:
            return await cls._request("GET", f"{CLIENTS_API}/get/{email}")
        except VPNPanelError:
            return None

    @classmethod
    async def update_client(cls, client: dict, email: str | None = None) -> None:
        """Replace an existing client row. The server does NOT patch, so the
        caller must send the full payload (use get_client first)."""
        target = email or client.get("email")
        if not target:
            raise VPNPanelError("update_client requires a client with an email.")
        await cls._request("POST", f"{CLIENTS_API}/update/{target}", client)

    @classmethod
    async def extend_client(cls, email: str, duration_days: int, data_gb: int) -> dict:
        """Extend an existing client by `duration_days` and reset its traffic
        counters for a fresh period.

        Expiry is pushed forward from the later of (current expiry, now) so an
        already-expired client is revived to now+duration. `data_gb == 0` means
        unlimited data — its totalGB is left untouched. Returns the updated client.
        """
        client = await cls.get_client(email)
        if not client:
            raise VPNPanelError(f"Client {email} not found for renewal.")
        # Normalize: some panel versions wrap the client under a "client" key,
        # or omit the email field when fetched by email. Flatten and restore
        # the email so the full-payload update (which needs it in URL + body)
        # succeeds.
        if not client.get("email") and isinstance(client.get("client"), dict):
            client = client["client"]
        client["email"] = email
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        existing_expiry = int(client.get("expiryTime", 0) or 0)
        if data_gb and data_gb > 0:
            base = existing_expiry if existing_expiry > now_ms else now_ms
            client["expiryTime"] = base + duration_days * 86400 * 1000
            client["totalGB"] = data_gb * 1024**3
        elif existing_expiry and existing_expiry > 0:
            # Unlimited data but time-limited: only push the expiry forward.
            base = existing_expiry if existing_expiry > now_ms else now_ms
            client["expiryTime"] = base + duration_days * 86400 * 1000
        # else: fully unlimited (never expires) — nothing to extend.
        client["enable"] = True
        await cls.update_client(client, email)
        # Start the new period with a clean usage counter.
        try:
            await cls._request("POST", f"{CLIENTS_API}/resetTraffic/{email}")
        except VPNPanelError as exc:
            log.warning("Could not reset traffic for renewed client %s: %s", email, exc)
        return client

    @classmethod
    async def get_inbound_client_emails(cls) -> set[str]:
        """Emails of all clients currently attached to the configured inbound."""
        inbound = await cls._request("GET", f"{INBOUNDS_API}/get/{config.xui_inbound_id}")
        clients = (inbound or {}).get("settings", {}).get("clients", [])
        return {c["email"] for c in clients if isinstance(c, dict) and c.get("email")}

    @classmethod
    async def get_clients_by_telegram_id(cls, telegram_id: int) -> list[dict]:
        """Return all panel clients for a user, fetched via /clients/list.

        Each dict has: email, sub_id, enable, expiry_time, total_gb,
        limit_ip, links, sub_links
        """
        data = await cls._request("GET", f"{CLIENTS_API}/list")
        raw_clients = [c for c in (data if isinstance(data, list) else []) if isinstance(c, dict)]
        matching = [c for c in raw_clients if str(c.get("tgId")) == str(telegram_id)]

        # Limit concurrency to avoid panel rate-limit / FD exhaustion (previously 2*N concurrent)
        sem = asyncio.Semaphore(5)

        async def hydrate(c: dict) -> dict:
            email = c.get("email", "")
            sub_id = c.get("subId", "")
            async with sem:
                links, sub_links = await asyncio.gather(
                    cls.get_client_links(email),
                    cls.get_subscription_links(sub_id),
                )
            return {
                "email": email,
                "sub_id": sub_id,
                "enable": c.get("enable", True),
                "expiry_time": c.get("expiryTime", 0),
                "total_gb": c.get("totalGB", 0),
                "limit_ip": c.get("limitIp", 0),
                "links": links,
                "sub_links": sub_links,
            }

        return list(await asyncio.gather(*(hydrate(c) for c in matching)))

    @classmethod
    async def get_subscription_links(cls, sub_id: str) -> list[str]:
        """Fetch config links for a subscription ID (all clients with this subId)."""
        if not sub_id:
            return []
        try:
            return normalize_links(await cls._request("GET", f"{CLIENTS_API}/subLinks/{sub_id}"))
        except VPNPanelError:
            return []

    @classmethod
    async def get_client_links(cls, email: str) -> list[str]:
        """Fetch config links for a client by email."""
        if not email:
            return []
        try:
            return normalize_links(await cls._request("GET", f"{CLIENTS_API}/links/{email}"))
        except VPNPanelError:
            return []

    @classmethod
    async def get_online_emails(cls) -> set[str]:
        """Emails of clients with an active connection right now.

        Returns an empty set if the /onlines endpoint is unavailable
        (e.g. older panel versions) — online status is non-critical.
        """
        try:
            online = await cls._request("POST", f"{CLIENTS_API}/onlines")
        except VPNPanelError:
            return set()
        if isinstance(online, list):
            return {e for e in online if isinstance(e, str)}
        return set()

    @classmethod
    async def get_server_status(cls) -> dict:
        status = {
            "server": config.panel_url,
            "status": "unknown",
            "online_users": 0,
            "inbound_clients": 0,
        }
        try:
            online = await cls._request("POST", f"{CLIENTS_API}/onlines")
            if isinstance(online, list):
                status["online_users"] = len(online)
        except VPNPanelError:
            pass  # /onlines unavailable on this panel version
        try:
            inbound = await cls._request("GET", f"{INBOUNDS_API}/get/{config.xui_inbound_id}")
            clients = (inbound or {}).get("settings", {}).get("clients", [])
            status["inbound_clients"] = len(clients)
            status["status"] = "online"
        except VPNPanelError as exc:
            status["status"] = f"error ({exc})"
        return status


async def _selfcheck() -> None:
    """Standalone credential + API-version check: python -m vpn_service"""
    from models import Base  # noqa: F401  (ensures package imports cleanly)

    if not VPNPanelService.is_configured():
        print("ERROR: PANEL_URL / PANEL_API_TOKEN / XUI_INBOUND_ID not set.")
        return

    print(f"Panel base URL : {config.panel_url}")
    print(f"Inbound ID     : {config.xui_inbound_id}")
    print(f"TLS verify     : {config.panel_verify_ssl}")
    print("")

    # Step 1: auth + inbound reachable (endpoint exists in both v2.x and v3.x)
    try:
        inbound = await VPNPanelService._request(
            "GET", f"{INBOUNDS_API}/get/{config.xui_inbound_id}", retries=1
        )
    except VPNPanelError as exc:
        print(f"FAIL auth/inbound: {exc}")
        print("")
        print("Checklist:")
        print("  1. PANEL_URL must include the webBasePath, e.g. https://host:2053/<base>/")
        print("  2. Scheme must match the panel port (http:// vs https://)")
        print("  3. PANEL_VERIFY_SSL=false if the cert is self-signed")
        return
    remark = inbound.get("remark") if isinstance(inbound, dict) else None
    port = inbound.get("port") if isinstance(inbound, dict) else None
    protocol = inbound.get("protocol") if isinstance(inbound, dict) else None
    clients = (inbound or {}).get("settings", {}).get("clients", [])
    print(
        f"OK   auth + inbound: remark={remark!r} protocol={protocol} "
        f"port={port} clients={len(clients)}"
    )

    # Step 2: does the modern v3 client API exist on this panel?
    try:
        await VPNPanelService._request("GET", f"{CLIENTS_API}/list", retries=1)
    except VPNPanelError as exc:
        print(f"WARN modern client API: {exc}")
        print("")
        print(f"This panel does not answer GET {CLIENTS_API}/list.")
        print("It is likely an older 3x-ui (v2.x) which only has the legacy")
        print("/panel/api/inbounds/addClient API. Upgrade the panel to latest")
        print("MHSanaei/3x-ui, or ask for legacy-mode support in the bot.")
        return
    print("OK   modern client API (/panel/api/clients/*) available")
    print("")
    print("ALL GOOD — the bot can create real configs.")


if __name__ == "__main__":
    asyncio.run(_selfcheck())
