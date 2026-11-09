from tests.test_structure_scan import sample
from tools.structure_assessment import Context, assess
from tools.structure_scan import Candle, Setup


def evaluate(forming=(), bid=15, ask=15.1, stale=False, contexts=()):
    return assess(
        sample(),
        Setup("buy", 12, 10, 14, 6),
        list(forming),
        list(contexts),
        bid,
        ask,
        "TRADEABLE",
        stale,
        12.5,
    )


def test_waiting_and_risk_references():
    result = evaluate()
    assert result["status"] == "Waiting"
    assert result["stop"] == 9
    assert result["target"] is None
    assert result["reward_risk"] is None
    assert any("Structure conflict" in c for c in result["checks"])


def test_forming_touch_and_invalidation():
    assert evaluate([Candle(25200, 15, 16, 12, 15)])["status"] == "Touched"
    assert evaluate([Candle(25200, 15, 16, 8, 15)])["status"] == "Invalidated"


def test_missing_and_crossed_quotes():
    assert evaluate(bid=None, ask=None)["status"] == "Unverified"
    assert evaluate(bid=16, ask=15)["spread"] is None
    assert evaluate(bid=float("nan"))["status"] == "Unverified"
    assert evaluate(bid=11, ask=11.1)["status"] == "Touched"


def test_stale_and_context_conflict():
    result = evaluate(
        stale=True,
        contexts=[Context("HOUR", "sell"), Context("DAY", "unavailable", True)],
    )
    assert result["status"] == "Stale"
    assert any("CONFLICT" in c for c in result["checks"])
    assert any("unavailable or stale" in c for c in result["checks"])


def test_sell_symmetry():
    bars = [
        Candle(b.time, 30 - b.open, 30 - b.low, 30 - b.high, 30 - b.close)
        for b in sample()
    ]
    result = assess(
        bars,
        Setup("sell", 18, 16, 20, 6),
        [Candle(25200, 15, 18, 14, 15)],
        [],
        14.9,
        15,
        "TRADEABLE",
        False,
        None,
    )
    assert result["status"] == "Touched"
    assert result["stop"] == 21


def test_historical_untouched_target_and_consumed_target():
    bars = sample()
    bars[2] = Candle(bars[2].time, 12, 20, 10, 12)
    setup = Setup("buy", 12, 10, 14, 6)
    result = assess(bars, setup, [], [], 15, 15.1, "TRADEABLE", False, None)
    assert result["target"] == 20
    assert result["reward_risk"] > 0
    touched = assess(
        bars,
        setup,
        [Candle(25200, 15, 20, 14, 16)],
        [],
        15,
        15.1,
        "TRADEABLE",
        False,
        None,
    )
    assert touched["target"] is None
    mirrored = [
        Candle(b.time, 30 - b.open, 30 - b.low, 30 - b.high, 30 - b.close) for b in bars
    ]
    assert (
        assess(
            mirrored,
            Setup("sell", 18, 16, 20, 6),
            [],
            [],
            14.9,
            15,
            "TRADEABLE",
            False,
            None,
        )["target"]
        == 10
    )


def test_quote_beyond_stop_invalidates_without_forming_candle():
    assert evaluate(bid=8, ask=8.1)["status"] == "Invalidated"
