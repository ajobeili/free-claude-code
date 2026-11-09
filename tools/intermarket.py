"""Inter-market snapshot for the meta-controller.

DXY, US500, US100 and VIX hourly change, read from the authenticated broker
session. Symbols the broker cannot price are omitted; nothing is synthesized.
"""

import asyncio
import logging
import time

import httpx

from tools.capital_api import BrokerSessionExpired, CapitalAPI, resolve_epic

logger = logging.getLogger(__name__)

_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("DXY", "fx"),
    ("US500", "indices"),
    ("US100", "indices"),
    ("VIX", "indices"),
)

_CACHE_TTL_S = 30
_FETCH_TIMEOUT_S = 3.0
_CACHE: dict[str, tuple[float, dict[str, dict[str, float]]]] = {}


async def _hourly_change(api: CapitalAPI, symbol: str) -> dict[str, float] | None:
    try:
        async with asyncio.timeout(_FETCH_TIMEOUT_S):
            bars = await api.get_ohlcv(await resolve_epic(api, symbol), "HOUR", 2)
    except httpx.HTTPError, BrokerSessionExpired, TimeoutError:
        logger.debug("intermarket %s unavailable", symbol, exc_info=True)
        return None
    if len(bars) < 2:
        return None
    cur = float(bars[-1]["close"])
    prev = float(bars[-2]["close"])
    chg = (cur - prev) / prev * 100.0 if prev else 0.0
    return {"price": round(cur, 4), "change_1h_pct": round(chg, 3)}


async def fetch_intermarket(
    api: CapitalAPI, asset_class: str = "all", *, now: float | None = None
) -> dict[str, dict[str, float]]:
    """Return {symbol: {price, change_1h_pct}} for symbols in `asset_class`."""
    now = time.time() if now is None else now
    cached = _CACHE.get(asset_class)
    if cached and now - cached[0] < _CACHE_TTL_S:
        return cached[1]
    names = [n for n, cls in _SYMBOLS if asset_class in ("all", cls)]
    snaps = await asyncio.gather(*(_hourly_change(api, n) for n in names))
    out = {n: s for n, s in zip(names, snaps, strict=True) if s is not None}
    _CACHE[asset_class] = (now, out)
    return out
