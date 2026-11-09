"""Beginner research views read saved evidence and never create reports."""

from pathlib import Path
from urllib.parse import parse_qs, urlparse


def install(page, *, report=None):
    diagnostics = {
        "status": "current",
        "revision": 7,
        "processed_through_sequence": 23,
        "processed_through_time": "2026-11-04T10:00:00Z",
        "lag": 0,
        "accepted_quotes": 20,
        "attempts": 2,
        "parity": "unverified",
        "limitations": ["History checks were unavailable before capture."],
        "latest_attempt": {
            "market": "GOLD",
            "outcome": "expired",
            "reason": "No return inside within 90 seconds",
            "started": "2026-11-04T09:58:00Z",
        },
    }
    requests = []
    state = {"retired": False, "failed": False}
    page.route(
        "http://evaluation.test/pattern",
        lambda route: route.fulfill(
            content_type="text/html",
            body=Path("tools/pattern_scan.html").read_text(encoding="utf-8"),
        ),
    )
    page.route(
        "**/module_activity.js",
        lambda route: route.fulfill(content_type="application/javascript", body=""),
    )

    def api(route):
        requests.append((route.request.method, route.request.url))
        if route.request.url.endswith("/api/pattern/evaluation"):
            route.fulfill(
                json={
                    "diagnostics": diagnostics,
                    "report": report,
                    "report_status": "completed" if report else "unavailable",
                }
            )
        elif "/api/pattern/evaluation/records?" in route.request.url:
            query = parse_qs(urlparse(route.request.url).query)
            if state["retired"]:
                route.fulfill(status=409, json={"detail": "Refresh"})
            elif state["failed"]:
                route.fulfill(status=503, json={"detail": "Unavailable"})
            else:
                later = query.get("offset") == ["25"]
                kind = query["kind"][0]
                columns = {
                    "stages": ["market", "unit", "count", "reason"],
                    "attempts": ["market", "outcome", "reason", "history_hash"],
                    "coverage": ["market", "seconds", "paired"],
                    "followups": [
                        "market",
                        "horizon_seconds",
                        "points",
                        "status",
                        "reason",
                    ],
                    "late_returns": ["market", "status", "first_return", "reason"],
                }.get(kind, ["market", "decision"])
                values = {
                    "market": "GOLD",
                    "unit": "quote",
                    "count": 20,
                    "reason": "<b>Literal evidence</b> " + "x" * 160,
                    "outcome": "expired",
                    "history_hash": "a" * 64,
                    "seconds": 15,
                    "paired": True,
                    "horizon_seconds": 180,
                    "points": None,
                    "status": "incomplete",
                    "first_return": "2026-11-04T10:02:00Z",
                    "decision": "insufficient",
                }
                route.fulfill(
                    json={
                        "columns": columns,
                        "rows": [] if later else [[values[key] for key in columns]],
                        "has_more": not later,
                        "offset": 25 if later else 0,
                        "diagnostic_revision": 7,
                        "limitations": [],
                    }
                )
        else:
            route.fulfill(json={"authenticated": True})

    page.route("**/api/**", api)
    page.add_init_script(
        "window.scanEvents=[];window.EventSource=class{constructor(url){this.url=url;window.scanEvents.push(this)}close(){}};"
    )
    page.goto("http://evaluation.test/pattern")
    page.locator("#evaluationRecords tbody tr").wait_for()
    return diagnostics, requests, state


def test_rejected_attempts_are_visible_next_to_alerts_without_opening_journal(page):
    install(page)
    page.evaluate("""() => window.scanEvents.at(-1).onmessage({data:JSON.stringify({
        authenticated:true,feed_connected:true,status:'watching',markets:[],alerts:[],
        journal:{running:true,quotes:48274,attempts:11,qualified:0,complete_outcomes:1,
        incomplete_outcomes:0,pending_outcomes:2,dropped:0,recent:[
            {epic:'GOLD',started:'2026-11-05T04:45:24.420Z',outcome:'context_failed',
             reason:'Other market did not move oppositely during the attempt.'},
            {epic:'OIL_CRUDE',started:'2026-11-04T21:50:09.976Z',outcome:'expired',
             reason:'No return inside the frozen range within 90 seconds.'}
        ],markouts:[]}
    })})""")
    attempts = page.get_by_role("region", name="Recent detected attempts")
    assert attempts.is_visible()
    assert "11 detected attempts" in attempts.inner_text()
    assert "0 fully matched alerts" in attempts.inner_text()
    assert "Other market did not confirm" in attempts.inner_text()
    assert "Return took too long" in attempts.inner_text()
    assert "No fully matched alert yet" in page.locator("#empty").inner_text()
    assert not page.locator('section[aria-label="Research journal"]').is_visible()
    for width in (1100, 390):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")


