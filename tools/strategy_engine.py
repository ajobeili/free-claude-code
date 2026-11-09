"""Trading strategy engine — 8 strategies + backtest engine.

Pure numpy-free. Bars: [{"time", "open", "high", "low", "close", "volume"}, ...]
"""

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC
from typing import Literal, TypedDict

# ─── Regime detection ─────────────────────────────────────────────────────────
# Each strategy declares which regimes it should fire in. Strategies whose
# regime doesn't match the current market get suppressed (None returned).
# Regime is computed from the bar series — see _classify_regime() below.

_REGIME_TRENDING = "trending"
_REGIME_RANGING = "ranging"
_REGIME_VOLATILE = "volatile"
_REGIME_CALM = "calm"

# strategy -> allowed regimes (None = any)
STRATEGY_REGIMES: dict[str, set[str] | None] = {
    "momentum_breakout": {_REGIME_TRENDING, _REGIME_VOLATILE},
    "mean_reversion_rsi_bb": {_REGIME_RANGING, _REGIME_CALM},
    "order_flow_volume": None,  # volume signals fire in any regime
    "breakout_volatility": {_REGIME_VOLATILE, _REGIME_CALM},
    "trend_ema_pullback": {_REGIME_TRENDING},
    "scalping_volatility": None,  # multi-mode — already self-gates
    "fib_retracement": {_REGIME_TRENDING, _REGIME_RANGING},
    "fvg_retracement": None,  # FVG validates its own displacement/retest
}


def _classify_regime(bars: list[dict]) -> str:
    """Classify the last 100 bars into one regime bucket.

    Uses ADX for trend strength and BB-width (relative to its own history) for
    volatility state. Combinations map to one of 4 buckets.
    """
    if len(bars) < 100:
        return _REGIME_CALM  # insufficient data — let signals through
    adx = compute_adx(bars, 14)
    bb = compute_bb(bars, 20, 2.0)
    i = len(bars) - 1
    a = adx[i]
    w = bb["width"][i]
    if math.isnan(a) or math.isnan(w):
        return _REGIME_CALM
    # Compute BB-width relative to its own 100-bar median.
    widths = [
        bb["width"][j]
        for j in range(max(0, i - 99), i + 1)
        if not math.isnan(bb["width"][j])
    ]
    if len(widths) < 50:
        return _REGIME_CALM
    median_w = sorted(widths)[len(widths) // 2]
    if median_w <= 0:
        return _REGIME_CALM
    width_ratio = w / median_w
    is_trending = a >= 22
    is_volatile = width_ratio >= 1.25
    if is_trending and is_volatile:
        return _REGIME_TRENDING  # trending + expanding — breakouts likely
    if is_trending:
        return _REGIME_TRENDING
    if is_volatile:
        return _REGIME_VOLATILE
    # ADX < 22 and width < 1.25x median — quiet / ranging
    return _REGIME_RANGING


# ─── Indicator helpers ────────────────────────────────────────────────────────


def _closes(bars: list[dict]) -> list[float]:
    return [b["close"] for b in bars]


def _highs(bars: list[dict]) -> list[float]:
    return [b["high"] for b in bars]


def _lows(bars: list[dict]) -> list[float]:
    return [b["low"] for b in bars]


def _vols(bars: list[dict]) -> list[float]:
    return [b["volume"] for b in bars]


def compute_ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [math.nan] * len(values)
    k = 2.0 / (period + 1)
    out: list[float] = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def compute_sma(values: list[float], period: int) -> list[float]:
    out: list[float] = []
    for i in range(len(values)):
        if i + 1 < period:
            out.append(math.nan)
        else:
            out.append(sum(values[i + 1 - period : i + 1]) / period)
    return out


def compute_atr(bars: list[dict], period: int = 14) -> list[float]:
    if len(bars) < 2:
        return [math.nan] * len(bars)
    trs: list[float] = [bars[0]["high"] - bars[0]["low"]]
    for i in range(1, len(bars)):
        h, low, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))
    out: list[float] = [math.nan] * len(trs)
    if len(trs) < period:
        return out
    first = sum(trs[:period]) / period
    out[period - 1] = first
    for i in range(period, len(trs)):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period
    return out


def compute_rsi(bars: list[dict], period: int = 14) -> list[float]:
    cs = _closes(bars)
    if len(cs) < period + 1:
        return [math.nan] * len(cs)
    gains = [0.0] + [max(cs[i] - cs[i - 1], 0) for i in range(1, len(cs))]
    losses = [0.0] + [max(cs[i - 1] - cs[i], 0) for i in range(1, len(cs))]
    out: list[float] = [math.nan] * len(cs)
    avg_g = sum(gains[1 : period + 1]) / period
    avg_l = sum(losses[1 : period + 1]) / period
    rs = avg_g / avg_l if avg_l > 0 else float("inf")
    out[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period + 1, len(cs)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = avg_g / avg_l if avg_l > 0 else float("inf")
        out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def compute_bb(bars: list[dict], period: int = 20, num_std: float = 2.0) -> dict:
    cs = _closes(bars)
    mid = compute_sma(cs, period)
    upper: list[float] = []
    lower: list[float] = []
    width: list[float] = []
    for i in range(len(cs)):
        if i + 1 < period:
            upper.append(math.nan)
            lower.append(math.nan)
            width.append(math.nan)
            continue
        window = cs[i + 1 - period : i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        s = var**0.5
        upper.append(m + num_std * s)
        lower.append(m - num_std * s)
        width.append(upper[-1] - lower[-1])
    return {"upper": upper, "middle": mid, "lower": lower, "width": width}


def compute_adx(bars: list[dict], period: int = 14) -> list[float]:
    if len(bars) < period * 2:
        return [math.nan] * len(bars)
    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    trs: list[float] = [0.0]
    for i in range(1, len(bars)):
        up = bars[i]["high"] - bars[i - 1]["high"]
        dn = bars[i - 1]["low"] - bars[i]["low"]
        plus_dm.append(max(up, 0) if up > dn and up > 0 else 0.0)
        minus_dm.append(max(dn, 0) if dn > up and dn > 0 else 0.0)
        h, low, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))

    def _wilder(arr: list[float], p: int) -> list[float]:
        out = [0.0] * len(arr)
        if len(arr) < p:
            return out
        out[p - 1] = sum(arr[:p])
        for i in range(p, len(arr)):
            out[i] = out[i - 1] - out[i - 1] / p + arr[i]
        return out

    tr_n = _wilder(trs, period)
    pdm_n = _wilder(plus_dm, period)
    mdm_n = _wilder(minus_dm, period)
    dx: list[float] = [0.0] * len(bars)
    for i in range(period - 1, len(bars)):
        if tr_n[i] == 0:
            continue
        pdi = 100 * pdm_n[i] / tr_n[i]
        mdi = 100 * mdm_n[i] / tr_n[i]
        dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0

    out: list[float] = [math.nan] * len(bars)
    seed_i = period * 2 - 2
    if seed_i < len(bars):
        out[seed_i] = sum(dx[period - 1 : seed_i + 1]) / period
        for i in range(seed_i + 1, len(bars)):
            out[i] = (out[i - 1] * (period - 1) + dx[i]) / period
    return out


