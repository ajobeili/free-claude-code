"""Twitter Buyer Finder - searches Twitter for posts matching product keywords."""

import asyncio
import logging
from typing import Optional

import httpx
from src.sales_engine.core.config import settings

logger = logging.getLogger(__name__)


class TwitterLead:
    """Represents a potential lead found on Twitter/X."""

    def __init__(
        self,
        tweet_id: str,
        text: str,
        author_id: str,
        author_username: str,
        created_at: str,
        intent_score: float = 0.0,
    ):
        self.tweet_id = tweet_id
        self.text = text
        self.author_id = author_id
        self.author_username = author_username
        self.created_at = created_at
        self.intent_score = intent_score


class TwitterBuyerFinder:
    """Finds buyers on Twitter/X by searching for posts matching product keywords."""

    BASE_URL = "https://api.twitter.com/2"

    def __init__(self):
        self.bearer_token = settings.twitter_bearer_token

    async def search_tweets(
        self,
        query: str,
        max_results: int = 10,
    ) -> list[TwitterLead]:
        """Search Twitter for tweets matching a query."""
        if not self.bearer_token:
            logger.warning("No Twitter bearer token configured")
            return []

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.BASE_URL}/tweets/search/recent",
                params={
                    "query": f"{query} -is:retweet lang:en",
                    "max_results": min(max_results, 100),
                    "tweet.fields": "created_at,author_id",
                    "user.fields": "username",
                    "expansions": "author_id",
                },
                headers={
                    "Authorization": f"Bearer {self.bearer_token}",
                },
            )

            if response.status_code != 200:
                logger.error(f"Twitter search failed: {response.status_code}")
                return []

            data = response.json()
            users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}

            leads = []
            for tweet in data.get("data", []):
                user = users.get(tweet["author_id"], {})
                leads.append(
                    TwitterLead(
                        tweet_id=tweet["id"],
                        text=tweet["text"],
                        author_id=tweet["author_id"],
                        author_username=user.get("username", "unknown"),
                        created_at=tweet["created_at"],
                    )
                )
            return leads

    async def search_multiple_queries(
        self,
        queries: list[str],
        max_per_query: int = 10,
    ) -> list[TwitterLead]:
        """Search multiple queries."""
        all_leads = []

        for query in queries:
            try:
                leads = await self.search_tweets(query, max_per_query)
                all_leads.extend(leads)
                await asyncio.sleep(2)  # Rate limiting
            except Exception as e:
                logger.error(f"Failed to search Twitter for '{query}': {e}")

        # Remove duplicates
        seen = set()
        unique_leads = []
        for lead in all_leads:
            if lead.tweet_id not in seen:
                seen.add(lead.tweet_id)
                unique_leads.append(lead)

        return unique_leads

    async def send_dm(self, user_id: str, message: str) -> bool:
        """Send a DM to a Twitter user (requires elevated access)."""
        if not self.bearer_token:
            return False

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/dm_conversations/with/{user_id}/messages",
                json={"text": message},
                headers={"Authorization": f"Bearer {self.bearer_token}"},
            )
            return response.status_code in [200, 201]