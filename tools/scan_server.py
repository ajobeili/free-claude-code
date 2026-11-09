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

import asyncio
import csv
import hashlib
import io
import json
import logging
import math
import os
import secrets
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import aiohttp
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, SecretStr

from tools import agent_orchestrator, state
from tools.agent_orchestrator import (
    choose_trade_plan,
    evaluate_signal_stub,
)
from tools.capital_api import (
    BrokerSessionExpired,
    CapitalAPI,
    compute_size_capital,
    resolve_epic,
)
from tools.config import get_settings
from tools.pattern_evaluation_events import (
    MARKETS,
    VARIANTS,
    Bar,
    ControlEvidence,
    HistoryEvidence,
    OperatingDecision,
    QuoteEvidence,
)
from tools.pattern_evaluation_store import (
    DIAGNOSTIC_KINDS,
    KINDS,
    RECORD_COLUMNS,
    EvaluationRecorder,
    RetiredRevision,
)
from tools.pattern_events import DecisionRecord, GapRecord, QuoteRecord, ResearchRecord
from tools.pattern_journal import JournalRecorder
from tools.pattern_notifications import PatternNotifier, alert_plan
from tools.pattern_trigger import (
    EPICS,
    PatternAlert,
    PatternSettings,
    PatternTrigger,
    fetch_ranges,
    state_payload,
)
from tools.strategy_engine import (
    ALL_STRATEGIES,
    UNSCORED_SETUP,
    compute_adx,
    compute_atr,
    compute_bb,
    compute_ema,
    compute_rsi,
)
from tools.structure_assessment import Context, assess
from tools.structure_scan import Candle, Setup, analyze
from tools.trade_planner import (
    Choice,
    Plan,
    PlannerRequest,
    PlannerStore,
    advance,
    candidates_for,
    quote_from,
    validate_candidate,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await _init()
    try:
        yield
    finally:
        await _shutdown()


app = FastAPI(title="FCC Trader Workbench API", version="1.0", lifespan=_lifespan)

_LIVE_ENGINE_TASK: asyncio.Task | None = None
_CAPITAL_STREAM_TASK: asyncio.Task | None = None
_PATTERN_NOTIFY_TASK: asyncio.Task | None = None
_PATTERN_JOURNAL_TASK: asyncio.Task | None = None
_LIVE_TICK_EVENT: asyncio.Event | None = None
_STREAM_STATUS: dict[str, object] = {"connected": False, "last_quote": 0.0}


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
    size: float | None = None  # accepted for old clients; server computes safe size
    sl: float | None = None
    tp: float | None = None
    risk_pct: float = Field(default=0.005, gt=0, le=0.005)
    account_id: str | None = (
        None  # optional override; default = CAPITAL_ACCOUNT_ID or first account
    )


class SettingsUpdate(BaseModel):
    capital_environment: str | None = None


class LoginRequest(BaseModel):
    capital_api_key: str
    capital_login: str
    capital_password: str
    capital_environment: str = "demo"


class AgentsUpdate(BaseModel):
    agents: list[dict]
    risk_manager: dict | None = None


# ─── Lifecycle ───────────────────────────────────────────────────────────────

# Manually init client because we need it before login
# CAPITAL_ACCOUNT_ID env var lets you pin the trading account (e.g. "1038584"
# for "Account 1 DEMO") when your login has multiple CFD accounts.
API: CapitalAPI = CapitalAPI(
    demo=True,
    account_id=None,
)
_WEB_SESSION_TOKEN: str | None = None
_ACTIVE_MODULE = "home"
_MODULE_LOCK = asyncio.Lock()
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "fcc_session",
        token,
        httponly=True,
        samesite="strict",
        max_age=60 * 60 * 24 * 30,
    )


def _grant_restored_session(request: Request, response: Response) -> None:
    """Share a credential-restored session with same-machine browsers only.

    The server may bind to every interface; remote clients must log in."""
    client_host = request.client.host if request.client else ""
    if _WEB_SESSION_TOKEN and client_host in _LOOPBACK_HOSTS:
        _set_session_cookie(response, _WEB_SESSION_TOKEN)


@app.middleware("http")
async def require_web_login(request: Request, call_next):
    path = request.url.path
    if (
        path.startswith("/api/")
        and path != "/api/login"
        and (
            not _WEB_SESSION_TOKEN
            or request.cookies.get("fcc_session") != _WEB_SESSION_TOKEN
        )
    ):
        response = Response(
            content='{"detail":"Login required"}',
            status_code=401,
            media_type="application/json",
        )
        # A server restart rotates the in-memory token while the browser
        # still holds the previous cookie.
        _grant_restored_session(request, response)
        return response
    module = request.headers.get("X-FCC-Module")
    if (
        module
        and module != _ACTIVE_MODULE
        and request.url.path.startswith("/api/")
        and request.url.path
        not in ("/api/module", "/api/login", "/api/logout", "/api/session")
    ):
        return Response(
            content='{"detail":"Module paused. Return to this module to resume."}',
            status_code=409,
            media_type="application/json",
        )
    return await call_next(request)


async def _init():
    global _WEB_SESSION_TOKEN, _PATTERN_NOTIFY_TASK, _PATTERN_JOURNAL_TASK
    import httpx as _httpx

    # Keep broker traffic independent from any local proxy inherited by the
    # Codex/desktop host. See CapitalAPI.__aenter__ for the same invariant.
    API._client = _httpx.AsyncClient(timeout=30.0, trust_env=False)
    # Credentials are entered through the browser login. Successful logins are
    # persisted in tools/.env so the next server start can restore the session.
    API.preferred_account_id = None
    ok = await API.login()
    if ok:
        _WEB_SESSION_TOKEN = secrets.token_urlsafe(32)
        logger.info("Capital.com session restored from saved credentials")
    else:
        logger.info("Trader server ready; waiting for browser login")
    try:
        await _PATTERN_EVALUATION.start()
        _PATTERN_EVALUATION.control("server_start", False)
        _PATTERN_EVALUATION.control("operating_independent_v2")
    except OSError, ValueError:
        logger.warning("Research capture unavailable; operating scanner remains active")
    _PATTERN_NOTIFY_TASK = asyncio.create_task(_PATTERN_NOTIFIER.run())
    await _PATTERN_JOURNAL.start(
        {
            "rule_version": "independent-rejection-v2",
            "detector_sha256": hashlib.sha256(
                Path(__file__).with_name("pattern_trigger.py").read_bytes()
            ).hexdigest(),
            "settings": json.dumps(
                asdict(PatternSettings(independent=True)), sort_keys=True
            ),
            "markout_horizons_seconds": "60,180,300",
            "maximum_quote_gap_seconds": 5,
            "cost_basis": "bid/offer spread only; no commission, slippage or funding",
            "cohort": "observed baseline attempts; descriptive markouts, not independent strategy tests",
        }
    )
    _PATTERN_JOURNAL_TASK = asyncio.create_task(_PATTERN_JOURNAL.run())
    # Start the live engine (it self-pauses if mode == offline).
    try:
        import asyncio as _aio

        global _CAPITAL_STREAM_TASK, _LIVE_ENGINE_TASK, _LIVE_TICK_EVENT
        _LIVE_TICK_EVENT = _aio.Event()
        # Workbench background tasks start only when its page is active.
    except Exception as exc:
        logger.warning("live engine failed to start: %s", exc)


@app.post("/api/login")
async def login(req: LoginRequest, response: Response):
    global _WEB_SESSION_TOKEN
    if req.capital_environment != "demo":
        raise HTTPException(403, "Live trading is disabled; select the demo account")
    API.api_key = req.capital_api_key.strip()
    API.login_id = req.capital_login.strip()
    API.password = req.capital_password
    API.demo = True
    API.base = "https://demo-api-capital.backend-capital.com"
    API.preferred_account_id = None
    API.cst = None
    API.x_security = None
    if not await API.login():
        raise HTTPException(
            401,
            "Capital.com login failed. Check the API key, email, password, and environment.",
        )
    _WEB_SESSION_TOKEN = secrets.token_urlsafe(32)
    _write_env(
        {
            "CAPITAL_API_KEY": API.api_key,
            "CAPITAL_LOGIN": API.login_id,
            "CAPITAL_PASSWORD": API.password,
            "CAPITAL_ENVIRONMENT": "demo",
            "CAPITAL_ACCOUNT_ID": "",
        }
    )
    _set_session_cookie(response, _WEB_SESSION_TOKEN)
    account = API.primary_account()
    return {
        "ok": True,
        "account_id": account.account_id if account else None,
        "account_name": account.account_name if account else None,
    }


@app.get("/api/session")
async def session():
    account = API.primary_account()
    return {
        "authenticated": bool(API.cst and API.x_security),
        "account_id": account.account_id if account else None,
        "account_name": account.account_name if account else None,
    }


@app.post("/api/logout")
async def logout(response: Response):
    global _WEB_SESSION_TOKEN
    await API.logout()
    _WEB_SESSION_TOKEN = None
    response.delete_cookie("fcc_session")
    return {"ok": True}


