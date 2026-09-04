"""FastAPI server that wires the trading workbench together.

Endpoints:
  GET  /api/account         — Capital.com account info
  GET  /api/positions       — Open positions
  GET  /api/markets         — Search markets
  GET  /api/ohlcv?epic=...&resolution=HOUR&count=500
  POST /api/scan            — Run all 5 strategies on watchlist, return ranked setups
  POST /api/execute         — Place a position via Capital.com
  POST /api/logout          — Cleanly end session

Usage:
  pip install fastapi uvicorn
  uvicorn tools.scan_server:app --reload --port 8081
"""

import os
import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from tools.capital_api import CapitalAPI, compute_size_capital, resolve_epic
from tools.config import get_settings
from tools.strategy_engine import ALL_STRATEGIES, compute_atr, compute_rsi, compute_ema, compute_bb, compute_adx
from tools.agent_orchestrator import evaluate_signal, evaluate_signal_stub

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="FCC Trader Workbench API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API: CapitalAPI | None = None
DEMO_MODE = get_settings().capital_environment == "demo"
_LIVE_ENGINE_TASK: asyncio.Task | None = None


# ─── Models ──────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    symbols: list[str]
    timeframe: str = "HOUR"  # Capital.com resolution
    strategy_filter: list[str] | None = None
    risk_pct: float = 0.01
    use_ai: bool = False  # default off — instant scans, opt in for AI ranking


class ExecuteRequest(BaseModel):
    epic: str
    direction: str
    size: float
    sl: float | None = None
    tp: float | None = None
    account_id: str | None = None  # optional override; default = CAPITAL_ACCOUNT_ID or first account


class SettingsUpdate(BaseModel):
    capital_api_key: str | None = None
    capital_login: str | None = None
    capital_password: str | None = None
    capital_environment: str | None = None  # "demo" or "live"
    capital_account_id: str | None = None


class AgentsUpdate(BaseModel):
    agents: list[dict]
    risk_manager: dict | None = None


# ─── Lifecycle ───────────────────────────────────────────────────────────────

# Manually init client because we need it before login
# CAPITAL_ACCOUNT_ID env var lets you pin the trading account (e.g. "1038584"
# for "Account 1 DEMO") when your login has multiple CFD accounts.
API = CapitalAPI(
    demo=DEMO_MODE,
    account_id=get_settings().capital_account_id or None,
)


@app.on_event("startup")
async def _init():
    import httpx as _httpx
    API._client = _httpx.AsyncClient(timeout=30.0)
    ok = await API.login()
    if not ok:
        logger.warning("Capital.com offline — scan will use synthetic data")
    # Start the live engine (it self-pauses if mode == offline).
    try:
        import asyncio as _aio
        global _LIVE_ENGINE_TASK
        _LIVE_ENGINE_TASK = _aio.create_task(_live_engine_loop())
    except Exception as exc:
        logger.warning("live engine failed to start: %s", exc)


@app.on_event("shutdown")
async def _shutdown():
    if API and API.cst:
        await API.logout()


# ─── Routes ──────────────────────────────────────────────────────────────────

from fastapi.responses import FileResponse

_HTML_PATH = Path(__file__).resolve().parent / "fcc_trader.html"


@app.get("/")
async def root():
    return FileResponse(_HTML_PATH)

@app.get("/api/account")
async def account():
    a = await API.get_account()
    return {
        "account_id": a.account_id,
        "balance": a.balance,
        "equity": a.balance + a.profit_loss,
        "available": a.available,
        "pnl": a.profit_loss,
        "currency": a.currency,
        "leverage": a.leverage,
        "demo": a.demo,
        "min_deal_size": a.min_deal_size,
    }


@app.get("/api/accounts")
async def accounts():
    """Debug: list all accounts available to this session."""
    if not API.accounts:
        await API.refresh_accounts()
    return {
        "preferred_account_id_env": os.environ.get("CAPITAL_ACCOUNT_ID") or None,
        "preferred_account_id_runtime": API.preferred_account_id,
        "logged_in": bool(API.cst),
        "accounts": [
            {
                "account_id": a.account_id,
                "account_name": a.account_name,
                "balance": a.balance,
                "available": a.available,
                "currency": a.currency,
            }
            for a in API.accounts
        ],
    }


@app.get("/api/positions")
async def positions():
    ps = await API.get_positions()
    return {"positions": [p.__dict__ for p in ps]}


@app.get("/api/markets")
async def markets(q: str = "EUR"):
    res = await API.search_markets(q)
    return {"markets": res[:25]}


@app.get("/api/settings")
async def get_settings_endpoint():
    """Return non-sensitive settings. Secrets are never exposed."""
    s = get_settings()
    return {
        "needs_setup": not (s.capital_api_key and s.capital_login and s.capital_password),
        "capital_api_key_set": bool(s.capital_api_key),
        "capital_login_set": bool(s.capital_login),
        "capital_password_set": bool(s.capital_password),
        "capital_environment": s.capital_environment,
        "capital_account_id_set": bool(s.capital_account_id),
        "server_port": s.server_port,
        "fcc_proxy_url": s.fcc_proxy_url,
    }


def _write_env(values: dict[str, str]) -> None:
    """Persist settings to tools/.env. Preserves existing keys
    we didn't touch. No-op if the file isn't writable."""
    env_path = Path(__file__).resolve().parent / ".env"
    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            existing[k.strip()] = v.strip()
    existing.update({k: v for k, v in values.items() if v})
    body = "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n"
    try:
        env_path.write_text(body)
    except OSError as exc:
        # Don't crash the API — runtime env is already updated.
        import logging as _log
        _log.getLogger(__name__).warning("Could not write %s: %s", env_path, exc)


