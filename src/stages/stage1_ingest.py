"""
Stage 1 — Media Ingest & Idea Extraction.

Workflow:
  1. Fetch all RSS feeds, API news, and podcast transcripts (async I/O)
  2. Deduplicate articles by headline similarity
  3. Batch-send to Gemma 4 26B-A4B for structured idea extraction
  4. Validate output against IdeaSummary schema
  5. Save to 01_raw_articles.json and 02_ideas.json
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
import structlog

from src.data.dedup import deduplicate_articles, ArticleGroup
from src.data.models import IdeaSummary, Stage1Output, RawArticle
from src.data.storage import save_json, load_json, load_seen_media, save_seen_media
from src.fetchers.rss import fetch_all_feeds
from src.fetchers.news_api import fetch_finnhub_market_news, fetch_finnhub_ticker_news, fetch_newsapi
from src.fetchers.podcast import fetch_podcast_transcripts
from src.llm.client import get_client
from src.llm.structured import parse_idea_list
from src.utils.dashboard import state as dash_state

log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────────────────────
ARTICLES_PER_BATCH = 5      # Number of article groups to send in one LLM call
MAX_CHARS_PER_ARTICLE = 800 # Truncate individual articles to keep context manageable


def _load_prompt() -> str:
    prompt_path = Path("config/prompts/stage1_extract.md")
    return prompt_path.read_text(encoding="utf-8")


def _build_user_prompt(groups: list[ArticleGroup], run_date: str) -> str:
    """Format article groups into a prompt for Gemma 4."""
    lines = [f"Today's date: {run_date}\n", "Process the following financial articles:\n"]
    for i, group in enumerate(groups):
        lines.append(f"=== ARTICLE GROUP {i+1} ({group.source_count} source(s)) ===")
        lines.append(group.combined_text(max_chars_per_article=MAX_CHARS_PER_ARTICLE))
        lines.append("")
    return "\n".join(lines)


def _sequential_id(date: str, idx: int) -> str:
    return f"idea_{date}_{idx:03d}"


async def run(
    sources_config: dict,
    run_date: Optional[str] = None,
    skip_podcasts: bool = False,
    force: bool = False,
    num_gpu: Optional[int] = None,
) -> Stage1Output:
    """Execute Stage 1 — returns Stage1Output and persists JSON files."""
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    start_time = time.time()

    log.info("stage1.start", date=run_date, force=force)

    # ── 1. Fetch all sources ──────────────────────────────────────────────────
    all_articles: list[RawArticle] = []

    fetch_cfg = sources_config.get("fetch", {})
    max_articles = fetch_cfg.get("max_articles_per_feed", 10)
    max_age_hours = fetch_cfg.get("max_age_hours", 24)
    concurrency = fetch_cfg.get("rss_concurrency", 10)
    user_agent = fetch_cfg.get("user_agent", "TradingInsiderBot/1.0")

    # RSS feeds
    rss_articles = await fetch_all_feeds(
        feeds=sources_config.get("rss_feeds", []),
        max_articles_per_feed=max_articles,
        max_age_hours=max_age_hours,
        concurrency=concurrency,
        user_agent=user_agent,
    )
    all_articles.extend(rss_articles)

    # API sources
    finnhub_key = os.getenv("FINNHUB_API_KEY", "")
    newsapi_key = os.getenv("NEWSAPI_API_KEY", "")

    api_cfg = sources_config.get("api_sources", {})
    async with aiohttp.ClientSession() as session:
        if finnhub_key and api_cfg.get("finnhub", {}).get("enabled"):
            finnhub_cfg = api_cfg["finnhub"]

            market_articles = await fetch_finnhub_market_news(
                session, finnhub_cfg.get("categories", ["general"]), finnhub_key
            )
            all_articles.extend(market_articles)

            ticker_articles = await fetch_finnhub_ticker_news(
                session, finnhub_cfg.get("tracked_tickers", []), finnhub_key
            )
            all_articles.extend(ticker_articles)

        if newsapi_key and api_cfg.get("newsapi", {}).get("enabled"):
            newsapi_cfg = api_cfg["newsapi"]
            newsapi_articles = await fetch_newsapi(
                session,
                queries=newsapi_cfg.get("queries", []),
                api_key=newsapi_key,
                language_filter=newsapi_cfg.get("language_filter"),
            )
            all_articles.extend(newsapi_articles)

    # Podcasts (optional, requires whisper.cpp)
    podcast_minutes = 0.0
    if not skip_podcasts:
        podcast_articles = await fetch_podcast_transcripts(
            podcast_configs=sources_config.get("podcasts", []),
            max_age_hours=fetch_cfg.get("podcast_max_age_hours", 48),
        )
        all_articles.extend(podcast_articles)

    log.info("stage1.fetched_total", total=len(all_articles))

    # Filter out already seen articles (unless force is True)
    if not force:
        seen_media = load_seen_media()
        new_articles = []
        for a in all_articles:
            identifier = a.url or a.title
            if identifier and identifier not in seen_media:
                new_articles.append(a)

        log.info("stage1.filtered_seen", original=len(all_articles), new=len(new_articles))
        all_articles = new_articles
    else:
        log.info("stage1.force_mode_active", hint="Skipping seen media filtering")
    
    dash_state.articles_fetched = len(all_articles)

    if not all_articles:
        log.warning("stage1.no_new_articles")
        output = Stage1Output(
            run_date=run_date,
            ideas=[],
            total_articles_processed=0,
            total_podcast_minutes=podcast_minutes,
            processing_duration_seconds=round(time.time() - start_time, 1),
        )
        return output

    # Save raw articles for debugging
    save_json(
        [a.model_dump(mode="json") for a in all_articles],
        "01_raw_articles.json",
        run_date,
    )

    # ── 2. Deduplicate ────────────────────────────────────────────────────────
    groups = deduplicate_articles(all_articles, similarity_threshold=72)
    log.info("stage1.deduplicated", groups=len(groups), original=len(all_articles))

    # ── 3. LLM Extraction in batches ─────────────────────────────────────────
    model = os.getenv("STAGE1_MODEL", "gemma4:27b")
    llm = get_client()
    system_prompt = _load_prompt()

    all_raw_ideas: list[dict] = []
    batches = [groups[i:i+ARTICLES_PER_BATCH] for i in range(0, len(groups), ARTICLES_PER_BATCH)]

    log.info("stage1.llm_start", model=model, batches=len(batches))

    for batch_idx, batch in enumerate(batches):
        user_prompt = _build_user_prompt(batch, run_date)
        log.info("stage1.batch", idx=batch_idx+1, total=len(batches),
                 groups_in_batch=len(batch))

        try:
            raw_response, _ = await llm.complete(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,      # Low temp for deterministic extraction
                max_tokens=4096,
                thinking=False,       # No thinking needed for extraction
                require_json=True,
                num_gpu=num_gpu,
            )
            ideas_in_batch = parse_idea_list(raw_response)
            all_raw_ideas.extend(ideas_in_batch)
            
            # Dashboard live update
            for raw in ideas_in_batch:
                if raw.get("ticker") and raw.get("headline"):
                    # Basic sanity check to avoid 'undefined' rows
                    dash_state.ideas_data.append(raw)
                    dash_state.ideas_extracted += 1
            dash_state.articles_summarized += len(batch)
            
            log.info("stage1.batch_done", batch=batch_idx+1, ideas=len(ideas_in_batch))

        except Exception as e:
            log.error("stage1.batch_failed", batch=batch_idx+1, error=str(e))
            continue

    # ── 4. Validate and re-index ideas ───────────────────────────────────────
    valid_ideas: list[IdeaSummary] = []
    for raw in all_raw_ideas:
        ticker = str(raw.get("ticker", "")).strip().upper()
        
        # Skip empty/null tickers
        if not ticker or ticker in ("NULL", "N/A", "NONE", "UNKNOWN"):
            continue

        # Basic ticker cleaning (e.g., "Hertz (HTZ)" -> "HTZ")
        if "(" in ticker and ")" in ticker:
            match = re.search(r"\(([^)]+)\)", ticker)
            if match:
                ticker = match.group(1).strip().upper()
        
        # Fix common hallucinations (e.g., HRTZ -> HTZ)
        ticker_map = {"HRTZ": "HTZ", "SAMSUNG": "005930.KS", "HYUNDAI": "005380.KS"}
        ticker = ticker_map.get(ticker, ticker)
        
        raw["ticker"] = ticker

        try:
            # Overwrite ID to ensure consistent sequential format
            raw["id"] = _sequential_id(run_date, len(valid_ideas) + 1)
            idea = IdeaSummary.model_validate(raw)
            valid_ideas.append(idea)
        except Exception as e:
            log.warning("stage1.validation_skip", error=str(e), ticker=ticker)
            continue

    log.info("stage1.ideas_extracted", total=len(valid_ideas))

    # ── 5. Save output ────────────────────────────────────────────────────────
    duration = time.time() - start_time
    output = Stage1Output(
        run_date=run_date,
        ideas=valid_ideas,
        total_articles_processed=len(all_articles),
        total_podcast_minutes=podcast_minutes,
        processing_duration_seconds=round(duration, 1),
    )

    save_json(output, "02_ideas.json", run_date)
    log.info("stage1.complete", ideas=len(valid_ideas), duration_s=round(duration, 1))

    # Update seen media cache
    for a in all_articles:
        identifier = a.url or a.title
        if identifier:
            seen_media.add(identifier)
    save_seen_media(seen_media)

    return output