def test_alert_evidence_remains_open_during_price_updates_and_new_alert(page):
    install(page)
    page.evaluate("""() => {
        const alert={epic:'GOLD',market:'Gold',direction:'UP',trigger_time:'2026-11-05T05:18:01Z',breakout_time:'2026-11-05T05:17:40Z',range_high:4121,range_low:4111,breakout_price:4109,return_price:4112,spread:.5,other_market:'',other_start:null,other_end:null,rule_version:'independent-rejection-v2',quote_delay_seconds:1};
        window.alertTestState={authenticated:true,feed_connected:true,status:'watching',markets:[],alerts:[{...alert,trigger_time:'2026-11-05T05:32:25Z'}, {...alert,epic:'OIL_CRUDE',market:'Crude oil',trigger_time:'2026-11-05T05:30:10Z'},alert]};
        window.pushAlertTest=()=>window.scanEvents.at(-1).onmessage({data:JSON.stringify(window.alertTestState)});
        window.pushAlertTest();
    }""")
    assert page.locator("#alerts .alert").count() == 3
    evidence = page.locator("#alerts .alert").first.locator("details")
    evidence.locator("summary").click()
    assert evidence.get_attribute("open") is not None
    page.evaluate(
        """() => {for(let i=0;i<5;i++){window.alertTestState.version=i;window.alertTestState.quote_delay_seconds=i;window.pushAlertTest()}}"""
    )
    assert evidence.get_attribute("open") is not None
    page.evaluate(
        """() => {window.alertTestState.alerts.unshift({...window.alertTestState.alerts[0],trigger_time:'2026-11-05T05:35:00Z'});window.pushAlertTest()}"""
    )
    previous = page.locator("#alerts .alert").nth(1).locator("details")
    assert previous.get_attribute("open") is not None
    assert "Breakout midpoint" in previous.inner_text()
    assert "Recorded alert history" in page.locator("#alertHistoryNote").inner_text()
    for width in (1100, 390):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")


def test_alert_card_matches_brief_message_and_keeps_explanation_in_evidence(page):
    install(page)
    page.evaluate("""() => window.scanEvents.at(-1).onmessage({data:JSON.stringify({
        authenticated:true,feed_connected:true,status:'watching',markets:[],alerts:[{
          epic:'GOLD',market:'Gold',direction:'DOWN',trigger_time:'2026-11-05T09:09:46Z',
          breakout_time:'2026-11-05T09:09:40Z',range_high:110,range_low:90,
          breakout_price:114,return_price:109,spread:1,breakout_extreme:115,
          other_market:'',rule_version:'independent-rejection-v2',quote_delay_seconds:2,
          plan:{action:'SELL',entry:'108.5',stop_loss:'115.5',take_profit:'94.5',status:''}
        }]
    })})""")
    card = page.locator("#alerts .alert").first
    assert card.get_by_role(
        "heading", name="Gold | SELL | DEMO", exact=True
    ).is_visible()
    levels = card.locator(".alert-levels")
    assert levels.is_visible()
    assert "108.5" in levels.inner_text()
    assert "Stop loss" in levels.inner_text() and "115.5" in levels.inner_text()
    assert "Take profit (2R)" in levels.inner_text() and "94.5" in levels.inner_text()
    assert "broke above" not in card.inner_text()
    card.get_by_text("See pattern evidence", exact=True).click()
    assert "broke above" in card.inner_text()
    for width in (1100, 390):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    card.get_by_text("See pattern evidence", exact=True).click()
    Path("research-output").mkdir(exist_ok=True)
    card.screenshot(path="research-output/brief-alert-card.png")


