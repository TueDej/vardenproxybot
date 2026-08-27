import contextlib
import json
import pathlib

# Default packages — used as fallback if admin_settings.json missing (harder discount: 45% max)
DEFAULT_PACKAGES = [
    {"label": "10GB", "data_gb": 10, "price": 45000},
    {"label": "20GB", "data_gb": 20, "price": 75000},
    {"label": "40GB", "data_gb": 40, "price": 135000},
    {"label": "100GB", "data_gb": 100, "price": 245000},
    {"label": "Unlimited", "data_gb": 0, "price": 500000},
]

DEFAULT_BASE_PRICE = 4500  # toman per GB (10GB anchor)
DEFAULT_DISCOUNT_MAX_PCT = 45  # harder discount: 0% at 10GB → 45% at 100GB (selectable via admin panel)

DURATION_DAYS = 30

_SETTINGS_PATH = pathlib.Path(__file__).parent / "admin_settings.json"


def _calc_discount(gb: int, discount_max_pct: int | None = None) -> float:
    """Harder tiered discount: 0% at 10GB, ~15% at 20GB, ~22% at 40GB, max at 100GB.

    discount_max_pct is the max discount at 100GB (0-60, selectable in admin
    panel). Harder than the old 28% cap — defaults to 45%. Curve is piecewise
    linear: 0 at 10GB → 33% of max at 20GB → max at 100GB.
    """
    if gb <= 10 or gb == 0:
        return 0.0
    if discount_max_pct is None:
        # Use current global if available, else default
        try:
            discount_max_pct = int(globals().get("_DISCOUNT_MAX_PCT", DEFAULT_DISCOUNT_MAX_PCT))
        except Exception:
            discount_max_pct = DEFAULT_DISCOUNT_MAX_PCT
    try:
        max_d = max(0, min(int(discount_max_pct), 60)) / 100.0
    except Exception:
        max_d = DEFAULT_DISCOUNT_MAX_PCT / 100.0
    base_20 = max_d * 0.33  # discount at 20GB is 33% of max
    if gb <= 20:
        return (gb - 10) / 10.0 * base_20
    # 20GB → 100GB linear to max
    return min(max_d, base_20 + (gb - 20) / 80.0 * (max_d - base_20))


def calc_price(
    base_per_gb: int, data_gb: int, manual_price: int | None = None, discount_max_pct: int | None = None
) -> int:
    """Calculate price with discount, rounded down to multiple of 5000 toman.

    Unlimited (0) uses manual_price.
    discount_max_pct overrides the stored global when provided.
    """
    if data_gb == 0:
        # Unlimited must have manual price
        if manual_price is not None:
            return int(manual_price)
        return 500000
    discount = _calc_discount(data_gb, discount_max_pct)
    raw = base_per_gb * data_gb * (1 - discount)
    # Round down to nearest 5000
    price = (int(raw) // 5000) * 5000
    return max(5000, price)


def _load_settings() -> dict:
    try:
        if _SETTINGS_PATH.exists():
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def load_packages() -> tuple[list[dict], int, bool, int]:
    """Return (packages, base_price_per_gb, payments_paused, discount_max_pct). Validates and falls back."""
    settings = _load_settings()
    base = settings.get("base_price_per_gb", DEFAULT_BASE_PRICE)
    try:
        base = int(base)
    except (ValueError, TypeError):
        base = DEFAULT_BASE_PRICE
    base = max(1000, min(base, 100000))

    payments_paused = bool(settings.get("payments_paused", False))

    discount_max_pct = settings.get("discount_max_pct", DEFAULT_DISCOUNT_MAX_PCT)
    try:
        discount_max_pct = int(discount_max_pct)
    except (ValueError, TypeError):
        discount_max_pct = DEFAULT_DISCOUNT_MAX_PCT
    discount_max_pct = max(0, min(discount_max_pct, 60))

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
            return validated, base, payments_paused, discount_max_pct

    # Fallback: return defaults as-is (no recalc until admin saves)
    return [dict(p) for p in DEFAULT_PACKAGES], base, payments_paused, discount_max_pct


def save_packages(
    packages: list[dict] | None = None,
    base_price_per_gb: int | None = None,
    payments_paused: bool | None = None,
    discount_max_pct: int | None = None,
) -> tuple[list[dict], int, bool, int]:
    """Persist packages/settings atomically. Returns saved (packages, base, paused, discount_max)."""
    current_pkgs, current_base, current_paused, current_discount_max = load_packages()

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

    if discount_max_pct is None:
        discount_max_pct = current_discount_max
    else:
        try:
            discount_max_pct = int(discount_max_pct)
        except (ValueError, TypeError):
            discount_max_pct = current_discount_max
        discount_max_pct = max(0, min(discount_max_pct, 60))

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
            # For Unlimited, respect provided price; for others recalc with current base + discount
            raw_price = p.get("price")
            if data_gb == 0:
                try:
                    price = int(raw_price) if raw_price is not None else 500000
                except (ValueError, TypeError):
                    price = 500000
            else:
                # Recalculate with current base + discount (harder curve via discount_max)
                price = calc_price(base_price_per_gb, data_gb, None, discount_max_pct)
            validated.append({"label": label, "data_gb": data_gb, "price": price})
        if validated:
            current_pkgs = validated
        else:
            # No valid packages provided but discount/base may have changed — recalc existing package prices
            recalculated: list[dict] = []
            for p in current_pkgs:
                if p["data_gb"] == 0:
                    recalculated.append(dict(p))
                else:
                    recalculated.append(
                        {
                            "label": p["label"],
                            "data_gb": p["data_gb"],
                            "price": calc_price(base_price_per_gb, p["data_gb"], None, discount_max_pct),
                        }
                    )
            current_pkgs = recalculated
    else:
        # No packages payload — still recalc if base or discount changed
        if discount_max_pct != current_discount_max or base_price_per_gb != current_base:
            recalculated: list[dict] = []
            for p in current_pkgs:
                if p["data_gb"] == 0:
                    recalculated.append(dict(p))
                else:
                    recalculated.append(
                        {
                            "label": p["label"],
                            "data_gb": p["data_gb"],
                            "price": calc_price(base_price_per_gb, p["data_gb"], None, discount_max_pct),
                        }
                    )
            current_pkgs = recalculated

    # Re-ensure Unlimited has a price
    for p in current_pkgs:
        if p["data_gb"] == 0 and not p.get("price"):
            p["price"] = 500000

    data = {
        "base_price_per_gb": base_price_per_gb,
        "payments_paused": payments_paused,
        "discount_max_pct": discount_max_pct,
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
    global PACKAGES, _BASE_PRICE, _PAYMENTS_PAUSED, _DISCOUNT_MAX_PCT
    PACKAGES = current_pkgs
    _BASE_PRICE = base_price_per_gb
    _PAYMENTS_PAUSED = payments_paused
    _DISCOUNT_MAX_PCT = discount_max_pct
    return current_pkgs, base_price_per_gb, payments_paused, discount_max_pct


# Runtime loaded packages (single source of truth for keyboards/handlers)
PACKAGES, _BASE_PRICE, _PAYMENTS_PAUSED, _DISCOUNT_MAX_PCT = load_packages()

# Keep DURATION_DAYS as is
