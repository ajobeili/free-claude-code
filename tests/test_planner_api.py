"""Planner endpoint contracts with stubbed AI and broker data."""

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from tests.test_structure_scan import sample, scan_server
from tools.trade_planner import PlannerRequest, PlannerStore


def test_total_deadline_cancels_market_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        scan_server, "_PLANNER_STORE", PlannerStore(tmp_path / "deadline.sqlite3")
    )
    monkeypatch.setattr(scan_server, "_PLANNER_LOCK", asyncio.Lock())
    monkeypatch.setattr(scan_server, "_PLANNER_DEADLINE_SECONDS", 0.01)
    cancelled = []

    async def slow_snapshot(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(scan_server, "structure_scan", slow_snapshot)

    async def run():
        with pytest.raises(scan_server.HTTPException) as error:
            await scan_server.create_plan(PlannerRequest(symbol="GOLD"))
        assert error.value.status_code == 504
        assert cancelled == [True]
        assert not scan_server._PLANNER_LOCK.locked()
        assert scan_server._PLANNER_STORE.latest() is None

    asyncio.run(run())


def test_create_plan_validates_ai_and_prevents_replacement(monkeypatch, tmp_path):
    monkeypatch.setattr(
        scan_server, "_PLANNER_STORE", PlannerStore(tmp_path / "plans.sqlite3")
    )

    async def snapshot(*args):
        return {
            "epic": "GOLD",
            "stale": False,
            "bars": [asdict(b) for b in sample()],
            "setup": {
                "direction": "buy",
                "entry": 12,
                "lower": 10,
                "upper": 14,
                "index": 6,
            },
            "assessment": {
                "status": "Waiting",
                "stop": 9,
                "target": 20,
                "contexts": [{"bias": "buy", "stale": False}],
            },
        }

    async def quote(*args):
        return {
            "bid": 15,
            "offer": 15.1,
            "market_status": "TRADEABLE",
            "last_update": datetime.now(UTC).isoformat(),
        }

    async def ai(*args):
        return {
            "text": '{"candidate_id":"structure","reasoning":"Wait for retracement","counterargument":"Trend can reverse"}'
        }

    monkeypatch.setattr(scan_server, "structure_scan", snapshot)
    monkeypatch.setattr(scan_server, "tick", quote)
    monkeypatch.setattr(scan_server, "choose_trade_plan", ai)

    async def run():
        result = await scan_server.create_plan(PlannerRequest(symbol="GOLD"))
        assert result["plan"]["status"] == "pending"
        assert result["plan"]["candidate"]["entry"] == 12
        assert result["plan"]["risk_budget"] == 50
        with pytest.raises(scan_server.HTTPException) as error:
            await scan_server.create_plan(PlannerRequest(symbol="GOLD"))
        assert error.value.status_code == 409
        await scan_server.finish_plan()

        async def invalid(*args):
            return {
                "text": '{"candidate_id":"invented","reasoning":"Test","counterargument":"Test"}'
            }

        monkeypatch.setattr(scan_server, "choose_trade_plan", invalid)
        with pytest.raises(scan_server.HTTPException) as error:
            await scan_server.create_plan(PlannerRequest(symbol="GOLD"))
        assert error.value.status_code == 422
        latest = scan_server._PLANNER_STORE.latest()
        assert latest is not None and latest.status == "cancelled"

        async def timeout(*args):
            return {"error": "timeout"}

        monkeypatch.setattr(scan_server, "choose_trade_plan", timeout)
        with pytest.raises(scan_server.HTTPException) as error:
            await scan_server.create_plan(PlannerRequest(symbol="GOLD"))
        assert error.value.status_code == 504
        latest = scan_server._PLANNER_STORE.latest()
        assert latest is not None and latest.status == "cancelled"

        async def unexpected_ai(*args):
            raise AssertionError("No eligible candidates should skip AI")

        monkeypatch.setattr(scan_server, "choose_trade_plan", unexpected_ai)
        monkeypatch.setattr(
            scan_server, "candidates_for", lambda *args: ([], ["Spread too wide"])
        )
        result = await scan_server.create_plan(PlannerRequest(symbol="GOLD"))
        assert result["plan"]["status"] == "wait"
        assert result["plan"]["ai_used"] is False
        assert "Spread too wide" in result["plan"]["reasoning"]
        assert result["plan"]["candidate"] is None

    asyncio.run(run())
