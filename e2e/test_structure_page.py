from pathlib import Path

from playwright.sync_api import Page

from tests.test_structure_scan import sample
from tools.structure_scan import analyze


def test_page_scan_and_error(page: Page):

    payload = dict(
        analyze(sample()),
        symbol="EURUSD",
        epic="EURUSD",
        timeframe="HOUR",
        as_of=21600,
        stale=False,
    )
    page.route("**/api/structure?*", lambda route: route.fulfill(json=payload))
    page.route(
        "http://structure.test/",
        lambda route: route.fulfill(
            content_type="text/html",
            body=Path("tools/structure_scan.html").read_text(encoding="utf-8"),
        ),
    )
    page.goto("http://structure.test/")
    page.get_by_role("button", name="Run scan").click()
    page.wait_for_function(
        "document.getElementById('status').textContent.includes('BUY candidate')"
    )
    assert page.locator("#chart").is_visible()
    assert page.locator("#currentCandle").inner_text() == "Latest candle #7 · Close 15"
    assert (
        "Wait for price to retrace down"
        in page.locator("#chartExplanation").inner_text()
    )
    assert "50% of the gap" in page.locator("#chartExplanation").inner_text()
    assert "BUY entry: midpoint retracement" in page.locator("#chartKey").inner_text()
    page.screenshot(path=".uv-cache/structure-explained.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 1280, "height": 720})
    page.evaluate("data.stale=true; explainChart(); draw()")
    assert "stale" in page.locator("#chartExplanation").inner_text()
    assert "STALE" in page.locator("#currentCandle").inner_text()
    assert "BUY entry" not in page.locator("#chartKey").inner_text()
    page.evaluate("data.stale=false; data.setup=null; explainChart(); draw()")
    assert "no entry line" in page.locator("#chartExplanation").inner_text()
    page.evaluate(
        """data.assessment={status:'Touched',bid:12,ask:12.1,spread:.1,stop:9,target:null,reward_risk:null,checks:['Forming candle reached entry','HOUR: CONFLICT with entry direction']}; explainChart(); draw()"""
    )
    assert "Touched" in page.locator("#assessmentStatus").inner_text()
    assert "already been reached" in page.locator("#chartExplanation").inner_text()
    assert "CONFLICT" in page.locator("#assessmentChecks").inner_text()
    page.unroute("**/api/structure?*")
    page.route(
        "**/api/structure?*",
        lambda route: route.fulfill(status=502, json={"detail": "Broker unavailable"}),
    )
    page.get_by_role("button", name="Run scan").click()
    page.wait_for_function(
        "document.getElementById('status').textContent === 'Broker unavailable'"
    )
    assert page.locator("#result").is_hidden()


def test_watchlist_and_all_timeframes(page: Page):
    requests = []

    def respond(route):
        url = route.request.url
        requests.append(url)
        if "symbol=OIL_CRUDE" in url:
            route.fulfill(status=502, json={"detail": "Broker unavailable"})
        else:
            route.fulfill(
                json=dict(
                    analyze(sample()),
                    symbol="EURUSD",
                    epic="EURUSD",
                    timeframe="HOUR",
                    as_of=21600,
                    stale=False,
                )
            )

    page.route("**/api/structure?*", respond)
    page.route(
        "http://structure.test/",
        lambda route: route.fulfill(
            content_type="text/html",
            body=Path("tools/structure_scan.html").read_text(encoding="utf-8"),
        ),
    )
    page.goto("http://structure.test/")
    page.get_by_role("button", name="Opportunity Scan", exact=True).click()
    page.wait_for_function(
        "document.getElementById('batchProgress').textContent.includes('Completed 2/2')"
    )
    assert len(requests) == 2
    assert "1 failed" in page.locator("#batchProgress").inner_text()
    assert page.locator("#batchRows tr").count() == 2
    page.get_by_role("button", name="Open chart").click()
    assert page.locator("#chart").is_visible()
    requests.clear()
    page.get_by_role("button", name="Scan All Timeframes").click()
    page.wait_for_function(
        "document.getElementById('batchProgress').textContent.includes('Completed 7/7')"
    )
    assert len(requests) == 7
    assert len(set(requests)) == 7
    assert all("symbol=EURUSD" in url for url in requests)
    assert page.locator("#batchRows tr").count() == 7
    for checkbox in page.locator("#watchlistChoices input").all():
        checkbox.uncheck()
    requests.clear()
    page.get_by_role("button", name="Opportunity Scan", exact=True).click()
    assert "Select at least one" in page.locator("#batchProgress").inner_text()
    assert not requests


def test_batch_refresh_and_pause(page: Page):
    requests = []

    def respond(route):
        requests.append(route.request.url)
        route.fulfill(
            json=dict(
                analyze(sample()),
                symbol="EURUSD",
                epic="EURUSD",
                timeframe="HOUR",
                as_of=21600,
                stale=False,
            )
        )

    page.route("**/api/structure?*", respond)
    page.route(
        "http://structure.test/",
        lambda route: route.fulfill(
            content_type="text/html",
            body=Path("tools/structure_scan.html").read_text(encoding="utf-8"),
        ),
    )
    page.goto("http://structure.test/")
    page.clock.install()
    page.get_by_role("button", name="Scan All Timeframes").click()
    page.wait_for_function(
        "document.getElementById('batchProgress').textContent.includes('Completed 7/7')"
    )
    assert "Last updated" in page.locator("#batchProgress").inner_text()
    assert "Next refresh in" in page.locator("#refreshStatus").inner_text()
    assert len(requests) == 7
    page.clock.fast_forward(61000)
    page.wait_for_function(
        "document.getElementById('batchProgress').textContent.includes('Completed 7/7') && !scanning"
    )
    assert len(requests) == 14
    page.locator("#refreshInterval").select_option("0")
    page.clock.fast_forward(120000)
    assert len(requests) == 14
    assert "paused" in page.locator("#refreshStatus").inner_text()
    page.unroute("**/api/structure?*")
    page.route(
        "**/api/structure?*",
        lambda route: route.fulfill(status=401, json={"detail": "Login required"}),
    )
    page.get_by_role("button", name="Scan All Timeframes").click()
    page.wait_for_function("!scanning")
    assert "access was limited" in page.locator("#refreshStatus").inner_text()
    assert page.locator("#refreshInterval").input_value() == "0"
