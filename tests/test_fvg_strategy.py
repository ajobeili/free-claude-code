"""Contracts for the three-candle FVG retracement strategy."""

from tools.strategy_engine import ALL_STRATEGIES, fvg_retracement


def _bar(time: int, low: float, high: float, close: float) -> dict:
    return {
        "time": time,
        "open": (low + high) / 2,
        "high": high,
        "low": low,
        "close": close,
        "volume": 100.0,
    }


def _base_bars() -> list[dict]:
    return [_bar(index, 98.0, 100.0, 99.0) for index in range(20)]


def test_bullish_fvg_retest_produces_two_r_long_signal() -> None:
    bars = _base_bars()
    # First / middle / third FVG candles: the first high (100) is below the
    # third low (103), creating a 100-103 bullish imbalance.
    bars.extend(
        [
            _bar(20, 98.0, 100.0, 99.0),
            _bar(21, 99.0, 104.0, 103.5),
            _bar(22, 103.0, 105.0, 104.0),
            _bar(23, 101.5, 104.0, 103.2),
        ]
    )

    signal = fvg_retracement(bars, "XAUUSD")

    assert signal is not None
    assert signal.strategy == "fvg_retracement"
    assert signal.direction == "long"
    assert signal.stop_loss < signal.entry < signal.take_profit
    assert (
        round(
            (signal.take_profit - signal.entry) / (signal.entry - signal.stop_loss), 6
        )
        == 2
    )


def test_filled_fvg_is_never_reused() -> None:
    bars = _base_bars()
    bars.extend(
        [
            _bar(20, 98.0, 100.0, 99.0),
            _bar(21, 99.0, 104.0, 103.5),
            _bar(22, 103.0, 105.0, 104.0),
            # A later low through the lower edge fully invalidates this gap.
            _bar(23, 99.5, 104.0, 102.0),
            _bar(24, 101.5, 104.0, 103.2),
        ]
    )

    assert fvg_retracement(bars, "XAUUSD") is None


def test_bearish_fvg_retest_produces_two_r_short_signal() -> None:
    bars = _base_bars()
    bars.extend(
        [
            _bar(20, 105.0, 107.0, 106.0),
            _bar(21, 101.0, 106.0, 101.5),
            _bar(22, 99.0, 102.0, 100.0),
            _bar(23, 99.5, 104.0, 100.8),
        ]
    )

    signal = fvg_retracement(bars, "XAUUSD")

    assert signal is not None
    assert signal.direction == "short"
    assert signal.take_profit < signal.entry < signal.stop_loss
    assert (
        round(
            (signal.entry - signal.take_profit) / (signal.stop_loss - signal.entry), 6
        )
        == 2
    )


def test_fvg_strategy_is_available_to_scan_and_backtest_paths() -> None:
    assert ALL_STRATEGIES["fvg_retracement"] is not None
