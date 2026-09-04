"""Capital.com REST API connector — no MT5 installation required.

Authentication:
  Capital.com uses a session-based flow:
  1. POST /api/v1/session with API key + identifier (login) + password → returns CST + X-SECURITY-TOKEN
  2. All subsequent requests must include those as headers
  3. Use /api/v1/logout on shutdown

Env vars:
  CAPITAL_API_KEY     — required
  CAPITAL_LOGIN       — your Capital.com LOGIN EMAIL (not MT5 account id).
                        The session /api/v1/session uses the same identifier
                        you use to log into capital.com (the web frontend).
  CAPITAL_PASSWORD    — your password. If your API key was created with
                        "Custom password: YES" in API integrations, this must
                        be the API-specific password you saved at creation —
                        not your web login password.
  CAPITAL_ENVIRONMENT — "demo" for paper trading, "live" for real money
  CAPITAL_ACCOUNT_ID  — optional. The MT5 / accountId of the account you want
                        to trade on (e.g. "1038584"). When set, the primary
                        account is the matching one instead of accounts[0].
                        You can also override per-request via place_order().
"""

import os  # Centralized config; reads .env via python-dotenv if available
import time
import math
import logging
import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional, Union

import httpx

from tools.config import get_settings

logger = logging.getLogger(__name__)

DEMO_BASE = "https://demo-api-capital.backend-capital.com"
LIVE_BASE = "https://api-capital.backend-capital.com"


@dataclass
class Account:
    account_id: str
    account_name: str
    account_type: str  # CFD / SPREADBET
    balance: float
    deposit: float
    profit_loss: float
    available: float
    currency: str
    leverage: int
    demo: bool = True
    min_deal_size: float = 0.0  # account-level min deal size (0 = no floor)


@dataclass
class SymbolInfo:
    epic: str
    name: str
    instrument_type: str
    digits: int
    point: float
    min_deal_size: float
    max_deal_size: float
    step: float
    bid: float
    offer: float
    spread: float
    currency: str
    margin_factor: float  # leverage
    min_stop_distance: float = 0.0  # min stop distance in price units (e.g. 239 for ETHUSD)


@dataclass
class Position:
    deal_id: str
    epic: str
    direction: str  # BUY / SELL
    size: float
    level: float
    stop_level: Optional[float]
    limit_level: Optional[float]
    profit: float
    currency: str


