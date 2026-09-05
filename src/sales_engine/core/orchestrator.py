"""Orchestrator - coordinates the full pipeline: upload → distribute → find → outreach."""

import logging
import asyncio
from dataclasses import dataclass

from src.sales_engine.core.product_analyzer import ProductAnalyzer, ProductInfo, FileAnalysis
from src.sales_engine.platforms.gumroad import GumroadDistributor
from src.sales_engine.platforms.lemonsqueezy import LemonSqueezyDistributor
from src.sales_engine.platforms.alternativeto import AlternativeToDistributor
from src.sales_engine.platforms.indiehackers import IndieHackersDistributor
from src.sales_engine.platforms.odoo import OdooDistributor
from src.sales_engine.buyer_finder.reddit import RedditBuyerFinder
from src.sales_engine.buyer_finder.twitter import TwitterBuyerFinder
from src.sales_engine.outreach.intent_scorer import AIMessageGenerator
from src.sales_engine.outreach.email_sender import EmailSender
from src.sales_engine.outreach.email_finder import EmailFinder

logger = logging.getLogger(__name__)


@dataclass
class DistributionResult:
    platform: str
    success: bool
    message: str
    url: str = ""


@dataclass
class BuyerSearchResult:
    platform: str
    leads_found: int
    high_intent_leads: int
    messages_sent: int


