"""Read authorized local observations and run the offline pattern study."""

import argparse
import csv
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from tools.pattern_report import candle_chart, feasibility, render
from tools.pattern_simulation import HOUR, MARKETS, VARIANTS, Bar, Series, simulate
from tools.pattern_statistics import bounds, summarize


def timestamp(value: str) -> datetime:
    time = datetime.fromisoformat(value)
    if time.utcoffset() is None:
        raise ValueError("Timezone required")
    return time.astimezone(UTC)


def load(path: Path) -> tuple[dict[tuple[str, str], Series], list[dict[str, object]]]:
    audit: list[dict[str, object]] = []
    books: dict[tuple[str, str], Series] = {}
    identities: dict[tuple[str, str, datetime], list[int]] = {}
    conflicts: set[tuple[str, str, datetime]] = set()
    with path.open(encoding="utf-8-sig", newline="") as file:
        for index, row in enumerate(csv.DictReader(file), 2):
            item: dict[str, object] = {
                "row": index,
                "source": row.get("source"),
                "status": "accepted",
            }
            audit.append(item)
            try:
                market, side, time = (
                    row["market"],
                    row["quote_side"],
                    timestamp(row["hour_start"]),
                )
                if (
                    market not in MARKETS
                    or side not in ("bid", "ask")
                    or not row["source"]
                    or row["completed"] != "true"
                    or time != time.replace(minute=0, second=0, microsecond=0)
                ):
                    raise ValueError(
                        "Invalid market, side, provenance or completed hour"
                    )
                values = [Decimal(row[key]) for key in ("open", "high", "low", "close")]
                if any(not value.is_finite() or value <= 0 for value in values):
                    raise ValueError("Invalid price")
                bar = Bar(*values)
                if (
                    not bar.low
                    <= min(bar.open, bar.close)
                    <= max(bar.open, bar.close)
                    <= bar.high
                ):
                    raise ValueError("Invalid OHLC")
                identity = (market, side, time)
                series = books.setdefault((market, side), {})
                indices = identities.setdefault(identity, [])
                indices.append(len(audit) - 1)
                if identity in conflicts or (time in series and series[time] != bar):
                    conflicts.add(identity)
                    series.pop(time, None)
                    for row_index in indices:
                        audit[row_index].update(
                            status="excluded", reason="conflicting duplicate"
                        )
                elif time in series:
                    item["status"] = "duplicate"
                else:
                    series[time] = bar
            except (KeyError, ValueError, TypeError, ArithmeticError) as error:
                item.update(status="excluded", reason=str(error))
    return books, audit


