"""AI Message Generator - uses OpenRouter to generate personalized messages."""

import json
import logging

import httpx
from src.sales_engine.core.config import settings

logger = logging.getLogger(__name__)


class AIMessageGenerator:
    """Generates personalized messages for outreach using OpenRouter AI."""

    def __init__(self):
        self.api_key = settings.openrouter_api_key
        self.base_url = f"{settings.openrouter_base_url}/chat/completions"

    async def score_intent(
        self,
        post_text: str,
        product_keywords: list[str],
    ) -> dict:
        """Score the buyer intent of a post (0.0 to 1.0)."""
        prompt = f"""Analyze this post for buyer intent:

Post: "{post_text}"

Product keywords: {', '.join(product_keywords)}

Score the buyer's intent from 0.0 to 1.0:
- 0.9-1.0: Actively looking to buy, mentions budget, urgent need
- 0.7-0.8: Clearly has the problem, looking for solutions
- 0.5-0.6: Discussing the topic, may be interested
- 0.0-0.4: Just discussing, not a buyer

Output ONLY valid JSON: {{"intent_score": 0.85, "reasoning": "..."}}"""

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.base_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "http://localhost:3000",
                        "X-Title": "Sales Engine",
                    },
                    json={
                        "model": settings.openrouter_model,
                        "messages": [
                            {
                                "role": "system",
                                "content": "You are an expert at analyzing buyer intent. Output valid JSON only.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.3,
                    },
                )
                content = response.json()["choices"][0]["message"]["content"]
                return json.loads(content)
        except Exception as e:
            logger.error(f"Intent scoring failed: {e}")
            return {"intent_score": 0.5, "reasoning": "Error in scoring"}

    async def generate_message(
        self,
        platform: str,
        product_name: str,
        product_link: str,
        product_description: str,
        post_text: str,
    ) -> str:
        """Generate a personalized, value-first message for a specific post."""
        prompt = f"""Write a {platform} reply to this post that:

1. Leads with empathy/understanding of their problem
2. Offers genuine value (not just "buy my thing")
3. Mentions {product_name} as a possible solution
4. Includes link: {product_link}
5. Doesn't sound spammy or salesy
6. Sounds like a real person helping

Product description: {product_description}

Post: "{post_text}"

Write the reply. Output only the message text, no preamble."""

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.base_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "http://localhost:3000",
                        "X-Title": "Sales Engine",
                    },
                    json={
                        "model": settings.openrouter_model,
                        "messages": [
                            {
                                "role": "system",
                                "content": "You write helpful, genuine replies that happen to mention a relevant product. Output only the message.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.7,
                    },
                )
                return response.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Message generation failed: {e}")
            return ""

    async def generate_email(
        self,
        product_name: str,
        product_link: str,
        product_description: str,
        buyer_name: str,
        buyer_context: str,
    ) -> dict:
        """Generate a personalized cold email with subject line."""
        prompt = f"""Write a personalized cold email to:

Buyer: {buyer_name}
Context: {buyer_context}

Product: {product_name}
Description: {product_description}
Link: {product_link}

Output valid JSON: {{"subject": "...", "body": "..."}}

The email should:
- Be 100-150 words max
- Lead with value, not pitch
- Mention the product as a possible solution
- Feel like a real person reaching out
- Not be spammy"""

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.base_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "http://localhost:3000",
                        "X-Title": "Sales Engine",
                    },
                    json={
                        "model": settings.openrouter_model,
                        "messages": [
                            {
                                "role": "system",
                                "content": "You write personalized cold emails. Output only valid JSON.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.7,
                    },
                )
                content = response.json()["choices"][0]["message"]["content"]
                return json.loads(content)
        except Exception as e:
            logger.error(f"Email generation failed: {e}")
            return {"subject": f"Quick question about {product_name}", "body": ""}