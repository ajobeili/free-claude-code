"""Hunter.io Email Finder - finds emails for leads."""

import logging

import httpx
from src.sales_engine.core.config import settings

logger = logging.getLogger(__name__)


class EmailFinder:
    """Find emails for leads using Hunter.io (25 free searches/month)."""

    BASE_URL = "https://api.hunter.io/v2"

    def __init__(self):
        self.api_key = settings.hunter_api_key

    async def find_email(
        self,
        first_name: str,
        last_name: str,
        domain: str,
    ) -> str | None:
        """Find an email by name and domain."""
        if not self.api_key:
            logger.warning("No Hunter API key")
            return None

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.BASE_URL}/email-finder",
                    params={
                        "first_name": first_name,
                        "last_name": last_name,
                        "domain": domain,
                        "api_key": self.api_key,
                    },
                )
                data = response.json()
                return data.get("data", {}).get("email")
        except Exception as e:
            logger.error(f"Hunter email finder failed: {e}")
            return None

    async def find_emails_for_company(
        self,
        domain: str,
        limit: int = 10,
    ) -> list[dict]:
        """Find emails at a company domain."""
        if not self.api_key:
            return []

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.BASE_URL}/domain-search",
                    params={
                        "domain": domain,
                        "api_key": self.api_key,
                        "limit": limit,
                    },
                )
                data = response.json()
                return data.get("data", {}).get("emails", [])
        except Exception as e:
            logger.error(f"Hunter domain search failed: {e}")
            return []