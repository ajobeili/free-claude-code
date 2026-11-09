"""Validated paper trade plans and durable, append-only decision history."""

import math
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from tools.strategy_engine import ALL_STRATEGIES


class Candidate(BaseModel):
    id: str
    strategy: str
    direction: Literal["buy", "sell"]
    entry: float
    stop: float
    target: float
    reason: str


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str | None
    reasoning: str = Field(min_length=1, max_length=3000)
    counterargument: str = Field(min_length=1, max_length=2000)


class PlannerRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9._-]+$")
    timeframe: Literal["MINUTE_5", "MINUTE_15", "HOUR", "HOUR_4"] = "MINUTE_15"
    balance: float = Field(default=10000, gt=0, le=100000000, allow_inf_nan=False)
    risk_pct: float = Field(default=0.5, gt=0, le=2, allow_inf_nan=False)
    minimum_rr: float = Field(default=1.5, ge=1, le=5, allow_inf_nan=False)
    expires_minutes: int = Field(default=60, ge=5, le=240)


class Quote(BaseModel):
    bid: float
    ask: float
    at: float


def quote_from(raw: dict, now: float) -> Quote:
    """Use the broker timestamp, not the HTTP request timestamp."""
    if raw.get("error"):
        raise ValueError(
            "Broker quote lookup failed. Market availability could not be confirmed."
        )
    status = raw.get("market_status")
    if status != "TRADEABLE":
        if status == "CLOSED":
            raise ValueError("Broker reports this market is closed.")
        raise ValueError(
            "Broker has not confirmed tradeable market status. Retry the quote lookup."
        )
    stamp = str(raw.get("last_update", ""))
    try:
        if "T" not in stamp and " " not in stamp:
            stamp = datetime.fromtimestamp(now, UTC).date().isoformat() + "T" + stamp
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        at = (
            parsed.replace(tzinfo=UTC).timestamp()
            if parsed.tzinfo is None
            else parsed.timestamp()
        )
        if at - now > 43200:
            at -= 86400
        bid, ask = float(raw["bid"]), float(raw["offer"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("Broker quote timestamp or prices unavailable.") from exc
    if (
        not all(math.isfinite(v) and v > 0 for v in (bid, ask))
        or ask < bid
        or not -5 <= now - at <= 90
    ):
        raise ValueError(
            "Broker quote is stale or invalid. No simulated fill recorded."
        )
    return Quote(bid=bid, ask=ask, at=at)


def candidates_for(
    snapshot: dict, request: PlannerRequest, quote: Quote
) -> tuple[list[Candidate], list[str]]:
    candidates = []
    rejected = []
    if snapshot.get("stale"):
        return [], ["Completed candles are stale."]
    setup = snapshot.get("setup")
    assessment = snapshot.get("assessment", {})
    if (
        setup
        and assessment.get("status") == "Waiting"
        and assessment.get("target") is not None
        and assessment.get("stop") is not None
    ):
        candidates.append(
            Candidate(
                id="structure",
                strategy="Structure FVG retracement",
                direction=setup["direction"],
                entry=setup["entry"],
                stop=assessment["stop"],
                target=assessment["target"],
                reason="Confirmed structure break and untouched FVG midpoint.",
            )
        )
    bars = [{**b, "volume": 0} for b in snapshot["bars"]]
    for name in ("trend_ema_pullback", "momentum_breakout"):
        signal = ALL_STRATEGIES[name](bars, request.symbol)
        if signal:
            candidates.append(
                Candidate(
                    id=name,
                    strategy=name.replace("_", " "),
                    direction="buy" if signal.direction == "long" else "sell",
                    entry=signal.entry,
                    stop=signal.stop_loss,
                    target=signal.take_profit,
                    reason=signal.reasoning,
                )
            )
    valid = []
    for c in candidates:
        reason = validate_candidate(c, request, quote, assessment.get("contexts", []))
        if reason:
            rejected.append(c.strategy + ": " + reason)
        else:
            valid.append(c)
    return valid, rejected


def validate_candidate(
    c: Candidate, request: PlannerRequest, quote: Quote, contexts: list[dict]
) -> str | None:
    if not all(math.isfinite(v) and v > 0 for v in (c.entry, c.stop, c.target)):
        return "Invalid price levels."
    if not (
        c.stop < c.entry < c.target
        if c.direction == "buy"
        else c.target < c.entry < c.stop
    ):
        return "Entry, stop and target are inconsistent."
    if c.entry >= quote.ask if c.direction == "buy" else c.entry <= quote.bid:
        return "Retracement entry is already reached or on the wrong side of the quote."
    if not contexts or any(
        cxt.get("stale") or cxt.get("bias") in ("unavailable", None) for cxt in contexts
    ):
        return "Higher-timeframe context is unavailable."
    if any(cxt.get("bias") not in (c.direction, "neutral") for cxt in contexts):
        return "Higher-timeframe direction conflicts with the plan."
    spread = quote.ask - quote.bid
    if (abs(c.target - c.entry) - spread) / (
        abs(c.entry - c.stop) + spread
    ) < request.minimum_rr:
        return "Reward-to-risk after spread is below the selected minimum."
    return None


class Scenario(BaseModel):
    name: str
    trigger: str
    action: str
    invalidation: str


def build_scenarios(bars: list[dict[str, float]]) -> list[Scenario]:
    """Observable boundaries, not predicted prices or executable entries."""
    if len(bars) < 21:
        return []
    window = bars[-21:-1]
    upper = max(b["high"] for b in window)
    lower = min(b["low"] for b in window)
    return [
        Scenario(
            name="Bullish breakout",
            trigger=f"Completed candle closes above {upper:g} (prior 20-bar high).",
            action="Reassess breakout or pullback strategy after confirmation; do not enter on a wick alone.",
            invalidation=f"Breakout closes back inside the range below {upper:g}.",
        ),
        Scenario(
            name="Bearish breakout",
            trigger=f"Completed candle closes below {lower:g} (prior 20-bar low).",
            action="Reassess breakout or pullback strategy after confirmation; request a fresh risk-checked plan.",
            invalidation=f"Breakout closes back inside the range above {lower:g}.",
        ),
        Scenario(
            name="Inside range",
            trigger=f"Completed closes stay between {lower:g} and {upper:g}.",
            action="Avoid chasing a breakout. No range trade is validated by these boundaries alone.",
            invalidation="A completed close leaves the range.",
        ),
    ]


class Plan(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    created: float
    expires: float
    request: PlannerRequest
    candidate: Candidate | None
    reasoning: str
    counterargument: str
    status: Literal[
        "wait", "pending", "open", "closed", "expired", "cancelled", "review"
    ]
    ai_used: bool
    quantity: float = 0
    risk_budget: float = 0
    last_observed: float | None = None
    last_quote_at: float | None = None
    entry_fill: float | None = None
    exit_fill: float | None = None
    pnl: float | None = None
    note: str = ""
    chart_bars: list[dict[str, float]] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    alternatives: list[Candidate] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)


class PlannerStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, plan_id TEXT, at REAL, message TEXT)"
        )
        try:
            with db:
                yield db
        finally:
            db.close()

    def latest(self) -> Plan | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT body FROM plans ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return Plan.model_validate_json(row[0]) if row else None

    def save(self, plan: Plan, message: str) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO plans VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (plan.id, plan.model_dump_json()),
            )
            if message:
                db.execute(
                    "INSERT INTO events(plan_id,at,message) VALUES (?,?,?)",
                    (plan.id, time.time(), message),
                )

    def history(self) -> dict:
        with self.connection() as db:
            events = [
                {"id": r[0], "at": r[1], "message": r[2]}
                for r in db.execute(
                    "SELECT plan_id,at,message FROM events ORDER BY sequence DESC LIMIT 100"
                )
            ]
            plans = [
                Plan.model_validate_json(row[0])
                for row in db.execute("SELECT body FROM plans ORDER BY rowid")
            ]
        closed = [p for p in plans if p.pnl is not None]
        groups: dict[str, list[float]] = {}
        for p in closed:
            if p.candidate and p.risk_budget:
                label = f"{p.request.symbol} · {p.candidate.strategy}"
                groups.setdefault(label, []).append((p.pnl or 0) / p.risk_budget)
        return {
            "performance": [
                {"group": k, "trades": len(v), "mean_r": sum(v) / len(v)}
                for k, v in groups.items()
            ],
            "events": events,
            "closed_count": len(closed),
            "net_pnl": sum(p.pnl or 0 for p in closed),
            "mean_r": sum((p.pnl or 0) / p.risk_budget for p in closed if p.risk_budget)
            / len(closed)
            if closed
            else None,
        }


