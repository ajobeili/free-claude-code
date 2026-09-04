"""Inter-market snapshot provider for the meta-controller.

Pulls DXY, ES, NQ, VIX via Capital.com (already authenticated) and returns
their current price + 1h change. Graceful fallback to yfinance if Capital.com
doesn't have a particular symbol. Returns {} on any failure — never raises.

These four tickers are the standard "inter-market dashboard" for FX + US
equity trading. DXY tells us the broad USD regime; ES/NQ are the US equity
leadership indices; VIX is the fear gauge.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Symbol -> (Capital.com epic, asset_class). Capital.com uses epic codes that
# differ from ticker symbols. We try common conventions here; missing epics
# will fall through to yfinance.
_INTERMARKET_SYMBOLS: list[tuple[str, str, str]] = [
    # (display name, capital_epic, asset_class)
    ("DXY",  "USDOLLAR",      "fx"),
    ("ES",   "ES100",         "indices"),
    ("NQ",   "NQ100",         "indices"),
    ("VIX",  "VIX500",        "indices"),
]

# Per-symbol cap to bound the request count.
_CACHE_TTL_S = 30  # 30s; the live engine ticks faster but the inter-market moves slowly
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _safe_get_capital_prices(epics: list[str]) -> dict[str, dict[str, Any]]:
    """Try Capital.com. Returns {epic: {price, change_1h_pct}} or {} on failure."""
    try:
        from tools.capital_api import get_market_snapshot  # local import
    except Exception as exc:
        logger.debug("capital_api import failed: %s", exc)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for epic in epics:
        try:
            snap = get_market_snapshot(epic, lookback_bars=2, timeframe="H1")
            prices = snap.get("closes") or []
            if len(prices) < 2:
                continue
            cur = float(prices[-1])
            prev = float(prices[-2])
            chg = ((cur - prev) / prev * 100.0) if prev else 0.0
            out[epic] = {"price": round(cur, 4), "change_1h_pct": round(chg, 3)}
        except Exception as exc:
            logger.debug("capital snapshot %s failed: %s", epic, exc)
            continue
    return out


def _safe_get_yfinance(symbol: str) -> dict[str, Any] | None:
    """Try yfinance for one symbol. Returns {price, change_1h_pct} or None."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="2d", interval="1h", progress=False)
        if hist is None or hist.empty or len(hist) < 2:
            return None
        cur = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        chg = ((cur - prev) / prev * 100.0) if prev else 0.0
        return {"price": round(cur, 4), "change_1h_pct": round(chg, 3)}
    except Exception as exc:
        logger.debug("yfinance %s failed: %s", symbol, exc)
        return None


# Map display name -> yfinance symbol for fallback
_YF_FALLBACK = {
    "DXY": "DX-Y.NYB",
    "ES":  "ES=F",
    "NQ":  "NQ=F",
    "VIX": "^VIX",
}


def fetch_intermarket(asset_class: str = "all") -> dict[str, dict[str, Any]]:
    """Return {DXY: {...}, ES: {...}, NQ: {...}, VIX: {...}}.

    `asset_class` is a hint ("fx", "indices", "all"). Missing symbols are
    simply omitted from the returned dict. Cached for 30s.
    """
    cache_key = asset_class
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_S:
        return cached[1]

    epics = [epic for _, epic, cls in _INTERMARKET_SYMBOLS
             if asset_class == "all" or asset_class == cls]
    cap_data = _safe_get_capital_prices(epics)

    out: dict[str, dict[str, Any]] = {}
    for name, epic, _ in _INTERMARKET_SYMBOLS:
        if asset_class != "all" and asset_class != _:
            continue
        # Prefer capital, fall back to yfinance.
        snap = cap_data.get(epic)
        if snap is None:
            snap = _safe_get_yfinance(_YF_FALLBACK.get(name, ""))
        if snap is not None:
            out[name] = snap

    _CACHE[cache_key] = (time.time(), out)
    return out


if __name__ == "__main__":  # smoke test
    import json
    print(json.dumps(fetch_intermarket(), indent=2))
