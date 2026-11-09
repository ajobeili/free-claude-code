"""Reference windows must be contiguous, completed, and executable quote pairs."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tools.capital_api import CapitalAPI
from tools.pattern_trigger import fetch_ranges

AS_OF = datetime(2026, 11, 4, 9, 10, 15, tzinfo=UTC)


def records():
    result = []
    for offset in range(25):
        time = AS_OF.replace(second=0) - timedelta(minutes=offset)
        high = 10000 if offset in (0, 21, 22, 23, 24) else 110
        result.append(
            {
                "snapshotTimeUTC": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "openPrice": {"bid": 99, "ask": 101},
                "highPrice": {"bid": high - 1, "ask": high + 1},
                "lowPrice": {"bid": 89, "ask": 91},
                "closePrice": {"bid": 99, "ask": 101},
            }
        )
    return result


async def run_history(rows):
    api = CapitalAPI()

    def respond(request):
        assert request.url.params["resolution"] == "MINUTE"
        return httpx.Response(200, json={"prices": rows})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api._client = client
        return await fetch_ranges(api, AS_OF)


def test_forming_and_older_candles_do_not_contaminate_twenty_candle_range():
    ranges = asyncio.run(run_history(records()))
    assert set(ranges) == {"GOLD", "OIL_CRUDE"}
    assert ranges["GOLD"].high == 110
    assert ranges["GOLD"].low == 90


def test_missing_minute_keeps_detector_warming():
    rows = records()
    del rows[5]
    assert asyncio.run(run_history(rows)) == {}


@pytest.mark.parametrize("problem", ["crossed", "invalid_ohlc", "conflict"])
def test_invalid_or_conflicting_history_is_rejected(problem):
    rows = records()
    if problem == "crossed":
        rows[1]["highPrice"] = {"bid": 112, "ask": 110}
    elif problem == "invalid_ohlc":
        rows[1]["lowPrice"] = {"bid": 119, "ask": 121}
    else:
        row = {**rows[1], "highPrice": {"bid": 119, "ask": 121}}
        rows.append(row)
    with pytest.raises(ValueError):
        asyncio.run(run_history(rows))
