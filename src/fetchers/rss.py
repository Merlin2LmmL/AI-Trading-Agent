"""
Async RSS feed fetcher. Pulls all configured feeds concurrently.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional
import time

import aiohttp
import feedparser
import structlog

from src.data.models import RawArticle, SourceType, Credibility

log = structlog.get_logger()


def _parse_date(entry: feedparser.FeedParserDict) -> Optional[str]:
    """Extract and normalise a publish date from a feed entry."""
    for field in ("published", "updated", "created"):
        raw = entry.get(field)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                return dt.isoformat()
            except Exception:
                pass
    return None


def _is_recent(date_str: Optional[str], max_age_hours: int) -> bool:
    """Return True if the article was published within max_age_hours."""
    if not date_str:
        return True  # If no date, include it (can't filter)
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        return dt >= cutoff
    except Exception:
        return True


def _get_full_text(entry: feedparser.FeedParserDict) -> Optional[str]:
    """Extract the best available text content from a feed entry."""
    # Try content first (full article)
    if hasattr(entry, "content") and entry.content:
        return entry.content[0].get("value", "")
    # Fall back to summary
    if hasattr(entry, "summary"):
        return entry.summary
    return None


async def fetch_feed(
    session: aiohttp.ClientSession,
    feed_config: dict,
    max_articles: int,
    max_age_hours: int,
    user_agent: str,
) -> list[RawArticle]:
    """Fetch a single RSS feed asynchronously."""
    url = feed_config["url"]
    name = feed_config["name"]
    language = feed_config.get("language", "en")
    credibility = Credibility(feed_config.get("credibility", "MEDIUM"))

    try:
        headers = {"User-Agent": user_agent}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                log.warning("rss.fetch_failed", url=url, status=resp.status)
                return []
            content = await resp.read()
    except Exception as e:
        log.warning("rss.fetch_error", url=url, error=str(e))
        return []

    feed = feedparser.parse(content)
    articles = []

    for entry in feed.entries[:max_articles]:
        date_str = _parse_date(entry)
        if not _is_recent(date_str, max_age_hours):
            continue

        title = entry.get("title", "").strip()
        if not title:
            continue

        article = RawArticle(
            source_name=name,
            source_type=SourceType.NEWS,
            language=language,
            credibility=credibility,
            title=title,
            url=entry.get("link"),
            published=date_str,
            summary=entry.get("summary", "")[:500] if entry.get("summary") else None,
            full_text=_get_full_text(entry),
        )
        articles.append(article)

    log.info("rss.fetched", source=name, count=len(articles))
    return articles


async def fetch_all_feeds(
    feeds: list[dict],
    max_articles_per_feed: int = 10,
    max_age_hours: int = 24,
    concurrency: int = 10,
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
) -> list[RawArticle]:
    """Fetch all RSS feeds concurrently, respecting a concurrency limit."""
    semaphore = asyncio.Semaphore(concurrency)
    all_articles: list[RawArticle] = []

    async def _guarded_fetch(session, feed_config):
        async with semaphore:
            return await fetch_feed(
                session, feed_config, max_articles_per_feed, max_age_hours, user_agent
            )

    connector = aiohttp.TCPConnector(limit=concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [_guarded_fetch(session, feed) for feed in feeds]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, list):
            all_articles.extend(result)
        elif isinstance(result, Exception):
            log.warning("rss.task_error", error=str(result))

    log.info("rss.all_done", total_articles=len(all_articles))
    return all_articles