class CapitalAPI:
    """Async client for Capital.com's REST API."""

    def __init__(self, demo: bool = True, account_id: Optional[str] = None):
        self.base = DEMO_BASE if demo else LIVE_BASE
        self.demo = demo
        _cfg = get_settings()
        self.api_key = _cfg.capital_api_key
        self.login_id = _cfg.capital_login
        self.password = _cfg.capital_password
        self.preferred_account_id = account_id or _cfg.capital_account_id or None
        self.cst: Optional[str] = None
        self.x_security: Optional[str] = None
        self.accounts: list[Account] = []
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        h = {
            "X-CAP-API-KEY": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.cst:
            h["CST"] = self.cst
        if self.x_security:
            h["X-SECURITY-TOKEN"] = self.x_security
        return h

    async def login(self) -> bool:
        if not (self.api_key and self.login_id and self.password):
            logger.warning("CAPITAL_API_KEY / CAPITAL_LOGIN / CAPITAL_PASSWORD not all set")
            return False
        url = f"{self.base}/api/v1/session"
        body = {
            "identifier": self.login_id,
            "password": self.password,
            "rememberMe": True,
        }
        try:
            r = await self._client.post(url, headers=self._headers(), json=body)
        except Exception as exc:
            logger.error("Login network error: %s", exc)
            return False
        if r.status_code != 200:
            # Surface a diagnostic that points to the actual cause.
            detail = r.text[:300]
            try:
                err_code = (r.json() or {}).get("errorCode", "")
            except Exception:
                err_code = ""
            if r.status_code == 401:
                hint = (
                    "401 Unauthorized from Capital.com. Most common causes:\n"
                    "  1. CAPITAL_LOGIN is the MT5 account number, not the login email.\n"
                    "     The /api/v1/session 'identifier' must be the EMAIL you use to log into capital.com.\n"
                    "  2. CAPITAL_PASSWORD doesn't match. If the API key was created with\n"
                    "     'Custom password: YES' in API integrations, you need the API-specific\n"
                    "     password you saved at creation — not your web login password.\n"
                    "  3. CAPITAL_API_KEY was created under a different account."
                )
                if err_code == "error.invalid.details":
                    hint += "\n  Server returned errorCode=error.invalid.details → identifier/password mismatch."
                logger.error("Login 401.\n%s\nServer said: %s", hint, detail)
            else:
                logger.error("Login failed (%d): %s", r.status_code, detail)
            return False
        self.cst = r.headers.get("CST", "")
        self.x_security = r.headers.get("X-SECURITY-TOKEN", "")
        if not (self.cst and self.x_security):
            logger.error("Login succeeded but no CST/X-SECURITY-TOKEN returned")
            return False
        logger.info("Capital.com login OK (%s env)", "demo" if self.demo else "LIVE")
        # refresh accounts
        await self.refresh_accounts()
        return True

    async def logout(self) -> None:
        if not (self.cst and self._client):
            return
        try:
            await self._client.delete(f"{self.base}/api/v1/session", headers=self._headers())
        finally:
            self.cst = None
            self.x_security = None
            logger.info("Capital.com logout OK")

    async def refresh_accounts(self) -> list[Account]:
        if not self._client:
            return []
        r = await self._client.get(f"{self.base}/api/v1/accounts", headers=self._headers())
        if r.status_code != 200:
            logger.error("accounts fetch failed (%d): %s", r.status_code, r.text[:200])
            return []
        data = r.json()
        out: list[Account] = []
        for a in data.get("accounts", []):
            balance = a.get("balance", {}).get("balance", 0.0)
            deposit = a.get("balance", {}).get("deposit", 0.0)
            pl = a.get("balance", {}).get("profitLoss", 0.0)
            avail = a.get("balance", {}).get("available", 0.0)
            # Account-level min deal size (some demo accounts enforce 1.0 lots
            # for forex). Falls back to 0 if the field is absent.
            acct_min = 0.0
            try:
                acct_min = float((a.get("minDealSize", {}) or {}).get("value", 0) or 0)
            except (TypeError, ValueError):
                pass
            out.append(Account(
                account_id=a.get("accountId", ""),
                account_name=a.get("accountName", ""),
                account_type=a.get("accountType", "CFD"),
                balance=balance,
                deposit=deposit,
                profit_loss=pl,
                available=avail,
                currency=a.get("balance", {}).get("currency", "USD"),
                leverage=int(a.get("leverage", {}).get("leverage", 30)),
                demo=self.demo,
                min_deal_size=acct_min,
            ))
        self.accounts = out
        return out

    def primary_account(self) -> Optional[Account]:
        if not self.accounts:
            return None
        if self.preferred_account_id:
            wanted = str(self.preferred_account_id).strip()
            for a in self.accounts:
                if a.account_id == wanted:
                    return a
            logger.warning(
                "CAPITAL_ACCOUNT_ID=%s didn't match any account. Available: %s. "
                "Falling back to first account.",
                wanted, [a.account_id for a in self.accounts],
            )
        return self.accounts[0]

    async def get_account(self) -> Account:
        if not self.accounts:
            await self.refresh_accounts()
        a = self.primary_account()
        if a is None:
            # demo fallback so the UI keeps rendering
            return Account("DEMO", "Demo", "CFD", 10_000.0, 10_000.0, 124.5, 10_124.5, "USD", 30, True)
        return a

    async def search_markets(self, query: str) -> list[dict]:
        """Search for instrument epics by name. Returns [{epic, instrumentName, type}]."""
        if not self._client:
            return []
        url = f"{self.base}/api/v1/markets?searchTerm={query}"
        r = await self._client.get(url, headers=self._headers())
        if r.status_code != 200:
            logger.error("markets search failed (%d): %s", r.status_code, r.text[:200])
            return []
        return r.json().get("markets", [])

    async def get_market_info(self, epic: str) -> Optional[SymbolInfo]:
        if not self._client:
            return None
        # Capital.com's new API uses numeric instrument IDs in some endpoints.
        # Try the markets search first to get the ID, then use /marketinfo/{id}.
        # Fall back to epic if the ID-based path also 404s (older API behavior).
        candidate_paths = [f"/api/v1/marketinfo/{epic}"]
        try:
            markets = await self.search_markets(epic)
            for m in markets:
                if m.get("epic", "").upper() == epic.upper() and m.get("instrumentId"):
                    candidate_paths.insert(0, f"/api/v1/marketinfo/{m['instrumentId']}")
                    break
        except Exception:
            pass
        d: dict = {}
        for path in candidate_paths:
            r = await self._client.get(f"{self.base}{path}", headers=self._headers())
            if r.status_code == 200:
                d = r.json()
                break
            logger.warning("marketinfo %s → %d, trying next", path, r.status_code)
        if not d:
            logger.error("marketinfo failed for %s after trying %d paths", epic, len(candidate_paths))
            return None
        ins = d.get("instrument", {})
        rules = d.get("dealingRules", {})
        snapshot = d.get("snapshot", {})
        bid = snapshot.get("bid", 0.0)
        offer = snapshot.get("offer", 0.0)
        margin = 20.0
        try:
            margin = float(ins.get("marginFactor", ins.get("marginDepositFactor", 20)))
        except (TypeError, ValueError):
            pass
        digits = int(ins.get("decimalPlaces", 5))
        point = float(ins.get("onePipMeans", "0.0001"))
        if point == 0:
            point = 0.0001 if digits == 5 else 0.01
        # dealingRules.minDealSize.unit must equal "LOTS" for our size to be in lots
        # (some instruments use "SHARES" for share-dealing).
        min_unit = (rules.get("minDealSize", {}) or {}).get("unit", "LOTS")
        min_value = float(rules.get("minDealSize", {}).get("value", 0.1))
        max_value = float(rules.get("maxDealSize", {}).get("value", 1000.0))
        step = float(rules.get("minStepDistance", {}).get("value", 0.0)) or 0.01
        min_stop = float(rules.get("minStopDistance", {}).get("value", 0.0) or 0.0)
        # For SHARES, 1 lot = 1 share, so min is reported in shares.
        # Capital.com's /positions endpoint accepts size in lots (= shares for share-dealing).
        if min_unit == "SHARES" and min_value < 1:
            min_value = 1.0  # you can't buy fractional shares on Capital.com demo
        return SymbolInfo(
            epic=epic,
            name=ins.get("name", epic),
            instrument_type=ins.get("type", "CURRENCIES"),
            digits=digits,
            point=point,
            min_deal_size=min_value,
            max_deal_size=max_value,
            step=step,
            bid=bid,
            offer=offer,
            spread=snapshot.get("spread", 0),
            currency=ins.get("currencies", ["USD"])[0] if ins.get("currencies") else "USD",
            margin_factor=margin,
            min_stop_distance=min_stop,
        )

    async def get_ohlcv(self, epic: str, resolution: str = "HOUR", count: int = 500) -> list[dict]:
        """Get OHLCV bars. resolution: MINUTE, MINUTE_5, MINUTE_15, MINUTE_30, HOUR, HOUR_4, DAY, WEEK."""
        if not self._client:
            return []
        url = f"{self.base}/api/v1/prices/{epic}?resolution={resolution}&max={count}"
        r = await self._client.get(url, headers=self._headers())
        if r.status_code != 200:
            logger.error("prices failed for %s (%d): %s", epic, r.status_code, r.text[:200])
            return []
        d = r.json()
        prices = d.get("prices", [])
        if not prices:
            logger.warning("prices for %s returned empty array (market may be closed)", epic)
            return []
        out: list[dict] = []

        def _extract_price(field) -> float:
            """Extract a price from Capital.com's nested dict.

            Shape: {"bid": 1.085, "ask": 1.0851, "lastTraded": 1.08505}
            Some fields return just a number (e.g. for snapshot).
            Prefer lastTraded, fall back to mid of bid/ask, then bid alone.
            """
            if isinstance(field, dict):
                lt = field.get("lastTraded")
                if lt is not None:
                    try:
                        v = float(lt)
                        if v > 0:
                            return v
                    except (TypeError, ValueError):
                        pass
                bid = field.get("bid")
                ask = field.get("ask")
                try:
                    bid_f = float(bid) if bid is not None else None
                    ask_f = float(ask) if ask is not None else None
                    if bid_f and ask_f and bid_f > 0 and ask_f > 0:
                        return (bid_f + ask_f) / 2
                    if bid_f and bid_f > 0:
                        return bid_f
                except (TypeError, ValueError):
                    pass
                return 0.0
            try:
                return float(field)
            except (TypeError, ValueError):
                return 0.0

        for p in prices:
            o = _extract_price(p.get("openPrice", {}))
            h = _extract_price(p.get("highPrice", {}))
            l = _extract_price(p.get("lowPrice", {}))
            c = _extract_price(p.get("closePrice", {}))
            t = p.get("snapshotTime", "")
            # Drop bars where close is zero — they poison indicators downstream
            if c == 0:
                continue
            out.append({
                "time": int(_parse_iso_time(t)),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 0,  # Capital.com doesn't return tick volume
            })
        if out:
            logger.info("OHLCV %s %s: %d bars (last close=%s)",
                       epic, resolution, len(out), f"{out[-1]['close']:.5f}")
        return out

    async def get_positions(self) -> list[Position]:
        if not self._client:
            return []
        r = await self._client.get(f"{self.base}/api/v1/positions", headers=self._headers())
        if r.status_code != 200:
            logger.error("positions fetch failed (%d): %s", r.status_code, r.text[:200])
            return []
        data = r.json()
        out: list[Position] = []
        for p in data.get("positions", []):
            pos = p.get("position", {})
            out.append(Position(
                deal_id=pos.get("dealId", ""),
                epic=pos.get("epic", ""),
                direction=pos.get("direction", "BUY"),
                size=float(pos.get("size", 0.0)),
                level=float(pos.get("level", 0.0)),
                stop_level=pos.get("stopLevel"),
                limit_level=pos.get("limitLevel"),
                profit=float(pos.get("profit", 0.0)),
                currency=pos.get("currency", "USD"),
            ))
        return out

    async def place_order(
        self,
        epic: str,
        direction: str,  # "BUY" or "SELL"
        size: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        guaranteed_stop: bool = False,
        account_id: Optional[str] = None,
    ) -> dict:
        if not self._client:
            return {"error": "not connected"}
        # Resolve which account to trade on.
        # 1) explicit per-request override wins
        # 2) else the preferred account from env (self.primary_account)
        # 3) else error — never silently place on the wrong account
        acct: Optional[Account] = None
        if account_id is not None:
            for a in self.accounts:
                if a.account_id == str(account_id):
                    acct = a
                    break
            if acct is None:
                return {"error": f"account_id {account_id!r} not found on this session"}
        if acct is None:
            acct = self.primary_account()
        if acct is None:
            return {"error": "no account available — login may have failed"}

        # Fetch market info once; reuse for both the stop guard and the size guard
        # below. Skip the call entirely if neither guard would use it.
        info = None
        if sl is not None or size > 0:
            info = await self.get_market_info(epic)

        # Stop-loss minimum-distance guard
        # Capital.com enforces a per-symbol min stop distance (e.g. ~810 pts for BTCUSD).
        # If the API returns 0 (field missing), fall back to a hardcoded conservative floor
        # per instrument type to avoid "error.invalid.stoploss.minvalue" rejections.
        min_stop = info.min_stop_distance if info else 0.0
        if min_stop <= 0 and sl is not None and info is not None:
            # Hardcoded floors when Capital.com omits minStopDistance from market info.
            # Values are conservative — well above what any instrument actually requires.
            instrument = info.instrument_type.lower()
            entry_price = info.offer if direction.upper() == "BUY" else info.bid
            epic_upper = epic.upper()
            if "crypto" in instrument or "BITCOIN" in epic_upper or "BTC" in epic_upper:
                # Crypto: 1% of price as a safe minimum stop distance in points.
                min_stop = max(min_stop, entry_price * 0.01)
            elif "INDICES" in instrument or "INDEX" in instrument:
                min_stop = max(min_stop, 500.0)  # indices often require 500+ point stops
            elif "COMMODITY" in instrument or "metals" in instrument.lower():
                min_stop = max(min_stop, 50.0)   # commodities: 50 point floor
        if sl is not None and min_stop > 0 and info is not None:
            # Entry price: offer for BUY (long), bid for SELL (short)
            entry_price = info.offer if direction.upper() == "BUY" else info.bid
            stop_dist = abs(float(sl) - entry_price)
            if stop_dist < min_stop:
                logger.warning(
                    f"Stop distance {stop_dist} < min {min_stop} for {epic}. "
                    f"Adjusting stop outward."
                )
                if direction.upper() == "BUY":
                    # LONG: move stop away from entry (lower)
                    sl = entry_price - min_stop
                else:
                    # SHORT: move stop away from entry (higher)
                    sl = entry_price + min_stop

        # Size minimum guard — Capital.com returns error.invalid.size.minvalue
        # when the requested lot size is below the minimum.
        # There are TWO sources of truth: symbol-level min (from /marketinfo) and
        # account-level min (from /accounts, e.g. 1.0 lots for many demo forex accounts).
        # We must take the MAX so the order always satisfies both broker constraints.
        # Round UP to the min, snapped to the next step multiple.
        symbol_min = info.min_deal_size if info and info.min_deal_size > 0 else 0.0
        acct_min = acct.min_deal_size if acct.min_deal_size > 0 else 0.0
        broker_min = max(symbol_min, acct_min)
        broker_step = info.step if info and info.step > 0 else 0.01
        if broker_min > 0 and size < broker_min:
            adjusted = math.ceil(broker_min / broker_step) * broker_step
            logger.warning(
                f"Requested size {size} < min {broker_min} (symbol={symbol_min}, account={acct_min}) "
                f"for {epic}. Bumping up to {adjusted} lots."
            )
            size = adjusted
        elif broker_min == 0 and size < 0.5:
            # No broker info available — use a conservative 0.5-lot floor so
            # we don't get a minvalue rejection. Capital.com's smallest
            # realistic min is around 0.5 lots for most retail instruments.
            logger.warning(
                f"No market info for {epic}; bumping size {size} to 0.5 lots as a safe floor."
            )
            size = 0.5

        body: dict[str, Any] = {
            "epic": epic,
            "direction": direction.upper(),
            "size": size,
            "orderType": "MARKET",
            "guaranteedStop": guaranteed_stop,
            "accountId": acct.account_id,
        }
        if sl is not None:
            body["stopLevel"] = float(sl)
        if tp is not None:
            body["limitLevel"] = float(tp)
        # Try the order POST. Capital.com sessions expire (~6h); if the token is
        # stale the API returns {"errorCode": "error.invalid.session.token"}. We
        # re-login once and retry exactly one time. Any other error code keeps
        # the original failure path so callers see the real cause.
        r = await self._client.post(f"{self.base}/api/v1/positions", headers=self._headers(), json=body)
        if r.status_code not in (200, 201):
            err_code = ""
            try:
                err_code = (r.json() or {}).get("errorCode", "")
            except Exception:
                pass
            if err_code == "error.invalid.session.token":
                logger.info("Capital.com session token expired on place_order; re-logging in")
                if await self.login():
                    r = await self._client.post(f"{self.base}/api/v1/positions", headers=self._headers(), json=body)
                else:
                    logger.error("place_order re-login failed; returning original session error")
            if r.status_code not in (200, 201):
                logger.error("place_order failed (%d): %s", r.status_code, r.text[:300])
                return {"error": f"HTTP {r.status_code}", "detail": r.text[:500]}
        d = r.json()
        ref = d.get("dealReference", "")
        # confirm
        if ref:
            conf = await self._client.get(f"{self.base}/api/v1/confirms/{ref}", headers=self._headers())
            if conf.status_code == 200:
                cd = conf.json()
                return {
                    "deal_id": cd.get("dealId", ""),
                    "epic": cd.get("epic", epic),
                    "direction": cd.get("direction", direction),
                    "size": cd.get("size", size),
                    "level": cd.get("level", 0.0),
                    "status": cd.get("status", "OPEN"),
                }
        return {"dealReference": ref, "status": "SUBMITTED"}


def _parse_iso_time(s: str) -> int:
    """Parse ISO8601 timestamp to unix seconds (best-effort)."""
    if not s:
        return int(time.time())
    try:
        # Capital.com format: /Date(1700000000000)/
        if s.startswith("/Date(") and s.endswith(")/"):
            return int(s[6:-2]) // 1000
        # ISO 8601 fallback
        from datetime import datetime
        if "T" in s:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        pass
    return int(time.time())


# ─── Symbol resolution helper ─────────────────────────────────────────────────

async def resolve_epic(api: CapitalAPI, user_symbol: str) -> str:
    """Map a user-friendly symbol (EURUSD) to a Capital.com epic."""
    mapping = {
        "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "USDJPY": "USDJPY",
        "AUDUSD": "AUDUSD", "NZDUSD": "NZDUSD", "USDCAD": "USDCAD",
        "USDCHF": "USDCHF", "EURJPY": "EURJPY", "GBPJPY": "GBPJPY",
        "XAUUSD": "GOLD", "XAGUSD": "SILVER", "BTCUSD": "BTCUSD",
        "ETHUSD": "ETHUSD", "US500": "US500", "US100": "US100",
        "US30": "US30", "DE40": "DE40", "UK100": "UK100",
        "JP225": "JP225", "AAPL": "AAPL", "TSLA": "TSLA",
        "NVDA": "NVDA", "META": "META", "MSFT": "MSFT",
        "GOOG": "GOOG", "AMZN": "AMZN",
        # Commodities — Capital.com epic codes; verify if a search lands the wrong one.
        "CRUDEOIL": "OILUSD", "USOIL": "OILUSD", "WTI": "OILUSD",
        "WTICUSD": "OILUSD", "OILUSD": "OILUSD",
        "UKOIL": "OILCUSD", "BRENT": "OILCUSD", "OILCUSD": "OILCUSD",
        "NATGAS": "NATGASUSD", "NATGASUSD": "NATGASUSD",
        "COPPER": "COPPER", "COPPERUSD": "COPPER",
        "WHEAT": "WHEATUSD", "WHEATUSD": "WHEATUSD",
        "CORN": "CORNUSD", "CORNUSD": "CORNUSD",
    }
    upper = user_symbol.upper()
    if upper in mapping:
        return mapping[upper]
    # try search
    markets = await api.search_markets(user_symbol)
    if markets:
        return markets[0].get("epic", upper)
    return upper


# ─── Position sizer ───────────────────────────────────────────────────────────

def compute_size_capital(
    balance: float,
    risk_pct: float,
    entry: float,
    sl_price: float,
    margin_factor: float = 20.0,
    min_size: float = 0.1,
    max_size: float = 1000.0,
    step: float = 0.01,
    point: float = 0.0001,
) -> float:
    """Position sizer for Capital.com. risk_pct fraction of balance."""
    sl_dist = abs(entry - sl_price)
    if sl_dist <= 0:
        raise ValueError("entry and sl must differ")
    risk_dollars = balance * risk_pct
    # pip value approximation
    raw = risk_dollars / sl_dist
    # clamp to step
    steps = raw / step
    lots = max(min_size, min(max_size, round(steps) * step))
    return float(round(lots, 8))


# ─── Smoke test ───────────────────────────────────────────────────────────────

async def _main():  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    async with CapitalAPI(demo=True) as api:
        if not await api.login():
            print("Login failed — set CAPITAL_API_KEY, CAPITAL_LOGIN, CAPITAL_PASSWORD")
            return
        acct = await api.get_account()
        print(f"Account: {acct.balance:.2f} {acct.currency} (avail {acct.available:.2f})")
        positions = await api.get_positions()
        print(f"Open positions: {len(positions)}")
        for p in positions:
            print(f"  {p.epic} {p.direction} {p.size} @ {p.level}, P&L {p.profit:.2f}")
        # Test market search
        markets = await api.search_markets("EUR")
        print(f"Found {len(markets)} EUR markets")
        for m in markets[:5]:
            print(f"  {m.get('epic')} — {m.get('instrumentName')}")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_main())
