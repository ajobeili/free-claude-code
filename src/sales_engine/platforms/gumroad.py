"""Gumroad Platform Distributor."""

import httpx
import logging
from typing import Optional

from src.sales_engine.core.config import settings

logger = logging.getLogger(__name__)


class GumroadDistributor:
    """Handles auto-distribution to Gumroad."""

    BASE_URL = "https://api.gumroad.com/v2"

    def __init__(self, access_code: Optional[str] = None):
        self.access_code = access_code or settings.gumroad_access_code

    async def create_product(
        self,
        name: str,
        description: str,
        price: float = 0.0,
        tags: list[str] = None,
    ) -> dict:
        """Create a product on Gumroad."""
        if not self.access_code:
            return {"success": False, "error": "No Gumroad access code"}

        try:
            async with httpx.AsyncClient() as client:
                data = {
                    "access_token": self.access_code,
                    "name": name,
                    "description": description,
                    "price": str(int(price * 100)),  # Price in cents
                    "currency": "USD",
                    "tags": ",".join(tags or []),
                    "published": True,
                }

                response = await client.post(
                    f"{self.BASE_URL}/products",
                    data=data,
                )
                return {"success": True, "data": response.json()}
        except Exception as e:
            logger.error(f"Gumroad create product failed: {e}")
            return {"success": False, "error": str(e)}

    async def update_product(self, product_id: str, **kwargs) -> dict:
        """Update an existing Gumroad product."""
        if not self.access_code:
            return {"success": False, "error": "No Gumroad access code"}

        try:
            async with httpx.AsyncClient() as client:
                data = {"access_token": self.access_code, **kwargs}
                response = await client.put(
                    f"{self.BASE_URL}/products/{product_id}",
                    data=data,
                )
                return {"success": True, "data": response.json()}
        except Exception as e:
            logger.error(f"Gumroad update product failed: {e}")
            return {"success": False, "error": str(e)}

    async def get_product(self, product_id: str) -> dict:
        """Get a Gumroad product."""
        if not self.access_code:
            return {"success": False, "error": "No Gumroad access code"}

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.BASE_URL}/products/{product_id}",
                    params={"access_token": self.access_code},
                )
                return {"success": True, "data": response.json()}
        except Exception as e:
            logger.error(f"Gumroad get product failed: {e}")
            return {"success": False, "error": str(e)}