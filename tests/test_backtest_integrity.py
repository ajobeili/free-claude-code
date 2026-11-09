from typing import Literal

import pytest

from tools.strategy_engine import Signal, backtest


def _flat_bars(n: int = 230) -> list[dict]:
    return [
        {"time": i, "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0}
        for i in range(n)
    ]


def _fire_once(direction: Literal["long", "short"], stop: float, target: float):
    def strategy(window: list[dict], symbol: str) -> Signal | None:
        if len(window) != 221:
            return None
        return Signal("t", symbol, direction, 100.0, stop, target, 0.5, "t", 1.0)

    return strategy


@pytest.mark.parametrize(
    "direction,stop,target",
    [
        ("long", 101.0, 105.0),  # next open already below the stop
        ("long", 95.0, 99.5),  # next open already beyond the target
        ("short", 99.0, 95.0),  # next open already above a short stop
    ],
)
def test_gapped_brackets_are_not_traded(direction, stop, target) -> None:
    result = backtest(_flat_bars(), _fire_once(direction, stop, target), "X")

    assert result == {"error": "no trades generated", "sample": 0}


def test_long_stop_gapped_through_fills_at_open() -> None:
    bars = _flat_bars()
    bars[222] = {"time": 222, "open": 95.0, "high": 95.2, "low": 94.5, "close": 95}

    result = backtest(bars, _fire_once("long", 99.0, 105.0), "X")

    trade = result["trades"][0]
    assert trade["exit"] == 95.0
    assert trade["r_multiple"] == pytest.approx(-5.0)
    assert result["win_rate"] == 0.0


def test_short_stop_gapped_through_fills_at_open() -> None:
    bars = _flat_bars()
    bars[222] = {"time": 222, "open": 104.0, "high": 104.5, "low": 103.8, "close": 104}

    result = backtest(bars, _fire_once("short", 101.0, 95.0), "X")

    trade = result["trades"][0]
    assert trade["exit"] == 104.0
    assert trade["r_multiple"] == pytest.approx(-4.0)


def test_intrabar_stop_fills_at_stop_and_target_reports_r() -> None:
    bars = _flat_bars()
    bars[222] = {"time": 222, "open": 100.0, "high": 102.5, "low": 99.95, "close": 102}

    result = backtest(bars, _fire_once("long", 99.0, 102.0), "X")

    assert result["trades"][0]["exit"] == 102.0
    assert result["trades"][0]["r_multiple"] == pytest.approx(2.0)
    assert result["win_rate"] == 1.0


def test_open_position_closed_at_end_reports_its_r_multiple() -> None:
    bars = _flat_bars()
    bars[-1] = {"time": 229, "open": 100.0, "high": 100.6, "low": 99.9, "close": 100.5}

    result = backtest(bars, _fire_once("long", 99.0, 105.0), "X")

    assert result["trades"][0]["r_multiple"] == pytest.approx(0.5)