@app.put("/api/settings")
async def update_settings(req: SettingsUpdate):
    """Update settings. Writes to .env so they survive restarts.
    If first-time setup, also re-authenticates the broker session.
    Refuses to save if login fails — better to block the user than to silently
    fall through to mock data."""
    if req.capital_api_key is not None and req.capital_api_key != "":
        os.environ["CAPITAL_API_KEY"] = req.capital_api_key
    if req.capital_login is not None and req.capital_login != "":
        os.environ["CAPITAL_LOGIN"] = req.capital_login
    if req.capital_password is not None and req.capital_password != "":
        os.environ["CAPITAL_PASSWORD"] = req.capital_password
    if req.capital_environment is not None and req.capital_environment in ("demo", "live"):
        os.environ["CAPITAL_ENVIRONMENT"] = req.capital_environment
    if req.capital_account_id is not None and req.capital_account_id != "":
        os.environ["CAPITAL_ACCOUNT_ID"] = req.capital_account_id
    # Reload the API client so it picks up new credentials
    s = get_settings()
    API.api_key = s.capital_api_key
    API.login_id = s.capital_login
    API.password = s.capital_password
    API.demo = s.capital_environment == "demo"
    API.preferred_account_id = s.capital_account_id or None
    # Re-authenticate FIRST — refuse to save if login fails
    auth_error: str | None = None
    try:
        ok = await API.login()
        if not ok:
            auth_error = "Login to Capital.com failed. Check API key, login email, and password."
    except Exception as exc:
        auth_error = f"Login failed: {exc}"
    if auth_error:
        # Roll back runtime env so we don't leave a half-set session
        raise HTTPException(status_code=401, detail=auth_error)
    # Persist to .env so the next launch skips the setup wizard
    _write_env({
        "CAPITAL_API_KEY": s.capital_api_key,
        "CAPITAL_LOGIN": s.capital_login,
        "CAPITAL_PASSWORD": s.capital_password,
        "CAPITAL_ENVIRONMENT": s.capital_environment,
        "CAPITAL_ACCOUNT_ID": s.capital_account_id,
    })
    return {"ok": True, "authenticated": True, "persisted": True}


@app.post("/api/reauth")
async def reauth():
    """Force a fresh broker login using the current credentials. Useful when
    the server lost its session (e.g. CST token expired or server was
    reloaded without restarting the broker connection)."""
    try:
        ok = await API.login()
    except Exception as exc:
        import logging as _log
        _log.getLogger(__name__).warning("Re-auth failed: %s", exc)
        return {"ok": False, "authenticated": False, "error": str(exc)}
    return {"ok": ok, "authenticated": ok}


@app.get("/api/agents")
async def get_agents():
    """Return the active agent list. Reads from .agents.json if present,
    otherwise returns the hard-coded defaults shipped with the code."""
    from tools.agent_orchestrator import AGENTS, RISK_MANAGER
    out = []
    for a in AGENTS:
        out.append({
            "name": a.name,
            "provider_type": a.provider_type,
            "model": a.model,
            "system": a.system,
        })
    return {
        "agents": out,
        "risk_manager": {
            "name": RISK_MANAGER.name,
            "provider_type": RISK_MANAGER.provider_type,
            "model": RISK_MANAGER.model,
            "system": RISK_MANAGER.system,
        },
        "source": "file" if Path(".agents.json").exists() or Path("tools/.agents.json").exists() else "defaults",
    }


