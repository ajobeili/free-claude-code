from decimal import Decimal as D

from tools.pattern_simulation import Bar, exit_bar, signal


def test_both_boundaries_are_rejected():
    prior = [Bar(D(10), D(12), D(8), D(10))] * 20
    assert signal(prior, Bar(D(10), D(13), D(7), D(10))) == 0


def test_adverse_gap_and_ambiguous_bar():
    bar = Bar(D(7), D(15), D(6), D(10))
    assert exit_bar(bar, 1, D(8), D(14)) == (D(7), "stop")


def test_offline_replay(tmp_path):
    import json
    from pathlib import Path

    from tools.pattern_study import run

    fixture = Path("tests/fixtures/pattern_study")
    for name in ("first", "second"):
        run(fixture / "observations.csv", fixture / "manifest.json", tmp_path / name)
    first = (tmp_path / "first/study.json").read_bytes()
    assert first == (tmp_path / "second/study.json").read_bytes()
    result = json.loads(first)
    assert len(result["groups"]) == 12
    assert result["audit"][-1]["status"] == "excluded"
    assert all(
        group["status"] == "insufficient evidence" for group in result["groups"][6:]
    )
    for group in (result["groups"][0], result["groups"][3]):
        assert group["profitable"] == 1
        assert group["trades"] == 2
        assert set(group["occurrences"][0]["sizing"]) == {"100", "200"}


def test_context_and_future_independence():
    from datetime import UTC, datetime, timedelta

    from tools.pattern_simulation import context

    time = datetime(2026, 1, 1, 1, tzinfo=UTC)
    prior = time - timedelta(hours=1)
    low = Bar(D(10), D(12), D(8), D(10))
    high = Bar(D(10), D(12), D(8), D(11))
    rising = {prior: low, time: high}
    falling = {prior: high, time: low}
    assert context(rising, rising, time) == "same"
    assert context(rising, falling, time) == "opposite"
    assert context(rising, {prior: low, time: low}, time) == "neutral"
    assert context(rising, {}, time) == "unavailable"
    rising[time + timedelta(hours=1)] = low
    assert context(rising, falling, time) == "opposite"


def test_missing_terms_and_minimum_size():
    from datetime import UTC, datetime

    from tools.pattern_report import feasibility
    from tools.pattern_simulation import Trade

    time = datetime(2026, 1, 1, tzinfo=UTC)
    trade = Trade(
        time,
        1,
        "same",
        D(100),
        D(99),
        D(102),
        D(99),
        time,
        "stop",
        D(-1),
        D(-1),
        False,
        (),
    )
    assert feasibility(trade, 100, {})["status"] == "unknown"
    terms = {"units": "1", "minimum": "2", "step": "1", "margin": ".1", "adverse": ".1"}
    assert feasibility(trade, 100, terms)["status"] == "unavailable"


def test_costs_and_measured_quote_side():
    from datetime import UTC, datetime, timedelta

    from tools.pattern_simulation import simulate

    start = datetime(2026, 1, 1, tzinfo=UTC)
    ask = {
        start + timedelta(hours=i): Bar(D(10), D(12), D(8), D(10)) for i in range(27)
    }
    signal_time = start + timedelta(hours=20)
    ask[signal_time] = Bar(D(10), D(13), D(9), D(10))
    bid = {
        time: Bar(
            bar.open - D(".1"),
            bar.high - D(".1"),
            bar.low - D(".1"),
            bar.close - D(".1"),
        )
        for time, bar in ask.items()
    }
    trades, _ = simulate(ask, bid, ask, "baseline", D(".2"), D(99), set())
    assert trades[0].entry == D("9.9")
    assert trades[0].net == (D("-.1") - D(".2")) / D("3.1")


def test_contaminated_validation_refused(tmp_path):
    import json
    from pathlib import Path

    import pytest

    from tools.pattern_study import run

    source = Path("tests/fixtures/pattern_study")
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["development"] = ["2026-09-01T00:00Z", "2026-09-03T00:00Z"]
    manifest["validation"] = ["2026-10-21T00:00Z", "2026-10-23T00:00Z"]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="inspected"):
        run(source / "observations.csv", path, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_paired_bounds_reproduce_and_handle_empty_groups():
    from datetime import date, timedelta

    from tools.pattern_statistics import bounds

    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(6)]
    assert bounds([[] for _ in range(6)], dates, repetitions=20) == [None] * 10
    assert bounds([[] for _ in range(6)], dates[:2]) == [None] * 10


def test_sufficient_gate_and_measured_cost_requirement(tmp_path, monkeypatch):
    import csv
    import hashlib
    import json
    from datetime import UTC, datetime, timedelta

    from tools import pattern_study
    from tools.pattern_simulation import Trade

    start = datetime(2027, 1, 1, tzinfo=UTC)
    times = [start + timedelta(days=i) for i in range(60)]
    observations = tmp_path / "history.csv"
    with observations.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "market",
                "quote_side",
                "hour_start",
                "open",
                "high",
                "low",
                "close",
                "completed",
                "source",
            ]
        )
        for market in ("Gold", "Crude Oil Spot"):
            for side, price in (("ask", 100), ("bid", 99)):
                for time in times:
                    writer.writerow(
                        (
                            market,
                            side,
                            time.isoformat(),
                            price,
                            price + 1,
                            price - 1,
                            price,
                            "true",
                            "test history",
                        )
                    )
    trades = [
        Trade(
            time,
            1,
            "same",
            D(100),
            D(99),
            D(102),
            D(102),
            time,
            "target",
            D(1),
            D(".5"),
            True,
            (),
        )
        for time in times
        for _ in range(2)
    ]
    monkeypatch.setattr(pattern_study, "simulate", lambda *args: (trades, {}))
    monkeypatch.setattr(pattern_study, "bounds", lambda *args: [0.5] * 10)
    terms = {
        "cost_source": "fixture",
        "boundary_source": "fixture",
        "boundaries": ["2027-01-01T22:00Z"],
    }
    manifest = {
        "version": 1,
        "sha256": hashlib.sha256(observations.read_bytes()).hexdigest(),
        "development": ["2026-01-01T00:00Z", "2026-02-01T00:00Z"],
        "validation": ["2027-01-01T00:00Z", "2027-03-03T00:00Z"],
        "frozen_at": "2026-12-01T00:00Z",
        "inspected": [],
        "evidence": "historical",
        "method": "paired-5day-50000-seed204-bonferroni10-v1",
        "shared_session_hours": [t.isoformat() for t in times],
        "session_calendar_source": "fixture",
        "markets": {"Gold": terms, "Crude Oil Spot": terms},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    pattern_study.run(observations, path, tmp_path / "positive")
    groups = json.loads((tmp_path / "positive/study.json").read_text())["groups"][6:]
    assert all(
        groups[i]["status"] == "promising for further demo study" for i in (1, 2, 4, 5)
    )
    manifest["evidence"] = "synthetic"
    path.write_text(json.dumps(manifest))
    pattern_study.run(observations, path, tmp_path / "synthetic")
    groups = json.loads((tmp_path / "synthetic/study.json").read_text())["groups"][6:]
    assert all(group["status"] == "insufficient evidence" for group in groups)
