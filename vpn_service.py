import asyncio
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx

from config import config

CLIENTS_API = "/panel/api/clients"
INBOUNDS_API = "/panel/api/inbounds"


class VPNPanelError(Exception):
    """Raised when the 3x-ui panel API returns an error or is unreachable."""


def build_expiry_ms(duration_days: int) -> int:
    expires = datetime.now(timezone.utc) + timedelta(days=duration_days)
    return int(expires.timestamp() * 1000)


def normalize_links(obj) -> list[str]:
    """Normalize the /links/{email} response payload to a list of URI strings."""
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
            links.append(item)
        elif isinstance(item, dict) and isinstance(item.get("url"), str):
            links.append(item["url"])
    return links


class VPNPanelService:
    """Async client for the 3x-ui v3.x REST API (Bearer token auth)."""

    @staticmethod
    def is_configured() -> bool:
        return config.panel_configured

    @staticmethod
    def subscription_url(sub_id: str | None) -> str:
        if not sub_id or not config.subscription_base_url:
            return ""
        return f"{config.subscription_base_url}/sub/{sub_id}"

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
                    timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0),
                ) as client:
                    resp = await client.request(method, path, json=json_body)
            except httpx.HTTPError as exc:
                last_error = VPNPanelError(f"Panel unreachable: {exc}")
            else:
                if resp.status_code in (401, 403):
                    raise VPNPanelError("Panel authentication failed — check PANEL_API_TOKEN.")
                try:
                    data = resp.json()
                except ValueError as exc:
                    last_error = VPNPanelError(f"Panel returned non-JSON response (HTTP {resp.status_code}).")
                    data = None
                if data is not None:
                    if not data.get("success"):
                        raise VPNPanelError(f"Panel error: {data.get('msg') or resp.status_code}")
                    return data.get("obj")
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
        for _ in range(5):
            links = normalize_links(await cls._request("GET", f"{CLIENTS_API}/links/{email}"))
            if links:
                return {"email": email, "sub_id": sub_id, "links": links}
            await asyncio.sleep(0.4)
        raise VPNPanelError(f"Client {email} was created but no config links were returned.")

    @classmethod
    async def delete_client(cls, email: str) -> bool:
        """Delete a client by its panel email. Returns True on success."""
        try:
            await cls._request("POST", f"{CLIENTS_API}/del/{email}")
            return True
        except VPNPanelError as exc:
            print(f"WARN: failed to delete panel client {email}: {exc}")
            return False

    @classmethod
    async def get_server_status(cls) -> dict:
        status = {
            "server": config.panel_url,
            "status": "unknown",
            "online_users": 0,
            "inbound_clients": 0,
        }
        try:
            online = await cls._request("GET", f"{CLIENTS_API}/onlines")
            if isinstance(online, list):
                status["online_users"] = len(online)
            inbound = await cls._request("GET", f"{INBOUNDS_API}/get/{config.xui_inbound_id}")
            clients = (inbound or {}).get("settings", {}).get("clients", [])
            status["inbound_clients"] = len(clients)
            status["status"] = "online"
        except VPNPanelError as exc:
            status["status"] = f"error ({exc})"
        return status


async def _selfcheck() -> None:
    """Standalone credential check: python -m vpn_service"""
    from models import Base  # noqa: F401  (ensures package imports cleanly)

    if not VPNPanelService.is_configured():
        print("ERROR: PANEL_URL / PANEL_API_TOKEN / XUI_INBOUND_ID not set.")
        return
    print(f"Panel: {config.panel_url}")
    try:
        inbound = await VPNPanelService._request(
            "GET", f"{INBOUNDS_API}/get/{config.xui_inbound_id}"
        )
    except VPNPanelError as exc:
        print(f"FAIL: {exc}")
        return
    remark = inbound.get("remark") if isinstance(inbound, dict) else None
    port = inbound.get("port") if isinstance(inbound, dict) else None
    protocol = inbound.get("protocol") if isinstance(inbound, dict) else None
    clients = (inbound or {}).get("settings", {}).get("clients", [])
    print(f"OK: inbound #{config.xui_inbound_id} -> remark={remark!r} "
          f"protocol={protocol} port={port} clients={len(clients)}")


if __name__ == "__main__":
    asyncio.run(_selfcheck())