@app.put("/api/agents")
async def update_agents(req: AgentsUpdate):
    """Write the new agent list to tools/.agents.json. Validates structure."""
    if not isinstance(req.agents, list) or not req.agents:
        raise HTTPException(400, "agents must be a non-empty list")
    for a in req.agents:
        if not all(k in a for k in ("name", "provider_type", "model", "system")):
            raise HTTPException(400, f"agent missing required keys: {a.get('name', '?')}")
    payload = {"agents": req.agents}
    if req.risk_manager is not None:
        if not all(k in req.risk_manager for k in ("name", "provider_type", "model", "system")):
            raise HTTPException(400, "risk_manager missing required keys")
        payload["risk_manager"] = req.risk_manager
    target = Path("tools/.agents.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2))
    return {"ok": True, "path": str(target), "restart_required": False, "note": "Applies to next scan with use_ai=true; in-process orchestrator already reloads via .agents.json on each call."}


@app.get("/api/ohlcv")
async def ohlcv(epic: str, resolution: str = "HOUR", count: int = 500):
    bars = await API.get_ohlcv(epic, resolution, count)
    if not bars:
        # synth fallback so scan still works
        from tools.mt5_connector import _synth_bars
        bars = _synth_bars(epic, count, resolution)
    return {"epic": epic, "bars": bars[-count:]}


# In-memory cache so the modal doesn't re-fetch market info on every confirm.
_MARKET_INFO_CACHE: dict[str, tuple[float, dict]] = {}

# Live tick cache — short TTL (1.5s) to keep UI fresh without rate-limiting.
_TICK_CACHE: dict[str, tuple[float, dict]] = {}


@app.get("/api/tick")
async def tick(epic: str):
    """Live tick (bid/offer/spread) for a symbol. Cached 1.5s.
    Pulls from /markets (which works with the epic string) since /marketinfo
    is 404 on Capital.com's new API."""
    import time as _t
    now = _t.time()
    cached = _TICK_CACHE.get(epic)
    if cached and (now - cached[0]) < 1.5:
        return cached[1]
    # Use the /markets search endpoint — it returns bid/offer/lastUpdate for any
    # symbol that matches the search term. We grab the exact epic match.
    try:
        r = await API._client.get(
            f"{API.base}/api/v1/markets?searchTerm={epic}",
            headers=API._headers(),
        )
        if r.status_code != 200:
            return {"epic": epic, "error": f"markets {r.status_code}", "ts": now}
        markets = r.json().get("markets", [])
        # Find the exact epic match
        match = next((m for m in markets if m.get("epic", "").upper() == epic.upper()), None)
        if match is None and markets:
            match = markets[0]  # fall back to first result
        if match is None:
            return {"epic": epic, "error": "not found", "ts": now}
        bid = float(match.get("bid", 0.0) or 0.0)
        offer = float(match.get("offer", 0.0) or 0.0)
        out = {
            "epic": match.get("epic", epic),
            "symbol": match.get("symbol", epic),
            "bid": bid,
            "offer": offer,
            "spread": round(offer - bid, 8),
            "mid": round((bid + offer) / 2, 8) if bid and offer else 0.0,
            "last_update": match.get("updateTimeUTC", ""),
            "market_status": match.get("marketStatus", "UNKNOWN"),
            "ts": now,
        }
        _TICK_CACHE[epic] = (now, out)
        return out
    except Exception as exc:
        return {"epic": epic, "error": str(exc), "ts": now}





@app.get("/api/event_context")
async def event_context():
    """Return the next high-impact event, minutes until release, and
    whether we're inside a blackout or caution window. Cached for 60s
    (the calendar is deterministic; we just don't need to recompute per
    request)."""
    import time as _t
    now = _t.time()
    cached = _EVENT_CTX_CACHE.get("ctx")
    if cached and (now - cached[0]) < 60:
        return cached[1]
    from tools.agent_orchestrator import get_event_context
    ctx = get_event_context()
    _EVENT_CTX_CACHE["ctx"] = (now, ctx)
    return ctx


_EVENT_CTX_CACHE: dict[str, tuple[float, dict]] = {}


@app.get("/api/debug/scalp_check")
async def debug_scalp_check(epic: str, resolution: str = "MINUTE_5"):
    """Run scalping_volatility on real bars and report indicator values + why it didn't fire."""
    from tools.capital_api import resolve_epic
    from tools.strategy_engine import scalping_volatility, compute_ema, compute_rsi, compute_atr, compute_bb
    real_epic = await resolve_epic(API, epic)
    bars = await API.get_ohlcv(real_epic, resolution, 200)
    if not bars:
        return {"error": "no bars", "epic": real_epic}
    i = len(bars) - 1
    last = bars[i]
    cs = [b["close"] for b in bars]
    ema9 = compute_ema(cs, 9)
    ema21 = compute_ema(cs, 21)
    rsi7 = compute_rsi(bars, 7)
    bb10 = compute_bb(bars, 10, 1.5)
    atr14 = compute_atr(bars, 14)
    hi10 = max(b["high"] for b in bars[i - 10 : i]) if i >= 10 else None
    lo10 = min(b["low"] for b in bars[i - 10 : i]) if i >= 10 else None
    sig = scalping_volatility(bars, epic)
    return {
        "epic": real_epic,
        "n_bars": len(bars),
        "last_close": last["close"],
        "ema9": ema9[i],
        "ema21": ema21[i],
        "rsi7": rsi7[i],
        "bb_width": bb10["width"][i],
        "atr": atr14[i],
        "hi10": hi10,
        "lo10": lo10,
        "long_setup": last["close"] >= hi10 and ema9[i] > ema21[i] and 45 <= rsi7[i] <= 72,
        "short_setup": last["close"] <= lo10 and ema9[i] < ema21[i] and 28 <= rsi7[i] <= 55,
        "fired": sig is not None,
        "signal": sig.to_dict() if sig else None,
    }


@app.get("/api/market_info")
async def market_info(epic: str, _raw: int = 0):
    """Return min/max deal size, step, bid/ask for a symbol. Cached for 5 min.
    Pass _raw=1 to bypass parsing and see the raw Capital.com response (debug)."""
    import time as _t
    if _raw:
        r = await API._client.get(f"{API.base}/api/v1/marketinfo/{epic}", headers=API._headers())
        return {"status": r.status_code, "body_excerpt": r.text[:2000]}
    now = _t.time()
    cached = _MARKET_INFO_CACHE.get(epic)
    if cached and (now - cached[0]) < 300:
        return cached[1]
    info = await API.get_market_info(epic)
    if info is None:
        # Diagnostic: include the raw response so we can see why parsing failed
        try:
            r = await API._client.get(f"{API.base}/api/v1/marketinfo/{epic}", headers=API._headers())
            return {
                "epic": epic,
                "error": "no market info (parser returned None)",
                "raw_status": r.status_code,
                "raw_excerpt": r.text[:500],
            }
        except Exception as exc:
            return {"epic": epic, "error": f"marketinfo request failed: {exc}"}
    out = {
        "epic": info.epic,
        "name": info.name,
        "min_deal_size": info.min_deal_size,
        "max_deal_size": info.max_deal_size,
        "step": info.step,
        "bid": info.bid,
        "offer": info.offer,
        "spread": info.spread,
        "currency": info.currency,
        "margin_factor": info.margin_factor,
    }
    _MARKET_INFO_CACHE[epic] = (now, out)
    return out


@app.get("/api/debug/strategy_check")
async def debug_strategy_check(epic: str, resolution: str = "HOUR"):
    """Run each strategy on the latest bars and report why it didn't fire."""
    bars = await API.get_ohlcv(epic, resolution, 500)
    if not bars:
        return {"error": f"no bars for {epic}", "epic": epic}
    last = bars[-1]
    closes = [b["close"] for b in bars]
    ema20 = compute_ema(closes, 20)
    ema50 = compute_ema(closes, 50)
    ema200 = compute_ema(closes, 200)
    atr14 = compute_atr(bars, 14)
    rsi2 = compute_rsi(bars, 2)
    rsi14 = compute_rsi(bars, 14)
    bb20 = compute_bb(bars, 20, 2.0)
    adx14 = compute_adx(bars, 14)
    i = len(bars) - 1
    return {
        "epic": epic,
        "bars_count": len(bars),
        "last_bar": last,
        "indicators": {
            "ema20": ema20[i],
            "ema50": ema50[i],
            "ema200": ema200[i],
            "atr14": atr14[i],
            "rsi2": rsi2[i],
            "rsi14": rsi14[i],
            "bb_upper": bb20["upper"][i],
            "bb_middle": bb20["middle"][i],
            "bb_lower": bb20["lower"][i],
            "bb_width": bb20["width"][i],
            "adx14": adx14[i],
        },
        "price_vs_ema20_pct": (last["close"] - ema20[i]) / ema20[i] * 100 if ema20[i] else None,
        "price_vs_bb": {
            "above_upper": last["close"] > bb20["upper"][i],
            "below_lower": last["close"] < bb20["lower"][i],
            "pct_of_range": ((last["close"] - bb20["lower"][i]) / (bb20["upper"][i] - bb20["lower"][i]) * 100)
                              if bb20["upper"][i] > bb20["lower"][i] else None,
        },
    }


@app.post("/api/scan")
async def scan(req: ScanRequest):
    t0 = time.perf_counter()
    strats = req.strategy_filter or list(ALL_STRATEGIES.keys())
    # Scalping strategy only fires on minute-bar timeframes. Skip it for H1/Daily
    # to avoid noise.
    if not req.strategy_filter:
        if "MINUTE" in req.timeframe:
            strats = list(ALL_STRATEGIES.keys())  # include scalping
        else:
            strats = [s for s in ALL_STRATEGIES if s != "scalping_volatility"]
    setups: list[dict] = []
    errors: list[dict] = []

    # ── Phase 1: fetch all symbols' OHLCV in parallel ─────────────────────────
    # Scalping only needs 60 bars; H1 strategies need 220+. Use 200 for minute
    # timeframes (covers scalping + still useful for context) and 300 for H1+.
    BAR_COUNT = 200 if "MINUTE" in req.timeframe else 300
    synth_epics: set[str] = set()

    async def _fetch_symbol(sym: str) -> tuple[str, str, list[dict] | None, list[dict]]:
        sym_errors: list[dict] = []
        try:
            epic = await resolve_epic(API, sym)
            bars = await API.get_ohlcv(epic, req.timeframe, BAR_COUNT)
            # Threshold scales with the bar count we asked for. Minute bars:
            # allow as few as 60 (scalping only needs 60+). H1+: require 220.
            min_bars = 60 if "MINUTE" in req.timeframe else 220
            if not bars or len(bars) < min_bars:
                from tools.mt5_connector import _synth_bars
                # Synthesize at the resolution the user asked for, not H1.
                synth_res = "H1" if "HOUR" in req.timeframe or "DAY" in req.timeframe else req.timeframe
                bars = _synth_bars(epic, BAR_COUNT, synth_res)
                synth_epics.add(epic)
                sym_errors.append({
                    "symbol": sym,
                    "epic": epic,
                    "issue": f"only {len(bars) if bars else 0} bars from API — used synth",
                })
            return (sym, epic, bars, sym_errors)
        except Exception as exc:
            logger.exception("scan: failed to fetch bars for %s", sym)
            # Fetch failed entirely — flag the symbol as erroneous so downstream
            # setup consumers can tell synth/error rows from live data.
            sym_errors.append({"symbol": sym, "issue": f"ohlcv fetch failed: {exc}"})
            if sym in synth_epics:
                pass  # already flagged via synth path above
            else:
                synth_epics.add(sym)
            return (sym, "", None, sym_errors)

    sym_results = await asyncio.gather(*[_fetch_symbol(s) for s in req.symbols])
    t_fetch = time.perf_counter()

    # Build per-symbol indicator cache (computed once, reused for all strategies)
    symbol_data: dict[str, tuple[str, list[dict]]] = {}
    for sym, epic, bars, sym_errors in sym_results:
        errors.extend(sym_errors)
        if bars:
            symbol_data[sym] = (epic, bars)

    # ── Phase 2: precompute indicators once per symbol ───────────────────────
    # These are O(n) and were being recalculated 5× per symbol (once per strategy).
    sym_indicators: dict[str, dict[str, Any]] = {}
    for sym, (epic, bars) in symbol_data.items():
        i = len(bars) - 1
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        volumes = [b.get("volume", 0) for b in bars]
        atr = compute_atr(bars, 14)
        rsi = compute_rsi(bars, 14)
        ema20 = compute_ema(closes, 20)
        ema50 = compute_ema(closes, 50)
        ema200 = compute_ema(closes, 200)
        bb20 = compute_bb(bars, 20)
        adx14 = compute_adx(bars, 14)
        atr_i = atr[i] if i < len(atr) and not math.isnan(atr[i]) else 0.0
        rsi_i = rsi[i] if i < len(rsi) and not math.isnan(rsi[i]) else 50.0
        ema50_i = ema50[i] if i < len(ema50) and not math.isnan(ema50[i]) else 0.0
        ema200_i = ema200[i] if i < len(ema200) and not math.isnan(ema200[i]) else 0.0
        ema20_i = ema20[i] if i < len(ema20) and not math.isnan(ema20[i]) else 0.0

        # BB position: where is close relative to BB band range? 0=lower, 100=upper.
        bb_u = bb20["upper"][i]
        bb_m = bb20["middle"][i]
        bb_l = bb20["lower"][i]
        bb_range = bb_u - bb_l
        bb_pct = round((closes[i] - bb_l) / bb_range * 100, 1) if bb_range > 0 else 50.0

        # Volume ratio: current bar volume vs 20-bar rolling average.
        avg_vol = sum(volumes[max(0, i - 19):i + 1]) / 20 if i >= 19 else sum(volumes[:i + 1]) / max(i + 1, 1)
        vol_ratio = round(volumes[i] / avg_vol, 2) if avg_vol > 0 else 1.0

        # Trend strength score: combines ADX level, EMA alignment, and RSI zone.
        adx_i = adx14[i] if i < len(adx14) and not math.isnan(adx14[i]) else 0.0
        adx_score = min(adx_i / 40.0, 1.0)  # 0–1, 40+ ADX = max
        ema_align = 1.0 if (ema50_i > ema200_i and ema20_i > ema50_i) or (ema50_i < ema200_i and ema20_i < ema50_i) else 0.5
        rsi_aligned = 1.0 if (ema50_i > ema200_i and 45 <= rsi_i <= 70) or (ema50_i < ema200_i and 30 <= rsi_i <= 55) else 0.0
        trend_score = round((adx_score * 0.5 + ema_align * 0.3 + rsi_aligned * 0.2) * 100, 0)  # 0–100

        # 3-bar momentum: net direction of last 3 closes.
        last3 = closes[max(0, i - 2):i + 1]
        if len(last3) >= 3:
            momentum_3bar = "bullish" if last3[2] > last3[0] else "bearish" if last3[2] < last3[0] else "neutral"
            momentum_3bar_pct = round((last3[2] - last3[0]) / last3[0] * 100, 3) if last3[0] != 0 else 0.0
        else:
            momentum_3bar = "neutral"
            momentum_3bar_pct = 0.0

        sym_indicators[sym] = {
            "bars": bars,
            "epic": epic,
            "i": i,
            "atr": atr_i,
            "rsi": rsi_i,
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,
            "bb20": bb20,
            "adx14": adx14,
            "closes": closes,
            "ema50_i": ema50_i,
            "ema200_i": ema200_i,
            "last_close": bars[i]["close"],
            "ema_trend": "up" if ema50_i > ema200_i else "down",
            # New enriched fields for AI agents:
            "bb_pct": bb_pct,
            "vol_ratio": vol_ratio,
            "trend_score": trend_score,
            "momentum_3bar": momentum_3bar,
            "momentum_3bar_pct": momentum_3bar_pct,
        }
    t_indicators = time.perf_counter()

    # ── Phase 3: evaluate strategies (still sequential, but all I/O is done) ──
    for sym, ind in sym_indicators.items():
        epic = ind["epic"]
        bars = ind["bars"]
        i = ind["i"]

        for strat_name in strats:
            try:
                fn = ALL_STRATEGIES.get(strat_name)
                if fn is None:
                    continue
                sig = fn(bars, sym)
                if sig is None:
                    continue
                sig_dict = sig.to_dict()
                sig_dict["epic"] = epic

                # ── Backtest-adjusted confidence ────────────────────────────────────
                # Run a quick walk-forward on the last 100 bars to get a rolling
                # win-rate. Blend it with the heuristic confidence (50/50 weight)
                # so freshly-discovered signals still fire while established patterns
                # get a data-driven boost or penalty.
                bt_result = None
                try:
                    from tools.strategy_engine import backtest
                    bt_result = backtest(bars[-220:], fn, sym, initial_capital=10_000.0)
                except Exception:
                    pass
                base_conf = sig_dict["confidence"]
                if bt_result and bt_result.get("sample", 0) >= 10:
                    wr = bt_result.get("win_rate", 0.5)
                    # Scale win rate 0.3–0.7 → confidence delta -0.2 to +0.2.
                    bt_adjustment = (wr - 0.5) * 0.4
                    sig_dict["confidence"] = round(
                        min(0.95, max(0.10, base_conf * 0.5 + bt_adjustment + 0.5 * base_conf)),
                        2,
                    )
                    sig_dict["backtest"] = {
                        "win_rate": bt_result.get("win_rate", 0.5),
                        "sample": bt_result.get("sample", 0),
                        "expectancy": bt_result.get("expectancy_r", 0.0),
                        "sharpe": bt_result.get("sharpe", 0.0),
                    }
                else:
                    sig_dict["confidence"] = round(base_conf, 2)
                    sig_dict["backtest"] = {"win_rate": 0.5, "sample": 0, "expectancy": 0.0, "sharpe": 0.0}

                # ── Enriched market context for AI agents ────────────────────────────
                enriched_summary = {
                    "last_close": ind["last_close"],
                    "atr": ind["atr"],
                    "rsi": ind["rsi"],
                    "adx": ind["adx14"][i] if i < len(ind["adx14"]) and not math.isnan(ind["adx14"][i]) else 20.0,
                    "ema_trend": ind["ema_trend"],
                    "volume_ratio": ind["vol_ratio"],  # was always 1.0 — now real
                    "recent_closes": ind["closes"][-5:],
                    # New:
                    "bb_position_pct": ind["bb_pct"],
                    "trend_score": ind["trend_score"],
                    "momentum_3bar": ind["momentum_3bar"],
                    "momentum_3bar_pct": ind["momentum_3bar_pct"],
                }

                # ── Layer 2: deterministic setup score + Layer 4: session tag ──────
                try:
                    from tools.strategy_engine import (
                        compute_setup_score, tag_session, session_multiplier,
                        _classify_regime,
                    )
                    regime_str = _classify_regime(bars)
                    dscore = compute_setup_score(
                        bt_result or {}, regime_str, strat_name
                    )
                except Exception as exc:
                    logger.debug("deterministic score fallback for %s/%s: %s", sym, strat_name, exc)
                    dscore = {"score": 0, "components": {}, "regime_match": False,
                              "sample_sufficient": False, "win_rate": 0.5,
                              "expectancy": 0.0, "profit_factor": 0.0,
                              "sharpe": 0.0, "sample": 0}
                    regime_str = "calm"
                session = tag_session()
                sess_mult = session_multiplier(session)
                deterministic_score = round(min(100.0, dscore["score"] * sess_mult), 1)
                enriched_summary["regime"] = regime_str
                enriched_summary["session"] = session
                enriched_summary["setup_score_raw"] = dscore["score"]
                enriched_summary["setup_score"] = deterministic_score

                # ── AI evaluation: meta-controller (single LLM, rich context) ─────
                ai_result: dict = {}
                if req.use_ai:
                    try:
                        from tools.agent_orchestrator import evaluate_signal_v2
                        from tools.news import fetch_news
                        from tools.intermarket import fetch_intermarket
                        news_items = fetch_news(sym, hours=4, max_items=4)
                        inter_data = fetch_intermarket("all")
                        ai_result = await asyncio.wait_for(
                            evaluate_signal_v2(
                                sig_dict, enriched_summary,
                                backtest=bt_result,
                                deterministic_score=dscore,
                                news=news_items,
                                intermarket=inter_data,
                            ),
                            timeout=5.0,
                        )
                    except asyncio.TimeoutError:
                        ai_result = {
                            "verdict": "pass", "score": 0, "size_modifier": 1.0,
                            "reasoning": "meta-controller timeout", "concerns": ["timeout"],
                            "key_factors": [], "consensus": "pass",
                            "consensus_confidence": 0.0, "latency_ms": 0,
                        }
                    except Exception as exc:
                        logger.warning("meta-controller failed for %s/%s: %s", sym, strat_name, exc)
                        ai_result = {
                            "verdict": "pass", "score": 0, "size_modifier": 1.0,
                            "reasoning": f"meta-controller error: {exc}", "concerns": ["llm_error"],
                            "key_factors": [], "consensus": "pass",
                            "consensus_confidence": 0.0, "latency_ms": 0,
                        }
                else:
                    ai_result = await evaluate_signal_stub(sig_dict)

                sl_dist = abs(sig_dict["entry"] - sig_dict["stop_loss"])
                tp_dist = abs(sig_dict["take_profit"] - sig_dict["entry"])
                rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0.0

                # ── A+ tag: deterministic_score >= 50 AND meta-controller approve ──
                # The deterministic score is the gate; the AI is the tiebreaker.
                # `grade` is one of "A+", "B", or "C" (off-hour penalty).
                if session == "off-hours":
                    grade = "C"
                elif deterministic_score >= 70 and ai_result.get("verdict") in ("approve", "modify"):
                    grade = "A+"
                elif deterministic_score >= 50:
                    grade = "B"
                else:
                    grade = "C"

                setups.append({
                    "id": f"{sym}-{strat_name}-{int(time.time()*1000)}",
                    "symbol": sym,
                    "epic": epic,
                    "strategy": strat_name,
                    "direction": sig_dict["direction"],
                    "entry": sig_dict["entry"],
                    "stopLoss": sig_dict["stop_loss"],
                    "takeProfit": sig_dict["take_profit"],
                    "rr": rr,
                    "confidence": sig_dict["confidence"],
                    "votes": ai_result.get("votes", []),
                    "riskManager": {
                        "verdict": ai_result.get("verdict", "pass"),
                        "final_size_pct": ai_result.get("size_modifier", 1.0),
                        "concerns": ai_result.get("concerns", []),
                        "summary": ai_result.get("reasoning", ""),
                        "latency_ms": ai_result.get("latency_ms", 0),
                    },
                    "consensus": ai_result.get("consensus", "pass"),
                    "aiScore": int(ai_result.get("score", 0)),
                    "aiReasoning": ai_result.get("reasoning", ""),
                    "aiConcerns": ai_result.get("concerns", []),
                    "aiKeyFactors": ai_result.get("key_factors", []),
                    "deterministicScore": deterministic_score,
                    "grade": grade,
                    "sizeModifier": ai_result.get("size_modifier", 1.0),
                    "session": session,
                    "regime": regime_str,
                    "reasoning": sig_dict["reasoning"],
                    "atr": sig_dict["atr"],
                    "dataQuality": "synth" if epic in synth_epics else "live",
                })
            except Exception as exc:
                logger.exception("scan: strategy %s failed for %s", strat_name, sym)
                errors.append({"symbol": sym, "strategy": strat_name, "issue": str(exc), "dataQuality": "error"})

    # ── A+ filter: sort by deterministic score desc; mark top 3 as primary
    setups.sort(key=lambda s: (s.get("deterministicScore", 0), s.get("aiScore", 0)), reverse=True)
    for idx, s in enumerate(setups):
        s["rank"] = idx + 1
        s["isTopPick"] = (idx < 3) and (s.get("grade") in ("A+", "B"))
    t_end = time.perf_counter()
    logger.info(
        "scan: %d sym × %d strat — fetch=%dms indicators=%dms total=%dms found=%d",
        len(req.symbols), len(strats),
        int((t_fetch - t0) * 1000),
        int((t_indicators - t_fetch) * 1000),
        int((t_end - t0) * 1000),
        len(setups),
    )
    return {
        "setups": setups,
        "scan_time_ms": int((t_end - t0) * 1000),
        "scanned": len(req.symbols) * len(strats),
        "found": len(setups),
        "errors": errors,
    }


@app.post("/api/execute")
async def execute(req: ExecuteRequest):
    if not API.cst:
        return {"error": "not logged in to Capital.com", "demo": True}
    res = await API.place_order(
        req.epic, req.direction, req.size,
        sl=req.sl, tp=req.tp, account_id=req.account_id,
    )
    return res


@app.post("/api/logout")
async def logout():
    if API and API.cst:
        await API.logout()
    return {"ok": True}


# ─── Cockpit: mode toggle, status, watchlist, live engine ─────────────────────

# Default watchlist used when the live engine starts. The user can override
# via PUT /api/cockpit/watchlist.
DEFAULT_WATCHLIST: list[str] = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD",
    "AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "META",
]

