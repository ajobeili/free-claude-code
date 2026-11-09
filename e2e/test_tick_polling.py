from pathlib import Path


def test_tick_polling_does_not_overlap(page):
    page.set_content(
        '<div class="tick-cell" data-epic="GOLD"><span class="tick-status"></span></div>'
    )
    source = Path("tools/fcc_trader.html").read_text(encoding="utf-8")
    code = source[
        source.index("let tickPollBusy = false;") : source.index(
            "function startTickPolling()"
        )
    ]
    page.evaluate(
        "() => { window.tickCache = new Map(); window.calls = 0; window.fetch = () => {calls++; return new Promise(resolve => {window.finishQuote = () => resolve(new Response(JSON.stringify({error:'markets 429'})));});}; }"
    )
    page.add_script_tag(content=code)
    page.evaluate("void pollTicks(); void pollTicks();")
    assert page.evaluate("calls") == 1
    page.evaluate("finishQuote()")
    page.wait_for_function(
        "document.querySelector('.tick-status').textContent === 'Quote unavailable'"
    )
    assert page.locator(".tick-status").get_attribute("title") == "markets 429"
    assert page.evaluate("tickCache.size") == 0


def test_scan_failure_does_not_generate_mock_results(page):
    page.set_content(
        '<button id="scanBtn"></button><input id="watchlist" value="BTCUSD"><span id="scanTime"></span>'
    )
    source = Path("tools/fcc_trader.html").read_text(encoding="utf-8")
    code = source[
        source.index("async function runScan()") : source.index(
            "async function quickScan()"
        )
    ]
    page.evaluate(
        "() => { window.account={riskPct:1}; window.setups=[{symbol:'OLD'}]; window.renderSetups=()=>{}; window.showToast=()=>{}; window.fetch=async()=>new Response(JSON.stringify({detail:'Broker unavailable'}),{status:502}); }"
    )
    page.add_script_tag(content=code)
    page.evaluate("runScan()")
    assert page.evaluate("setups.length") == 0
    assert "Broker unavailable" in page.locator("#scanTime").inner_text()
    assert page.locator("#scanBtn").is_enabled()
