"""MT5 connector for Capital.com / any MetaTrader 5 broker.

Falls back to deterministic demo mode when the terminal is not running.
"""

import os
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as MT5
except ImportError:
    MT5 = None  # type: ignore[assignment]


DEMO = False


@dataclass
class Account:
    login: int
    balance: float
    equity: float
    margin: float
    free_margin: float
    profit: float
    currency: str
    leverage: int
    server: str
    demo: bool = False


@dataclass
class SymbolInfo:
    name: str
    digits: int
    point: float
    volume_min: float
    volume_max: float
    volume_step: float
    contract_size: float
    spread: int
    currency_profit: str


def connect(
    login: int | None = None,
    password: str | None = None,
    server: str | None = None,
) -> bool:
    """Initialize MT5. Returns True on success; sets DEMO flag on failure."""
    global DEMO
    if MT5 is None:
        logger.warning("MetaTrader5 package not installed — DEMO mode")
        DEMO = True
        return False

    kwargs: dict[str, Any] = {}
    if login is not None:
        kwargs["login"] = int(login)
    if password is not None:
        kwargs["password"] = password
    if server is not None:
        kwargs["server"] = server

    ok = MT5.initialize(**kwargs)
    if not ok:
        logger.warning("MT5.initialize failed (%s) — DEMO mode", MT5.last_error())
        DEMO = True
        return False
    DEMO = False
    info = MT5.terminal_info()
    logger.info("MT5 connected: %s", info.name if info else "?")
    return True


def connect_from_env() -> bool:
    login = os.environ.get("MT5_LOGIN")
    password = os.environ.get("MT5_PASSWORD")
    server = os.environ.get("MT5_SERVER")
    if not (login and password and server):
        logger.info("MT5_* env vars not all set — DEMO mode")
        global DEMO
        DEMO = True
        return False
    return connect(int(login), password, server)


def shutdown() -> None:
    if MT5 is not None and not DEMO:
        MT5.shutdown()


def get_account() -> Account:
    if DEMO or MT5 is None:
        return Account(
            login=100000,
            balance=10_000.0,
            equity=10_124.50,
            margin=320.0,
            free_margin=9_804.50,
            profit=124.50,
            currency="USD",
            leverage=30,
            server="Capital.com-Demo",
            demo=True,
        )
    info = MT5.account_info()
    if info is None:
        raise RuntimeError(f"MT5.account_info failed: {MT5.last_error()}")
    return Account(
        login=info.login,
        balance=info.balance,
        equity=info.equity,
        margin=info.margin,
        free_margin=info.margin_free,
        profit=info.profit,
        currency=info.currency,
        leverage=info.leverage,
        server=info.server,
        demo=False,
    )


def get_symbol_info(symbol: str) -> SymbolInfo:
    if DEMO or MT5 is None:
        is_forex = "USD" in symbol and len(symbol) == 6
        is_crypto = "BTC" in symbol or "ETH" in symbol
        if is_forex:
            return SymbolInfo(symbol, 5, 0.00001, 0.01, 100.0, 0.01, 100_000, 10, "USD")
        if is_crypto:
            return SymbolInfo(symbol, 2, 0.01, 0.01, 50.0, 0.01, 1, 80, "USD")
        return SymbolInfo(symbol, 2, 0.01, 0.1, 1000.0, 0.1, 1, 2, "USD")

    info = MT5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Unknown symbol: {symbol}")
    return SymbolInfo(
        name=info.name,
        digits=info.digits,
        point=info.point,
        volume_min=info.volume_min,
        volume_max=info.volume_max,
        volume_step=info.volume_step,
        contract_size=info.trade_contract_size,
        spread=info.spread,
        currency_profit=info.currency_profit,
    )


def get_positions() -> list[dict]:
    if DEMO or MT5 is None:
        return [
            {
                "ticket": 1234567,
                "symbol": "EURUSD",
                "type": "buy",
                "volume": 0.5,
                "price_open": 1.0820,
                "sl": 1.0790,
                "tp": 1.0890,
                "profit": 124.50,
                "time": int(time.time()) - 3600,
            }
        ]
    positions = MT5.positions_get()
    if positions is None:
        return []
    return [
        {
            "ticket": p.ticket,
            "symbol": p.symbol,
            "type": "buy" if p.type == 0 else "sell",
            "volume": p.volume,
            "price_open": p.price_open,
            "sl": p.sl,
            "tp": p.tp,
            "profit": p.profit,
            "time": p.time,
        }
        for p in positions
    ]


