"""Async client for the Zarinpal payment gateway (PG v4 REST API).

Authentication uses the merchant/terminal UUID supplied via
ZARINPAL_ACCESS_TOKEN. All requests use a direct connection — gateway
traffic must never traverse the SOCKS proxy used for Telegram.
"""

import logging
from typing import Any

import httpx

from config import config

log = logging.getLogger(__name__)

REQUEST_PATH = "/pg/v4/payment/request.json"
VERIFY_PATH = "/pg/v4/payment/verify.json"

# Gateway codes that indicate a successful transaction.
VERIFIED_NEW = 100
VERIFIED_ALREADY = 101

_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)


class ZarinpalError(Exception):
    """Raised when the Zarinpal gateway rejects a request or is unreachable."""


def _error_detail(data: Any, status_code: int, path: str) -> str:
    """Extract a human-readable message from a Zarinpal error payload."""
    msgs: list[str] = []
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, list):
        for err in errors:
            if isinstance(err, dict):
                msgs.append(str(err.get("message") or err.get("code") or err))
            elif err:
                msgs.append(str(err))
    elif isinstance(errors, dict):
        msgs.append(str(errors.get("message") or errors.get("code") or errors))
    if not msgs and isinstance(data, dict) and isinstance(data.get("data"), dict):
        inner = data["data"]
        code = inner.get("code")
        if code not in (None, VERIFIED_NEW, VERIFIED_ALREADY):
            msgs.append(f"code {code}: {inner.get('message')}" if inner.get("message") else f"code {code}")
    return "; ".join(msgs) or f"HTTP {status_code}"


async def _post(path: str, payload: dict) -> dict:
    """POST JSON to the gateway and return the decoded response object."""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{config.zarinpal_base_url}{path}",
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise ZarinpalError(f"Gateway unreachable [{path}]: {exc}") from exc

    try:
        data = resp.json()
    except ValueError as exc:
        snippet = " ".join((resp.text or "").split())[:160]
        raise ZarinpalError(f"Non-JSON response (HTTP {resp.status_code}) [{path}]: {snippet}") from exc

    if not isinstance(data, dict):
        raise ZarinpalError(f"Unexpected response shape [{path}] (HTTP {resp.status_code})")
    log.debug("Zarinpal %s -> %s", path, data)
    return data


async def request_payment(order_id: int, amount_toomans: int, description: str) -> dict:
    """Create a payment session.

    Returns {"authority", "startpay_url"}; raises ZarinpalError on failure.
    """
    payload = {
        "merchant_id": config.zarinpal_access_token,
        "amount": amount_toomans,
        "currency": "IRT",
        "description": description,
        "callback_url": config.zarinpal_callback_url,
        "metadata": {"order_id": str(order_id)},
    }
    data = await _post(REQUEST_PATH, payload)
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    authority = inner.get("authority")
    if not authority or inner.get("code") != VERIFIED_NEW or data.get("errors"):
        raise ZarinpalError(_error_detail(data, 200, REQUEST_PATH))
    return {
        "authority": authority,
        "startpay_url": f"{config.zarinpal_base_url}/pg/StartPay/{authority}",
    }


async def verify_payment(authority: str, amount_toomans: int) -> dict:
    """Verify a transaction server-side.

    Returns {"ok": True, "already_done", "ref_id", "card_pan"} on success;
    raises ZarinpalError when the payment is unverified/cancelled/failed.
    """
    payload = {
        "merchant_id": config.zarinpal_access_token,
        "amount": amount_toomans,
        "currency": "IRT",
        "authority": authority,
    }
    data = await _post(VERIFY_PATH, payload)
    inner = data.get("data") if isinstance(data.get("data"), dict) else None
    code = inner.get("code") if inner else None
    if code not in (VERIFIED_NEW, VERIFIED_ALREADY) or data.get("errors"):
        raise ZarinpalError(_error_detail(data, 200, VERIFY_PATH))
    return {
        "ok": True,
        "already_done": code == VERIFIED_ALREADY,
        "ref_id": inner.get("ref_id"),
        "card_pan": inner.get("card_pan"),
    }
