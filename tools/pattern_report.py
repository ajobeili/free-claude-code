"""Sizing and standalone report rendering."""

import json
from decimal import Decimal
from html import escape

from tools.pattern_simulation import Trade


def feasibility(trade: Trade, balance: int, terms: dict[str, str]) -> dict[str, object]:
    try:
        units, minimum, step, margin, adverse = (
            Decimal(terms[key])
            for key in ("units", "minimum", "step", "margin", "adverse")
        )
    except KeyError, ArithmeticError:
        return {"status": "unknown"}
    if any(
        not value.is_finite() or value <= 0
        for value in (units, minimum, step, margin, adverse)
    ):
        return {"status": "unknown"}
    risk = units * (abs(trade.entry - trade.stop) + adverse)
    margin_per = units * trade.entry * margin
    quantity = (
        min(Decimal(balance) / 100 / risk, Decimal(balance) / margin_per) // step * step
    )
    if quantity < minimum:
        return {"status": "unavailable"}
    return {
        "status": "available",
        "quantity": str(quantity),
        "risk": str(risk * quantity),
        "margin": str(margin_per * quantity),
    }


def render(result: dict[str, object], groups: list[dict[str, object]]) -> str:
    sections = []
    for group in groups:
        heading = escape(
            " / ".join(str(group[key]) for key in ("market", "phase", "variant"))
        )
        occurrences = group["occurrences"]
        assert isinstance(occurrences, list)
        rows = [
            f"<details><summary>{escape(str(occurrence['time']))} · "
            f"{escape(str(occurrence['reason']))} · {escape(str(occurrence['net']))} R"
            f"</summary>{occurrence.get('chart', '')}<pre>{escape(json.dumps({k: v for k, v in occurrence.items() if k != 'chart'}, indent=2, default=str))}</pre></details>"
            for occurrence in occurrences
        ]
        summary = {key: value for key, value in group.items() if key != "occurrences"}
        sections.append(
            f"<section><h2>{heading}</h2><pre>"
            f"{escape(json.dumps(summary, indent=2, default=str))}</pre>{''.join(rows)}</section>"
        )
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'><title>Gold and oil study</title>"
        "<h1>Gold and oil study</h1><p>Simulated outcomes. R is the entry-to-stop distance.</p>"
        f"<p>Evidence {escape(str(result.get('manifest', {})))}</p><p>{escape(str(result['next_action']))}</p>{''.join(sections)}</html>"
    )


def candle_chart(trade: Trade) -> str:
    if not trade.candles:
        return ""
    prices = [price for bar in trade.candles for price in (bar.low, bar.high)]
    low, high = (
        min(*prices, trade.stop, trade.target),
        max(*prices, trade.stop, trade.target),
    )
    span = high - low or Decimal(1)

    def y(price: Decimal) -> float:
        return 250 - float((price - low) / span) * 220

    marks = []
    for index, bar in enumerate(trade.candles):
        x = 55 + index * 21
        color = "#a04600" if index == 20 else "#263544"
        marks.append(
            f"<line x1='{x}' x2='{x}' y1='{y(bar.high)}' y2='{y(bar.low)}' stroke='{color}'/>"
            f"<rect x='{x - 5}' y='{y(max(bar.open, bar.close))}' width='10' "
            f"height='{max(1, abs(y(bar.open) - y(bar.close)))}' fill='{color}'/>"
        )
    for label, price in (
        ("Entry", trade.entry),
        ("Stop", trade.stop),
        ("Target", trade.target),
    ):
        marks.append(
            f"<line x1='485' x2='620' y1='{y(price)}' y2='{y(price)}' stroke='#35637a'/>"
            f"<text x='625' y='{y(price) + 4}'>{label} {price}</text>"
        )
    return (
        "<svg viewBox='0 0 800 290' role='img' aria-label='Hourly source candles and trade levels'>"
        "<rect width='800' height='290' fill='white'/>"
        f"{''.join(marks)}<text x='470' y='20'>Signal</text>"
        "<text x='55' y='280'>Previous 20 hourly candles</text>"
        "<text x='500' y='280'>Entry and six-hour outcome window</text></svg>"
    )
