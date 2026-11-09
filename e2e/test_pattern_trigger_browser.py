"""Render real event payloads; never submit orders."""

from pathlib import Path


def test_pattern_page_displays_live_stages_and_research_alert(page):

    page.route(
        "http://trigger.test/pattern",
        lambda route: route.fulfill(
            content_type="text/html",
            body=Path("tools/pattern_scan.html").read_text(encoding="utf-8"),
        ),
    )

    page.route(
        "**/module_activity.js",
        lambda route: route.fulfill(
            content_type="application/javascript",
            body=Path("tools/module_activity.js").read_text(),
        ),
    )

    page.route("**/api/**", lambda route: route.fulfill(json={"authenticated": True}))

    def records(route):
        if "/quotes.json" in route.request.url:
            route.fulfill(status=503)
            return
        later = "offset=25" in route.request.url
        route.fulfill(
            json={
                "columns": ["epic", "started", "outcome", "reason"],
                "rows": [
                    ["GOLD", "2026-11-04T10:00:00Z", "qualified", "<b>Evidence</b>"]
                ]
                if not later
                else [],
                "has_more": not later,
                "offset": 25 if later else 0,
            }
        )

    page.route("**/api/pattern/journal/*.json?*", records)

    page.add_init_script("""

        window.scanEvents=[];

        window.EventSource=class {

            constructor(url){this.url=url;window.scanEvents.push(this)}

            close(){}

        };

    """)

    page.goto("http://trigger.test/pattern")

    page.wait_for_function("window.scanEvents.length > 0")

    state = {
        "authenticated": True,
        "feed_connected": True,
        "status": "watching",
        "markets": [
            {
                "market": "Gold",
                "phase": "watching",
                "price": 100,
                "range_high": 110,
                "range_low": 90,
                "detail": "Watching live prices",
            }
        ],
        "alerts": [],
    }

    def send():

        page.evaluate(
            "state => window.scanEvents.at(-1).onmessage({data:JSON.stringify(state)})",
            state,
        )

    send()

    assert page.get_by_text(
        "Scanner is watching for a pattern", exact=True
    ).is_visible()

    state["markets"][0]["phase"] = "breakout"

    send()

    assert page.get_by_text(
        "Price moved outside its range — watching for a return", exact=True
    ).is_visible()

    state["alerts"] = [
        {
            "market": "Gold",
            "direction": "DOWN",
            "trigger_time": "09:10:15Z",
            "breakout_time": "09:10:13Z",
            "breakout_price": 114,
            "return_price": 109,
            "range_low": 90,
            "range_high": 110,
            "other_market": "Crude oil",
            "other_start": 50,
            "other_end": 49,
        }
    ]

    send()

    assert page.get_by_text("Gold | SELL | DEMO", exact=True).is_visible()

    assert page.get_by_text(
        "Unvalidated demo. No trade placed.", exact=True
    ).is_visible()

    state["markets"][0]["phase"] = "watching"

    state["quote_delay_seconds"] = 15

    state["delayed_research"] = True

    state["alerts"][0]["data_mode"] = "DELAYED RESEARCH"

    state["alerts"][0]["quote_delay_seconds"] = 16

    send()

    assert page.get_by_text(
        "Scanner is watching — delayed prices", exact=True
    ).is_visible()

    assert "Delayed prices" in page.locator("#dataMode").inner_text()

    assert "not live entry alerts" in page.locator("#dataMode").inner_text()

    state["notifications"] = {
        "enabled": True,
        "status": "Ready — Hermes Telegram",
        "sent": 0,
        "failed": 0,
    }

    send()

    assert "Telegram via Hermes" in page.locator("#notifications").inner_text()

    state["journal"] = {
        "running": True,
        "quotes": 100,
        "attempts": 2,
        "qualified": 1,
        "complete_outcomes": 1,
        "incomplete_outcomes": 0,
        "pending_outcomes": 2,
        "dropped": 0,
        "recent": [
            {
                "epic": "GOLD",
                "outcome": "context_failed",
                "reason": "Other market did not move oppositely",
                "started": "09:10:00Z",
            }
        ],
        "markouts": [
            {
                "epic": "GOLD",
                "seconds": 60,
                "observations": 2,
                "qualified_observations": 1,
                "mean_change_points": 0.2,
                "qualified_mean_change_points": 0.4,
            }
        ],
    }

    send()

    page.get_by_text("Research journal & results", exact=True).click()
    assert "2 price moves checked" in page.locator("#attemptProgress").inner_text()
    assert (
        "the other market did not confirm"
        in page.locator("#attemptProgress").inner_text()
    )
    page.get_by_role("cell", name="All rules matched", exact=True).wait_for()
    assert page.locator("#recordList tbody tr").count() == 1
    assert page.locator("#recordList b").count() == 0
    page.get_by_role("button", name="View details", exact=True).click()
    panel = page.get_by_role("dialog", name="Record 1 details", exact=True)
    assert panel.is_visible()
    assert panel.get_by_text("<b>Evidence</b>", exact=True).is_visible()
    for width in (1100, 390):
        page.set_viewport_size({"width": width, "height": 844})
        assert panel.evaluate("el => el.scrollWidth <= el.clientWidth")
        assert panel.evaluate("el => el.getBoundingClientRect().right <= innerWidth")
    page.get_by_role("button", name="Close details", exact=True).click()
    assert not panel.is_visible()
    assert page.get_by_role("button", name="View details", exact=True).evaluate(
        "el => el === document.activeElement"
    )
    page.set_viewport_size({"width": 1280, "height": 844})
    assert (
        page.locator("#recordList")
        .get_by_role("columnheader", name="Market", exact=True)
        .is_visible()
    )
    assert page.get_by_role(
        "columnheader", name="Pattern result", exact=True
    ).is_visible()
    assert page.locator("#recordPrevious").is_disabled()
    page.get_by_role("button", name="Next 25", exact=True).click()
    page.get_by_text("No saved records in this list yet.", exact=True).wait_for()
    page.get_by_role("button", name="Previous 25", exact=True).click()
    page.get_by_role("cell", name="All rules matched", exact=True).wait_for()
    page.get_by_label("Show", exact=True).select_option("quotes")
    page.get_by_text("Could not load saved records.", exact=False).wait_for()
    assert page.locator("#recordList tbody tr").count() == 0
    assert page.locator("#recordDownload").get_attribute("href").endswith("quotes.csv")
    page.get_by_label("Show", exact=True).select_option("attempts")
    page.get_by_role("cell", name="All rules matched", exact=True).wait_for()

    assert "100 quotes saved" in page.locator("#journalStatus").inner_text()

    assert (
        "Other market did not move oppositely"
        in page.locator("#journalRecent").inner_text()
    )

    assert "GOLD" in page.locator("#journalMarkouts").inner_text()

    assert (
        page.get_by_role("link", name="Download attempts CSV").get_attribute("href")
        == "/api/pattern/journal/attempts.csv"
    )

    state["status"] = "delayed"

    state["feed_connected"] = True

    send()

    assert page.get_by_text("Paused — prices are too delayed", exact=True).is_visible()

    state["feed_connected"] = False

    send()

    assert page.get_by_text("Paused — price feed unavailable", exact=True).is_visible()

    assert page.get_by_text("Gold | SELL | DEMO", exact=True).is_visible()

    state["authenticated"] = False

    send()

    assert "login required" in page.locator("#feed").inner_text()

    assert "Price unavailable" in page.locator("#markets").inner_text()

    state["authenticated"] = True

    state["feed_connected"] = True

    state["status"] = "warming"

    send()

    assert "Getting ready" in page.locator("#trigger").inner_text()

    page.evaluate("window.scanEvents.at(-1).onerror()")

    assert "Paused" in page.locator("#trigger").inner_text()

    assert page.get_by_text("Gold | SELL | DEMO", exact=True).is_visible()

    page.set_viewport_size({"width": 390, "height": 844})

    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    source = Path("tools/pattern_scan.html").read_text(encoding="utf-8")

    assert "setInterval" not in source

    assert "waiting for the next candle-close" not in source
