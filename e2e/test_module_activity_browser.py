from pathlib import Path


def test_inactive_module_does_not_send_requests(page):
    page.route(
        "http://module.test/planner",
        lambda r: r.fulfill(body="<html></html>", content_type="text/html"),
    )
    calls = []

    def api(route):
        calls.append(route.request.url)
        route.fulfill(json={"ok": True})

    page.route("**/api/**", api)
    page.goto("http://module.test/planner")
    page.add_script_tag(content=Path("tools/module_activity.js").read_text())
    assert page.evaluate("async () => (await fetch('/api/planner')).status") == 200
    before = len(calls)
    page.evaluate("localStorage.setItem('fcc-active-module','structure')")
    assert page.evaluate("async () => (await fetch('/api/planner')).status") == 409
    assert len(calls) == before
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    assert page.evaluate("async () => (await fetch('/api/planner')).status") == 200