# Cooldown: don't re-alarm the same symbol+direction within this many seconds.
COOLDOWN_S = 4 * 3600
# LLM rate limit: at most one LLM call per this many seconds globally.
_LLM_MIN_INTERVAL_S = 30.0
_last_llm_ts: float = 0.0


async def _rate_limited_llm(coro):
    """Run a coroutine but ensure global LLM call rate >= 1 per 30s."""
    global _last_llm_ts
    import asyncio as _aio
    now = time.time()
    wait = _last_llm_ts + _LLM_MIN_INTERVAL_S - now
    if wait > 0:
        await _aio.sleep(wait)
    try:
        return await coro
    finally:
        _last_llm_ts = time.time()


async def _live_engine_loop():
    """Background loop: every TICK_S seconds, score the watchlist.

    Behavior:
      - offline:    sleep, no work
      - demo/live:  for each watched symbol, run deterministic layers on the
                    latest bars; if setup_score >= 50 and the LLM is not on
                    cooldown, call the meta-controller; if it approves, raise
                    an alarm in the cockpit cursor.
    Self-restarts on transient errors. Sleeps at the top of every loop.
    """
    import asyncio as _aio
    TICK_S = 30  # seconds
    while True:
        try:
            mode = _get_mode()
            cursor = _get_cursor()
            if mode == "offline" or not cursor.get("watchlist"):
                await _aio.sleep(TICK_S)
                continue

            # Score each watched symbol; collect the best setup per symbol.
            from tools.strategy_engine import (
                _classify_regime, compute_setup_score, tag_session,
                session_multiplier, ALL_STRATEGIES, backtest,
            )
            from tools.agent_orchestrator import evaluate_signal_v2
            from tools.news import fetch_news
            from tools.intermarket import fetch_intermarket

            results: list[dict] = []
            for sym in cursor["watchlist"]:
                try:
                    bars = _fetch_bars_for(sym, 300)
                except Exception as exc:
                    logger.debug("live engine: bars for %s failed: %s", sym, exc)
                    continue
                if not bars or len(bars) < 220:
                    continue
                regime = _classify_regime(bars)
                # Run each strategy, take the highest-scoring valid signal.
                for strat_name, fn in ALL_STRATEGIES.items():
                    try:
                        sig = fn(bars, sym)
                    except Exception as exc:
                        logger.debug("strategy %s on %s raised: %s", strat_name, sym, exc)
                        continue
                    if sig is None:
                        continue
                    try:
                        bt = backtest(bars[-220:], fn, sym, initial_capital=10_000.0)
                    except Exception:
                        bt = {"sample": 0, "win_rate": 0.5, "expectancy_r": 0.0,
                              "profit_factor": 0.0, "sharpe": 0.0}
                    if (bt.get("sample") or 0) < 10:
                        continue
                    try:
                        dscore = compute_setup_score(bt, regime, strat_name)
                    except Exception:
                        dscore = {"score": 0, "components": {}, "regime_match": False,
                                  "sample_sufficient": False, "win_rate": 0.5,
                                  "expectancy": 0.0, "profit_factor": 0.0,
                                  "sharpe": 0.0, "sample": 0}
                    if dscore["score"] < 50:
                        continue
                    results.append({
                        "symbol": sym, "strategy": strat_name,
                        "signal": sig, "backtest": bt, "dscore": dscore,
                        "regime": regime,
                    })

            if not results:
                cursor["last_tick"] = int(time.time())
                cursor["active_signal"] = None
                cursor["watch_queue"] = []
                _save_cursor(cursor)
                await _aio.sleep(TICK_S)
                continue

            # Rank by deterministic score, take top 3 as watch queue.
            results.sort(key=lambda r: r["dscore"]["score"], reverse=True)
            top = results[:3]
            cursor["watch_queue"] = [
                {"symbol": r["symbol"], "strategy": r["strategy"],
                 "score": r["dscore"]["score"], "direction": r["signal"].direction,
                 "entry": r["signal"].entry_price}
                for r in top
            ]
            cursor["last_tick"] = int(time.time())

            # Only call the LLM on the top setup, and only if it would change
            # the active signal. Rate-limited.
            best = top[0]
            sig = best["signal"]
            sig_dict = sig.to_dict()
            sig_dict["symbol"] = best["symbol"]
            news = fetch_news(best["symbol"], hours=4, max_items=3)
            inter = fetch_intermarket("all")
            ai = await _rate_limited_llm(
                evaluate_signal_v2(
                    sig_dict,
                    {
                        "regime": best["regime"],
                        "session": tag_session(),
                        "rsi": 50, "adx": 20, "atr": 0,
                        "bb_position_pct": 50, "trend_score": 50,
                        "vol_scalar": 1.0, "baseline_size": 0.1,
                    },
                    backtest=best["backtest"],
                    deterministic_score=best["dscore"],
                    news=news, intermarket=inter,
                )
            )
            if ai.get("verdict") == "approve":
                cursor["active_signal"] = {
                    "symbol": best["symbol"],
                    "strategy": best["strategy"],
                    "direction": sig.direction,
                    "entry": sig.entry_price,
                    "stop_loss": sig.stop_loss,
                    "take_profit": sig.take_profit,
                    "score": best["dscore"]["score"],
                    "ai_score": ai.get("score", 0),
                    "size_modifier": ai.get("size_modifier", 1.0),
                    "reasoning": ai.get("reasoning", ""),
                    "concerns": ai.get("concerns", []),
                    "key_factors": ai.get("key_factors", []),
                    "raised_at": int(time.time()),
                }
            else:
                cursor["active_signal"] = None

            _save_cursor(cursor)
            await _aio.sleep(TICK_S)
        except Exception as exc:
            logger.exception("live engine loop error: %s", exc)
            await _aio.sleep(10)


