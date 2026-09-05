# Sales Engine

AI-Powered Digital Product Sales Engine. Upload a product folder → AI auto-distributes to multiple platforms and finds buyers.

## Features

- **Auto-Distribution**: Lists products on Gumroad, Lemon Squeezy, AlternativeTo, IndieHackers, Odoo
- **Buyer Finder**: Monitors Reddit, Twitter, and forums for people asking about your product
- **Auto-Outreach**: AI generates personalized messages and sends them automatically
- **Dashboard**: Track listings, leads, messages, and sales in one place

## Setup

```bash
cp .env.example .env
# Add your API keys to .env

uv sync
uv run fastapi dev src/sales_engine/api/main.py
```

## API Keys Needed

- `OPENROUTER_API_KEY` - AI (free tier)
- `RESEND_API_KEY` - Email sending (3k free/month)
- `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` - Reddit API
- `TWITTER_BEARER_TOKEN` - Twitter API
- `GUMROAD_ACCESS_CODE` - Gumroad
- `HUNTER_API_KEY` - Email finder (25 free/month)