_TF_MAP: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 16385,
    "H4": 16388,
    "D1": 16408,
    "W1": 32769,
    "MN": 49153,
}


def get_ohlcv(symbol: str, timeframe: str = "H1", count: int = 500) -> list[dict]:
    """Get OHLCV bars. timeframe in {M1,M5,M15,M30,H1,H4,D1,W1,MN}."""
    tf = _TF_MAP.get(timeframe, 16385)
    if DEMO or MT5 is None:
        return _synth_bars(symbol, count, timeframe)
    rates = MT5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None:
        logger.warning("copy_rates_from_pos failed for %s — synth", symbol)
        return _synth_bars(symbol, count, timeframe)
    return [
        {
            "time": int(r["time"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": int(r["tick_volume"]),
        }
        for r in rates
    ]


def compute_size(
    symbol: str,
    account_balance: float,
    risk_pct: float,
    entry: float,
    sl_price: float,
) -> float:
    """Kelly-lite position sizer. Returns lot size clamped to symbol limits."""
    info = get_symbol_info(symbol)
    sl_dist = abs(entry - sl_price)
    if sl_dist <= 0:
        raise ValueError("entry and sl_price must differ")
    risk_dollars = account_balance * risk_pct
    raw = risk_dollars / (sl_dist * info.contract_size)
    steps = raw / info.volume_step
    lots = max(info.volume_min, min(info.volume_max, round(steps) * info.volume_step))
    return float(round(lots, 8))


def place_order(
    symbol: str,
    side: str,
    lots: float,
    sl: float | None = None,
    tp: float | None = None,
    comment: str = "fcc_trader",
) -> dict:
    if DEMO or MT5 is None:
        return {
            "retcode": 10009,
            "ticket": random.randint(100_000, 9_999_999),
            "volume": lots,
            "price": 0.0,
            "comment": "DEMO ORDER",
            "demo": True,
        }
    tick = MT5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError(f"No tick for {symbol}")
    price = tick.ask if side == "buy" else tick.bid
    request: dict[str, Any] = {
        "action": MT5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lots,
        "type": MT5.ORDER_TYPE_BUY if side == "buy" else MT5.ORDER_TYPE_SELL,
        "price": price,
        "deviation": 20,
        "magic": 7770001,
        "comment": comment,
        "type_time": MT5.ORDER_TIME_GTC,
        "type_filling": MT5.ORDER_FILLING_FOK,
    }
    if sl is not None:
        request["sl"] = float(sl)
    if tp is not None:
        request["tp"] = float(tp)
    result = MT5.order_send(request)
    if result is None:
        raise RuntimeError(f"order_send returned None: {MT5.last_error()}")
    return {
        "retcode": result.retcode,
        "ticket": result.order,
        "volume": result.volume,
        "price": result.price,
        "comment": result.comment,
        "demo": False,
    }


# ─── Deterministic synthetic bars ────────────────────────────────────────────

def _synth_bars(symbol: str, count: int, timeframe: str) -> list[dict]:
    rng = random.Random(hash(symbol + timeframe) & 0xFFFFFFFF)
    if "USD" in symbol and len(symbol) == 6:
        base, step = 1.08, 0.0005
    elif "BTC" in symbol:
        base, step = 65_000.0, 80.0
    elif "XAU" in symbol:
        base, step = 2_330.0, 2.0
    else:
        base, step = 100.0, 0.5
    now = int(time.time())
    secs = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}.get(timeframe, 3600)
    out: list[dict] = []
    price = base
    for i in range(count):
        t = now - (count - i) * secs
        drift = rng.gauss(0, step)
        o = price
        c = max(0.01, o + drift)
        h = max(o, c) + abs(rng.gauss(0, step * 0.5))
        l = min(o, c) - abs(rng.gauss(0, step * 0.5))
        v = int(rng.gauss(50_000, 15_000))
        out.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": max(v, 1000)})
        price = c
    return out


# ─── CLI smoke test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    connect_from_env()
    acct = get_account()
    sym = get_symbol_info("EURUSD")
    bars = get_ohlcv("EURUSD", "H1", 200)
    size = compute_size("EURUSD", acct.balance, 0.01, 1.0850, 1.0820)
    print(f"Account: {acct.balance:.2f} {acct.currency} (equity {acct.equity:.2f}, demo={acct.demo})")
    print(f"EURUSD digits={sym.digits} contract={sym.contract_size}")
    print(f"200 H1 bars: first={bars[0]['close']:.5f} last={bars[-1]['close']:.5f}")
    print(f"Size @ 1% risk: {size} lots")