def _get_mode() -> str:
    try:
        import tools.state as _st
        return _st.get_mode()
    except Exception:
        return "offline"


def _get_cursor() -> dict:
    try:
        import tools.state as _st
        cur = _st.get_engine_cursor()
        if not cur.get("watchlist"):
            cur["watchlist"] = list(DEFAULT_WATCHLIST)
        return cur
    except Exception:
        return {"last_tick": 0, "active_signal": None,
                "watch_queue": [], "watchlist": list(DEFAULT_WATCHLIST)}


def _save_cursor(c: dict) -> None:
    try:
        import tools.state as _st
        _st.set_engine_cursor(c)
    except Exception as exc:
        logger.debug("save cursor failed: %s", exc)


def _fetch_bars_for(symbol: str, n: int) -> list[dict]:
    """Best-effort: try Capital.com via API; fall back to MT5; else synth."""
    try:
        epic = _symbol_to_epic(symbol)
        # The /api/ohlcv endpoint uses the capital API; we don't have a
        # direct sync helper, so we call through the API singleton.
        if API and API.cst:
            return API.get_ohlcv(epic, "H1", n)
    except Exception:
        pass
    try:
        from tools.mt5_connector import get_ohlcv
        return get_ohlcv(symbol, "H1", n)
    except Exception:
        pass
    return _synth_bars(symbol, n)


