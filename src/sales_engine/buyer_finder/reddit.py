"""Reddit Buyer Finder - searches Reddit for posts matching product keywords."""

import asyncio
import logging
from typing import Optional

import httpx
from src.sales_engine.core.config import settings

logger = logging.getLogger(__name__)


class RedditLead:
    """Represents a potential lead found on Reddit."""

    def __init__(
        self,
        post_id: str,
        title: str,
        content: str,
        author: str,
        subreddit: str,
        url: str,
        created_utc: float,
        intent_score: float = 0.0,
    ):
        self.post_id = post_id
        self.title = title
        self.content = content
        self.author = author
        self.subreddit = subreddit
        self.url = url
        self.created_utc = created_utc
        self.intent_score = intent_score


class RedditBuyerFinder:
    """Finds buyers on Reddit by searching for posts matching product keywords."""

    BASE_URL = "https://oauth.reddit.com"
    AUTH_URL = "https://www.reddit.com/api/v1/access_token"

    def __init__(self):
        self.client_id = settings.reddit_client_id
        self.client_secret = settings.reddit_client_secret
        self.user_agent = settings.reddit_user_agent
        self.access_token: Optional[str] = None

    async def _authenticate(self) -> str:
        """Authenticate with Reddit and get access token."""
        if self.access_token:
            return self.access_token

        async with httpx.AsyncClient() as client:
            auth = httpx.BasicAuth(self.client_id, self.client_secret)
            response = await client.post(
                self.AUTH_URL,
                auth=auth,
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": self.user_agent},
            )
            data = response.json()
            self.access_token = data["access_token"]
            return self.access_token

    async def search_subreddit(
        self,
        subreddit: str,
        query: str,
        limit: int = 25,
        sort: str = "new",
        time_filter: str = "week",
    ) -> list[RedditLead]:
        """Search a subreddit for posts matching a query."""
        token = await self._authenticate()

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.BASE_URL}/r/{subreddit}/search",
                params={
                    "q": query,
                    "limit": limit,
                    "sort": sort,
                    "t": time_filter,
                    "restrict_sr": "true",
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": self.user_agent,
                },
            )
            data = response.json()

            leads = []
            for post in data.get("data", {}).get("children", []):
                post_data = post["data"]
                leads.append(
                    RedditLead(
                        post_id=post_data["id"],
                        title=post_data["title"],
                        content=post_data.get("selftext", ""),
                        author=post_data["author"],
                        subreddit=post_data["subreddit"],
                        url=f"https://reddit.com{post_data['permalink']}",
                        created_utc=post_data["created_utc"],
                    )
                )
            return leads

    async def search_multiple_subreddits(
        self,
        subreddits: list[str],
        keywords: list[str],
        limit_per_search: int = 10,
    ) -> list[RedditLead]:
        """Search multiple subreddits for multiple keywords."""
        all_leads = []

        for subreddit in subreddits:
            for keyword in keywords:
                try:
                    leads = await self.search_subreddit(
                        subreddit=subreddit,
                        query=keyword,
                        limit=limit_per_search,
                    )
                    all_leads.extend(leads)
                    await asyncio.sleep(2)  # Rate limiting
                except Exception as e:
                    logger.error(f"Failed to search r/{subreddit} for '{keyword}': {e}")

        # Remove duplicates
        seen = set()
        unique_leads = []
        for lead in all_leads:
            if lead.post_id not in seen:
                seen.add(lead.post_id)
                unique_leads.append(lead)

        return unique_leads

    async def send_reply(self, post_id: str, message: str) -> bool:
        """Reply to a Reddit post."""
        token = await self._authenticate()

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/api/comment",
                data={
                    "thing_id": f"t3_{post_id}",
                    "text": message,
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": self.user_agent,
                },
            )
            return response.status_code == 200

    async def send_dm(self, username: str, subject: str, message: str) -> bool:
        """Send a DM to a Reddit user."""
        token = await self._authenticate()

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/api/compose",
                data={
                    "api_type": "json",
                    "subject": subject,
                    "text": message,
                    "to": username,
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": self.user_agent,
                },
            )
            return response.status_code == 200