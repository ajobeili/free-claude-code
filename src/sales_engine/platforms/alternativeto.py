"""AlternativeTo Platform Distributor (via browser automation)."""

import logging
from typing import Optional

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)


class AlternativeToDistributor:
    """Handles auto-distribution to AlternativeTo via browser automation."""

    def __init__(self):
        pass

    async def submit_listing(
        self,
        name: str,
        description: str,
        category: str,
        website_url: str,
        license_type: str = "Commercial",
    ) -> dict:
        """Submit a listing to AlternativeTo."""
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()

                # Navigate to AlternativeTo add product page
                await page.goto("https://alternativeto.net/new/")

                # Fill in the form
                await page.fill('input[name="name"]', name)
                await page.fill('textarea[name="description"]', description)
                await page.fill('input[name="url"]', website_url)
                await page.select_option('select[name="license"]', license_type)

                # Submit
                await page.click('button[type="submit"]')

                await browser.close()
                return {"success": True, "message": "Submitted to AlternativeTo"}

        except Exception as e:
            logger.error(f"AlternativeTo submission failed: {e}")
            return {"success": False, "error": str(e)}