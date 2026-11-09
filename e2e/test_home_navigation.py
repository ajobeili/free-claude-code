from pathlib import Path

from playwright.sync_api import Page


def test_home_app_navigation(page: Page):
    page.route(
        "http://apps.test/",
        lambda route: route.fulfill(
            content_type="text/html",
            body=Path("tools/trader_home.html").read_text(encoding="utf-8"),
        ),
    )
    page.route(
        "http://apps.test/workbench",
        lambda route: route.fulfill(body="Trading Workbench"),
    )
    page.route(
        "http://apps.test/structure", lambda route: route.fulfill(body="Structure Scan")
    )
    page.goto("http://apps.test/")
    assert page.get_by_role("heading", name="Your apps").is_visible()
    page.get_by_role("link", name="Open Trading Workbench").click()
    assert page.url == "http://apps.test/workbench"
    page.goto("http://apps.test/")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("link", name="Open Structure Scan").click()
    assert page.url == "http://apps.test/structure"


def test_successful_login_opens_apps(page: Page):
    page.route(
        "http://apps.test/workbench",
        lambda route: route.fulfill(
            content_type="text/html",
            body=Path("tools/fcc_trader.html").read_text(encoding="utf-8"),
        ),
    )
    page.route(
        "http://apps.test/",
        lambda route: route.fulfill(
            content_type="text/html",
            body=Path("tools/trader_home.html").read_text(encoding="utf-8"),
        ),
    )
    page.route(
        "**/api/**",
        lambda route: route.fulfill(status=401, json={"detail": "Login required"}),
    )
    submitted = []

    def login_response(route):
        submitted.append(route.request.post_data_json)
        route.fulfill(json={"ok": True})

    page.route("**/api/login", login_response)
    page.goto("http://apps.test/workbench")
    page.locator("#setup_api_key").fill("test-key")
    page.locator("#setup_login").fill("test@example.com")
    page.locator("#setup_password").fill("test-password")
    page.locator("#setupSubmitBtn").click()
    page.wait_for_url("http://apps.test/")
    assert page.get_by_role("heading", name="Your apps").is_visible()
    assert submitted[0]["capital_api_key"] == "test-key"
    assert submitted[0]["capital_password"] == "test-password"


def test_home_logout_and_failure(page: Page):
    page.route(
        "http://apps.test/",
        lambda route: route.fulfill(
            content_type="text/html",
            body=Path("tools/trader_home.html").read_text(encoding="utf-8"),
        ),
    )
    page.route("http://apps.test/workbench", lambda route: route.fulfill(body="Login"))
    page.route(
        "**/api/logout",
        lambda route: route.fulfill(status=500, json={"detail": "Failed"}),
    )
    page.goto("http://apps.test/")
    page.get_by_role("button", name="Logout", exact=True).click()
    page.wait_for_function("!document.getElementById('logoutError').hidden")
    assert page.url == "http://apps.test/"
    assert page.get_by_role("button", name="Logout", exact=True).is_enabled()
    page.unroute("**/api/logout")
    page.route("**/api/logout", lambda route: route.fulfill(json={"ok": True}))
    page.get_by_role("button", name="Logout", exact=True).click()
    page.wait_for_url("http://apps.test/workbench")