class SalesEngine:
    """Main orchestrator for the full sales pipeline."""

    def __init__(self):
        self.analyzer = ProductAnalyzer()
        self.gumroad = GumroadDistributor()
        self.lemon_squeezy = LemonSqueezyDistributor()
        self.alternativeto = AlternativeToDistributor()
        self.indiehackers = IndieHackersDistributor()
        self.odoo = OdooDistributor()
        self.reddit = RedditBuyerFinder()
        self.twitter = TwitterBuyerFinder()
        self.ai = AIMessageGenerator()
        self.email_sender = EmailSender()
        self.email_finder = EmailFinder()

        self.current_product: ProductInfo | None = None

    async def upload_and_analyze(
        self, file_path: str, file_type: str
    ) -> FileAnalysis:
        """Upload a product file and analyze it with AI."""
        logger.info(f"Analyzing product: {file_path}")
        analysis = await self.analyzer.analyze_product(file_path, file_type)
        if analysis.product_info:
            self.current_product = analysis.product_info
        return analysis

    async def distribute_to_all_platforms(
        self, price: float = 0.0
    ) -> list[DistributionResult]:
        """Distribute the current product to all platforms."""
        if not self.current_product:
            return []

        product = self.current_product
        results = []

        # Gumroad
        logger.info("Creating product on Gumroad...")
        result = await self.gumroad.create_product(
            name=product.name,
            description=product.platform_descriptions.get("gumroad", product.description),
            price=price,
            tags=product.keywords,
        )
        results.append(
            DistributionResult(
                platform="gumroad",
                success=result.get("success", False),
                message=str(result),
                url=result.get("data", {}).get("short_url", ""),
            )
        )

        # Lemon Squeezy
        logger.info("Creating product on Lemon Squeezy...")
        result = await self.lemon_squeezy.create_product(
            name=product.name,
            description=product.platform_descriptions.get("lemonsqueezy", product.description),
            price=price,
        )
        results.append(
            DistributionResult(
                platform="lemonsqueezy",
                success=result.get("success", False),
                message=str(result),
                url=result.get("data", {}).get("data", {}).get("attributes", {}).get("urls", {}).get("default", ""),
            )
        )

        # AlternativeTo
        logger.info("Submitting to AlternativeTo...")
        result = await self.alternativeto.submit_listing(
            name=product.name,
            description=product.platform_descriptions.get("alternativeto", product.description),
            category=product.category,
            website_url="https://example.com",  # Would be the Gumroad/Lemon Squeezy URL
        )
        results.append(
            DistributionResult(
                platform="alternativeto",
                success=result.get("success", False),
                message=result.get("message", "Submitted"),
            )
        )

        # IndieHackers
        logger.info("Posting to IndieHackers...")
        result = await self.indiehackers.post_showcase(
            title=f"I built {product.name}",
            content=product.platform_descriptions.get("indiehackers", product.description),
            product_url="https://example.com",
        )
        results.append(
            DistributionResult(
                platform="indiehackers",
                success=result.get("success", False),
                message=result.get("message", "Posted"),
            )
        )

        return results

    async def find_and_contact_buyers(
        self, max_leads: int = 20, auto_send: bool = True
    ) -> list[BuyerSearchResult]:
        """Find buyers and contact them automatically."""
        if not self.current_product:
            return []

        product = self.current_product
        results = []

        # Determine target subreddits based on category
        subreddit_map = {
            "Notion Templates": ["Notion", "NotionTemplates", "productivity", "organization"],
            "Python Scripts": ["Python", "learnpython", "PythonProjects"],
            "Design Assets": ["design", "graphic_design", "canva", "Photoshop"],
            "Marketing Tools": ["marketing", "digitalmarketing", "startups", "entrepreneur"],
            "Courses": ["learnprogramming", "udemy", "onlineeducation", "selfimprovement"],
            "default": ["startups", "entrepreneur", "smallbusiness", "productivity"],
        }

        target_subreddits = subreddit_map.get(product.category, subreddit_map["default"])

        # Search Reddit
        logger.info("Searching Reddit for buyers...")
        reddit_leads = await self.reddit.search_multiple_subreddits(
            subreddits=target_subreddits,
            keywords=product.keywords[:10],
            limit_per_search=5,
        )

        reddit_high_intent = 0
        reddit_sent = 0

        for lead in reddit_leads[:max_leads]:
            # Score intent
            intent = await self.ai.score_intent(
                post_text=f"{lead.title}\n{lead.content}",
                product_keywords=product.keywords,
            )
            lead.intent_score = intent.get("intent_score", 0.5)

            if lead.intent_score >= 0.7 and auto_send:
                # Generate message
                message = await self.ai.generate_message(
                    platform="reddit",
                    product_name=product.name,
                    product_link="https://gumroad.com/example",  # Real product link
                    product_description=product.description,
                    post_text=f"{lead.title}\n{lead.content}",
                )

                # Send reply
                sent = await self.reddit.send_reply(lead.post_id, message)
                if sent:
                    reddit_sent += 1
                reddit_high_intent += 1

        results.append(
            BuyerSearchResult(
                platform="reddit",
                leads_found=len(reddit_leads),
                high_intent_leads=reddit_high_intent,
                messages_sent=reddit_sent,
            )
        )

        # Search Twitter
        logger.info("Searching Twitter for buyers...")
        twitter_leads = await self.twitter.search_multiple_queries(
            queries=product.search_queries[:5],
            max_per_query=5,
        )

        twitter_high_intent = 0
        twitter_sent = 0

        for lead in twitter_leads[:max_leads]:
            intent = await self.ai.score_intent(
                post_text=lead.text,
                product_keywords=product.keywords,
            )
            lead.intent_score = intent.get("intent_score", 0.5)

            if lead.intent_score >= 0.7 and auto_send:
                # For now, just log
                twitter_high_intent += 1

        results.append(
            BuyerSearchResult(
                platform="twitter",
                leads_found=len(twitter_leads),
                high_intent_leads=twitter_high_intent,
                messages_sent=twitter_sent,
            )
        )

        return results

    async def run_full_pipeline(
        self, file_path: str, file_type: str, price: float = 0.0
    ) -> dict:
        """Run the full pipeline: analyze → distribute → find buyers → outreach."""
        # Step 1: Analyze
        analysis = await self.upload_and_analyze(file_path, file_type)
        if not analysis.product_info:
            return {"error": "Failed to analyze product"}

        # Step 2: Distribute
        distribution = await self.distribute_to_all_platforms(price=price)

        # Step 3: Find and contact buyers
        buyer_results = await self.find_and_contact_buyers(auto_send=True)

        return {
            "product": analysis.product_info,
            "distribution": [
                {
                    "platform": r.platform,
                    "success": r.success,
                    "message": r.message,
                    "url": r.url,
                }
                for r in distribution
            ],
            "buyer_results": [
                {
                    "platform": r.platform,
                    "leads_found": r.leads_found,
                    "high_intent_leads": r.high_intent_leads,
                    "messages_sent": r.messages_sent,
                }
                for r in buyer_results
            ],
        }