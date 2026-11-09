"""Paper planner risk, recovery and persistence tests."""

from datetime import UTC, datetime

import pytest

from tools.trade_planner import (
    Candidate,
    Plan,
    PlannerRequest,
    PlannerStore,
    Quote,
    advance,
    quote_from,
    validate_candidate,
)


def paper():
    return Plan(
        created=1000,
        expires=2000,
        request=PlannerRequest(symbol="GOLD"),
        candidate=Candidate(
            id="test",
            strategy="Test",
            direction="buy",
            entry=100,
            stop=98,
            target=105,
            reason="Test",
        ),
        reasoning="Test",
        counterargument="Risk",
        status="pending",
        ai_used=True,
        quantity=10,
        risk_budget=20,
        last_observed=1000,
    )


def test_paper_entry_and_target():
    p = paper()
    assert advance(p, Quote(bid=100, ask=101, at=1005), 1005) is None
    assert p.status == "pending"
    assert advance(p, Quote(bid=99.5, ask=100, at=1010), 1010)
    assert p.entry_fill == 100 and p.status == "open"
    advance(p, Quote(bid=106, ask=107, at=1015), 1015)
    assert p.status == "closed" and p.exit_fill == 105 and p.pnl == 50


def test_gap_and_stop_slippage():
    p = paper()
    advance(p, Quote(bid=99.5, ask=100, at=1005), 1005)
    advance(p, Quote(bid=96, ask=97, at=1010), 1010)
    assert p.pnl == -40
    p = paper()
    advance(p, Quote(bid=99.5, ask=100, at=1100), 1100)
    assert p.status == "expired" and p.entry_fill is None
    p = paper()
    p.status = "open"
    p.entry_fill = 100
    advance(p, Quote(bid=105, ask=106, at=1100), 1100)
    assert p.status == "review" and p.exit_fill is None


def test_pre_entry_stop_and_expiration():
    p = paper()
    advance(p, Quote(bid=97, ask=98, at=1005), 1005)
    assert p.status == "cancelled"
    p = paper()
    advance(p, Quote(bid=99, ask=100, at=2100), 2100)
    assert p.status == "expired"


def test_risk_validation():
    p = paper()
    c = p.candidate
    assert (
        validate_candidate(
            c,
            p.request,
            Quote(bid=101, ask=101.1, at=1000),
            [{"bias": "buy", "stale": False}],
        )
        is None
    )
    conflict = validate_candidate(
        c, p.request, Quote(bid=101, ask=101.1, at=1000), [{"bias": "sell"}]
    )
    assert conflict is not None and "conflicts" in conflict
    with pytest.raises(ValueError):
        PlannerRequest(symbol="GOLD", risk_pct=10)
    with pytest.raises(ValueError):
        PlannerRequest(symbol="GOLD", balance=float("nan"))


def test_quote_freshness():
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC).timestamp()
    raw = {
        "bid": 100,
        "offer": 101,
        "last_update": "12:00:00",
        "market_status": "TRADEABLE",
    }
    assert quote_from(raw, now).bid == 100
    with pytest.raises(ValueError):
        quote_from(raw, now + 100)
    with pytest.raises(ValueError):
        quote_from({**raw, "last_update": ""}, now)


def test_persistence_and_journal(tmp_path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    p = paper()
    store.save(p, "Original decision")
    p.status = "cancelled"
    store.save(p, "Cancelled")
    latest = store.latest()
    assert latest is not None and latest.status == "cancelled"
    assert [e["message"] for e in store.history()["events"]] == [
        "Cancelled",
        "Original decision",
    ]


def test_sell_fill_uses_bid_and_exit_uses_ask():
    p = paper()
    p.candidate.direction = "sell"
    p.candidate.stop = 102
    p.candidate.target = 95
    advance(p, Quote(bid=100, ask=100.5, at=1005), 1005)
    assert p.status == "open"
    advance(p, Quote(bid=94, ask=95, at=1010), 1010)
    assert p.pnl == 50


def test_repeated_and_preplan_quotes_do_not_fill():
    p = paper()
    advance(p, Quote(bid=99, ask=100, at=999), 1005)
    assert p.status == "pending"
    p.last_quote_at = 1004
    advance(p, Quote(bid=99, ask=100, at=1004), 1005)
    assert p.status == "pending"


def test_scenarios_exclude_latest_candle():
    from tools.trade_planner import build_scenarios

    bars = [{"high": 12.0, "low": 8.0} for _ in range(20)]
    bars.append({"high": 100.0, "low": 1.0})
    result = build_scenarios(bars)
    assert len(result) == 3
    assert "12" in result[0].trigger
    assert "8" in result[1].trigger
    assert "100" not in result[0].trigger
    assert build_scenarios(bars[:20]) == []