async def _shutdown():
    _PATTERN_EVALUATION.control("server_shutdown", False)
    await _PATTERN_EVALUATION.close()
    tasks = [
        task
        for task in (_CAPITAL_STREAM_TASK, _LIVE_ENGINE_TASK, _PATTERN_NOTIFY_TASK)
        if task is not None
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if _PATTERN_JOURNAL_TASK is not None:
        _PATTERN_JOURNAL.enqueue(GapRecord(datetime.now(UTC), "server_shutdown"))
        try:
            async with asyncio.timeout(5):
                await _PATTERN_JOURNAL.drain()
        except TimeoutError:
            logger.warning(
                "Journal shutdown flush timed out; evidence may be incomplete"
            )
        _PATTERN_JOURNAL_TASK.cancel()
        await asyncio.gather(_PATTERN_JOURNAL_TASK, return_exceptions=True)
    if API.cst:
        await API.logout()


# ─── Routes ──────────────────────────────────────────────────────────────────


_HTML_PATH = Path(__file__).resolve().parent / "fcc_trader.html"


def _serve_trader_page(request: Request, path: Path):
    page = FileResponse(path, headers={"Cache-Control": "no-store"})
    _grant_restored_session(request, page)
    return page


async def _activate_module(module: str):
    global _ACTIVE_MODULE, _LIVE_ENGINE_TASK, _CAPITAL_STREAM_TASK
    async with _MODULE_LOCK:
        if module == _ACTIVE_MODULE:
            return
        _ACTIVE_MODULE = module
        tasks = [
            task
            for task in (_LIVE_ENGINE_TASK, _CAPITAL_STREAM_TASK)
            if task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        _LIVE_ENGINE_TASK = None
        _CAPITAL_STREAM_TASK = None
        _STREAM_STATUS.update(connected=False, subscribed=[])
        await _pattern_feed_status(False)
        if module == "workbench":
            _LIVE_ENGINE_TASK = asyncio.create_task(_live_engine_loop())
            _CAPITAL_STREAM_TASK = asyncio.create_task(_capital_stream_loop())
        elif module == "pattern":
            _CAPITAL_STREAM_TASK = asyncio.create_task(_capital_stream_loop())


@app.post("/api/module")
async def activate_module(module: str):
    if module not in ("home", "workbench", "structure", "planner", "pattern"):
        raise HTTPException(422, "Unknown module")
    await _activate_module(module)
    return {"module": _ACTIVE_MODULE}


@app.get("/module_activity.js")
async def module_script():
    return FileResponse(
        Path(__file__).with_name("module_activity.js"),
        media_type="text/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/")
async def root(request: Request):
    return _serve_trader_page(request, Path(__file__).with_name("trader_home.html"))


@app.get("/workbench")
async def workbench_page(request: Request):
    return _serve_trader_page(request, _HTML_PATH)


@app.get("/structure")
async def structure_page(request: Request):
    return _serve_trader_page(request, Path(__file__).with_name("structure_scan.html"))


@app.get("/api/structure")
async def structure_scan(symbol: str, timeframe: str = "HOUR"):
    durations = {
        "MINUTE": 60,
        "MINUTE_5": 300,
        "MINUTE_15": 900,
        "MINUTE_30": 1800,
        "HOUR": 3600,
        "HOUR_4": 14400,
        "DAY": 86400,
    }
    symbol = symbol.strip().upper()
    if (
        not symbol
        or len(symbol) > 40
        or not all(c.isalnum() or c in "._-" for c in symbol)
    ):
        raise HTTPException(422, "Enter one valid broker symbol")
    if timeframe not in durations:
        raise HTTPException(422, "Unsupported timeframe")
    try:
        epic = await resolve_epic(API, symbol)
        now = time.time()
        raw = await _cached_ohlcv(epic, timeframe, 300, now)
        bars = [
            Candle(
                float(b["time"]),
                float(b["open"]),
                float(b["high"]),
                float(b["low"]),
                float(b["close"]),
            )
            for b in raw
            if float(b["time"]) + durations[timeframe] <= now
        ]
        result = analyze(bars)
    except BrokerSessionExpired as exc:
        raise HTTPException(401, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.exception("Structure scan failed")
        raise HTTPException(
            502, "Broker candles unavailable; retry after logging in"
        ) from exc
    higher = {
        "MINUTE": ["MINUTE_5", "MINUTE_15"],
        "MINUTE_5": ["MINUTE_15", "HOUR"],
        "MINUTE_15": ["HOUR", "HOUR_4"],
        "MINUTE_30": ["HOUR", "HOUR_4"],
        "HOUR": ["HOUR_4", "DAY"],
        "HOUR_4": ["DAY"],
        "DAY": [],
    }

    async def context_for(resolution: str) -> Context:
        try:
            async with asyncio.timeout(10):
                candles = await _cached_ohlcv(epic, resolution, 300, now)
            completed = [
                Candle(
                    float(b["time"]),
                    float(b["open"]),
                    float(b["high"]),
                    float(b["low"]),
                    float(b["close"]),
                )
                for b in candles
                if float(b["time"]) + durations[resolution] <= now
            ]
            analysis = analyze(completed)
            return Context(
                resolution,
                analysis["bias"],
                now - completed[-1].time > durations[resolution] * 3,
            )
        except Exception:
            return Context(resolution, "unavailable", True)

    async def quote_for_scan() -> dict:
        try:
            async with asyncio.timeout(10):
                return await tick(epic)
        except Exception:
            return {}

    contexts = await asyncio.gather(*(context_for(tf) for tf in higher[timeframe]))
    quote = await quote_for_scan()
    forming = [
        Candle(
            float(b["time"]),
            float(b["open"]),
            float(b["high"]),
            float(b["low"]),
            float(b["close"]),
        )
        for b in raw
        if bars[-1].time < float(b["time"]) <= now
    ]
    candidate = Setup(**result["setup"]) if result["setup"] else None
    result["assessment"] = assess(
        bars,
        candidate,
        forming,
        contexts,
        quote.get("bid"),
        quote.get("offer"),
        quote.get("market_status", "UNKNOWN"),
        now - bars[-1].time > durations[timeframe] * 3,
        result["sell_break"]
        if candidate and candidate.direction == "buy"
        else result["buy_break"],
    )
    return {
        **result,
        "symbol": symbol,
        "epic": epic,
        "timeframe": timeframe,
        "as_of": bars[-1].time,
        "stale": now - bars[-1].time > durations[timeframe] * 3,
    }


_PLANNER_STORE = PlannerStore(Path(__file__).with_name(".planner.sqlite3"))
_PLANNER_LOCK = asyncio.Lock()

_CANDLE_CACHE: dict[tuple[str, str, int], tuple[float, list[dict]]] = {}
_CANDLE_CACHE_LOCK = asyncio.Lock()
_CANDLE_FULL_REFRESH_SECONDS = 300
_CANDLE_DELTA_SAFETY_SECONDS = 60


def _merge_ohlcv(cached: list[dict], updates: list[dict], count: int) -> list[dict]:
    by_time = {int(bar["time"]): bar for bar in cached}
    by_time.update({int(bar["time"]): bar for bar in updates})
    return sorted(by_time.values(), key=lambda bar: int(bar["time"]))[-count:]


async def _cached_ohlcv(
    epic: str, resolution: str, count: int, now: float | None = None
) -> list[dict]:
    """Reuse candles and refresh only the current/newest bars between full refreshes."""
    if now is None:
        now = time.time()
    key = (epic, resolution, count)
    async with _CANDLE_CACHE_LOCK:
        cached_entry = _CANDLE_CACHE.get(key)
        if (
            cached_entry is None
            or now - cached_entry[0] >= _CANDLE_FULL_REFRESH_SECONDS
        ):
            bars = await API.get_ohlcv(epic, resolution, count)
            if bars:
                _CANDLE_CACHE[key] = (now, bars)
            return bars

        cached = cached_entry[1]
        if not cached:
            return []
        delta_to = max(
            float(cached[-1]["time"]) + 1,
            now - _CANDLE_DELTA_SAFETY_SECONDS,
        )
        updates = await API.get_ohlcv(
            epic,
            resolution,
            3,
            from_time=float(cached[-1]["time"]),
            to_time=delta_to,
        )
        merged = _merge_ohlcv(cached, updates, count) if updates else cached
        _CANDLE_CACHE[key] = (cached_entry[0], merged)
        return merged


@app.get("/planner")
async def planner_page(request: Request):
    return _serve_trader_page(request, Path(__file__).with_name("trade_planner.html"))


class PlannerAIKey(BaseModel):
    api_key: SecretStr


class PlannerAIModel(BaseModel):
    provider: str = Field(default="openrouter", min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=200)


@app.post("/api/planner/ai-model")
async def save_planner_ai_model(req: PlannerAIModel):
    provider = req.provider.strip().lower()
    model = req.model.strip()
    if provider != "openrouter":
        raise HTTPException(
            422, "The planner currently supports the OpenRouter provider."
        )
    if any(char.isspace() for char in model):
        raise HTTPException(422, "Model IDs cannot contain spaces.")
    path = agent_orchestrator.PLANNER_AI_FILE
    try:
        path.write_text(json.dumps({"provider": provider, "model": model}) + "\n")
    except OSError as exc:
        raise HTTPException(500, "Could not save the planner model selection.") from exc
    return {"saved": True, "provider": provider, "model": model}


@app.get("/api/planner/ai-models")
async def planner_ai_models():
    from urllib.parse import urlsplit

    import httpx

    proxy = urlsplit(get_settings().fcc_proxy_url)
    if proxy.hostname not in ("localhost", "127.0.0.1", "::1") or proxy.scheme not in (
        "http",
        "https",
    ):
        raise HTTPException(
            400, "Available models can only be loaded from the local FCC AI server."
        )
    url = f"{proxy.scheme}://{proxy.netloc}/admin/api/models"
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.get(url)
        if response.status_code != 200:
            raise HTTPException(
                502, "FCC AI server did not return its available models."
            )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            502, "Could not load available models from FCC AI server."
        ) from exc
    raw_models = payload.get("models", [])
    if not isinstance(raw_models, list):
        raise HTTPException(502, "FCC AI server returned an invalid model list.")
    models = []
    for raw_model in raw_models:
        model = str(raw_model).strip()
        if model.startswith("open_router/"):
            model = model.removeprefix("open_router/")
        if model and model not in models:
            models.append(model)
    if not models:
        raise HTTPException(
            404, "No models are available for the configured AI provider."
        )
    return {"provider": "openrouter", "models": models}


@app.post("/api/planner/ai-key")
async def save_planner_ai_key(req: PlannerAIKey):
    from urllib.parse import urlsplit

    import httpx

    key = req.api_key.get_secret_value().strip()
    if not key or len(key) > 512 or any(c.isspace() for c in key):
        raise HTTPException(422, "Enter a nonempty API key without spaces.")
    proxy = urlsplit(get_settings().fcc_proxy_url)
    if proxy.hostname not in ("localhost", "127.0.0.1", "::1") or proxy.scheme not in (
        "http",
        "https",
    ):
        raise HTTPException(
            400, "Key editing is supported only for the local FCC AI server."
        )
    url = f"{proxy.scheme}://{proxy.netloc}/admin/api/config/apply"
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(
                url, json={"values": {"OPENROUTER_API_KEY": key}}
            )
        if response.status_code != 200:
            raise HTTPException(
                502,
                "AI server did not accept the settings request. Key save not confirmed.",
            )
        result = response.json()
        if not result.get("applied"):
            raise HTTPException(
                422,
                "AI server rejected the change. The key may be controlled by its startup environment.",
            )
        pending = bool(result.get("pending_fields"))
        return {
            "saved": True,
            "message": "API key saved. "
            + (
                "AI server restart is required; it may restart automatically. "
                if pending
                else ""
            )
            + "Use Check AI access to verify generation. Saving does not prove remaining quota.",
        }
    except httpx.HTTPError, ValueError:
        raise HTTPException(
            502,
            "Could not confirm the key save. Check that FCC AI Server is running before retrying.",
        ) from None


@app.get("/api/planner/ai-settings")
async def planner_ai_settings():
    cfg = agent_orchestrator._resolve_meta_config()
    return {
        "provider": cfg.provider_type,
        "model": cfg.model,
        "quota": "Quota and remaining credits are not exposed by the local proxy. Check the provider dashboard.",
    }


_AI_CHECK_LOCK = asyncio.Lock()


@app.post("/api/planner/ai-check")
async def planner_ai_check():
    if _AI_CHECK_LOCK.locked():
        raise HTTPException(409, "An AI check is already running.")
    async with _AI_CHECK_LOCK:
        started = time.monotonic()
        try:
            async with asyncio.timeout(15):
                result = await choose_trade_plan(
                    'Connection check only. No market data or candidates. Return candidate_id null, reasoning "Connection check", counterargument "No market data".'
                )
            error = str(result.get("error", ""))
            if not error:
                Choice.model_validate_json(result.get("text", ""))
                status, message = (
                    "available",
                    "AI responded successfully. Access works now; this does not guarantee remaining quota.",
                )
            elif "401" in error or "403" in error:
                status, message = (
                    "authentication_error",
                    "Authentication rejected. Check the API key or local proxy token in FCC AI settings.",
                )
            elif "429" in error:
                status, message = (
                    "rate_limited",
                    "Provider rate limit reached. Check usage and limits in the provider dashboard.",
                )
            elif "402" in error:
                status, message = (
                    "credits_required",
                    "Provider reports insufficient credits. Check the provider dashboard.",
                )
            elif error == "timeout":
                status, message = (
                    "timeout",
                    "AI response timed out. This does not prove the key is invalid or quota is exhausted.",
                )
            else:
                status, message = (
                    "unavailable",
                    "AI unavailable or blocked by model policy. Check FCC AI server logs.",
                )
        except TimeoutError:
            status, message = (
                "timeout",
                "No AI response within 15 seconds. The proxy may be retrying provider errors. Check FCC AI server logs and provider usage.",
            )
        except ValueError:
            status, message = (
                "invalid_response",
                "AI replied, but its response did not match the required format.",
            )
        return {
            "status": status,
            "message": message,
            "elapsed_seconds": round(time.monotonic() - started, 1),
        }


@app.get("/api/planner")
async def planner_state():
    plan = _PLANNER_STORE.latest()
    return {"plan": plan.model_dump() if plan else None, **_PLANNER_STORE.history()}


_PLANNER_DEADLINE_SECONDS = 60


@app.post("/api/planner")
async def create_plan(req: PlannerRequest):
    try:
        async with asyncio.timeout(_PLANNER_DEADLINE_SECONDS):
            return await _create_plan(req)
    except TimeoutError as exc:
        raise HTTPException(
            504,
            "Planning exceeded the 60-second total limit; no plan created. The AI provider may be rate limited or unavailable.",
        ) from exc


async def _create_plan(req: PlannerRequest):
    async with _PLANNER_LOCK:
        prior = _PLANNER_STORE.latest()
        if prior and prior.status in ("pending", "open", "review"):
            raise HTTPException(
                409, "Finish or cancel the existing simulated plan first."
            )
        snapshot = await structure_scan(req.symbol, req.timeframe)
        try:
            quote = quote_from(await tick(snapshot["epic"]), time.time())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        candidates, rejected = await asyncio.to_thread(
            candidates_for, snapshot, req, quote
        )
        prompt = json.dumps(
            {
                "symbol": req.symbol,
                "timeframe": req.timeframe,
                "quote": quote.model_dump(),
                "candidates": [c.model_dump() for c in candidates],
                "rejected": rejected,
                "context": snapshot.get("assessment", {}).get("contexts", []),
                "limitations": "No news calendar. No measured strategy edge. Paper simulation only.",
            }
        )
        try:
            if candidates:
                async with asyncio.timeout(65):
                    answer = await choose_trade_plan(prompt)
                if answer.get("error") == "timeout":
                    raise TimeoutError("AI response exceeded its time limit")
                if answer.get("error"):
                    raise ValueError(
                        "AI unavailable. Check the configured FCC AI proxy and model."
                    )
                choice = Choice.model_validate_json(answer.get("text", ""))
            else:
                choice = Choice(
                    candidate_id=None,
                    reasoning="Rule-based WAIT: no candidate passed the market and risk checks. "
                    + (
                        " ".join(rejected)
                        if rejected
                        else "No qualifying retracement setup was found."
                    ),
                    counterargument="No AI opinion requested. A fresh scan may find a qualifying setup when conditions change.",
                )
            candidate = next(
                (c for c in candidates if c.id == choice.candidate_id), None
            )
            if choice.candidate_id is not None and candidate is None:
                raise ValueError("AI returned an unknown candidate. No plan accepted.")
            # Recheck the quote after the AI wait; never act on the older prompt quote.
            quote = quote_from(await tick(snapshot["epic"]), time.time())
            if candidate:
                error = validate_candidate(
                    candidate,
                    req,
                    quote,
                    snapshot.get("assessment", {}).get("contexts", []),
                )
                if error:
                    raise ValueError("Market changed during analysis: " + error)
        except TimeoutError as exc:
            raise HTTPException(
                504,
                "AI service did not respond within its time limit; no plan created. Check FCC AI server logs for provider rate limits or connection errors before retrying.",
            ) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if candidate:
            refreshed = await structure_scan(req.symbol, req.timeframe)
            try:
                quote = quote_from(await tick(refreshed["epic"]), time.time())
                latest_candidates, _ = await asyncio.to_thread(
                    candidates_for, refreshed, req, quote
                )
                if not any(
                    c.model_dump() == candidate.model_dump() for c in latest_candidates
                ):
                    raise ValueError(
                        "Candidate changed during AI analysis. No plan accepted; run again."
                    )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            snapshot = refreshed
        from tools.trade_planner import build_scenarios

        now = time.time()
        plan = Plan(
            created=now,
            expires=now + req.expires_minutes * 60,
            request=req,
            candidate=candidate,
            reasoning=choice.reasoning,
            counterargument=choice.counterargument,
            status="pending" if candidate else "wait",
            ai_used=bool(candidates),
            risk_budget=req.balance * req.risk_pct / 100,
            last_observed=now,
            last_quote_at=quote.at,
            chart_bars=snapshot["bars"],
            scenarios=build_scenarios(snapshot["bars"]),
            alternatives=candidates,
            rejection_reasons=rejected,
            note="Normalized paper units in quote currency; not broker lots. Quotes observed every 5 seconds while this page is open. No commissions; fills may be missed between polls.",
        )
        if candidate:
            plan.quantity = plan.risk_budget / (
                abs(candidate.entry - candidate.stop) + (quote.ask - quote.bid)
            )
        _PLANNER_STORE.save(
            plan,
            ("Original AI decision: " if plan.ai_used else "Original rule decision: ")
            + (
                "WAIT"
                if not candidate
                else f"{candidate.direction.upper()} · {candidate.strategy} · entry {candidate.entry:g} · stop {candidate.stop:g} · TP {candidate.target:g}"
            )
            + " — "
            + plan.reasoning
            + " Counterargument: "
            + plan.counterargument,
        )
        return {
            "plan": plan.model_dump(),
            "quote": quote.model_dump(),
            "bars": snapshot["bars"],
            **_PLANNER_STORE.history(),
        }


@app.post("/api/planner/observe")
async def observe_plan():
    async with _PLANNER_LOCK:
        plan = _PLANNER_STORE.latest()
        if not plan or plan.status not in ("pending", "open"):
            return await planner_state()
        if plan.status == "pending" and time.time() >= plan.expires:
            plan.status = "expired"
            _PLANNER_STORE.save(plan, "Plan expired without an observed entry.")
            return await planner_state()
        try:
            epic = await resolve_epic(API, plan.request.symbol)
            quote = quote_from(await tick(epic), time.time())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        message = advance(plan, quote, time.time())
        _PLANNER_STORE.save(plan, message or "")
        return {
            "plan": plan.model_dump(),
            "quote": quote.model_dump(),
            **_PLANNER_STORE.history(),
        }


@app.post("/api/planner/finish")
async def finish_plan():
    async with _PLANNER_LOCK:
        plan = _PLANNER_STORE.latest()
        if not plan:
            raise HTTPException(404, "No simulated plan.")
        if plan.status == "pending":
            plan.status = "cancelled"
            _PLANNER_STORE.save(plan, "User cancelled the pending simulation.")
        elif plan.status in ("open", "review") and plan.candidate:
            try:
                epic = await resolve_epic(API, plan.request.symbol)
                quote = quote_from(await tick(epic), time.time())
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            plan.exit_fill = (
                quote.bid if plan.candidate.direction == "buy" else quote.ask
            )
            assert plan.entry_fill is not None
            plan.pnl = (
                (plan.exit_fill - plan.entry_fill)
                * plan.quantity
                * (1 if plan.candidate.direction == "buy" else -1)
            )
            plan.status = "closed"
            _PLANNER_STORE.save(
                plan,
                "User closed the simulation at the observed quote; no broker order.",
            )
        return await planner_state()


@app.get("/api/account")
async def account():
    a = await API.get_account()
    if a is None:
        raise HTTPException(
            503, "Demo account unavailable. Log in to Capital.com demo and retry."
        )
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
                "preferred": a.preferred,
                "status": a.status,
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
    account = API.primary_account()
    return {
        "authenticated": bool(API.cst and API.x_security),
        "capital_environment": "demo",
        "preferred_account_id": account.account_id if account else None,
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
    for key, value in values.items():
        if value:
            existing[key] = value
        else:
            existing.pop(key, None)
    body = "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n"
    try:
        env_path.write_text(body)
    except OSError as exc:
        # Don't crash the API — runtime env is already updated.
        import logging as _log

        _log.getLogger(__name__).warning("Could not write %s: %s", env_path, exc)


@app.put("/api/settings")
async def update_settings(req: SettingsUpdate):
    """Update non-secret connection preferences only."""
    if req.capital_environment not in (None, "demo"):
        raise HTTPException(403, "Live trading is disabled; demo is the only mode")
    os.environ["CAPITAL_ENVIRONMENT"] = "demo"
    API.demo = True
    API.base = "https://demo-api-capital.backend-capital.com"
    return {"ok": True, "authenticated": bool(API.cst and API.x_security)}


@app.post("/api/reauth")
async def reauth(response: Response):
    """Force a fresh broker login using the current credentials. Useful when
    the server lost its session (e.g. CST token expired or server was
    reloaded without restarting the broker connection)."""
    global _WEB_SESSION_TOKEN
    try:
        ok = await API.login()
    except Exception as exc:
        import logging as _log

        _log.getLogger(__name__).warning("Re-auth failed: %s", exc)
        return {"ok": False, "authenticated": False, "error": str(exc)}
    if ok:
        _WEB_SESSION_TOKEN = secrets.token_urlsafe(32)
        _set_session_cookie(response, _WEB_SESSION_TOKEN)
    return {"ok": ok, "authenticated": ok}


@app.get("/api/agents")
async def get_agents():
    """Return the active agent list. Reads from .agents.json if present,
    otherwise returns the hard-coded defaults shipped with the code."""
    from tools.agent_orchestrator import AGENTS, RISK_MANAGER

    out = [
        {
            "name": a.name,
            "provider_type": a.provider_type,
            "model": a.model,
            "system": a.system,
        }
        for a in AGENTS
    ]
    return {
        "agents": out,
        "risk_manager": {
            "name": RISK_MANAGER.name,
            "provider_type": RISK_MANAGER.provider_type,
            "model": RISK_MANAGER.model,
            "system": RISK_MANAGER.system,
        },
        "source": "file"
        if Path(".agents.json").exists() or Path("tools/.agents.json").exists()
        else "defaults",
    }


@app.put("/api/agents")
async def update_agents(req: AgentsUpdate):
    """Write the new agent list to tools/.agents.json. Validates structure."""
    if not isinstance(req.agents, list) or not req.agents:
        raise HTTPException(400, "agents must be a non-empty list")
    for a in req.agents:
        if not all(k in a for k in ("name", "provider_type", "model", "system")):
            raise HTTPException(
                400, f"agent missing required keys: {a.get('name', '?')}"
            )
    payload = {"agents": req.agents}
    if req.risk_manager is not None:
        if not all(
            k in req.risk_manager for k in ("name", "provider_type", "model", "system")
        ):
            raise HTTPException(400, "risk_manager missing required keys")
        payload["risk_manager"] = req.risk_manager
    target = Path("tools/.agents.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2))
    return {
        "ok": True,
        "path": str(target),
        "restart_required": False,
        "note": "Applies to next scan with use_ai=true; in-process orchestrator already reloads via .agents.json on each call.",
    }


@app.get("/api/ohlcv")
async def ohlcv(epic: str, resolution: str = "HOUR", count: int = 500):
    bars = await API.get_ohlcv(epic, resolution, count)
    if not bars:
        raise HTTPException(502, f"Broker returned no {resolution} bars for {epic}.")
    return {"epic": epic, "bars": bars[-count:]}


# In-memory cache so the modal doesn't re-fetch market info on every confirm.
_MARKET_INFO_CACHE: dict[str, tuple[float, dict]] = {}

# Live tick cache — short TTL (1.5s) to keep UI fresh without rate-limiting.
_TICK_CACHE: dict[str, tuple[float, dict]] = {}

_CAPITAL_STREAM_URL = "wss://api-streaming-capital.backend-capital.com/connect"


async def _pattern_ranges(as_of: datetime, epics: tuple[str, ...] = EPICS):
    _PATTERN_EVALUATION.control("history_request")
    request_sequence = _PATTERN_EVALUATION.sequence()
    request_id = secrets.token_hex(12)

    def observed(epic: str, bars: tuple[Bar, ...], complete: bool) -> None:
        if epic in ("GOLD", "OIL_CRUDE"):
            _PATTERN_EVALUATION.enqueue(
                "history",
                HistoryEvidence(
                    request_id=request_id,
                    as_of=as_of,
                    bars=bars,
                    complete=complete,
                    request_sequence=request_sequence,
                ),
                epic=epic,
                source_time=as_of,
            )

    try:
        await API.ensure_session()
        return await fetch_ranges(API, as_of, observed, epics)
    except Exception:
        _PATTERN_EVALUATION.enqueue(
            "control",
            ControlEvidence(reason="history_failed"),
            epic=epics[0]
            if len(epics) == 1 and epics[0] in ("GOLD", "OIL_CRUDE")
            else None,
        )
        raise


async def _pattern_market_ranges(epic: str, as_of: datetime):
    return await _pattern_ranges(as_of, (epic,))


_notification_settings = get_settings()
_PATTERN_NOTIFIER = PatternNotifier(
    _notification_settings.pattern_telegram_enabled,
    shutil.which(_notification_settings.pattern_hermes_executable) or "",
    _notification_settings.pattern_telegram_target,
    socket_path=_notification_settings.pattern_hermes_socket,
)


def _queue_pattern_alert(alert: PatternAlert) -> None:
    _PATTERN_NOTIFIER.enqueue(alert)


_PATTERN_JOURNAL = JournalRecorder(Path(__file__).with_name(".pattern_journal.sqlite3"))
_PATTERN_EVALUATION = EvaluationRecorder(Path(get_settings().pattern_evaluation_store))
_PATTERN_CALIBRATION: str | None = None


async def _pattern_feed_status(connected: bool) -> None:
    await _PATTERN_TRIGGER.feed_status(connected)


def _capture_pattern_feed(connected: bool) -> None:
    _PATTERN_EVALUATION.control(
        "feed_connected" if connected else "feed_disconnected", connected
    )


def _record_pattern_event(event: ResearchRecord) -> None:
    _PATTERN_JOURNAL.enqueue(event)
    if isinstance(event, QuoteRecord) and event.epic in ("GOLD", "OIL_CRUDE"):
        _PATTERN_EVALUATION.enqueue(
            "quote",
            QuoteEvidence(bid=event.bid, offer=event.offer, quality=event.quality),
            epic=event.epic,
            source_time=event.time,
            received=event.received,
            calibration_id=_PATTERN_CALIBRATION,
        )
    elif isinstance(event, DecisionRecord) and event.epic in ("GOLD", "OIL_CRUDE"):
        _PATTERN_EVALUATION.enqueue(
            "decision",
            OperatingDecision(started=event.started, outcome=event.outcome),
            epic=event.epic,
            source_time=event.time,
        )
    elif isinstance(event, GapRecord):
        _PATTERN_EVALUATION.control(
            event.reason, False if event.reason == "feed_disconnected" else None
        )


_PATTERN_TRIGGER = PatternTrigger(
    _pattern_ranges,
    settings=PatternSettings(independent=True),
    market_history=_pattern_market_ranges,
    on_alert=_queue_pattern_alert,
    on_record=_record_pattern_event,
    on_feed=_capture_pattern_feed,
)


@app.get("/pattern")
async def pattern_page(request: Request):
    return _serve_trader_page(request, Path(__file__).with_name("pattern_scan.html"))


@app.get("/api/pattern/events")
async def pattern_events():
    async def events():
        trigger_updates = _PATTERN_TRIGGER.events()
        research_updates = _PATTERN_EVALUATION.updates()
        trigger_task = asyncio.ensure_future(anext(trigger_updates))
        research_task = asyncio.ensure_future(anext(research_updates))
        try:
            while True:
                done, _ = await asyncio.wait(
                    (trigger_task, research_task), return_when=asyncio.FIRST_COMPLETED
                )
                if trigger_task in done:
                    trigger_task.result()
                    trigger_task = asyncio.ensure_future(anext(trigger_updates))
                if research_task in done:
                    research_task.result()
                    research_task = asyncio.ensure_future(anext(research_updates))
                current = _PATTERN_TRIGGER.snapshot()
                payload = state_payload(current)
                payload["alerts"] = [
                    {**asdict(alert), "plan": alert_plan(alert)}
                    for alert in current.alerts
                ]
                payload["authenticated"] = bool(API.cst and API.x_security)
                payload["notifications"] = _PATTERN_NOTIFIER.snapshot()
                payload["journal"] = _PATTERN_JOURNAL.snapshot()
                payload["evaluation_capture"] = _PATTERN_EVALUATION.snapshot()
                payload["evaluation_diagnostics"] = _PATTERN_EVALUATION.diagnostics()
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            trigger_task.cancel()
            research_task.cancel()
            await asyncio.gather(trigger_task, research_task, return_exceptions=True)
            await trigger_updates.aclose()
            await research_updates.aclose()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/pattern/journal")
async def pattern_journal_status():
    return _PATTERN_JOURNAL.snapshot()


@app.get("/api/pattern/journal/{kind}.csv")
async def pattern_journal_export(kind: str):
    if kind not in ("quotes", "attempts", "outcomes", "runs", "ranges", "gaps"):
        raise HTTPException(422, "Unknown journal export")
    return StreamingResponse(
        _PATTERN_JOURNAL.export(kind),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="pattern-{kind}.csv"'},
    )


@app.get("/api/pattern/journal/{kind}.json")
async def pattern_journal_records(kind: str, offset: int = 0):
    try:
        return await _PATTERN_JOURNAL.records(kind, offset)
    except ValueError:
        raise HTTPException(422, "Unknown journal list or invalid page") from None


@app.middleware("http")
async def evaluation_private_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/pattern/evaluation"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/pattern/evaluation")
async def pattern_evaluation_summary(response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        report = await asyncio.to_thread(_PATTERN_EVALUATION.store.latest)
    except OSError, sqlite3.Error:
        raise HTTPException(
            503, "Research storage unavailable. Restart the server and try again."
        ) from None
    return {
        "schema_version": 1,
        "diagnostics": _PATTERN_EVALUATION.diagnostics(),
        "report": report,
        "report_status": "completed" if report else "unavailable",
    }


def _evaluation_selection(
    request: Request,
    report_id: str | None,
    diagnostic_revision: int | None,
    kind: str,
    market: str | None,
    variant: str | None,
    phase: str | None,
) -> dict[str, str]:
    allowed = {
        "report_id",
        "diagnostic_revision",
        "kind",
        "market",
        "variant",
        "phase",
        "offset",
        "limit",
    }
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(key)) != 1 for key in request.query_params
    ):
        raise HTTPException(422, "Unknown or repeated research selection")
    if (report_id is None) == (diagnostic_revision is None):
        raise HTTPException(422, "Choose exactly one report or diagnostic revision")
    if report_id is not None and (
        len(report_id) != 64
        or any(char not in "0123456789abcdef" for char in report_id)
    ):
        raise HTTPException(422, "Invalid report identifier")
    if diagnostic_revision is not None and (
        diagnostic_revision < 0 or kind not in DIAGNOSTIC_KINDS
    ):
        raise HTTPException(422, "Invalid diagnostic list selection")
    if (
        kind not in KINDS
        or (market is not None and market not in MARKETS)
        or (variant is not None and variant not in VARIANTS)
        or (phase is not None and phase not in ("development", "validation"))
    ):
        raise HTTPException(422, "Unknown research list or filter")
    return {
        key: value
        for key, value in (("market", market), ("variant", variant), ("phase", phase))
        if value is not None
    }


async def _evaluation_records(
    report_id: str | None,
    diagnostic_revision: int | None,
    kind: str,
    filters: dict[str, str],
    offset: int,
    limit: int,
):
    try:
        if diagnostic_revision is not None:
            return _PATTERN_EVALUATION.diagnostic_records(
                diagnostic_revision, kind, filters, offset, limit
            )
        if report_id is None:
            raise HTTPException(422, "Report selection required")
        return await asyncio.to_thread(
            _PATTERN_EVALUATION.store.records, report_id, kind, filters, offset, limit
        )
    except RetiredRevision:
        raise HTTPException(
            409, "These diagnostics have changed. Refresh the summary and list."
        ) from None
    except KeyError:
        raise HTTPException(404, "Completed research report not found") from None
    except ValueError:
        raise HTTPException(422, "Invalid research list selection") from None
    except OSError, sqlite3.Error:
        raise HTTPException(
            503, "Research storage unavailable. Restart the server and try again."
        ) from None


@app.get("/api/pattern/evaluation/records")
async def pattern_evaluation_records(
    request: Request,
    response: Response,
    kind: str,
    report_id: str | None = None,
    diagnostic_revision: int | None = None,
    market: str | None = None,
    variant: str | None = None,
    phase: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
):
    filters = _evaluation_selection(
        request, report_id, diagnostic_revision, kind, market, variant, phase
    )
    response.headers["Cache-Control"] = "no-store"
    return await _evaluation_records(
        report_id, diagnostic_revision, kind, filters, offset, limit
    )


@app.get("/api/pattern/evaluation/records.csv")
async def pattern_evaluation_export(
    request: Request,
    kind: str,
    report_id: str | None = None,
    diagnostic_revision: int | None = None,
    market: str | None = None,
    variant: str | None = None,
    phase: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
):
    filters = _evaluation_selection(
        request, report_id, diagnostic_revision, kind, market, variant, phase
    )
    pinned = None
    if diagnostic_revision is not None:
        try:
            pinned = _PATTERN_EVALUATION.diagnostic_export(
                diagnostic_revision, kind, filters
            )
        except RetiredRevision:
            raise HTTPException(
                409, "These diagnostics have changed. Refresh the summary and list."
            ) from None
        except ValueError:
            raise HTTPException(422, "Invalid research export selection") from None
        first = None
    else:
        first = await _evaluation_records(
            report_id, diagnostic_revision, kind, filters, 0, 100
        )
    columns = sorted(RECORD_COLUMNS[kind])

    async def chunks():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(columns)
        yield output.getvalue()
        if pinned is not None:
            for start in range(0, len(pinned), 100):
                output.seek(0)
                output.truncate()
                for row in pinned[start : start + 100]:
                    writer.writerow([row.get(key) for key in columns])
                yield output.getvalue()
            return
        if first is None:
            return
        result = first
        page_offset = 0
        while True:
            output.seek(0)
            output.truncate()
            page_columns = result.get("columns")
            rows = result.get("rows")
            if isinstance(page_columns, list) and isinstance(rows, list):
                positions = {
                    key: index
                    for index, key in enumerate(page_columns)
                    if isinstance(key, str)
                }
                for values in rows:
                    if isinstance(values, list):
                        writer.writerow(
                            [
                                values[positions[key]] if key in positions else None
                                for key in columns
                            ]
                        )
            yield output.getvalue()
            if not result.get("has_more"):
                break
            page_offset += 100
            result = await _evaluation_records(
                report_id, diagnostic_revision, kind, filters, page_offset, 100
            )

    return StreamingResponse(
        chunks(),
        media_type="text/csv",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="pattern-evaluation-{kind}.csv"',
        },
    )


async def _pattern_quote(
    epic: str, raw_time: object, received: float, bid: float, offer: float
):
    if _ACTIVE_MODULE != "pattern":
        return
    try:
        if isinstance(raw_time, (float, int)) and not isinstance(raw_time, bool):
            value = float(raw_time)
            if not math.isfinite(value):
                return
            quote_time = datetime.fromtimestamp(
                value / 1000 if value > 100_000_000_000 else value, UTC
            )
        elif isinstance(raw_time, str):
            quote_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        else:
            return
        await _PATTERN_TRIGGER.on_quote(
            epic, quote_time, datetime.fromtimestamp(received, UTC), bid, offer
        )
    except ValueError, OverflowError, OSError:
        return


class StreamSender(Protocol):
    async def send_json(self, data: dict[str, str], /) -> None: ...


async def _capital_keepalive(
    socket: StreamSender, cst: str, security_token: str
) -> None:
    """Capital requires its JSON ping in addition to WebSocket control pings."""
    while True:
        await asyncio.sleep(240)
        await socket.send_json(
            {
                "destination": "ping",
                "correlationId": "fcc-keepalive",
                "cst": cst,
                "securityToken": security_token,
            }
        )


async def _capital_stream_loop() -> None:
    """Keep one authenticated Capital WebSocket subscription for live quotes.

    The REST endpoints remain the historical-bar source. This stream is the
    authoritative real-time quote source for the UI and wakes the signal engine
    immediately when Capital publishes a tick.
    """
    global _PATTERN_CALIBRATION
    retry_s = 1.0
    while True:
        try:
            await API.ensure_session()
            broker_clock_offset = await API.clock_offset()
            _PATTERN_CALIBRATION = secrets.token_hex(12)
            _PATTERN_EVALUATION.enqueue(
                "control",
                ControlEvidence(
                    reason="clock_calibration", clock_offset_seconds=broker_clock_offset
                ),
                calibration_id=_PATTERN_CALIBRATION,
            )
            cursor = _get_cursor()
            epics = [str(symbol).upper() for symbol in cursor.get("watchlist", [])][:40]
            if _ACTIVE_MODULE == "pattern":
                epics = list(EPICS)
            if not epics:
                await asyncio.sleep(5)
                continue
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=20)
            async with (
                aiohttp.ClientSession(timeout=timeout, trust_env=False) as client,
                client.ws_connect(
                    _CAPITAL_STREAM_URL,
                    heartbeat=30,
                    autoping=True,
                ) as socket,
            ):
                await socket.send_json(
                    {
                        "destination": "marketData.subscribe",
                        "correlationId": "fcc-trader-quotes",
                        "cst": API.cst,
                        "securityToken": API.x_security,
                        "payload": {"epics": epics},
                    }
                )
                _STREAM_STATUS.update(
                    connected=False, error="Waiting for broker quotes", subscribed=epics
                )
                quotes_received: dict[str, float] = {}
                async with asyncio.TaskGroup() as group, asyncio.timeout(60) as idle:
                    group.create_task(
                        _capital_keepalive(socket, API.cst or "", API.x_security or "")
                    )
                    async for frame in socket:
                        if frame.type != aiohttp.WSMsgType.TEXT:
                            continue
                        idle.reschedule(asyncio.get_running_loop().time() + 60)
                        message = json.loads(frame.data)
                        if message.get("status") not in (None, "OK"):
                            raise ConnectionError("Broker rejected the stream request")
                        if message.get("destination") != "quote":
                            continue
                        quote = message.get("payload", {})
                        epic = str(quote.get("epic", "")).upper()
                        bid = float(quote.get("bid", 0.0) or 0.0)
                        offer = float(quote.get("ofr", 0.0) or 0.0)
                        if not (
                            epic in epics
                            and math.isfinite(bid)
                            and math.isfinite(offer)
                            and 0 < bid <= offer
                        ):
                            continue
                        now = time.time()
                        _TICK_CACHE[epic] = (
                            now,
                            {
                                "epic": epic,
                                "symbol": epic,
                                "bid": bid,
                                "offer": offer,
                                "spread": round(offer - bid, 8),
                                "mid": round((bid + offer) / 2, 8),
                                "last_update": quote.get("timestamp", now),
                                "market_status": "TRADEABLE",
                                "source": "stream",
                                "ts": now,
                            },
                        )
                        quotes_received[epic] = now
                        ready = True
                        _STREAM_STATUS.update(
                            last_quote=now,
                            connected=ready,
                            error="",
                        )
                        if _ACTIVE_MODULE == "pattern":
                            await _pattern_feed_status(ready)
                        if ready:
                            retry_s = 1.0
                        await _pattern_quote(
                            epic,
                            quote.get("timestamp"),
                            now - broker_clock_offset,
                            bid,
                            offer,
                        )
                        if _LIVE_TICK_EVENT:
                            _LIVE_TICK_EVENT.set()
                    raise ConnectionError("Broker closed the price stream")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _ACTIVE_MODULE == "pattern":
                await _pattern_feed_status(False)
            _STREAM_STATUS.update(connected=False, subscribed=[], error=str(exc)[:200])
            logger.warning("Capital quote stream disconnected: %s; retrying", exc)
            await asyncio.sleep(retry_s)
            retry_s = min(retry_s * 2, 30.0)
        finally:
            _STREAM_STATUS.update(connected=False, subscribed=[])
            if _ACTIVE_MODULE == "pattern":
                await _pattern_feed_status(False)


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
        r = await API.raw_get(
            "/api/v1/markets",
            params={
                "searchTerm": {"OIL_CRUDE": "Crude", "OIL_BRENT": "Brent"}.get(
                    epic.upper(), epic
                )
            },
        )
        if r.status_code != 200:
            return {"epic": epic, "error": f"markets {r.status_code}", "ts": now}
        markets = r.json().get("markets", [])
        # Find the exact epic match
        match = next(
            (m for m in markets if m.get("epic", "").upper() == epic.upper()), None
        )
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


