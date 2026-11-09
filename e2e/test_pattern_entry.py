from datetime import UTC, datetime

from tools.pattern_entry import build_card, render_cards


def test_wait_cards_explain_both_markets(page, tmp_path):
    cards = [
        build_card(market, {}, {}, datetime(2026, 11, 4, 8, 50, tzinfo=UTC))
        for market in ("Gold", "Crude Oil Spot")
    ]
    target = tmp_path / "cards.html"
    target.write_text(render_cards(cards), encoding="utf-8")
    page.goto(target.as_uri())
    assert page.locator("section").count() == 2
    assert page.locator(".action").all_text_contents() == ["WAIT", "WAIT"]
    assert "Completed signal candle" in page.locator("body").inner_text()
    page.locator("summary").first.click()
    assert "100" in page.locator("body").inner_text()
    assert "200" in page.locator("body").inner_text()