def compute_donchian(bars: list[dict], period: int = 20) -> dict:
    hs = _highs(bars)
    ls = _lows(bars)
    upper: list[float] = [math.nan] * len(bars)
    lower: list[float] = [math.nan] * len(bars)
    for i in range(period - 1, len(bars)):
        upper[i] = max(hs[i + 1 - period : i + 1])
        lower[i] = min(ls[i + 1 - period : i + 1])
    return {"upper": upper, "lower": lower}


# ─── Signal dataclass ─────────────────────────────────────────────────────────


@dataclass
class Signal:
    strategy: str
    symbol: str
    direction: Literal["long", "short"]
    entry: float
    stop_loss: float
    take_profit: float
    confidence: float
    reasoning: str
    atr: float

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Strategy functions ───────────────────────────────────────────────────────


def momentum_breakout(bars: list[dict], symbol: str = "?") -> Signal | None:
    if len(bars) < 60:
        return None
    i = len(bars) - 1
    last = bars[i]
    donch = compute_donchian(bars, 20)
    adx = compute_adx(bars, 14)
    atr = compute_atr(bars, 14)
    vols = _vols(bars)
    avg_vol = sum(vols[i - 20 : i]) / 20

    a = atr[i]
    adx_v = adx[i]
    if math.isnan(a) or math.isnan(adx_v) or avg_vol <= 0:
        return None
    if adx_v < 20 or last["volume"] < 1.5 * avg_vol:
        return None

    up = donch["upper"][i - 1]
    dn = donch["lower"][i - 1]
    if math.isnan(up) or math.isnan(dn):
        return None

    if last["close"] > up and last["high"] > up:
        entry = last["close"]
        sl = round(entry - 2 * a, 8)
        tp = round(entry + 3 * a, 8)
        conf = min(1.0, 0.5 + (adx_v - 20) / 50)
        return Signal(
            "momentum_breakout",
            symbol,
            "long",
            entry,
            sl,
            tp,
            conf,
            f"20-bar breakout (high={up:.5f}) on {last['volume'] / avg_vol:.1f}x vol, ADX={adx_v:.0f}",
            a,
        )
    if last["close"] < dn and last["low"] < dn:
        entry = last["close"]
        sl = round(entry + 2 * a, 8)
        tp = round(entry - 3 * a, 8)
        conf = min(1.0, 0.5 + (adx_v - 20) / 50)
        return Signal(
            "momentum_breakout",
            symbol,
            "short",
            entry,
            sl,
            tp,
            conf,
            f"20-bar breakdown (low={dn:.5f}) on {last['volume'] / avg_vol:.1f}x vol, ADX={adx_v:.0f}",
            a,
        )
    return None


def mean_reversion_rsi_bb(bars: list[dict], symbol: str = "?") -> Signal | None:
    if len(bars) < 220:
        return None
    i = len(bars) - 1
    last = bars[i]
    rsi = compute_rsi(bars, 2)
    bb = compute_bb(bars, 20, 2.0)
    ema200 = compute_ema(_closes(bars), 200)
    atr = compute_atr(bars, 14)

    r = rsi[i]
    a = atr[i]
    ema = ema200[i]
    upper, lower, mid = bb["upper"][i], bb["lower"][i], bb["middle"][i]
    if any(math.isnan(x) for x in (r, a, ema, upper)):
        return None

    if r < 5 and last["close"] <= lower and last["close"] > ema:
        entry = last["close"]
        sl = round(entry - 1.5 * a, 8)
        tp_val = round(mid, 8)
        conf = min(1.0, 0.4 + (5 - r) / 20)
        return Signal(
            "mean_reversion_rsi_bb",
            symbol,
            "long",
            entry,
            sl,
            tp_val,
            conf,
            f"RSI(2)={r:.1f} at lower BB, above 200-EMA ({ema:.5f})",
            a,
        )
    if r > 95 and last["close"] >= upper and last["close"] < ema:
        entry = last["close"]
        sl = round(entry + 1.5 * a, 8)
        tp_val = round(mid, 8)
        conf = min(1.0, 0.4 + (r - 95) / 20)
        return Signal(
            "mean_reversion_rsi_bb",
            symbol,
            "short",
            entry,
            sl,
            tp_val,
            conf,
            f"RSI(2)={r:.1f} at upper BB, below 200-EMA ({ema:.5f})",
            a,
        )
    return None


