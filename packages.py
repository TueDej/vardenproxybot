import contextlib
import json
import pathlib

# Default packages — used as fallback if admin_settings.json missing
DEFAULT_PACKAGES = [
    {"label": "10GB", "data_gb": 10, "price": 45000},
    {"label": "20GB", "data_gb": 20, "price": 80000},
    {"label": "40GB", "data_gb": 40, "price": 140000},
    {"label": "100GB", "data_gb": 100, "price": 300000},
    {"label": "Unlimited", "data_gb": 0, "price": 500000},
]

DEFAULT_BASE_PRICE = 4500  # toman per GB (10GB anchor)

DURATION_DAYS = 30

_SETTINGS_PATH = pathlib.Path(__file__).parent / "admin_settings.json"


def _calc_discount(gb: int) -> float:
    """Smart tiered discount: 0% at 10GB, ~10% at 20GB, ~16% at 40GB, ~28% at 100GB."""
    if gb <= 10 or gb == 0:
        return 0.0
    # diminishing curve
    return min(0.28, 0.10 + (gb - 20) * 0.003)


def calc_price(base_per_gb: int, data_gb: int, manual_price: int | None = None) -> int:
    """Calculate price with discount + .99 trick (just under round thousand).

    Example: base 6000*10=60000 raw → 59,999 (not 60,999) to look cheaper.
    Unlimited (0) uses manual_price.
    """
    if data_gb == 0:
        # Unlimited must have manual price
        if manual_price is not None:
            return int(manual_price)
        return 500000
    discount = _calc_discount(data_gb)
    raw = base_per_gb * data_gb * (1 - discount)
    # .99 trick: floor to next thousand then -1 (e.g. 60,000 → 59,999)
    price = (int(raw) // 1000) * 1000 - 1
    # If raw is exact thousand, the trick gives one below; if raw not exact, it floors then -1
    # Ensure at least 999 and not below 0
    # For raw <1000, floor is 0 → -1 would be negative, so clamp
    if price < 999:
        # For small values, use raw -1 floored? keep at least 999
        price = max(999, int(raw) - 1 if raw >= 1000 else 999)
    return price


def _load_settings() -> dict:
    try:
        if _SETTINGS_PATH.exists():
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def load_packages() -> tuple[list[dict], int, bool]:
    """Return (packages, base_price_per_gb, payments_paused). Validates and falls back."""
    settings = _load_settings()
    base = settings.get("base_price_per_gb", DEFAULT_BASE_PRICE)
    try:
        base = int(base)
    except (ValueError, TypeError):
        base = DEFAULT_BASE_PRICE
    base = max(1000, min(base, 100000))

    payments_paused = bool(settings.get("payments_paused", False))

    pkgs = settings.get("packages")
    if isinstance(pkgs, list) and pkgs:
        # Validate each entry
        validated: list[dict] = []
        for p in pkgs:
            if not isinstance(p, dict):
                continue
            try:
                label = str(p.get("label", "")).strip() or "Unknown"
                data_gb = int(p.get("data_gb", 0))
                # price may be recalculated, but respect saved price for Unlimited
                price = int(p.get("price", 0))
            except (ValueError, TypeError):
                continue
            if data_gb < 0 or data_gb > 10000:
                continue
            if price < 0 or price > 10_000_000:
                continue
            validated.append({"label": label, "data_gb": data_gb, "price": price})
        if validated:
            return validated, base, payments_paused

    # Fallback: return defaults as-is (no recalc until admin saves)
    return [dict(p) for p in DEFAULT_PACKAGES], base, payments_paused


def save_packages(
    packages: list[dict] | None = None,
    base_price_per_gb: int | None = None,
    payments_paused: bool | None = None,
) -> tuple[list[dict], int, bool]:
    """Persist packages/settings atomically. Returns saved (packages, base, paused)."""
    current_pkgs, current_base, current_paused = load_packages()

    if base_price_per_gb is not None:
        try:
            base_price_per_gb = int(base_price_per_gb)
        except (ValueError, TypeError):
            base_price_per_gb = current_base
        base_price_per_gb = max(1000, min(base_price_per_gb, 100000))
    else:
        base_price_per_gb = current_base

    if payments_paused is None:
        payments_paused = current_paused
    else:
        payments_paused = bool(payments_paused)

    if packages is not None:
        # Validate and normalize
        validated: list[dict] = []
        for p in packages:
            if not isinstance(p, dict):
                continue
            label = str(p.get("label", "")).strip()
            if not label:
                continue
            try:
                data_gb = int(p.get("data_gb", 0))
            except (ValueError, TypeError):
                continue
            # For Unlimited, respect provided price; for others recalc if not provided or base changed
            raw_price = p.get("price")
            if data_gb == 0:
                try:
                    price = int(raw_price) if raw_price is not None else 500000
                except (ValueError, TypeError):
                    price = 500000
            else:
                # Recalculate with current base + discount + .999
                price = calc_price(base_price_per_gb, data_gb)
            validated.append({"label": label, "data_gb": data_gb, "price": price})
        if validated:
            current_pkgs = validated

    # Re-ensure Unlimited has a price
    for p in current_pkgs:
        if p["data_gb"] == 0 and not p.get("price"):
            p["price"] = 500000

    data = {
        "base_price_per_gb": base_price_per_gb,
        "payments_paused": payments_paused,
        "packages": current_pkgs,
    }
    # Atomic write
    tmp = _SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(_SETTINGS_PATH)
    with contextlib.suppress(Exception):
        _SETTINGS_PATH.chmod(0o600)
    # Refresh module globals so keyboards/handlers see new values without restart
    global PACKAGES, _BASE_PRICE, _PAYMENTS_PAUSED
    PACKAGES = current_pkgs
    _BASE_PRICE = base_price_per_gb
    _PAYMENTS_PAUSED = payments_paused
    return current_pkgs, base_price_per_gb, payments_paused


# Runtime loaded packages (single source of truth for keyboards/handlers)
PACKAGES, _BASE_PRICE, _PAYMENTS_PAUSED = load_packages()

# Keep DURATION_DAYS as is