def advance(plan: Plan, quote: Quote, now: float) -> str | None:
    c = plan.candidate
    if not c or plan.status not in ("pending", "open"):
        return None
    if plan.status == "pending" and now >= plan.expires:
        plan.status = "expired"
        return "Plan expired without an observed entry."
    if plan.last_observed is not None and now - plan.last_observed > 45:
        plan.status = "expired" if plan.status == "pending" else "review"
        return "Monitoring gap exceeded 45 seconds. No fills inferred during the gap."
    if quote.at < plan.created or (
        plan.last_quote_at is not None and quote.at <= plan.last_quote_at
    ):
        return None
    plan.last_observed = now
    plan.last_quote_at = quote.at
    if plan.status == "pending":
        if quote.bid <= c.stop if c.direction == "buy" else quote.ask >= c.stop:
            plan.status = "cancelled"
            return "Stop reference crossed before entry; candidate cancelled."
        if quote.ask <= c.entry if c.direction == "buy" else quote.bid >= c.entry:
            # Conservative paper limit fill: no favorable price improvement assumed.
            plan.entry_fill = c.entry
            plan.status = "open"
            return f"Simulated {c.direction} opened at {c.entry:g}; no broker order."
    elif plan.status == "open":
        price = quote.bid if c.direction == "buy" else quote.ask
        stop = price <= c.stop if c.direction == "buy" else price >= c.stop
        target = price >= c.target if c.direction == "buy" else price <= c.target
        if stop or target:
            plan.exit_fill = price if stop else c.target
            assert plan.entry_fill is not None
            plan.pnl = (
                (plan.exit_fill - plan.entry_fill)
                * plan.quantity
                * (1 if c.direction == "buy" else -1)
            )
            plan.status = "closed"
            return (
                "Simulated stop exit at observed quote."
                if stop
                else "Simulated target exit at target limit."
            )
    return None
