"""
News API clients for Finnhub and NewsAPI.org (both free tiers).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import structlog

from src.data.models import RawArticle, SourceType, Credibility

log = structlog.get_logger()

FINNHUB_BASE = "https://finnhub.io/api/v1"
NEWSAPI_BASE = "https://newsapi.org/v2"


# ── Finnhub ───────────────────────────────────────────────────────────────────

async def fetch_finnhub_market_news(
    session: aiohttp.ClientSession,
    categories: list[str],
    api_key: str,
) -> list[RawArticle]:
    """Fetch general market news from Finnhub by category."""
    articles = []
    for category in categories:
        url = f"{FINNHUB_BASE}/news?category={category}&token={api_key}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.warning("finnhub.market_news_failed", category=category, status=resp.status)
                    continue
                data = await resp.json()
        except Exception as e:
            log.warning("finnhub.market_news_error", category=category, error=str(e))
            continue

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for item in data[:15]:
            dt = datetime.fromtimestamp(item.get("datetime", 0), tz=timezone.utc)
            if dt < cutoff:
                continue
            article = RawArticle(
                source_name=item.get("source", "Finnhub"),
                source_type=SourceType.NEWS,
                language="en",
                credibility=Credibility.MEDIUM,
                title=item.get("headline", ""),
                url=item.get("url"),
                published=dt.isoformat(),
                summary=item.get("summary", "")[:500],
            )
            articles.append(article)
        # Respect Finnhub rate limit (60/min → sleep briefly between categories)
        time.sleep(0.3)

    log.info("finnhub.market_news", count=len(articles))
    return articles


async def fetch_finnhub_ticker_news(
    session: aiohttp.ClientSession,
    tickers: list[str],
    api_key: str,
) -> list[RawArticle]:
    """Fetch news for specific tickers from Finnhub."""
    articles = []
    from_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    to_date = datetime.now().strftime("%Y-%m-%d")

    for ticker in tickers:
        url = (
            f"{FINNHUB_BASE}/company-news"
            f"?symbol={ticker}&from={from_date}&to={to_date}&token={api_key}"
        )
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
        except Exception as e:
            log.warning("finnhub.ticker_news_error", ticker=ticker, error=str(e))
            continue

        for item in data[:5]:  # Max 5 per ticker to avoid bloat
            article = RawArticle(
                source_name=item.get("source", "Finnhub"),
                source_type=SourceType.NEWS,
                language="en",
                credibility=Credibility.MEDIUM,
                title=item.get("headline", ""),
                url=item.get("url"),
                published=datetime.fromtimestamp(
                    item.get("datetime", 0), tz=timezone.utc
                ).isoformat(),
                summary=item.get("summary", "")[:500],
            )
            articles.append(article)
        time.sleep(0.5)  # Respect rate limit

    log.info("finnhub.ticker_news", tickers=len(tickers), articles=len(articles))
    return articles


# ── NewsAPI.org ───────────────────────────────────────────────────────────────

async def fetch_newsapi(
    session: aiohttp.ClientSession,
    queries: list[str],
    api_key: str,
    language_filter: Optional[list[str]] = None,
) -> list[RawArticle]:
    """
    Fetch financial news from NewsAPI.org using keyword queries.
    Free tier: 100 requests/day, no full article text.
    """
    articles = []
    seen_urls: set[str] = set()
    # Free tier works best with simple date strings
    from_dt = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    for query in queries:
        params = {
            "q": query,
            "from": from_dt,
            "sortBy": "relevancy",
            "apiKey": api_key,
            "pageSize": 10,
        }
        if language_filter and len(language_filter) == 1:
            params["language"] = language_filter[0]

        url = f"{NEWSAPI_BASE}/everything"
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 426:
                    log.warning("newsapi.rate_limit_upgrade_required")
                    break
                if resp.status == 429:
                    log.warning("newsapi.rate_limit_exceeded", hint="Free tier allows 100 req/day")
                    break
                if resp.status != 200:
                    log.warning("newsapi.failed", query=query, status=resp.status)
                    continue
                data = await resp.json()
        except Exception as e:
            log.warning("newsapi.error", query=query, error=str(e))
            continue

        for item in data.get("articles", []):
            article_url = item.get("url", "")
            if article_url in seen_urls:
                continue
            seen_urls.add(article_url)

            published = item.get("publishedAt", "")
            article = RawArticle(
                source_name=item.get("source", {}).get("name", "NewsAPI"),
                source_type=SourceType.NEWS,
                language="en",  # NewsAPI doesn't always return language per article
                credibility=Credibility.MEDIUM,
                title=item.get("title", ""),
                url=article_url,
                published=published,
                summary=item.get("description", "")[:500],
            )
            articles.append(article)

    log.info("newsapi.done", queries=len(queries), articles=len(articles))
    return articles
