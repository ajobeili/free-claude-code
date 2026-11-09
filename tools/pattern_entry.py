"""Prospective research entry cards from timestamped local observations."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import TypedDict

from tools.pattern_report import feasibility
from tools.pattern_simulation import HOUR, MARKETS, Series, Trade, context, signal
from tools.pattern_study import load, timestamp


class EntryCard(TypedDict):
    market: str
    as_of: str
    signal_time: str
    entry_time: str
    action: str
    pattern_direction: str
    context: str
    entry: str | None
    stop: str | None
    target: str | None
    range_high: str | None
    range_low: str | None
    sizing: dict[str, dict[str, object]]
    reasons: list[str]


ENTRY_TOLERANCE = timedelta(seconds=5)


def build_card(
    market: str,
    ask: Series,
    other: Series,
    as_of: datetime,
    entry_bid: Decimal | None = None,
    entry_ask: Decimal | None = None,
    boundaries: set[datetime] | None = None,
    terms: dict[str, str] | None = None,
    costs_verified: bool = False,
    position_clear: bool = False,
) -> EntryCard:
    if as_of.utcoffset() is None:
        raise ValueError("An aware snapshot timestamp is required")
    as_of = as_of.astimezone(UTC)
    entry_time = as_of.replace(minute=0, second=0, microsecond=0)
    signal_time = entry_time - HOUR
    card: EntryCard = {
        "market": market,
        "range_high": None,
        "range_low": None,
        "reasons": [],
        "as_of": as_of.isoformat(),
        "signal_time": signal_time.isoformat(),
        "entry_time": entry_time.isoformat(),
        "action": "WAIT",
        "pattern_direction": "none",
        "context": "unavailable",
        "entry": None,
        "stop": None,
        "target": None,
        "sizing": {"100": {"status": "unknown"}, "200": {"status": "unknown"}},
    }
    reasons: list[str] = []
    history = sorted(time for time in ask if time < signal_time)[-20:]
    if signal_time not in ask or len(history) < 20:
        card["reasons"] = [
            "Completed signal candle or 20 preceding candles are missing."
        ]
        return card
    prior = [ask[time] for time in history]
    card["range_high"] = str(max(bar.high for bar in prior))
    card["range_low"] = str(min(bar.low for bar in prior))
    direction = signal(prior, ask[signal_time])
    state = context(ask, other, signal_time)
    card["context"] = state
    card["pattern_direction"] = (
        "BUY" if direction == 1 else "SELL" if direction == -1 else "none"
    )
    if not direction:
        reasons.append(
            "The latest completed hour did not form the failed-breakout pattern."
        )
    if state != "opposite":
        reasons.append(
            f"Gold and oil context is {state}; opposite direction is required."
        )
    if as_of - entry_time > ENTRY_TOLERANCE:
        reasons.append(
            "The next-hour opening entry window has passed. Do not chase this entry."
        )
    if (
        entry_bid is None
        or entry_ask is None
        or not entry_bid.is_finite()
        or not entry_ask.is_finite()
        or entry_bid <= 0
        or entry_ask < entry_bid
    ):
        reasons.append("A verified paired bid/ask opening quote is missing.")
    elif direction:
        entry = entry_ask if direction == 1 else entry_bid
        stop = ask[signal_time].low if direction == 1 else ask[signal_time].high
        risk = direction * (entry - stop)
        if risk <= 0:
            reasons.append("The opening quote gives an invalid stop distance.")
        else:
            target = entry + direction * 2 * risk
            card.update(entry=str(entry), stop=str(stop), target=str(target))
            trade = Trade(
                signal_time,
                direction,
                state,
                entry,
                stop,
                target,
                entry,
                entry_time,
                "pending",
                Decimal(0),
                Decimal(0),
                False,
                (),
            )
            card["sizing"] = {
                str(balance): feasibility(trade, balance, terms or {})
                for balance in (100, 200)
            }
            if not any(
                value["status"] == "available" for value in card["sizing"].values()
            ):
                reasons.append(
                    "Contract sizing or cost evidence is missing, or minimum size exceeds the risk budget."
                )
    if boundaries is None:
        reasons.append(
            "Funding and price-adjustment boundaries have not been verified."
        )
    elif any(entry_time <= boundary < entry_time + 6 * HOUR for boundary in boundaries):
        reasons.append("The six-hour window crosses a funding or adjustment boundary.")
    if not costs_verified:
        reasons.append("Execution costs and adverse slippage have not been verified.")
    if not position_clear:
        reasons.append(
            "The same-market position check has not been cleared for this demo example."
        )
    if not reasons:
        card["action"] = card["pattern_direction"]
    card["reasons"] = reasons or [
        "Eligible hypothetical demo setup under the frozen study rules."
    ]
    return card


def render_cards(cards: list[EntryCard]) -> str:
    sections = []
    for card in cards:
        reasons = card["reasons"]
        assert isinstance(reasons, list)
        sizing = card["sizing"]
        assert isinstance(sizing, dict)
        levels = "".join(
            f"<dt>{label}</dt><dd>{escape(str(card.get(key) or 'Unavailable'))}</dd>"
            for key, label in (
                ("entry", "Opening entry"),
                ("stop", "Stop"),
                ("target", "Target"),
            )
        )
        sizes = "".join(
            f"<p>Virtual balance &#36;{balance} · sizing {escape(str(value['status']))}"
            f" · planned risk budget &#36;{Decimal(balance) / 100:g}"
            f" · quantity {escape(str(value.get('quantity', 'unknown')))}"
            f" · estimated cash risk {escape(str(value.get('risk', 'unknown')))}</p>"
            for balance, value in sizing.items()
        )
        sections.append(
            f"<section><h2>{escape(str(card['market']))}</h2>"
            f"<strong class='action'>{escape(str(card['action']))}</strong>"
            f"<p>Snapshot {escape(str(card['as_of']))}</p>"
            f"<p>Completed signal hour {escape(str(card['signal_time']))}</p>"
            f"<p>Pattern direction {escape(str(card['pattern_direction']))} · "
            f"Gold/oil context {escape(str(card['context']))}</p>"
            f"<p>Previous range {escape(str(card.get('range_low', 'unknown')))} to "
            f"{escape(str(card.get('range_high', 'unknown')))}</p>"
            f"<p>{escape(str(reasons[0]))}</p>"
            f"<details><summary>Entry prices, sizing and remaining checks</summary>"
            f"<ul>{''.join('<li>' + escape(str(reason)) + '</li>' for reason in reasons[1:])}</ul>"
            f"<dl>{levels}</dl>{sizes}</details></section>"
        )
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Gold and oil demo entry cards</title>"
        "<style>body{font:17px system-ui;max-width:1000px;margin:32px auto;padding:0 20px;color:#1a2932}"
        "section{padding:20px 0;border-top:1px solid #c6d2d8}p,li{line-height:1.5}"
        ".action{font-size:28px}dl{display:grid;grid-template-columns:150px 1fr}dd{margin:0}"
        "</style><h1>Gold and oil demo entry cards</h1>"
        "<p>Research snapshot · UTC timestamps · refresh requires a new capture.</p>"
        "<p>Rules use completed hourly candles. A price breaks the previous 20-candle range and closes back inside,"
        " while gold and oil move in opposite directions. Entry is at the next hour's opening quote."
        " The target is twice the entry-to-stop distance.</p>"
        f"{''.join(sections)}"
        "<p>Next check occurs at a completed hourly candle with fresh paired quotes and verified contract terms."
        " Validation collection begins 5 November 2026 UTC. The small historical sample still requires independent validation.</p></html>"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        books, audit = load(args.observations)
        cards = []
        for market in MARKETS:
            other = MARKETS[1 - MARKETS.index(market)]
            evidence = snapshot.get("markets", {}).get(market, {})
            quote_time = (
                timestamp(evidence["quote_time"])
                if evidence.get("quote_time")
                else None
            )
            now = timestamp(snapshot["as_of"])
            opening = now.replace(minute=0, second=0, microsecond=0)
            valid_quote = quote_time == opening
            cards.append(
                build_card(
                    market,
                    books.get((market, "ask"), {}),
                    books.get((other, "ask"), {}),
                    now,
                    Decimal(evidence["bid"])
                    if valid_quote and evidence.get("bid")
                    else None,
                    Decimal(evidence["ask"])
                    if valid_quote and evidence.get("ask")
                    else None,
                    set(map(timestamp, evidence["boundaries"]))
                    if evidence.get("boundary_source")
                    else None,
                    evidence.get("contract"),
                    bool(evidence.get("cost_source")),
                    evidence.get("position_clear") is True,
                )
            )
        payload = json.dumps(
            {"cards": cards, "audit": audit, "snapshot": snapshot}, indent=2
        )
        html = render_cards(cards)
        args.output.mkdir()
        (args.output / "entry-cards.json").write_text(payload, encoding="utf-8")
        (args.output / "entry-cards.html").write_text(html, encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        parser.exit(2, f"Entry-card error: {error}\n")


if __name__ == "__main__":
    main()