def order_flow_volume(bars: list[dict], symbol: str = "?") -> Signal | None:
    if len(bars) < 30:
        return None
    i = len(bars) - 1
    last = bars[i]
    avg_vol = sum(_vols(bars)[i - 20 : i]) / 20
    atr = compute_atr(bars, 14)
    a = atr[i]
    if math.isnan(a) or avg_vol <= 0 or last["volume"] < 3 * avg_vol:
        return None

    bar_range = last["high"] - last["low"]
    if bar_range <= 0:
        return None
    close_pos = (last["close"] - last["low"]) / bar_range

    if close_pos > 0.7:
        entry = last["close"]
        sl = round(entry - 2 * a, 8)
        tp_val = round(entry + 3 * a, 8)
        conf = min(1.0, 0.45 + (last["volume"] / avg_vol - 3) / 10)
        return Signal(
            "order_flow_volume",
            symbol,
            "long",
            entry,
            sl,
            tp_val,
            conf,
            f"Vol spike {last['volume'] / avg_vol:.1f}x, close at {close_pos:.0%} bar high",
            a,
        )
    if close_pos < 0.3:
        entry = last["close"]
        sl = round(entry + 2 * a, 8)
        tp_val = round(entry - 3 * a, 8)
        conf = min(1.0, 0.45 + (last["volume"] / avg_vol - 3) / 10)
        return Signal(
            "order_flow_volume",
            symbol,
            "short",
            entry,
            sl,
            tp_val,
            conf,
            f"Vol spike {last['volume'] / avg_vol:.1f}x, close at {close_pos:.0%} bar low",
            a,
        )
    return None


