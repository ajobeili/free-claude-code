"""Email Sender - sends emails via Resend API."""

import logging
from typing import Optional

import httpx
from src.sales_engine.core.config import settings

logger = logging.getLogger(__name__)


class EmailSender:
    """Sends emails via Resend API (3,000 free/month)."""

    BASE_URL = "https://api.resend.com"

    def __init__(self):
        self.api_key = settings.resend_api_key
        self.from_email = settings.resend_from_email

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        reply_to: Optional[str] = None,
    ) -> dict:
        """Send a single email via Resend."""
        if not self.api_key:
            return {"success": False, "error": "No Resend API key"}

        try:
            async with httpx.AsyncClient() as client:
                data = {
                    "from": f"Sales Engine <{self.from_email}>",
                    "to": [to_email],
                    "subject": subject,
                    "text": body,
                }
                if reply_to:
                    data["reply_to"] = reply_to

                response = await client.post(
                    f"{self.BASE_URL}/emails",
                    json=data,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
                return {"success": True, "data": response.json()}
        except Exception as e:
            logger.error(f"Email send failed: {e}")
            return {"success": False, "error": str(e)}

    async def send_bulk(
        self,
        emails: list[dict],
    ) -> list[dict]:
        """Send multiple emails."""
        results = []
        for email in emails:
            result = await self.send_email(
                to_email=email["to"],
                subject=email["subject"],
                body=email["body"],
            )
            results.append(result)
        return results