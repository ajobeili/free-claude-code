from dataclasses import asdict
from pathlib import Path

from playwright.sync_api import Page

from tests.test_structure_scan import sample
from tests.test_trade_planner import paper


def test_planner_creation_and_cancel(page: Page):
    p = paper()
    p.chart_bars = [
        {
            key: value if key == "time" else value + 90
            for key, value in asdict(b).items()
        }
        for b in sample()
    ]
    state = {
        "plan": None,
        "events": [],
        "closed_count": 0,
        "net_pnl": 0,
        "mean_r": None,
    }
    page.route(
        "http://planner.test/planner",
        lambda route: route.fulfill(
            content_type="text/html",
            body=Path("tools/trade_planner.html").read_text(encoding="utf-8"),
        ),
    )

    def api(route):
        if route.request.method == "POST":
            state["plan"] = p.model_dump()
        route.fulfill(json=state)

    page.route("**/api/planner", api)
    page.route("**/api/planner/observe", lambda route: route.fulfill(json=state))

    def finish(route):
        p.status = "cancelled"
        state["plan"] = p.model_dump()
        route.fulfill(json=state)

    page.route("**/api/planner/finish", finish)
    page.goto("http://planner.test/planner")
    assert page.locator("#symbol").get_attribute("list") == "plannerSymbols"
    assert page.locator('#plannerSymbols option[value="OIL_CRUDE"]').count() == 1
    assert page.locator('#plannerSymbols option[value="OIL_BRENT"]').count() == 1
    page.get_by_role("button", name="Ask AI for a plan").click()
    page.wait_for_function(
        "document.getElementById('decision').textContent.includes('PENDING')"
    )
    assert page.get_by_role("button", name="Ask AI for a plan").is_disabled()
    page.screenshot(path=".uv-cache/planner-preview.png", full_page=True)
    assert "No fills are invented" in page.locator("#path").inner_text()
    page.get_by_role("button", name="Cancel pending simulation").click()
    page.wait_for_function(
        "document.getElementById('decision').textContent.includes('CANCELLED')"
    )
    assert page.get_by_role("button", name="Ask AI for a plan").is_enabled()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