def breakout_volatility(bars: list[dict], symbol: str = "?") -> Signal | None:
    if len(bars) < 130:
        return None
    i = len(bars) - 1
    last = bars[i]
    bb = compute_bb(bars, 20, 2.0)
    atr = compute_atr(bars, 14)
    width = bb["width"]
    a = atr[i]
    if math.isnan(width[i]) or math.isnan(a):
        return None
    prior_widths = [w for w in width[i - 100 : i] if not math.isnan(w)]
    if len(prior_widths) < 50:
        return None
    median_w = sorted(prior_widths)[len(prior_widths) // 2]
    if width[i] <= 2 * median_w:
        return None

    upper, lower = bb["upper"][i], bb["lower"][i]
    if last["close"] > upper:
        entry = last["close"]
        sl = round(lower, 8)
        tp_val = round(entry + (upper - lower), 8)
        conf = min(1.0, 0.4 + (width[i] / median_w - 2) / 5)
        return Signal(
            "breakout_volatility",
            symbol,
            "long",
            entry,
            sl,
            tp_val,
            conf,
            f"BB-width expansion {width[i] / median_w:.1f}x median, close above upper",
            a,
        )
    if last["close"] < lower:
        entry = last["close"]
        sl = round(upper, 8)
        tp_val = round(entry - (upper - lower), 8)
        conf = min(1.0, 0.4 + (width[i] / median_w - 2) / 5)
        return Signal(
            "breakout_volatility",
            symbol,
            "short",
            entry,
            sl,
            tp_val,
            conf,
            f"BB-width expansion {width[i] / median_w:.1f}x median, close below lower",
            a,
        )
    return None


def trend_ema_pullback(bars: list[dict], symbol: str = "?") -> Signal | None:
    if len(bars) < 210:
        return None
    i = len(bars) - 1
    last = bars[i]
    cs = _closes(bars)
    ema20 = compute_ema(cs, 20)
    ema50 = compute_ema(cs, 50)
    ema200 = compute_ema(cs, 200)
    rsi = compute_rsi(bars, 14)
    atr = compute_atr(bars, 14)
    e20, e50, e200 = ema20[i], ema50[i], ema200[i]
    r = rsi[i]
    a = atr[i]
    # Guard against NaN AND zero denominators (XAUUSD GOLD can hit e20=0 with bad bars)
    if any(math.isnan(x) or x == 0 for x in (e20, e50, e200, r, a)):
        return None
    if e20 == 0:
        return None
    if abs(last["close"] - e20) / e20 > 0.002:
        return None

    if e20 > e50 > e200 and 40 <= r <= 60:
        entry = last["close"]
        sl = round(e50, 8)
        lookback = max(_highs(bars)[i - 50 : i + 1])
        tp_val = round(max(lookback, entry + 2 * (entry - sl)), 8)
        # e200 guarded non-zero above
        conf = min(0.95, 0.45 + min(0.3, (e20 - e200) / e200 * 50))
        return Signal(
            "trend_ema_pullback",
            symbol,
            "long",
            entry,
            sl,
            tp_val,
            conf,
            f"Uptrend EMAs aligned, pullback to 20-EMA ({e20:.5f}), RSI={r:.0f}",
            a,
        )
    if e20 < e50 < e200 and 40 <= r <= 60:
        entry = last["close"]
        sl = round(e50, 8)
        lookback = min(_lows(bars)[i - 50 : i + 1])
        tp_val = round(min(lookback, entry - 2 * (sl - entry)), 8)
        conf = min(0.95, 0.45 + min(0.3, (e200 - e20) / e200 * 50))
        return Signal(
            "trend_ema_pullback",
            symbol,
            "short",
            entry,
            sl,
            tp_val,
            conf,
            f"Downtrend EMAs aligned, pullback to 20-EMA ({e20:.5f}), RSI={r:.0f}",
            a,
        )
    return None


def scalping_volatility(bars: list[dict], symbol: str = "?") -> Signal | None:
    """Fast in/out scalping strategy for 1-min and 5-min bars.
    Multi-mode entries so something fires in any market condition:
      1. Momentum burst: close breaks 5-bar high with EMA9 > EMA21
      2. Trend pullback: price pulls back to EMA9 with EMA9 > EMA21
      3. Mean reversion: RSI(7) extreme (<25 or >75) at Bollinger band
      4. Donchian breakout: 10-bar high/low break (original)
    ATR(14) x 0.8 SL, ATR(14) x 1.5 TP.
    """
    if len(bars) < 30:
        return None
    i = len(bars) - 1
    last = bars[i]
    cs = _closes(bars)
    ema9 = compute_ema(cs, 9)
    ema21 = compute_ema(cs, 21)
    rsi7 = compute_rsi(bars, 7)
    bb10 = compute_bb(bars, 10, 1.5)
    atr14 = compute_atr(bars, 14)
    if i < 10:
        return None
    hi10 = max(b["high"] for b in bars[i - 10 : i])
    lo10 = min(b["low"] for b in bars[i - 10 : i])
    a = atr14[i]
    e9 = ema9[i]
    e21 = ema21[i]
    r = rsi7[i]
    bw = bb10["width"][i]
    if any(math.isnan(x) for x in (a, e9, e21, r, bw)):
        return None
    if a == 0 or hi10 == lo10:
        return None
    close = last["close"]
    bb_upper = bb10["upper"][i]
    bb_lower = bb10["lower"][i]
    if math.isnan(bb_upper) or math.isnan(bb_lower):
        return None
    # ── Mode 1: momentum burst (5-bar high break + uptrend) ──
    if i >= 5:
        hi5 = max(b["high"] for b in bars[i - 5 : i])
        if close >= hi5 and e9 > e21 and 40 <= r <= 70:
            entry = close
            return Signal(
                "scalping_volatility",
                symbol,
                "long",
                entry,
                round(entry - 0.8 * a, 8),
                round(entry + 1.5 * a, 8),
                0.6,
                f"⚡MOMENTUM 5-bar high break, EMA9>21 (RSI {r:.0f})",
                a,
            )
    # ── Mode 2: trend pullback to EMA9 ──
    if e9 > e21 and abs(close - e9) / e9 < 0.0005 and 45 <= r <= 65:
        entry = close
        return Signal(
            "scalping_volatility",
            symbol,
            "long",
            entry,
            round(entry - 0.8 * a, 8),
            round(entry + 1.5 * a, 8),
            0.55,
            f"⚡PULLBACK touched EMA9, EMA9>21 (RSI {r:.0f})",
            a,
        )
    # ── Mode 3: RSI mean reversion short at upper band ──
    if r > 75 and close >= bb_upper and e9 < e21:
        entry = close
        return Signal(
            "scalping_volatility",
            symbol,
            "short",
            entry,
            round(entry + 0.8 * a, 8),
            round(entry - 1.5 * a, 8),
            0.6,
            f"⚡REVERT RSI{r:.0f}>75 at upper BB",
            a,
        )
    # ── Mode 4: RSI mean reversion long at lower band ──
    if r < 25 and close <= bb_lower and e9 > e21:
        entry = close
        return Signal(
            "scalping_volatility",
            symbol,
            "long",
            entry,
            round(entry - 0.8 * a, 8),
            round(entry + 1.5 * a, 8),
            0.6,
            f"⚡REVERT RSI{r:.0f}<25 at lower BB",
            a,
        )
    # ── Mode 5: short pullback in downtrend ──
    if e9 < e21 and abs(close - e9) / e9 < 0.0005 and 35 <= r <= 55:
        entry = close
        return Signal(
            "scalping_volatility",
            symbol,
            "short",
            entry,
            round(entry + 0.8 * a, 8),
            round(entry - 1.5 * a, 8),
            0.55,
            f"⚡PULLBACK touched EMA9, EMA9<21 (RSI {r:.0f})",
            a,
        )
    # ── Mode 6: classic Donchian 10-bar breakout (last resort) ──
    if close >= hi10 and e9 > e21 and 45 <= r <= 72:
        entry = close
        return Signal(
            "scalping_volatility",
            symbol,
            "long",
            entry,
            round(entry - 0.8 * a, 8),
            round(entry + 1.5 * a, 8),
            0.55,
            f"⚡BREAKOUT 10-bar high, EMA9>21 (RSI {r:.0f})",
            a,
        )
    if close <= lo10 and e9 < e21 and 28 <= r <= 55:
        entry = close
        return Signal(
            "scalping_volatility",
            symbol,
            "short",
            entry,
            round(entry + 0.8 * a, 8),
            round(entry - 1.5 * a, 8),
            0.55,
            f"⚡BREAKOUT 10-bar low, EMA9<21 (RSI {r:.0f})",
            a,
        )
    return None


def fib_retracement(bars: list[dict], symbol: str = "?") -> Signal | None:
    """Fibonacci retracement strategy.

    Anchors fib levels to the most recent swing (50-bar lookback):
      - swing_low  = min low in window
      - swing_high = max high in window
      - Uptrend if swing_high comes after swing_low; downtrend otherwise.

    Long entries: uptrend + price retraces into the 0.5-0.618 demand zone
                  (price currently inside the zone AND the prior bar wicked
                  into it from above) → buy next bar.
    Short entries: mirror image on the supply zone (0.5-0.618 of an
                   upward move from low to high).

    Stop = beyond 0.786 invalidation. Target = 1.0 fib (opposite swing).
    Confidence boosted by EMA20/50 alignment and which level was touched.
    """
    if len(bars) < 80:
        return None
    i = len(bars) - 1
    last = bars[i]
    atr = compute_atr(bars, 14)
    a = atr[i]
    if math.isnan(a) or a == 0:
        return None

    # Lookback window: last 50 bars BEFORE the current one (so swing doesn't
    # include the bar we're about to act on — avoids self-reference).
    lookback = 50
    start = max(0, i - lookback)
    if i - start < 10:
        return None
    highs = _highs(bars[start:i])  # exclude current bar from swing window
    lows = _lows(bars[start:i])
    if not highs or not lows:
        return None

    swing_high = max(highs)
    swing_low = min(lows)
    high_idx = highs.index(swing_high)
    low_idx = lows.index(swing_low)
    if swing_high == swing_low:
        return None
    rng = swing_high - swing_low

    # Trend bias: which extreme came first in the window.
    uptrend = low_idx < high_idx
    ema20 = compute_ema(_closes(bars), 20)
    ema50 = compute_ema(_closes(bars), 50)
    e20, e50 = ema20[i], ema50[i]
    if math.isnan(e20) or math.isnan(e50):
        return None

    close = last["close"]
    # Use the prior bar to detect a wick *into* the zone (better retest signal).
    prev = bars[i - 1]
    tol = 0.3 * a  # tolerance band around the level

    if uptrend:
        # Fib retracement levels (drawn from low → high).
        # 0.5 and 0.618 are the classic demand zones.
        fib_50 = swing_low + 0.5 * rng
        fib_618 = swing_low + 0.618 * rng
        fib_786 = swing_low + 0.786 * rng

        # Long: price currently inside or just above 0.5-0.618 zone, with
        # prior bar wicked into the zone (low touched 0.5 or 0.618 within tol).
        in_zone = (fib_50 - tol) <= close <= (fib_618 + tol)
        prev_wick = (prev["low"] <= fib_618 + tol) and (prev["low"] >= fib_50 - tol)
        # EMA alignment for higher confidence (and required for short SL guard).
        ema_aligned = e20 > e50

        if in_zone and prev_wick and ema_aligned:
            entry = close
            sl = round(fib_786 - 0.25 * a, 8)  # just below 0.786
            # Guard: SL must be BELOW entry for a long. If the swing geometry
            # put fib_786 above the entry (rare but happens on tight swings),
            # fall back to an ATR-based stop.
            if sl >= entry:
                sl = round(entry - 1.5 * a, 8)
            target_1 = round(swing_high, 8)  # 1.0 fib = opposite extreme
            target_2 = round(entry + 2 * (entry - sl), 8)  # 2R
            tp = min(target_1, target_2)
            # Guard: TP must be ABOVE entry for a long.
            if tp <= entry:
                tp = round(entry + 2 * a, 8)
            # Confidence: 0.618 touch > 0.5 touch
            touched_618 = abs(prev["low"] - fib_618) < tol
            base = 0.70 if touched_618 else 0.55
            ema_bonus = min(0.15, (e20 - e50) / e50 * 50) if e50 != 0 else 0
            conf = min(0.95, base + ema_bonus)
            level = "0.618" if touched_618 else "0.500"
            return Signal(
                "fib_retracement",
                symbol,
                "long",
                entry,
                sl,
                tp,
                conf,
                f"Uptrend fib retest of {level} (level={fib_618 if touched_618 else fib_50:.5f}), "
                f"EMA20>EMA50, prev wick into zone",
                a,
            )

    else:
        # Downtrend (high came before low in the window — or close below mid).
        # Fib levels drawn from high → low; 0.5 and 0.618 are supply zones.
        fib_50 = swing_high - 0.5 * rng
        fib_618 = swing_high - 0.618 * rng
        fib_786 = swing_high - 0.786 * rng

        in_zone = (fib_618 - tol) <= close <= (fib_50 + tol)
        prev_wick = (prev["high"] >= fib_618 - tol) and (prev["high"] <= fib_50 + tol)
        ema_aligned = e20 < e50

        if in_zone and prev_wick and ema_aligned:
            entry = close
            sl = round(fib_786 + 0.25 * a, 8)
            # Guard: SL must be ABOVE entry for a short. If the swing geometry
            # put fib_786 below entry (e.g. ETHUSD rally-back case), fall back
            # to an ATR-based stop.
            if sl <= entry:
                sl = round(entry + 1.5 * a, 8)
            target_1 = round(swing_low, 8)
            target_2 = round(entry - 2 * (sl - entry), 8)
            tp = max(target_1, target_2)
            # Guard: TP must be BELOW entry for a short.
            if tp >= entry:
                tp = round(entry - 2 * a, 8)
            touched_618 = abs(prev["high"] - fib_618) < tol
            base = 0.70 if touched_618 else 0.55
            ema_bonus = min(0.15, (e50 - e20) / e50 * 50) if e50 != 0 else 0
            conf = min(0.95, base + ema_bonus)
            level = "0.618" if touched_618 else "0.500"
            return Signal(
                "fib_retracement",
                symbol,
                "short",
                entry,
                sl,
                tp,
                conf,
                f"Downtrend fib retest of {level} (level={fib_618 if touched_618 else fib_50:.5f}), "
                f"EMA20<EMA50, prev wick into zone",
                a,
            )

    return None


def fvg_retracement(bars: list[dict], symbol: str = "?") -> Signal | None:
    """Trade the first controlled retracement into a recent three-candle FVG.

    A bullish FVG spans the first candle's high to the third candle's low; a
    bearish FVG is the inverse. The gap must be materially sized, unfilled,
    and no older than 20 bars. The retest bar must close on the favourable
    side of the midpoint so a full fill is never treated as an entry.

    This is session-agnostic for the multi-market Trader. The TradingView
    script remains the opening-range variant from the referenced video.
    """
    if len(bars) < 20:
        return None

    i = len(bars) - 1
    atr = compute_atr(bars, 14)
    a = atr[i]
    if math.isnan(a) or a <= 0:
        return None

    last = bars[i]
    min_gap = 0.15 * a
    earliest = max(2, i - 20)
    # The third FVG candle must be complete before a later retest can fire.
    for end in range(i - 1, earliest - 1, -1):
        first = bars[end - 2]
        third = bars[end]

        bullish_lower = first["high"]
        bullish_upper = third["low"]
        if bullish_upper - bullish_lower >= min_gap:
            was_filled = any(bar["low"] <= bullish_lower for bar in bars[end + 1 : i])
            midpoint = (bullish_lower + bullish_upper) / 2
            touches_zone = (
                last["low"] <= bullish_upper and last["high"] >= bullish_lower
            )
            holds_favourable_half = (
                last["low"] > bullish_lower and last["close"] >= midpoint
            )
            if not was_filled and touches_zone and holds_favourable_half:
                entry = last["close"]
                stop_loss = round(bullish_lower - 0.25 * a, 8)
                if stop_loss < entry:
                    risk = entry - stop_loss
                    return Signal(
                        "fvg_retracement",
                        symbol,
                        "long",
                        entry,
                        stop_loss,
                        round(entry + 2 * risk, 8),
                        0.65,
                        f"Bullish 3-candle FVG {bullish_lower:.5f}-{bullish_upper:.5f} "
                        f"retested after {i - end} bars; close held above 50%",
                        a,
                    )

        bearish_lower = third["high"]
        bearish_upper = first["low"]
        if bearish_upper - bearish_lower >= min_gap:
            was_filled = any(bar["high"] >= bearish_upper for bar in bars[end + 1 : i])
            midpoint = (bearish_lower + bearish_upper) / 2
            touches_zone = (
                last["high"] >= bearish_lower and last["low"] <= bearish_upper
            )
            holds_favourable_half = (
                last["high"] < bearish_upper and last["close"] <= midpoint
            )
            if not was_filled and touches_zone and holds_favourable_half:
                entry = last["close"]
                stop_loss = round(bearish_upper + 0.25 * a, 8)
                if stop_loss > entry:
                    risk = stop_loss - entry
                    return Signal(
                        "fvg_retracement",
                        symbol,
                        "short",
                        entry,
                        stop_loss,
                        round(entry - 2 * risk, 8),
                        0.65,
                        f"Bearish 3-candle FVG {bearish_lower:.5f}-{bearish_upper:.5f} "
                        f"retested after {i - end} bars; close held below 50%",
                        a,
                    )
    return None


def _regime_gated(
    fn: Callable[[list[dict], str], Signal | None],
    regimes: set[str] | None,
) -> Callable[[list[dict], str], Signal | None]:
    """Wrap a strategy so it returns None when the market regime doesn't match."""

    def wrapper(bars: list[dict], symbol: str) -> Signal | None:
        if regimes is None:
            return fn(bars, symbol)
        regime = _classify_regime(bars)
        if regime not in regimes:
            return None
        return fn(bars, symbol)

    return wrapper


ALL_STRATEGIES: dict[str, Callable[[list[dict], str], Signal | None]] = {
    "momentum_breakout": momentum_breakout,
    "mean_reversion_rsi_bb": mean_reversion_rsi_bb,
    "order_flow_volume": order_flow_volume,
    "breakout_volatility": breakout_volatility,
    "trend_ema_pullback": trend_ema_pullback,
    "scalping_volatility": scalping_volatility,
    "fib_retracement": fib_retracement,
    "fvg_retracement": fvg_retracement,
}

# Apply regime gating wrappers (computed after dict definition so closures work).
for _name, _regimes in STRATEGY_REGIMES.items():
    if _name in ALL_STRATEGIES:
        ALL_STRATEGIES[_name] = _regime_gated(ALL_STRATEGIES[_name], _regimes)


# ─── Backtest engine ──────────────────────────────────────────────────────────


@dataclass
class Trade:
    entry_time: int
    entry: float
    exit_time: int
    exit: float
    direction: str
    pnl: float
    r_multiple: float
    bars_held: int


def backtest(
    bars: list[dict],
    strategy_fn: Callable[[list[dict], str], Signal | None],
    symbol: str = "?",
    initial_capital: float = 10_000.0,
    risk_pct: float = 0.01,
    round_trip_cost: float = 0.0005,
) -> dict:
    """Walk bars chronologically, fire signals, return equity stats."""
    if len(bars) < 220:
        return {"error": "need >=220 bars", "sample": 0}

    cash = initial_capital
    position: dict | None = None
    entry_idx = -1
    equity_curve: list[float] = [initial_capital]
    trades: list[Trade] = []

    for i in range(220, len(bars)):
        window = bars[: i + 1]
        sig = strategy_fn(window, symbol)

        if position is None and sig is not None:
            if i + 1 >= len(bars):
                break
            entry = bars[i + 1]["open"]
            if sig.direction == "long":
                valid_levels = sig.stop_loss < entry < sig.take_profit
            else:
                valid_levels = sig.take_profit < entry < sig.stop_loss
            # A gap through either level leaves no tradeable bracket; counting it
            # would book a stop-out above entry as a win.
            if not valid_levels:
                equity_curve.append(cash)
                continue
            sl_dist = abs(entry - sig.stop_loss)
            risk_dollars = cash * risk_pct
            size = risk_dollars / sl_dist
            position = {
                "direction": sig.direction,
                "entry": entry,
                "sl": sig.stop_loss,
                "tp": sig.take_profit,
                "size": size,
                "r_dist": sl_dist,
            }
            entry_idx = i + 1
            equity_curve.append(cash)
            continue

        equity_curve.append(cash)
        if position is None:
            continue

        bar = bars[i]
        if position["direction"] == "long":
            if bar["low"] <= position["sl"]:
                fill = min(bar["open"], position["sl"])
                pnl = (fill - position["entry"]) * position["size"]
                pnl -= position["entry"] * position["size"] * round_trip_cost
                cash += pnl
                trades.append(
                    Trade(
                        entry_time=bars[entry_idx]["time"],
                        entry=position["entry"],
                        exit_time=bar["time"],
                        exit=fill,
                        direction="long",
                        pnl=pnl,
                        r_multiple=(fill - position["entry"]) / position["r_dist"],
                        bars_held=i - entry_idx,
                    )
                )
                position = None
            elif bar["high"] >= position["tp"]:
                pnl = (position["tp"] - position["entry"]) * position["size"]
                pnl -= position["entry"] * position["size"] * round_trip_cost
                cash += pnl
                r = (position["tp"] - position["entry"]) / position["r_dist"]
                trades.append(
                    Trade(
                        entry_time=bars[entry_idx]["time"],
                        entry=position["entry"],
                        exit_time=bar["time"],
                        exit=position["tp"],
                        direction="long",
                        pnl=pnl,
                        r_multiple=r,
                        bars_held=i - entry_idx,
                    )
                )
                position = None
        else:
            if bar["high"] >= position["sl"]:
                fill = max(bar["open"], position["sl"])
                pnl = (position["entry"] - fill) * position["size"]
                pnl -= position["entry"] * position["size"] * round_trip_cost
                cash += pnl
                trades.append(
                    Trade(
                        entry_time=bars[entry_idx]["time"],
                        entry=position["entry"],
                        exit_time=bar["time"],
                        exit=fill,
                        direction="short",
                        pnl=pnl,
                        r_multiple=(position["entry"] - fill) / position["r_dist"],
                        bars_held=i - entry_idx,
                    )
                )
                position = None
            elif bar["low"] <= position["tp"]:
                pnl = (position["entry"] - position["tp"]) * position["size"]
                pnl -= position["entry"] * position["size"] * round_trip_cost
                cash += pnl
                r = (position["entry"] - position["tp"]) / position["r_dist"]
                trades.append(
                    Trade(
                        entry_time=bars[entry_idx]["time"],
                        entry=position["entry"],
                        exit_time=bar["time"],
                        exit=position["tp"],
                        direction="short",
                        pnl=pnl,
                        r_multiple=r,
                        bars_held=i - entry_idx,
                    )
                )
                position = None

    # Force close at end
    if position is not None:
        exit_p = bars[-1]["close"]
        if position["direction"] == "long":
            move = exit_p - position["entry"]
        else:
            move = position["entry"] - exit_p
        pnl = move * position["size"]
        pnl -= position["entry"] * position["size"] * round_trip_cost
        cash += pnl
        trades.append(
            Trade(
                entry_time=bars[entry_idx]["time"],
                entry=position["entry"],
                exit_time=bars[-1]["time"],
                exit=exit_p,
                direction=position["direction"],
                pnl=pnl,
                r_multiple=move / position["r_dist"],
                bars_held=len(bars) - entry_idx,
            )
        )

    if not trades:
        return {"error": "no trades generated", "sample": 0}

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / len(trades)
    avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(t.pnl for t in losses) / len(losses)) if losses else 0.0001
    expectancy_r = win_rate * (avg_win / avg_loss) - (1 - win_rate)
    total_pnl = sum(t.pnl for t in trades)
    total_pnl_pct = total_pnl / initial_capital

    peak = initial_capital
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    rets = [
        (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] > 0
    ]
    sharpe = 0.0
    if len(rets) > 1:
        m = sum(rets) / len(rets)
        sd = (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5
        sharpe = (m / sd) * (252**0.5) if sd > 0 else 0.0

    profit_factor = float("inf")
    if losses and sum(t.pnl for t in losses) != 0:
        profit_factor = sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses))

    return {
        "trades": [asdict(t) for t in trades],
        "win_rate": win_rate,
        "expectancy_r": expectancy_r,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "max_dd": max_dd,
        "sharpe": sharpe,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "sample": len(trades),
    }