def test_current_activity_before_report_with_readable_details_and_revisions(page):
    diagnostics, requests, state = install(page)
    assert "20 accepted price updates" in page.locator("#evaluationStatus").inner_text()
    assert (
        "No return inside within 90 seconds"
        in page.locator("#evaluationLatest").inner_text()
    )
    assert "No comparison report yet" in page.locator("#comparisonStatus").inner_text()
    assert "Processed record 23" in page.locator("#evaluationCursor").inner_text()
    assert page.locator("#evaluationRecords b").count() == 0
    assert "diagnostic_revision=7" in page.locator("#evaluationDownload").get_attribute(
        "href"
    )
    page.locator("#evaluationKind").select_option("attempts")
    page.locator("#evaluationRecords").get_by_role(
        "button", name="View evidence"
    ).click()
    dialog = page.get_by_role("dialog", name="Research evidence 1", exact=True)
    assert dialog.is_visible()
    for width in (1100, 390):
        page.set_viewport_size({"width": width, "height": 844})
        assert dialog.evaluate("el=>el.scrollWidth<=el.clientWidth")
        assert dialog.evaluate("el=>el.getBoundingClientRect().right<=innerWidth")
    page.keyboard.press("Escape")
    assert not dialog.is_visible()
    assert (
        page.locator("#evaluationRecords")
        .get_by_role("button", name="View evidence")
        .evaluate("el=>el===document.activeElement")
    )
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.locator("#evaluationNext").click()
    page.get_by_text("No records match these filters.", exact=False).wait_for()
    page.locator("#evaluationPrevious").click()
    page.locator("#evaluationRecords tbody tr").wait_for()

    # SSE advances the displayed activity; paging keeps its already-selected revision.
    diagnostics.update(revision=8, accepted_quotes=31, processed_through_sequence=35)
    page.evaluate(
        "data=>window.scanEvents.at(-1).onmessage({data:JSON.stringify({evaluation_diagnostics:data,authenticated:true,feed_connected:true,markets:[],alerts:[]})})",
        diagnostics,
    )
    assert "31 accepted price updates" in page.locator("#evaluationStatus").inner_text()
    assert "diagnostic_revision=7" in page.locator("#evaluationDownload").get_attribute(
        "href"
    )
    state["retired"] = True
    page.locator("#evaluationKind").select_option("stages")
    page.get_by_text("Activity has advanced.", exact=False).wait_for()
    state["retired"] = False
    page.locator("#evaluationRefresh").click()
    page.locator("#evaluationRecords tbody tr").wait_for()
    assert "diagnostic_revision=8" in page.locator("#evaluationDownload").get_attribute(
        "href"
    )
    assert all(method == "GET" for method, _url in requests)
    assert not any("replay" in url or "evaluate?" in url for _method, url in requests)
    Path("research-output").mkdir(exist_ok=True)
    page.screenshot(path="research-output/evaluation-activity-390.png", full_page=True)


def test_comparison_groups_limits_filters_and_failure_are_separate_from_scanner(page):
    variants = ("confirmed-90", "unfiltered-90", "confirmed-180", "unfiltered-180")
    report = {
        "report_id": "a" * 64,
        "label": "synthetic",
        "parity": "baseline_mismatch",
        "rule_version": "evaluation-v1",
        "snapshot": {"observed_through": "2026-11-04T10:00:00Z"},
        "limitations": ["Invented demonstration, not a market backtest"],
        "groups": [
            {
                "market": market,
                "variant": variant,
                "phase": "development",
                "starts": 10,
                "matches": 0,
                "expired": 10,
                "target_hours": 1,
                "paired_hours": 0.5,
                "normal_mean": None,
                "doubled_mean": None,
                "decision": "insufficient",
            }
            for market in ("GOLD", "OIL_CRUDE")
            for variant in variants
        ],
        "comparisons": [
            {
                "market": "GOLD",
                "variant": "unfiltered-90",
                "decision": "insufficient",
                "reason": "Missing session and cost evidence",
            }
        ],
    }
    _diagnostics, requests, state = install(page, report=report)
    assert page.locator("#evaluationGroups tbody tr").count() == 8
    assert "Baseline replay disagrees" in page.locator("#comparisonStatus").inner_text()
    assert (
        "Missing session and cost evidence"
        in page.locator("#evaluationDecisions").inner_text()
    )
    page.locator("#evaluationSource").select_option("report")
    page.locator("#evaluationKind").select_option("late_returns")
    page.locator("#evaluationMarket").select_option("GOLD")
    page.locator("#evaluationVariant").select_option("unfiltered-180")
    page.locator("#evaluationPhase").select_option("development")
    page.locator("#evaluationRecords tbody tr").wait_for()
    link = page.locator("#evaluationDownload").get_attribute("href")
    assert "report_id=" + "a" * 64 in link
    assert "market=GOLD" in link and "variant=unfiltered-180" in link
    assert (
        page.locator("#evaluationRecords")
        .get_by_role("cell", name="Incomplete", exact=True)
        .is_visible()
    )
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    state["failed"] = True
    page.locator("#evaluationKind").select_option("followups")
    page.get_by_text("Could not load research evidence.", exact=False).wait_for()
    assert page.locator("#evaluationDownload").is_hidden()
    assert all(method == "GET" for method, _url in requests)
    Path("research-output").mkdir(exist_ok=True)
    page.screenshot(
        path="research-output/evaluation-comparison-390.png", full_page=True
    )
