"""Async client for the Zarinpal payment gateway (PG v4 REST API).

Authentication uses the merchant/terminal UUID supplied via
ZARINPAL_ACCESS_TOKEN. All requests use a direct connection — gateway
traffic must never traverse the SOCKS proxy used for Telegram.
"""

import asyncio
import logging
from typing import Any

import httpx

from config import config

log = logging.getLogger(__name__)

REQUEST_PATH = "/pg/v4/payment/request.json"
VERIFY_PATH = "/pg/v4/payment/verify.json"
REVERSE_PATH = "/pg/v4/payment/reverse.json"

# Gateway codes that indicate a successful transaction.
VERIFIED_NEW = 100
VERIFIED_ALREADY = 101

# Rial per toman — toodej stores toman, Zarinpal v4 expects rial (like toodej)
RIAL_PER_TOMAN = 10

_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)


def _toman_to_rial(amount_toman: int) -> int:
    if amount_toman < 0:
        raise ZarinpalError(f"negative amount: {amount_toman}")
    return amount_toman * RIAL_PER_TOMAN


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
        if errors.get("message") and errors.get("code"):
            msgs.append(f"{errors['message']} (code {errors['code']})")
        else:
            msgs.append(str(errors.get("message") or errors.get("code") or errors))
    if not msgs and isinstance(data, dict) and isinstance(data.get("data"), dict):
        inner = data["data"]
        code = inner.get("code")
        if code not in (None, VERIFIED_NEW, VERIFIED_ALREADY):
            msgs.append(
                f"code {code}: {inner.get('message')}" if inner.get("message") else f"code {code}"
            )
    return "; ".join(msgs) or f"HTTP {status_code}"


async def _post(path: str, payload: dict, retries: int = 3) -> dict:
    """POST JSON to the gateway and return the decoded response object.

    Connection-level failures (DNS, timeouts, resets) and HTTP 5xx/429
    are retried with backoff so transient blips don't kill the order flow.
    Respects Retry-After on 429.
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=_TIMEOUT) as client:
                resp = await client.post(
                    f"{config.zarinpal_api_base_url}{path}",
                    json=payload,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
        except httpx.HTTPError as exc:
            last_exc = exc
            log.warning("Gateway %s attempt %d/%d failed: %s", path, attempt, retries, exc)
            if attempt < retries:
                await asyncio.sleep(0.5 * attempt)
            continue

        # Retry on server errors and rate-limit
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else 0.5 * attempt
                wait = max(0.5, min(wait, 10))
            except (TypeError, ValueError):
                wait = 0.5 * attempt
            snippet = " ".join((resp.text or "").split())[:160]
            last_exc = ZarinpalError(
                f"HTTP {resp.status_code} [{path}]: {snippet or resp.reason_phrase}"
            )
            log.warning(
                "Gateway %s attempt %d/%d HTTP %s, retrying in %.1fs",
                path,
                attempt,
                retries,
                resp.status_code,
                wait,
            )
            if attempt < retries:
                await asyncio.sleep(wait)
                continue
            raise last_exc

        try:
            data = resp.json()
        except ValueError as exc:
            snippet = " ".join((resp.text or "").split())[:160]
            raise ZarinpalError(
                f"Non-JSON response (HTTP {resp.status_code}) [{path}]: {snippet}"
            ) from exc

        if not isinstance(data, dict):
            raise ZarinpalError(f"Unexpected response shape [{path}] (HTTP {resp.status_code})")
        log.debug("Zarinpal %s -> %s", path, data)
        return data

    raise ZarinpalError(f"Gateway unreachable [{path}]: {last_exc}")


async def request_payment(order_id: int, amount_toomans: int, description: str) -> dict:
    """Create a payment session.

    Mirrors toodej (farmstore) — amount in rial, no currency field, so the
    same merchant does not trigger the checkout.toodej.shop custom checkout.
    Returns {"authority", "startpay_url"}; raises ZarinpalError on failure.
    """
    amount_rial = _toman_to_rial(amount_toomans)
    payload = {
        "merchant_id": config.zarinpal_access_token,
        "amount": amount_rial,
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
        "startpay_url": config.zarinpal_startpay_url(authority),
    }


async def verify_payment(authority: str, amount_toomans: int) -> dict:
    """Verify a transaction server-side.

    Amount must match the request amount (rial, like toodej).
    Returns {"ok": True, "already_done", "ref_id", "card_pan"} on success;
    raises ZarinpalError when the payment is unverified/cancelled/failed.
    """
    amount_rial = _toman_to_rial(amount_toomans)
    payload = {
        "merchant_id": config.zarinpal_access_token,
        "amount": amount_rial,
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


async def reverse_payment(authority: str) -> dict:
    """Reverse (auto-refund) a successful transaction.

    Only works within 30 minutes of the payment and requires the store's
    server IP to be whitelisted in the Zarinpal terminal settings (-62).
    Returns {"refunded": True} on success; raises ZarinpalError otherwise.
    """
    payload = {
        "merchant_id": config.zarinpal_access_token,
        "authority": authority,
    }
    data = await _post(REVERSE_PATH, payload)
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    if inner.get("code") != VERIFIED_NEW or data.get("errors"):
        raise ZarinpalError(_error_detail(data, 200, REVERSE_PATH))
    return {"refunded": True}
