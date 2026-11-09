"""Rendered offline research report contract."""

from pathlib import Path

from tools.pattern_study import run


def test_offline_occurrences_render(page, tmp_path):
    fixture = Path("tests/fixtures/pattern_study")
    output = tmp_path / "report"
    run(fixture / "observations.csv", fixture / "manifest.json", output)
    page.goto((output / "study.html").as_uri())
    assert page.locator("section").count() == 12
    assert "synthetic" in page.locator("body").inner_text()
    assert page.locator("details").count() == 4
    page.locator("summary").first.click()
    assert page.locator("details").first.get_attribute("open") is not None
    assert page.locator("svg").count() == 4