@app.get("/api/stream/status")
async def stream_status():
    """Expose real-time Capital stream health without leaking session tokens."""
    return {
        "connected": bool(_STREAM_STATUS.get("connected", False)),
        "last_quote": _STREAM_STATUS.get("last_quote", 0.0),
        "subscribed": _STREAM_STATUS.get("subscribed", []),
        "error": _STREAM_STATUS.get("error", ""),
    }


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
    from tools.strategy_engine import (
        compute_atr,
        compute_bb,
        compute_ema,
        compute_rsi,
        scalping_volatility,
    )

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
        "long_setup": last["close"] >= hi10
        and ema9[i] > ema21[i]
        and 45 <= rsi7[i] <= 72,
        "short_setup": last["close"] <= lo10
        and ema9[i] < ema21[i]
        and 28 <= rsi7[i] <= 55,
        "fired": sig is not None,
        "signal": sig.to_dict() if sig else None,
    }


@app.get("/api/market_info")
async def market_info(epic: str, _raw: int = 0):
    """Return min/max deal size, step, bid/ask for a symbol. Cached for 5 min.
    Pass _raw=1 to bypass parsing and see the raw Capital.com response (debug)."""
    import time as _t

    if _raw:
        r = await API.raw_get(f"/api/v1/marketinfo/{epic}")
        return {"status": r.status_code, "body_excerpt": r.text[:2000]}
    now = _t.time()
    cached = _MARKET_INFO_CACHE.get(epic)
    if cached and (now - cached[0]) < 300:
        return cached[1]
    info = await API.get_market_info(epic)
    if info is None:
        # Diagnostic: include the raw response so we can see why parsing failed
        try:
            r = await API.raw_get(f"/api/v1/marketinfo/{epic}")
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
        "price_vs_ema20_pct": (last["close"] - ema20[i]) / ema20[i] * 100
        if ema20[i]
        else None,
        "price_vs_bb": {
            "above_upper": last["close"] > bb20["upper"][i],
            "below_lower": last["close"] < bb20["lower"][i],
            "pct_of_range": (
                (last["close"] - bb20["lower"][i])
                / (bb20["upper"][i] - bb20["lower"][i])
                * 100
            )
            if bb20["upper"][i] > bb20["lower"][i]
            else None,
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

    async def _fetch_symbol(sym: str) -> tuple[str, str, list[dict] | None, list[dict]]:
        sym_errors: list[dict] = []
        try:
            epic = await resolve_epic(API, sym)
            bars = await _cached_ohlcv(epic, req.timeframe, BAR_COUNT)
            # Threshold scales with the bar count we asked for. Minute bars:
            # allow as few as 60 (scalping only needs 60+). H1+: require 220.
            min_bars = 60 if "MINUTE" in req.timeframe else 220
            if not bars or len(bars) < min_bars:
                received = len(bars) if bars else 0
                sym_errors.append(
                    {
                        "symbol": sym,
                        "epic": epic,
                        "issue": (
                            f"no usable broker bars (received {received}; "
                            f"need {min_bars})"
                        ),
                    }
                )
                return (sym, epic, None, sym_errors)
            return (sym, epic, bars, sym_errors)
        except Exception as exc:
            logger.exception("scan: failed to fetch bars for %s", sym)
            sym_errors.append(
                {
                    "symbol": sym,
                    "epic": sym,
                    "issue": f"broker bar fetch failed: {type(exc).__name__}: {exc}",
                }
            )
            return (sym, sym, None, sym_errors)

    sym_results = await asyncio.gather(*[_fetch_symbol(s) for s in req.symbols])
    t_fetch = time.perf_counter()

    # Build per-symbol indicator cache (computed once, reused for all strategies)
    symbol_data: dict[str, tuple[str, list[dict]]] = {}
    for sym, epic, bars, sym_errors in sym_results:
        errors.extend(sym_errors)
        if bars:
            symbol_data[sym] = (epic, bars)

    # ── Phase 2: precompute indicators once per symbol ───────────────────────
    # These are O(n) and were being recalculated 5x per symbol (once per strategy).
    sym_indicators: dict[str, dict[str, Any]] = {
        sym: {"bars": bars, "epic": epic, **_indicator_snapshot(bars)}
        for sym, (epic, bars) in symbol_data.items()
    }
    t_indicators = time.perf_counter()

    # ── Phase 3: evaluate strategies (still sequential, but all I/O is done) ──
    # These contexts are shared by all strategies for a symbol. Fetching them
    # inside each strategy made an AI scan repeatedly perform the same network
    # work and could block the FastAPI event loop.
    from tools.intermarket import fetch_intermarket
    from tools.news import fetch_news

    ai_context_cache: dict[str, tuple[list[dict], dict]] = {}
    intermarket_task: asyncio.Task | None = None

    async def _get_ai_context(sym: str) -> tuple[list[dict], dict]:
        nonlocal intermarket_task
        if intermarket_task is None:
            intermarket_task = asyncio.create_task(fetch_intermarket(API, "all"))
        cached = ai_context_cache.get(sym)
        if cached is not None:
            return cached

        async def _fetch_news() -> list[dict]:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(fetch_news, sym, hours=4, max_items=4),
                    timeout=3.0,
                )
            except TimeoutError:
                logger.warning("news context timed out for %s", sym)
                return []

        news_items, inter_data = await asyncio.gather(_fetch_news(), intermarket_task)
        ai_context_cache[sym] = (news_items, inter_data)
        return ai_context_cache[sym]

    scan_slots = asyncio.Semaphore(3 if req.use_ai else 1)

    async def evaluate_symbol(sym: str, ind: dict) -> None:
        async with scan_slots:
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

                        # A walk-forward backtest recalculates indicators for every bar.
                        # Keep that CPU work off FastAPI's event loop so account and
                        # cockpit polling remain responsive while a scan is running.
                        bt_result = await asyncio.to_thread(
                            backtest, bars[-220:], fn, sym, initial_capital=10_000.0
                        )
                    except Exception:
                        pass
                    base_conf = sig_dict["confidence"]
                    if bt_result and bt_result.get("sample", 0) >= 10:
                        wr = bt_result.get("win_rate", 0.5)
                        # Scale win rate 0.3-0.7 → confidence delta -0.2 to +0.2.
                        bt_adjustment = (wr - 0.5) * 0.4
                        sig_dict["confidence"] = round(
                            min(
                                0.95,
                                max(
                                    0.10,
                                    base_conf * 0.5 + bt_adjustment + 0.5 * base_conf,
                                ),
                            ),
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
                        sig_dict["backtest"] = {
                            "win_rate": 0.5,
                            "sample": 0,
                            "expectancy": 0.0,
                            "sharpe": 0.0,
                        }

                    # ── Enriched market context for AI agents ────────────────────────────
                    enriched_summary = {
                        "last_close": ind["last_close"],
                        "atr": ind["atr"],
                        "rsi": ind["rsi"],
                        "adx": ind["adx14"][i]
                        if i < len(ind["adx14"]) and not math.isnan(ind["adx14"][i])
                        else 20.0,
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
                            _classify_regime,
                            compute_setup_score,
                            session_multiplier,
                            tag_session,
                        )

                        regime_str = _classify_regime(bars)
                        dscore = compute_setup_score(
                            bt_result or {}, regime_str, strat_name
                        )
                    except Exception as exc:
                        logger.debug(
                            "deterministic score fallback for %s/%s: %s",
                            sym,
                            strat_name,
                            exc,
                        )
                        dscore = UNSCORED_SETUP.copy()
                        regime_str = "calm"
                    session = tag_session()
                    sess_mult = session_multiplier(session)
                    deterministic_score = round(
                        min(100.0, dscore["score"] * sess_mult), 1
                    )
                    enriched_summary["regime"] = regime_str
                    enriched_summary["session"] = session
                    enriched_summary["setup_score_raw"] = dscore["score"]
                    enriched_summary["setup_score"] = deterministic_score

                    # ── AI evaluation: meta-controller (single LLM, rich context) ─────
                    ai_result: dict = {}
                    if req.use_ai:
                        try:
                            from tools.agent_orchestrator import evaluate_signal_v2

                            news_items, inter_data = await _get_ai_context(sym)
                            ai_result = await asyncio.wait_for(
                                evaluate_signal_v2(
                                    sig_dict,
                                    enriched_summary,
                                    backtest=bt_result,
                                    deterministic_score=dscore,
                                    news=news_items,
                                    intermarket=inter_data,
                                ),
                                timeout=5.0,
                            )
                        except TimeoutError:
                            ai_result = {
                                "verdict": "pass",
                                "score": 0,
                                "size_modifier": 1.0,
                                "reasoning": "meta-controller timeout",
                                "concerns": ["timeout"],
                                "key_factors": [],
                                "consensus": "pass",
                                "consensus_confidence": 0.0,
                                "latency_ms": 0,
                            }
                        except Exception as exc:
                            logger.warning(
                                "meta-controller failed for %s/%s: %s",
                                sym,
                                strat_name,
                                exc,
                            )
                            ai_result = {
                                "verdict": "pass",
                                "score": 0,
                                "size_modifier": 1.0,
                                "reasoning": f"meta-controller error: {exc}",
                                "concerns": ["llm_error"],
                                "key_factors": [],
                                "consensus": "pass",
                                "consensus_confidence": 0.0,
                                "latency_ms": 0,
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
                    elif deterministic_score >= 70 and ai_result.get("verdict") in (
                        "approve",
                        "modify",
                    ):
                        grade = "A+"
                    elif deterministic_score >= 50:
                        grade = "B"
                    else:
                        grade = "C"

                    setups.append(
                        {
                            "id": f"{sym}-{strat_name}-{int(time.time() * 1000)}",
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
                            "aiEvaluated": req.use_ai,
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
                            "dataQuality": "live",
                        }
                    )
                except Exception as exc:
                    logger.exception("scan: strategy %s failed for %s", strat_name, sym)
                    errors.append(
                        {
                            "symbol": sym,
                            "strategy": strat_name,
                            "issue": str(exc),
                            "dataQuality": "error",
                        }
                    )

    await asyncio.gather(
        *(evaluate_symbol(sym, ind) for sym, ind in sym_indicators.items())
    )

    # Strategies can emit the same setup at slightly different evaluation paths.
    # Keep the strongest row instead of presenting duplicate trade instructions.
    unique_setups: dict[tuple, dict] = {}
    for setup in setups:
        key = (
            setup["symbol"],
            setup["direction"],
            round(setup["entry"], 8),
            round(setup["stopLoss"], 8),
            round(setup["takeProfit"], 8),
        )
        current = unique_setups.get(key)
        if current is None or (
            setup.get("deterministicScore", 0),
            setup.get("confidence", 0),
        ) > (
            current.get("deterministicScore", 0),
            current.get("confidence", 0),
        ):
            unique_setups[key] = setup
    setups = list(unique_setups.values())

    # ── A+ filter: sort by deterministic score desc; mark top 3 as primary
    setups.sort(
        key=lambda s: (s.get("deterministicScore", 0), s.get("aiScore", 0)),
        reverse=True,
    )
    for idx, s in enumerate(setups):
        s["rank"] = idx + 1
        s["isTopPick"] = (idx < 3) and (s.get("grade") in ("A+", "B"))
    t_end = time.perf_counter()
    logger.info(
        "scan: %d sym x %d strat — fetch=%dms indicators=%dms total=%dms found=%d",
        len(req.symbols),
        len(strats),
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
    if not API.demo:
        raise HTTPException(403, "Live trading is disabled; use a demo session")
    if not API.cst:
        return {"error": "not logged in to Capital.com", "demo": True}
    direction = req.direction.upper()
    if direction not in ("BUY", "SELL"):
        raise HTTPException(400, "direction must be BUY or SELL")
    if req.sl is None:
        raise HTTPException(400, "a stop loss is required for every order")
    account = await API.get_account()
    if account is None or not account.demo:
        raise HTTPException(503, "Demo account unavailable; order not submitted")
    info = await API.get_market_info(req.epic)
    if info is None or info.contract_size <= 0:
        raise HTTPException(
            503, "Broker contract details unavailable; order not submitted"
        )
    if info.currency != account.currency:
        raise HTTPException(
            400, "Currency conversion unavailable; cash risk cannot be bounded"
        )
    entry = info.offer if direction == "BUY" else info.bid
    if (direction == "BUY" and req.sl >= entry) or (
        direction == "SELL" and req.sl <= entry
    ):
        raise HTTPException(400, "stop loss is on the wrong side of the market")
    try:
        size = compute_size_capital(
            account.balance,
            req.risk_pct,
            entry,
            req.sl,
            contract_size=info.contract_size,
            min_size=max(info.min_deal_size, account.min_deal_size),
            max_size=info.max_deal_size,
            step=info.step,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    res = await API.place_order(
        req.epic,
        direction,
        size,
        sl=req.sl,
        tp=req.tp,
        account_id=req.account_id,
    )
    return res


# ─── Cockpit: mode toggle, status, watchlist, live engine ─────────────────────

# Default watchlist used when the live engine starts. The user can override
# via PUT /api/cockpit/watchlist.
DEFAULT_WATCHLIST: list[str] = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "NZDUSD",
    "USDCAD",
    "AAPL",
    "TSLA",
    "NVDA",
    "MSFT",
    "AMZN",
    "META",
]

# Cooldown: don't re-alarm the same symbol+direction within this many seconds.
COOLDOWN_S = 4 * 3600
# LLM rate limit: at most one LLM call per this many seconds globally.
_LLM_MIN_INTERVAL_S = 30.0
_last_llm_ts: float = 0.0
_LIVE_ENGINE_MAX_WORKERS = 4


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


def _indicator_snapshot(bars: list[dict]) -> dict[str, Any]:
    """Indicators for the latest bar, shared by the scan and the live engine."""
    i = len(bars) - 1
    closes = [b["close"] for b in bars]
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
    bb_l = bb20["lower"][i]
    bb_range = bb_u - bb_l
    bb_pct = round((closes[i] - bb_l) / bb_range * 100, 1) if bb_range > 0 else 50.0

    # Volume ratio: current bar volume vs 20-bar rolling average.
    avg_vol = (
        sum(volumes[max(0, i - 19) : i + 1]) / 20
        if i >= 19
        else sum(volumes[: i + 1]) / max(i + 1, 1)
    )
    vol_ratio = round(volumes[i] / avg_vol, 2) if avg_vol > 0 else 1.0

    # Trend strength score: combines ADX level, EMA alignment, and RSI zone.
    adx_i = adx14[i] if i < len(adx14) and not math.isnan(adx14[i]) else 0.0
    adx_score = min(adx_i / 40.0, 1.0)  # 0-1, 40+ ADX = max
    ema_align = (
        1.0
        if (ema50_i > ema200_i and ema20_i > ema50_i)
        or (ema50_i < ema200_i and ema20_i < ema50_i)
        else 0.5
    )
    rsi_aligned = (
        1.0
        if (ema50_i > ema200_i and 45 <= rsi_i <= 70)
        or (ema50_i < ema200_i and 30 <= rsi_i <= 55)
        else 0.0
    )
    trend_score = round(
        (adx_score * 0.5 + ema_align * 0.3 + rsi_aligned * 0.2) * 100, 0
    )  # 0-100

    # 3-bar momentum: net direction of last 3 closes.
    last3 = closes[max(0, i - 2) : i + 1]
    if len(last3) >= 3:
        momentum_3bar = (
            "bullish"
            if last3[2] > last3[0]
            else "bearish"
            if last3[2] < last3[0]
            else "neutral"
        )
        momentum_3bar_pct = (
            round((last3[2] - last3[0]) / last3[0] * 100, 3) if last3[0] != 0 else 0.0
        )
    else:
        momentum_3bar = "neutral"
        momentum_3bar_pct = 0.0

    return {
        "i": i,
        "atr": atr_i,
        "rsi": rsi_i,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "bb20": bb20,
        "adx14": adx14,
        "adx": adx_i,
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


async def _live_engine_loop():
    """Background loop: every TICK_S seconds, score the watchlist.

    Behavior:
      - offline:    sleep, no work
      - demo:       for each watched symbol, run deterministic layers on the
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
            # Heartbeat: write last_tick on every iteration so the UI sees
            # the engine is alive even when no bars can be fetched.
            cursor["last_tick"] = int(time.time())
            _save_cursor(cursor)
            if (
                mode == "offline"
                or not cursor.get("watchlist")
                or not (API.cst and API.x_security)
            ):
                await _aio.sleep(TICK_S)
                continue

            # Score each watched symbol; collect the best setup per symbol.
            from tools.agent_orchestrator import evaluate_signal_v2
            from tools.intermarket import fetch_intermarket
            from tools.news import fetch_news
            from tools.strategy_engine import tag_session

            # Resolution is set on the cursor; defaults to HOUR. The UI's
            # timeframe selector can update it via PUT /api/cockpit/watchlist
            # (we store it in the cursor so it persists).
            resolution = cursor.get("resolution", "HOUR")

            fetch_slots = _aio.Semaphore(_LIVE_ENGINE_MAX_WORKERS)
            fetched = await _aio.gather(
                *(
                    _fetch_live_symbol(sym, resolution, fetch_slots)
                    for sym in cursor["watchlist"]
                )
            )
            evaluations = await _evaluate_live_symbols(fetched)
            results = [
                result for symbol_results, _ in evaluations for result in symbol_results
            ]
            strat_hits: dict[str, int] = {}
            for _, hits in evaluations:
                for strategy, count in hits.items():
                    strat_hits[strategy] = strat_hits.get(strategy, 0) + count

            # Always log the best score seen this tick (even if below threshold)
            # so we can see what the strategies are finding in the wild.
            if results:
                best_seen = max(r["dscore"]["score"] for r in results)
                top3 = sorted(
                    results, key=lambda r: r["dscore"]["score"], reverse=True
                )[:3]
                top3_str = ", ".join(
                    f"{r['symbol']}/{r['strategy']}={r['dscore']['score']:.1f}"
                    for r in top3
                )
                print(
                    f"[ENGINE TICK] best={best_seen:.1f} top3=[{top3_str}] (watch_queue=ENABLED)"
                )
            else:
                hits_str = (
                    ", ".join(f"{k}={v}" for k, v in sorted(strat_hits.items()))
                    or "none"
                )
                print(
                    f"[ENGINE TICK] no setups — mode={mode} watchlist={len(cursor['watchlist'])} "
                    f"resolution={resolution} hits=[{hits_str}]"
                )

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
                {
                    "symbol": result["symbol"],
                    "strategy": result["strategy"],
                    "score": result["dscore"]["score"],
                    "direction": _signal_direction(result["signal"]),
                    "entry": _signal_value(result["signal"], "entry"),
                }
                for result in top
            ]
            cursor["last_tick"] = int(time.time())

            # Only call the LLM on the top setup, and only if it would change
            # the active signal. Rate-limited.
            best = top[0]
            sig = best["signal"]
            sig_dict = sig.to_dict()
            sig_dict["symbol"] = best["symbol"]
            ctx = best["context"]
            news, inter = await _aio.gather(
                _aio.to_thread(fetch_news, best["symbol"], hours=4, max_items=3),
                fetch_intermarket(API, "all"),
            )
            ai = await _rate_limited_llm(
                evaluate_signal_v2(
                    sig_dict,
                    {
                        "regime": best["regime"],
                        "session": tag_session(),
                        "rsi": round(ctx["rsi"], 1),
                        "adx": round(ctx["adx"], 1),
                        "atr": ctx["atr"],
                        "bb_position_pct": ctx["bb_pct"],
                        "trend_score": ctx["trend_score"],
                    },
                    backtest=best["backtest"],
                    deterministic_score=best["dscore"],
                    news=news,
                    intermarket=inter,
                )
            )
            if ai.get("verdict") == "approve":
                cursor["active_signal"] = {
                    "symbol": best["symbol"],
                    "strategy": best["strategy"],
                    "direction": _signal_direction(sig),
                    "entry": _signal_value(sig, "entry"),
                    "stop_loss": _signal_value(sig, "stop_loss"),
                    "take_profit": _signal_value(sig, "take_profit"),
                    "score": best["dscore"]["score"],
                    "ai_score": ai.get("score", 0),
                    "size_modifier": ai.get("size_modifier", 1.0),
                    "reasoning": ai.get("reasoning", ""),
                    "concerns": ai.get("concerns", []),
                    "key_factors": ai.get("key_factors", []),
                    "raised_at": int(time.time()),
                }
                auto_result = await _maybe_execute_demo_signal(
                    cursor["active_signal"],
                    cursor.get("demo_auto_trade"),
                )
                if auto_result:
                    cursor["active_signal"]["demo_auto_trade"] = auto_result
            else:
                cursor["active_signal"] = None

            _save_cursor(cursor)
            await _aio.sleep(TICK_S)
        except Exception as exc:
            logger.exception("live engine loop error: %s", exc)
            await _aio.sleep(10)


async def _fetch_live_symbol(
    symbol: str, resolution: str, slots: asyncio.Semaphore
) -> tuple[str, list[dict]]:
    """Fetch one live-engine symbol without exceeding the broker request limit."""
    async with slots:
        try:
            # Capital.com caps `max` per resolution. 5m allows 1500 (~5d), while
            # HOUR caps lower (~1000). Pick the safe ceiling per resolution.
            max_bars = 1500 if resolution == "MINUTE_5" else 1000
            bars = await _fetch_bars_for(symbol, max_bars, resolution=resolution)
        except Exception as exc:
            logger.debug("live engine: bars for %s failed: %s", symbol, exc)
            return symbol, []
    return symbol, bars if len(bars) >= 220 else []


async def _evaluate_live_symbols(
    symbol_bars: list[tuple[str, list[dict]]],
) -> list[tuple[list[dict], dict[str, int]]]:
    """Evaluate symbols in worker threads without starving FastAPI's event loop."""
    slots = asyncio.Semaphore(_LIVE_ENGINE_MAX_WORKERS)

    async def evaluate_symbol(
        symbol: str, bars: list[dict]
    ) -> tuple[list[dict], dict[str, int]]:
        if not bars:
            return [], {}
        async with slots:
            return await asyncio.to_thread(_evaluate_live_symbol, symbol, bars)

    return await asyncio.gather(
        *(evaluate_symbol(symbol, bars) for symbol, bars in symbol_bars)
    )


def _evaluate_live_symbol(
    symbol: str, bars: list[dict]
) -> tuple[list[dict], dict[str, int]]:
    """Run deterministic live-engine scoring for one symbol in a worker thread."""
    from tools.strategy_engine import (
        ALL_STRATEGIES,
        _classify_regime,
        backtest,
        compute_setup_score,
    )

    regime = _classify_regime(bars)
    context = _indicator_snapshot(bars)
    results: list[dict] = []
    strategy_hits: dict[str, int] = {}
    for strategy_name, strategy in ALL_STRATEGIES.items():
        try:
            signal = strategy(bars, symbol)
        except Exception as exc:
            logger.debug("strategy %s on %s raised: %s", strategy_name, symbol, exc)
            continue
        if signal is None:
            continue
        strategy_hits[strategy_name] = strategy_hits.get(strategy_name, 0) + 1
        try:
            backtest_result = backtest(
                bars[-1500:], strategy, symbol, initial_capital=10_000.0
            )
        except Exception:
            backtest_result = {
                "sample": 0,
                "win_rate": 0.0,
                "expectancy_r": 0.0,
                "profit_factor": 0.0,
                "sharpe": 0.0,
            }
        try:
            deterministic_score = compute_setup_score(
                backtest_result, regime, strategy_name
            )
        except Exception:
            deterministic_score = UNSCORED_SETUP.copy()
        if deterministic_score["score"] < 30:
            continue
        results.append(
            {
                "symbol": symbol,
                "strategy": strategy_name,
                "signal": signal,
                "backtest": backtest_result,
                "dscore": deterministic_score,
                "regime": regime,
                "context": context,
            }
        )
    return results, strategy_hits


def _signal_value(signal: object, name: str) -> object | None:
    """Read a signal field from either the strategy object or serialized payload."""
    if isinstance(signal, dict):
        return signal.get(name)
    return getattr(signal, name, None)


def _signal_direction(signal: object) -> str:
    """Normalize directions for the cockpit's LONG/SHORT rendering contract."""
    direction = _signal_value(signal, "direction")
    return str(direction).upper() if direction is not None else "LONG"


async def _maybe_execute_demo_signal(
    signal: dict, demo_auto_trade: object | None
) -> dict | None:
    """Place a demo order for an approved signal when the user opted in.

    This helper is intentionally fail-closed. It refuses to trade unless both
    the cockpit mode and the Capital.com API session are demo.
    """
    if not isinstance(demo_auto_trade, dict) or not demo_auto_trade.get("enabled"):
        return None
    if _get_mode() != "demo":
        return {"status": "skipped", "reason": "cockpit mode is not demo"}
    if not API.demo:
        return {"status": "skipped", "reason": "Capital.com session is not demo"}
    if not (API.cst and API.x_security):
        return {"status": "skipped", "reason": "not logged in to Capital.com"}

    try:
        max_open = int(demo_auto_trade.get("max_open_positions", 1) or 1)
    except TypeError, ValueError:
        max_open = 1
    max_open = max(1, min(5, max_open))

    positions = await API.get_positions()
    if len(positions) >= max_open:
        return {"status": "skipped", "reason": "max open positions reached"}

    symbol = str(signal.get("symbol", "")).strip().upper()
    if not symbol:
        return {"status": "skipped", "reason": "signal has no symbol"}
    epic = await resolve_epic(API, symbol)
    if any(position.epic.upper() == epic.upper() for position in positions):
        return {"status": "skipped", "reason": "position already open for symbol"}

    direction = "BUY" if str(signal.get("direction", "")).upper() == "LONG" else "SELL"
    entry = _float_or_none(signal.get("entry"))
    stop_loss = _float_or_none(signal.get("stop_loss"))
    take_profit = _float_or_none(signal.get("take_profit"))
    if entry is None or stop_loss is None or take_profit is None:
        return {"status": "skipped", "reason": "signal is missing price levels"}

    account = await API.get_account()
    if account is None or not account.demo:
        return {"status": "skipped", "reason": "demo account unavailable"}
    info = await API.get_market_info(epic)
    if info is None:
        return {"status": "skipped", "reason": "market info unavailable"}
    if info.contract_size <= 0:
        return {
            "status": "skipped",
            "reason": "broker contract size unavailable; cannot bound cash risk",
        }
    if info.currency != account.currency:
        return {
            "status": "skipped",
            "reason": "currency conversion unavailable; cannot bound cash risk",
        }
    market_entry = info.offer if direction == "BUY" else info.bid
    if (direction == "BUY" and stop_loss >= market_entry) or (
        direction == "SELL" and stop_loss <= market_entry
    ):
        return {"status": "skipped", "reason": "stop loss is stale or invalid"}

    try:
        risk_pct = float(demo_auto_trade.get("risk_pct", 0.005) or 0.005)
    except TypeError, ValueError:
        risk_pct = 0.005
    risk_pct = max(0.0001, min(0.005, risk_pct))
    size_modifier = _float_or_none(signal.get("size_modifier")) or 1.0
    risk_pct = risk_pct * max(0.25, min(1.0, size_modifier))

    try:
        size = compute_size_capital(
            account.balance,
            risk_pct,
            market_entry,
            stop_loss,
            contract_size=info.contract_size,
            min_size=max(info.min_deal_size, account.min_deal_size),
            max_size=info.max_deal_size,
            step=info.step,
        )
    except ValueError as exc:
        return {"status": "skipped", "reason": str(exc)}
    result = await API.place_order(
        epic,
        direction,
        size,
        sl=stop_loss,
        tp=take_profit,
    )
    audit = {
        "status": "submitted" if "error" not in result else "rejected",
        "epic": epic,
        "direction": direction,
        "size": size,
        "risk_pct": round(risk_pct, 5),
        "broker_result": result,
        "submitted_at": int(time.time()),
    }
    try:
        import tools.state as _st

        _st.append_signal({**signal, "demo_auto_trade": audit})
    except Exception as exc:
        logger.debug("demo auto trade audit failed: %s", exc)
    return audit


def _float_or_none(value: object) -> float | None:
    if not isinstance(value, int | float | str):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


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
        return {
            "last_tick": 0,
            "active_signal": None,
            "watch_queue": [],
            "watchlist": list(DEFAULT_WATCHLIST),
        }


def _save_cursor(c: dict) -> None:
    try:
        import tools.state as _st

        _st.set_engine_cursor(c)
    except Exception as exc:
        logger.debug("save cursor failed: %s", exc)


async def _fetch_bars_for(symbol: str, n: int, resolution: str = "HOUR") -> list[dict]:
    """Broker bars for the live engine; empty means the symbol sits out this tick.

    Never substitute synthetic or other-resolution data: these bars produce
    signals that the demo auto-trader may submit with their SL/TP levels.
    """
    if not API.cst:
        return []
    epic = await resolve_epic(API, symbol)
    return await API.get_ohlcv(epic, resolution, n)


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
    if req.mode not in ("offline", "demo"):
        return {"error": f"invalid mode: {req.mode}"}
    new_mode = state.set_mode(req.mode)
    return {"mode": new_mode, "ok": True}


class WatchlistRequest(BaseModel):
    symbols: list[str]


class ResolutionRequest(BaseModel):
    resolution: str


class DemoAutoTradeRequest(BaseModel):
    enabled: bool
    risk_pct: float = 0.005
    max_open_positions: int = 1


@app.put("/api/cockpit/watchlist")
async def set_cockpit_watchlist(req: WatchlistRequest):
    import tools.state as _st

    cur = _st.update_engine(watchlist=[s.upper() for s in req.symbols][:24])
    return {"watchlist": cur["watchlist"], "ok": True}


@app.put("/api/cockpit/resolution")
async def set_cockpit_resolution(req: ResolutionRequest):
    """Set the bar resolution the live engine should use. One of HOUR, MINUTE_5,
    MINUTE_15, MINUTE_30, HOUR_4, DAY. Persists across server restarts.
    """
    valid = {"HOUR", "MINUTE", "MINUTE_5", "MINUTE_15", "MINUTE_30", "HOUR_4", "DAY"}
    if req.resolution not in valid:
        return {"error": f"invalid resolution; expected one of {sorted(valid)}"}
    import tools.state as _st

    _st.update_engine(resolution=req.resolution)
    return {"resolution": req.resolution, "ok": True}


@app.put("/api/cockpit/demo-auto-trade")
async def set_demo_auto_trade(req: DemoAutoTradeRequest):
    """Enable demo-only automatic execution for approved cockpit signals."""
    if req.risk_pct <= 0 or req.risk_pct > 0.005:
        return {"error": "risk_pct must be > 0 and <= 0.005"}
    if req.max_open_positions < 1 or req.max_open_positions > 5:
        return {"error": "max_open_positions must be between 1 and 5"}
    import tools.state as _st

    demo_auto_trade = {
        "enabled": bool(req.enabled),
        "risk_pct": float(req.risk_pct),
        "max_open_positions": int(req.max_open_positions),
    }
    _st.update_engine(demo_auto_trade=demo_auto_trade)
    return {"demo_auto_trade": demo_auto_trade, "ok": True}


@app.post("/api/cockpit/ack")
async def ack_active_signal():
    """Trader has seen (or acted on) the active signal. Clear it from the cursor."""
    import tools.state as _st

    cur = _st.get_engine_cursor()
    if cur.get("active_signal"):
        _st.append_signal(cur["active_signal"])
    _st.update_engine(active_signal=None)
    return {"ok": True}


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    _s = get_settings()
    uvicorn.run(
        "tools.scan_server:app", host=_s.server_host, port=_s.server_port, reload=False
    )