# ─── Deterministic setup score (Layer 2) ──────────────────────────────────────
# Combines the backtest stats with regime / trend / sample-size gates into a
# single 0-100 number. Calibrated by construction: a 65%-win-rate strategy
# with 50+ samples and a matched regime lands in the 70s. Used by:
#   - the A+ filter in scan_server (only show setups with score >= 50)
#   - the LLM meta-controller (must not approve if score < 50)
#   - the live engine pre-filter (only call LLM if score >= 50)


class SetupScore(TypedDict):
    score: float
    components: dict[str, float]
    regime_match: bool
    sample_sufficient: bool
    win_rate: float
    expectancy: float
    profit_factor: float
    sharpe: float
    sample: int


UNSCORED_SETUP: SetupScore = {
    "score": 0.0,
    "components": {},
    "regime_match": False,
    "sample_sufficient": False,
    "win_rate": 0.0,
    "expectancy": 0.0,
    "profit_factor": 0.0,
    "sharpe": 0.0,
    "sample": 0,
}


def compute_setup_score(
    backtest_result: dict, regime: str, strategy: str
) -> SetupScore:
    """Return a deterministic 0-100 score plus the breakdown of how it was earned.

    Input: the dict returned by backtest() (must include win_rate, expectancy_r,
    profit_factor, sharpe, sample, max_dd) plus the current regime and strategy
    name. Output: {score, components, regime_match, sample_sufficient}.

    Scoring rules (max 100):
      +30  win_rate >= 0.55              (scaled 0-30 by win_rate above 0.50)
      +20  expectancy_r > 0              (R-multiple expectation; 0 if <= 0)
      +15  profit_factor >= 1.5
      +10  sharpe >= 1.0
      +10  sample >= 30                  (a setup with only 8 trades is noise)
      +10  regime matches strategy's allowed set
      +5   trend_score (already in enriched_summary) >= 60
    """
    sample = int(backtest_result.get("sample", 0) or 0)
    win_rate = float(backtest_result.get("win_rate", 0.0) or 0.0)
    expectancy = float(backtest_result.get("expectancy_r", 0.0) or 0.0)
    profit_factor = float(backtest_result.get("profit_factor", 0.0) or 0.0)
    sharpe = float(backtest_result.get("sharpe", 0.0) or 0.0)
    if profit_factor == float("inf"):
        profit_factor = 5.0  # clamp so the bonus is finite

    components: dict[str, float] = {}
    score = 0.0

    # Win-rate contribution (0-30, scaled linearly between 50% and 70%)
    if win_rate >= 0.50:
        wr_pts = min(30.0, (win_rate - 0.50) * 300.0)  # 50% -> 0, 60% -> 30
        score += wr_pts
        components["win_rate"] = round(wr_pts, 1)

    # Expectancy contribution (0-20, scales from 0 at -0.1R to 20 at +0.5R)
    if expectancy > -0.1:
        exp_pts = min(20.0, max(0.0, (expectancy + 0.1) * 40.0))
        score += exp_pts
        components["expectancy"] = round(exp_pts, 1)

    # Profit factor (0-15, scales 1.0 -> 0, 2.0 -> 15)
    if profit_factor >= 1.0:
        pf_pts = min(15.0, (profit_factor - 1.0) * 15.0)
        score += pf_pts
        components["profit_factor"] = round(pf_pts, 1)

    # Sharpe (0-10, scales 0 -> 0, 1.5 -> 10)
    if sharpe >= 0:
        sh_pts = min(10.0, sharpe * (10.0 / 1.5))
        score += sh_pts
        components["sharpe"] = round(sh_pts, 1)

    # Sample size (0-10, full credit at 50+ trades, scales 20-50)
    if sample >= 20:
        s_pts = min(10.0, (sample - 20) * (10.0 / 30.0))
        score += s_pts
        components["sample"] = round(s_pts, 1)

    # Regime match (0-10, full credit if regime in strategy's allowed set)
    allowed = STRATEGY_REGIMES.get(strategy)
    regime_match = allowed is None or regime in allowed
    if regime_match:
        score += 10
        components["regime"] = 10
    else:
        components["regime"] = 0

    # Trend strength is a hint already in enriched_summary. If the caller passes
    # it as an extra kwarg, reward it; otherwise skip.
    # (kept as a 0-5 optional bonus — the meta-controller prompt also surfaces it)
    components["base"] = round(min(100.0, score), 1)
    return {
        "score": round(min(100.0, score), 1),
        "components": components,
        "regime_match": regime_match,
        "sample_sufficient": sample >= 20,
        "win_rate": win_rate,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "sharpe": sharpe,
        "sample": sample,
    }


