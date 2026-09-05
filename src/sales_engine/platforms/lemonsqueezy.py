"""Lemon Squeezy Platform Distributor."""

import os
import httpx
import logging
from typing import Optional

from src.sales_engine.core.config import settings

logger = logging.getLogger(__name__)


class LemonSqueezyDistributor:
    """Handles auto-distribution to Lemon Squeezy."""

    BASE_URL = "https://api.lemonsqueezy.com/v1"

    def __init__(self, api_key: Optional[str] = None, store_id: Optional[str] = None):
        self.api_key = api_key or os.environ.get("LEMONSQUEEZY_API_KEY", "")
        self.store_id = store_id or os.environ.get("LEMONSQUEEZY_STORE_ID", "")

    async def create_product(
        self,
        name: str,
        description: str,
        price: float = 0.0,
    ) -> dict:
        """Create a product on Lemon Squeezy."""
        if not self.api_key or not self.store_id:
            return {"success": False, "error": "Missing Lemon Squeezy credentials"}

        try:
            async with httpx.AsyncClient() as client:
                data = {
                    "data": {
                        "type": "products",
                        "attributes": {
                            "name": name,
                            "description": description,
                            "price": str(int(price * 100)),  # Price in cents
                            "price_usd": str(int(price * 100)),
                            "status": "published",
                        },
                        "relationships": {
                            "store": {"data": {"type": "stores", "id": self.store_id}}
                        },
                    }
                }

                response = await client.post(
                    f"{self.BASE_URL}/products",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/vnd.api+json",
                        "Accept": "application/vnd.api+json",
                    },
                    json=data,
                )
                return {"success": True, "data": response.json()}
        except Exception as e:
            logger.error(f"Lemon Squeezy create product failed: {e}")
            return {"success": False, "error": str(e)}

    async def upload_file(self, product_id: str, file_path: str) -> dict:
        """Upload a digital file to the product."""
        if not self.api_key:
            return {"success": False, "error": "Missing Lemon Squeezy API key"}

        try:
            async with httpx.AsyncClient() as client:
                files = {"file": open(file_path, "rb")}
                response = await client.post(
                    f"{self.BASE_URL}/products/{product_id}/files",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files=files,
                )
                return {"success": True, "data": response.json()}
        except Exception as e:
            logger.error(f"Lemon Squeezy upload file failed: {e}")
            return {"success": False, "error": str(e)}