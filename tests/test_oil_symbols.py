import asyncio

import pytest

from tools.capital_api import CapitalAPI, resolve_epic


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("OIL_CRUDE", "OIL_CRUDE"),
        ("WTI", "OIL_CRUDE"),
        ("OILUSD", "OIL_CRUDE"),
        (" crudeoil ", "OIL_CRUDE"),
        ("OIL_CRUDE", "OIL_CRUDE"),
        ("UKOIL", "OIL_BRENT"),
        ("BRENT", "OIL_BRENT"),
        ("OILCUSD", "OIL_BRENT"),
        ("OIL_BRENT", "OIL_BRENT"),
    ],
)
def test_oil_aliases(symbol, expected):
    assert asyncio.run(resolve_epic(CapitalAPI(), symbol)) == expected