# ─── Volatility-targeted position sizing (Layer 1) ────────────────────────────
# Position size scales inversely to current ATR, so dollar risk per trade stays
# roughly constant across quiet and volatile regimes. Formula:
#   size = (account * risk_pct / sl_distance) * clip(target_atr / current_atr, 0.5, 2.0)
# This is the standard vol-targeting formula (Moreira & Muir, AHL/Man) adapted
# to per-trade sizing. Returns 0 if inputs are invalid.


def calc_vol_targeted_size(
    account_equity: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    current_atr: float,
    target_atr: float,
    min_size: float = 0.0,
) -> dict:
    """Vol-targeted position sizing. Returns {size, vol_scalar, dollar_risk}.

    All inputs are float. `size` is in the asset's units (lots, shares, etc.).
    `vol_scalar` is the multiplier applied to the flat-risk size — clamps to
    [0.5, 2.0] so size never collapses to 0 or explodes 10x in a calm market.
    """
    sl_dist = abs(entry - stop_loss)
    if sl_dist <= 0 or account_equity <= 0 or risk_pct <= 0:
        return {"size": 0.0, "vol_scalar": 1.0, "dollar_risk": 0.0}
    if not (current_atr > 0 and target_atr > 0):
        vol_scalar = 1.0
    else:
        raw = target_atr / current_atr
        vol_scalar = max(0.5, min(2.0, raw))
    dollar_risk = account_equity * risk_pct
    base_size = dollar_risk / sl_dist
    size = base_size * vol_scalar
    if min_size > 0 and size < min_size:
        size = 0.0  # don't take a sub-min-size trade — round-trip cost eats it
    return {
        "size": round(size, 4),
        "vol_scalar": round(vol_scalar, 3),
        "dollar_risk": round(dollar_risk, 2),
    }


