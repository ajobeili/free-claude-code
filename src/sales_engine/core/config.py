"""Configuration and settings for the Sales Engine."""

import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # OpenRouter (AI)
    openrouter_api_key: str = os.environ.get("OPENROUTER_API_KEY", "")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openai/gpt-4o-mini"

    # Resend (email)
    resend_api_key: str = os.environ.get("RESEND_API_KEY", "")
    resend_from_email: str = "noreply@sales-engine.com"

    # Reddit API
    reddit_client_id: str = os.environ.get("REDDIT_CLIENT_ID", "")
    reddit_client_secret: str = os.environ.get("REDDIT_CLIENT_SECRET", "")
    reddit_user_agent: str = os.environ.get("REDDIT_USER_AGENT", "SalesEngine/1.0")
    reddit_username: str = os.environ.get("REDDIT_USERNAME", "")
    reddit_password: str = os.environ.get("REDDIT_PASSWORD", "")

    # Twitter API
    twitter_bearer_token: str = os.environ.get("TWITTER_BEARER_TOKEN", "")

    # Gumroad
    gumroad_access_code: str = os.environ.get("GUMROAD_ACCESS_CODE", "")

    # Hunter.io (email finder)
    hunter_api_key: str = os.environ.get("HUNTER_API_KEY", "")

    # Database
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///./sales_engine.db")

    class Config:
        env_file = ".env"


settings = Settings()
