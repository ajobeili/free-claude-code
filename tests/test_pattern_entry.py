from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from tools.pattern_entry import build_card, render_cards
from tools.pattern_simulation import HOUR, Bar


def evidence():
    opening = datetime(2026, 1, 2, tzinfo=UTC)
    signal = opening - HOUR
    prior = [signal - HOUR * offset for offset in range(1, 21)]
    ask = {time: Bar(D(10), D(12), D(8), D(10)) for time in prior}
    ask[signal] = Bar(D(10), D(13), D(9), D(11))
    other = {
        signal - HOUR: Bar(D(10), D(12), D(8), D(10)),
        signal: Bar(D(10), D(12), D(8), D(9)),
    }
    terms = {
        "units": "1",
        "minimum": ".01",
        "step": ".01",
        "margin": ".005",
        "adverse": ".1",
    }
    return opening, ask, other, terms


def test_eligible_card_and_risk():
    opening, ask, other, terms = evidence()
    card = build_card(
        "Gold", ask, other, opening, D(11), D("11.5"), set(), terms, True, True
    )
    assert card["action"] == "SELL"
    assert card["entry"] == "11"
    assert card["stop"] == "13"
    assert card["target"] == "7"
    assert D(str(card["sizing"]["100"]["risk"])) <= 1


def test_missed_opening_and_unknown_terms_are_wait():
    opening, ask, other, _ = evidence()
    card = build_card("Gold", ask, other, opening + timedelta(minutes=20))
    assert card["action"] == "WAIT"
    assert any("window has passed" in reason for reason in card["reasons"])
    assert card["sizing"]["100"]["status"] == "unknown"


def test_wrong_context_and_boundary():
    opening, ask, other, terms = evidence()
    other[opening - HOUR] = Bar(D(10), D(12), D(8), D(11))
    card = build_card(
        "Gold",
        ask,
        other,
        opening,
        D(11),
        D("11.5"),
        {opening + HOUR},
        terms,
        True,
        True,
    )
    assert card["action"] == "WAIT"
    assert card["context"] == "same"
    assert any("boundary" in reason for reason in card["reasons"])


def test_missing_data_and_future_invariance():
    opening, ask, other, terms = evidence()
    assert build_card("Gold", {}, other, opening)["action"] == "WAIT"
    before = build_card(
        "Gold", ask, other, opening, D(11), D("11.5"), set(), terms, True, True
    )
    ask[opening + HOUR] = Bar(D(1000), D(2000), D(1), D(900))
    after = build_card(
        "Gold", ask, other, opening, D(11), D("11.5"), set(), terms, True, True
    )
    assert before == after


def test_render_escapes_evidence_and_includes_both_balances():
    opening, ask, other, _ = evidence()
    card = build_card("<script>Gold</script>", ask, other, opening)
    html = render_cards([card])
    assert "<script>" not in html
    assert "&#36;100" in html and "&#36;200" in html
