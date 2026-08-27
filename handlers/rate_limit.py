"""In-memory per-user cooldown for telegram handlers (safe for concurrent_updates=True)."""

import asyncio
import time

# (user_id, key) -> last monotonic timestamp
_last: dict[tuple[int, str], float] = {}
_lock = asyncio.Lock()


async def check_cooldown(user_id: int, key: str, seconds: int) -> bool:
    """Return True if action is allowed, False if still on cooldown.

    Updates the timestamp on success.
    """
    now = time.monotonic()
    k = (user_id, key)
    async with _lock:
        last = _last.get(k, 0)
        if now - last < seconds:
            return False
        _last[k] = now
        # Prune occasionally to avoid unbounded growth (every 1k ops)
        if len(_last) > 5000:
            cutoff = now - 3600
            for kk, ts in list(_last.items()):
                if ts < cutoff:
                    _last.pop(kk, None)
        return True


def check_cooldown_sync(user_id: int, key: str, seconds: int) -> bool:
    """Sync variant for contexts where async lock not needed (rare)."""
    now = time.monotonic()
    k = (user_id, key)
    last = _last.get(k, 0)
    if now - last < seconds:
        return False
    _last[k] = now
    return True