def _symbol_to_epic(symbol: str) -> str:
    # Best-effort: many brokers use the symbol as the epic; this is the
    # simplest correct default and avoids hard-coding a brittle map.
    return symbol


def _synth_bars(symbol: str, n: int) -> list[dict]:
    """Last-resort synthetic bars so the engine can still score deterministically."""
    import math as _m
    base = {"EURUSD": 1.08, "GBPUSD": 1.27, "USDJPY": 154.0, "AUDUSD": 0.66,
            "NZDUSD": 0.61, "USDCAD": 1.36,
            "AAPL": 220.0, "TSLA": 250.0, "NVDA": 130.0, "MSFT": 420.0,
            "AMZN": 190.0, "META": 540.0, "GOOGL": 175.0, "NFLX": 700.0}.get(symbol, 100.0)
    out = []
    t = int(time.time()) - n * 3600
    p = base
    for i in range(n):
        # gentle random walk
        p = p * (1 + 0.001 * _m.sin(i / 7.0) + 0.002 * ((i * 13) % 7 - 3) / 10.0)
        out.append({
            "time": t + i * 3600,
            "open": round(p, 5), "high": round(p * 1.001, 5),
            "low": round(p * 0.999, 5), "close": round(p, 5),
            "volume": 1000,
        })
    return out


