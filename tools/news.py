"""Free news headline fetcher for the meta-controller.

Sources: RSS feeds (Reuters Markets, Bloomberg Markets, FXStreet, Investing.com).
No API key, no payment. Graceful fallback — if a feed is unreachable, we just
return fewer headlines. The meta-controller prompt handles "no news" cleanly.

If a paid provider is desired later, set NEWS_API_KEY and add a fetcher that
calls it; the return shape is the same.
"""

import hashlib
import logging
import re
import time
from typing import Any
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

# RSS feeds keyed by symbol/asset class. Each entry is a list because some
# symbols are covered by multiple desks (e.g. EUR/USD shows up under both
# "forex" and the specific pair ticker).
SYMBOL_FEEDS: dict[str, list[str]] = {
    # Major FX pairs
    "EURUSD": [
        "https://feeds.reuters.com/reuters/financialNews",
        "https://www.fxstreet.com/rss/news",
    ],
    "GBPUSD": [
        "https://feeds.reuters.com/reuters/financialNews",
        "https://www.fxstreet.com/rss/news",
    ],
    "AUDUSD": [
        "https://feeds.reuters.com/reuters/financialNews",
        "https://www.fxstreet.com/rss/news",
    ],
    "NZDUSD": [
        "https://www.fxstreet.com/rss/news",
    ],
    "USDJPY": [
        "https://feeds.reuters.com/reuters/financialNews",
        "https://www.fxstreet.com/rss/news",
    ],
    "USDCAD": [
        "https://www.fxstreet.com/rss/news",
    ],
    # US equities
    "AAPL": ["https://feeds.reuters.com/reuters/financialNews"],
    "TSLA": ["https://feeds.reuters.com/reuters/financialNews"],
    "NVDA": ["https://feeds.reuters.com/reuters/financialNews"],
    "MSFT": ["https://feeds.reuters.com/reuters/financialNews"],
    "AMZN": ["https://feeds.reuters.com/reuters/financialNews"],
    "META": ["https://feeds.reuters.com/reuters/financialNews"],
    "GOOGL": ["https://feeds.reuters.com/reuters/financialNews"],
    "NFLX": ["https://feeds.reuters.com/reuters/financialNews"],
}

# Symbol -> search terms used to filter headlines that mention the instrument.
# Lowercased; match if any term appears in the title.
SYMBOL_TERMS: dict[str, list[str]] = {
    "EURUSD": ["eur/usd", "euro", "ecb", "lagarde"],
    "GBPUSD": ["gbp/usd", "sterling", "pound", "boe", "bailey"],
    "AUDUSD": ["aud/usd", "aussie", "rba"],
    "NZDUSD": ["nzd/usd", "kiwi", "rbnz"],
    "USDJPY": ["usd/jpy", "yen", "boj", "ueda"],
    "USDCAD": ["usd/cad", "loonie", "boc"],
    "AAPL": ["apple", "aapl", "iphone", "tim cook", "cupertino"],
    "TSLA": ["tesla", "tsla", "musk", "cybertruck", "model y"],
    "NVDA": ["nvidia", "nvda", "huang", "gpu", "ai chip"],
    "MSFT": ["microsoft", "msft", "nadella", "azure", "copilot"],
    "AMZN": ["amazon", "amzn", "bezos", "aws", "jassy"],
    "META": ["meta", "facebook", "zuckerberg", "instagram", "threads"],
    "GOOGL": ["google", "alphabet", "googl", "gemini", "pichai"],
    "NFLX": ["netflix", "nflx"],
}

# Per-source title cap to bound prompt size.
_PER_SOURCE_CAP = 3
_HTTP_TIMEOUT = 4.0  # seconds; keep the engine snappy
_CACHE_TTL_S = 300  # 5 min cache; news is slow-moving
_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _normalize_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip())


def _symbol_matches(symbol: str, title: str) -> bool:
    terms = SYMBOL_TERMS.get(symbol.upper(), [symbol.lower()])
    title_l = title.lower()
    return any(t in title_l for t in terms)


def _parse_rss(xml_bytes: bytes, now_ts: float) -> list[dict[str, Any]]:
    """Parse an RSS 2.0 feed into a list of headline dicts.

    Returns entries with: title, source (from feed/channel), age_min, link, id.
    Age is in minutes from `now_ts`. Skips items with no title.
    """
    out: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        logger.debug("RSS parse error: %s", exc)
        return out
    channel = root.find("channel")
    if channel is None:
        return out
    source_name = (channel.findtext("title") or "").strip() or "RSS"
    for item in channel.findall("item"):
        title = _normalize_title(item.findtext("title") or "")
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate") or ""
        # RFC 822 / RFC 1123 dates; we don't strictly need them, age is "best effort"
        age_min = 0
        if pub:
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(pub)
                if dt is not None:
                    age_min = max(0, int((now_ts - dt.timestamp()) / 60))
            except Exception:
                age_min = 0
        out.append(
            {
                "title": title,
                "source": source_name,
                "age_min": age_min,
                "link": link,
                "id": hashlib.md5((title + link).encode("utf-8")).hexdigest()[:12],
            }
        )
    return out


def _fetch_one(url: str) -> list[dict[str, Any]]:
    now_ts = time.time()
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": "fcc-trader/1.0"})
            if r.status_code != 200:
                logger.debug("RSS %s -> %d", url, r.status_code)
                return []
            return _parse_rss(r.content, now_ts)
    except Exception as exc:
        logger.debug("RSS fetch %s failed: %s", url, exc)
        return []


def fetch_news(symbol: str, hours: int = 4, max_items: int = 5) -> list[dict[str, Any]]:
    """Return recent headlines mentioning `symbol`, sorted by freshness.

    `hours` is a hint (we don't filter hard; sorted by age). `max_items` caps
    the list. Returns [] on any failure — never raises.
    """
    sym = symbol.upper()
    cache_key = f"{sym}:{hours}:{max_items}"
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_S:
        return cached[1]

    feeds = SYMBOL_FEEDS.get(sym, [])
    if not feeds:
        # Unknown symbol — fall back to Reuters general + filter by symbol terms.
        feeds = ["https://feeds.reuters.com/reuters/financialNews"]

    all_items: list[dict[str, Any]] = []
    for url in feeds:
        all_items.extend(
            it
            for it in _fetch_one(url)[:_PER_SOURCE_CAP]
            if _symbol_matches(sym, it["title"])
        )

    # Sort by freshness.
    all_items.sort(key=lambda x: x.get("age_min", 9999))
    out = all_items[:max_items]
    _CACHE[cache_key] = (time.time(), out)
    return out


if __name__ == "__main__":  # smoke test
    for s in ("EURUSD", "AAPL", "TSLA", "USDJPY"):
        print(f"--- {s} ---")
        for h in fetch_news(s, max_items=3):
            print(f"  [{h['age_min']:>4}m] {h['source']}: {h['title'][:80]}")