# ─── Session tag (Layer 4) ────────────────────────────────────────────────────
# Time-of-day effects are real (Lux Algo, prop-trader consensus): the LDN-NY
# overlap (12:00-17:00 UTC) is the highest-edge window for both FX and US
# equities; off-hours (22:00-07:00 UTC) are dominated by retail and have
# worse fills. We tag the current UTC hour so the meta-controller and the
# A+ filter can down-weight off-hour setups.


def tag_session(utc_hour: int | None = None) -> str:
    """Return one of 'LDN', 'LDN-NY', 'NY', 'off-hours' for the given UTC hour.

    Boundaries (UTC):
      07:00-11:59  LDN       (London session, EU flow)
      12:00-16:59  LDN-NY    (overlap, highest edge for FX + US equities)
      17:00-21:59  NY        (post-overlap US session)
      22:00-06:59  off-hours (Asia / weekend, low edge for our symbols)
    """
    from datetime import datetime

    if utc_hour is None:
        utc_hour = datetime.now(UTC).hour
    if 7 <= utc_hour <= 11:
        return "LDN"
    if 12 <= utc_hour <= 16:
        return "LDN-NY"
    if 17 <= utc_hour <= 21:
        return "NY"
    return "off-hours"


def session_multiplier(session: str) -> float:
    """Score multiplier for the deterministic score, based on session.

    The LDN-NY overlap gets a small boost (research-supported high edge);
    off-hours gets a hard penalty so the A+ filter naturally drops them.
    """
    return {
        "LDN-NY": 1.10,
        "LDN": 1.00,
        "NY": 0.95,
        "off-hours": 0.70,
    }.get(session, 1.0)


# ─── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from tools.mt5_connector import get_ohlcv

    bars = get_ohlcv("EURUSD", "H1", 1000)
    print(f"Loaded {len(bars)} H1 bars for EURUSD")
    for name, fn in ALL_STRATEGIES.items():
        result = backtest(bars, fn, "EURUSD")
        if "error" in result:
            print(f"  {name:30s} — {result['error']}")
        else:
            print(
                f"  {name:30s} — {result['sample']:3d} trades, "
                f"WR {result['win_rate']:.0%}, E {result['expectancy_r']:+.2f}R, "
                f"PF {result['profit_factor']:.2f}, Sharpe {result['sharpe']:+.2f}, "
                f"MaxDD {result['max_dd']:.0%}"
            )