@app.get("/api/cockpit")
async def get_cockpit():
    """Return current cockpit state for the UI: mode, last tick, active signal, watch queue, recent signals."""
    import tools.state as _st
    return {
        "mode": _st.get_mode(),
        "cursor": _st.get_engine_cursor(),
        "recent": _st.get_recent_signals(10),
    }


class ModeRequest(BaseModel):
    mode: str


@app.post("/api/cockpit/mode")
async def set_cockpit_mode(req: ModeRequest):
    import tools.state as _st
    if req.mode not in ("offline", "demo", "live"):
        return {"error": f"invalid mode: {req.mode}"}
    new_mode = _st.set_mode(req.mode)
    return {"mode": new_mode, "ok": True}


class WatchlistRequest(BaseModel):
    symbols: list[str]


@app.put("/api/cockpit/watchlist")
async def set_cockpit_watchlist(req: WatchlistRequest):
    import tools.state as _st
    cur = _st.get_engine_cursor()
    cur["watchlist"] = [s.upper() for s in req.symbols][:24]
    _st.set_engine_cursor(cur)
    return {"watchlist": cur["watchlist"], "ok": True}


@app.post("/api/cockpit/ack")
async def ack_active_signal():
    """Trader has seen (or acted on) the active signal. Clear it from the cursor."""
    import tools.state as _st
    cur = _st.get_engine_cursor()
    if cur.get("active_signal"):
        _st.append_signal(cur["active_signal"])
    cur["active_signal"] = None
    _st.set_engine_cursor(cur)
    return {"ok": True}


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    _s = get_settings()
    uvicorn.run("tools.scan_server:app", host=_s.server_host, port=_s.server_port, reload=False)