def run(observations: Path, manifest_path: Path, output: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    digest = hashlib.sha256(observations.read_bytes()).hexdigest()
    if manifest["sha256"] != digest or manifest["version"] != 1:
        raise ValueError("Frozen manifest does not match study inputs")
    development = tuple(map(timestamp, manifest["development"]))
    validation = (
        tuple(map(timestamp, manifest["validation"]))
        if manifest.get("validation")
        else None
    )
    inspected = [tuple(map(timestamp, interval)) for interval in manifest["inspected"]]
    inspected.append((timestamp("2026-10-21T00:00Z"), timestamp("2026-11-04T00:00Z")))
    if development[0] >= development[1] or (
        validation
        and (
            validation[0] >= validation[1]
            or validation[0] < development[1] + 6 * HOUR
            or any(
                validation[0] < end and validation[1] > start
                for start, end in inspected
            )
        )
    ):
        raise ValueError("Invalid or previously inspected phase")
    if validation and (
        not manifest.get("frozen_at")
        or timestamp(manifest["frozen_at"]) >= validation[0]
        or manifest.get("method") != "paired-5day-50000-seed204-bonferroni10-v1"
    ):
        raise ValueError("Validation requires a dated frozen method")
    books, audit = load(observations)
    groups: list[dict[str, object]] = []
    for phase, interval in (("development", development), ("validation", validation)):
        schedules = []
        primary = []
        coverage: list[set[datetime]] = []
        for market in MARKETS:
            other = MARKETS[1 - MARKETS.index(market)]
            terms = manifest.get("markets", {}).get(market, {})
            start, end = interval if interval else development

            def series(
                name: str, side: str, active=interval, left=start, right=end
            ) -> Series:
                return {
                    time: bar
                    for time, bar in books.get((name, side), {}).items()
                    if active and left <= time and time + HOUR <= right
                }

            ask, bid, counterpart = (
                series(market, "ask"),
                series(market, "bid"),
                series(other, "ask"),
            )
            for time in ask.keys() & bid.keys():
                if bid[time].open > ask[time].open or bid[time].close > ask[time].close:
                    raise ValueError(f"Crossed quotes for {market} at {time}")
            boundaries = set(map(timestamp, terms.get("boundaries", [])))
            extra, spread = (
                Decimal(str(terms.get("extra", 0))),
                Decimal(str(terms.get("spread", 0))),
            )
            if any(not value.is_finite() or value < 0 for value in (extra, spread)):
                raise ValueError("Costs must be finite and nonnegative")
            measured = bool(
                bid
                and terms.get("cost_source")
                and terms.get("boundary_source")
                and boundaries
                and manifest.get("evidence") == "historical"
            )
            coverage.append(ask.keys() & bid.keys())
            for variant in VARIANTS:
                trades, skipped = simulate(
                    ask, bid, counterpart, variant, extra, spread, boundaries
                )
                schedules.append(trades)
                primary.append(measured)
                groups.append(
                    {
                        "phase": phase,
                        "market": market,
                        "variant": variant,
                        **summarize(trades),
                        "eligible_signals": len(trades) + sum(skipped.values()),
                        "exclusions": skipped,
                        "cost_source": terms.get("cost_source", "assumed"),
                        "extra_cost_per_unit": str(extra),
                        "assumed_spread_per_unit": str(spread),
                        "status": "exploratory"
                        if phase == "development"
                        else "insufficient evidence",
                        "occurrences": [
                            {
                                **asdict(trade),
                                "chart": candle_chart(trade),
                                "sizing": {
                                    str(balance): feasibility(
                                        trade, balance, terms.get("contract", {})
                                    )
                                    for balance in (100, 200)
                                },
                            }
                            for trade in trades
                        ],
                    }
                )
        if phase == "validation" and interval:
            expected = {
                timestamp(value) for value in manifest.get("shared_session_hours", [])
            }
            calendar = {
                hour.date() for hour in expected if interval[0] <= hour < interval[1]
            }
            covered = set.intersection(*coverage)
            complete = {
                day
                for day in calendar
                if all(hour in covered for hour in expected if hour.date() == day)
            }
            trade_days = {trade.time.date() for trades in schedules for trade in trades}
            if (
                all(primary)
                and len(complete) >= 60
                and complete == calendar
                and trade_days <= complete
                and manifest.get("session_calendar_source")
            ):
                limits = bounds(schedules, sorted(complete))
                for comparison, index in enumerate((1, 2, 4, 5), 6):
                    group = groups[-6 + index]
                    trades = schedules[index]
                    mean, improvement = limits[index], limits[comparison]
                    group.update(
                        mean_lower=mean,
                        improvement_lower=improvement,
                        status="inconclusive",
                    )
                    if (
                        len(trades) >= 100
                        and len({t.time.date() for t in trades}) >= 60
                    ):
                        if (
                            mean is not None
                            and improvement is not None
                            and min(mean, improvement) > 0
                            and sum(t.doubled for t in trades) > 0
                        ):
                            group["status"] = "promising for further demo study"
                        else:
                            group["status"] = "rejected"
                    else:
                        group["status"] = "insufficient evidence"
    promising = any(
        group["status"] == "promising for further demo study" for group in groups
    )
    result = {
        "sha256": digest,
        "manifest": manifest,
        "audit": audit,
        "groups": groups,
        "next_action": (
            "Repeat on a fresh demo holdout."
            if promising
            else "Review the examples and obtain fresh, matched prices and verified cost records before drawing a trading conclusion."
        ),
    }
    html = render(result, groups)
    payload = json.dumps(result, indent=2, default=str, allow_nan=False)
    output.mkdir()
    (output / "study.json").write_text(payload, encoding="utf-8")
    (output / "study.html").write_text(html, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("observations", "manifest", "output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    args = parser.parse_args()
    try:
        run(args.observations, args.manifest, args.output)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        parser.exit(2, f"Study error: {error}\n")


if __name__ == "__main__":
    main()
