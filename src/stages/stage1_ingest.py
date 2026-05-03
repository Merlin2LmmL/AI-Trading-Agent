import asyncio
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import structlog
import aiohttp
from src.data.models import RawArticle, Stage1Output, IdeaSummary, SourceType, Credibility, Direction, TimeHorizon
from src.data.storage import save_json, save_seen_media, load_seen_media
from src.fetchers.rss import fetch_all_feeds
from src.fetchers.news_api import fetch_finnhub_market_news, fetch_finnhub_ticker_news, fetch_newsapi
from src.fetchers.podcast import fetch_podcast_transcripts
from src.fetchers.gemini_research import fetch_portfolio_research
from src.llm.client import get_client
from src.utils import dashboard

log = structlog.get_logger()

def parse_idea_list(raw_response: str) -> list[dict]:
    """
    Parses a JSON list of ideas from the LLM response.
    """
    from src.llm.structured import extract_json_from_response
    import json
    
    cleaned = extract_json_from_response(raw_response)
    if not cleaned:
        return []
    
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Handle cases where LLM wraps the list in an object
            for key in ["ideas", "signals", "reports"]:
                if key in data and isinstance(data[key], list):
                    return data[key]
            return [data]
        return []
    except Exception as e:
        log.warning("stage1.parse_error", error=str(e))
        return []

async def run(
    sources_config: dict,
    run_date: Optional[str] = None,
    skip_podcasts: bool = False,
    force: bool = False,
    num_gpu: Optional[int] = None,
    live_portfolio: Optional[dict] = None,
) -> Stage1Output:
    """Execute Stage 1 — returns Stage1Output and persists JSON files."""
    from src.utils.dashboard import state as dash_state
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    start_time = time.time()

    log.info("stage1.start", date=run_date, force=force)
    
    fetch_cfg = sources_config.get("fetch", {})
    all_articles: list[RawArticle] = []
    podcast_minutes = 0.0

    # ── 1. Fetch or load raw articles ─────────────────────────────────────────
    raw_articles_path = Path(f"output/{run_date}/01_raw_articles.json")
    if not force and raw_articles_path.exists():
        log.info("stage1.resume_fetch", path=str(raw_articles_path))
        from src.data.storage import load_json
        raw_data = load_json("01_raw_articles.json", run_date)
        all_articles = [RawArticle(**a) for a in raw_data]
    else:
        # Fetch RSS
        max_articles = fetch_cfg.get("max_articles_per_feed", 10)
        max_age_hours = fetch_cfg.get("max_age_hours", 24)
        concurrency = fetch_cfg.get("rss_concurrency", 10)
        user_agent = fetch_cfg.get("user_agent", "TradingInsiderBot/1.0")

        rss_articles = await fetch_all_feeds(
            feeds=sources_config.get("rss_feeds", []),
            max_articles_per_feed=max_articles,
            max_age_hours=max_age_hours,
            concurrency=concurrency,
            user_agent=user_agent,
        )
        all_articles.extend(rss_articles)
        dash_state.articles_fetched = len(all_articles)

        # Fetch APIs
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
                dash_state.articles_fetched = len(all_articles)

            if newsapi_key and api_cfg.get("newsapi", {}).get("enabled"):
                newsapi_cfg = api_cfg["newsapi"]
                newsapi_articles = await fetch_newsapi(
                    session,
                    queries=newsapi_cfg.get("queries", []),
                    api_key=newsapi_key,
                    language_filter=newsapi_cfg.get("language_filter"),
                )
                all_articles.extend(newsapi_articles)
                dash_state.articles_fetched = len(all_articles)

        # Fetch Podcasts
        if not skip_podcasts:
            dash_state.current_task = "Transcribing Podcasts"
            podcast_articles = await fetch_podcast_transcripts(
                podcast_configs=sources_config.get("podcasts", []),
                max_age_hours=fetch_cfg.get("podcast_max_age_hours", 48),
            )
            all_articles.extend(podcast_articles)
            dash_state.articles_fetched = len(all_articles)
            dash_state.current_task = ""

        # Save raw articles
        save_json([a.model_dump() for a in all_articles], "01_raw_articles.json", run_date)

    # ── 2. Deduplicate and filter ─────────────────────────────────────────────
    seen_media = load_seen_media()
    
    if force:
        new_articles = all_articles
        log.info("stage1.force_reprocess_all")
    else:
        new_articles = [a for a in all_articles if (a.url or a.title) not in seen_media]
        
        # If everything was 'seen' but we have no ideas yet today, let's process them anyway
        if not new_articles and all_articles:
            log.info("stage1.retry_seen_articles", count=len(all_articles))
            new_articles = all_articles

    log.info("stage1.filtered_seen", original=len(all_articles), new=len(new_articles))
    
    if not new_articles:
        return Stage1Output(
            run_date=run_date, 
            ideas=[], 
            total_articles_processed=len(all_articles),
            total_podcast_minutes=0.0,
            processing_duration_seconds=round(time.time() - start_time, 1)
        )

    # Portfolio research (always run as it's targeted)
    portfolio_research = await fetch_portfolio_research(live_portfolio)
    new_articles.extend(portfolio_research)

    # Simple group-by-source or similar? No, just batch them for Gemini
    batch_size = 5
    all_raw_ideas = []
    
    llm = get_client()
    
    with open("config/prompts/stage1_extract.md", "r") as f:
        prompt_template = f.read()

    # ── 3. Batch Extraction ──────────────────────────────────────────────────
    dash_state.articles_summarized = 0
    dash_state.total_articles = len(new_articles)
    
    for batch_idx, i in enumerate(range(0, len(new_articles), batch_size)):
        batch = new_articles[i:i+batch_size]
        log.info("stage1.batch", idx=batch_idx+1, total=(len(new_articles)//batch_size)+1, groups_in_batch=len(batch))
        
        batch_text = ""
        for idx, art in enumerate(batch):
            text_snippet = art.full_text[:3000] if art.full_text else "No content available."
            batch_text += f"--- ARTICLE {idx+1} ---\nSource: {art.source_name}\nTitle: {art.title}\nText: {text_snippet}\n\n"

        user_prompt = f"### CURRENT DATE: {run_date}\n\n### ARTICLES TO ANALYZE:\n{batch_text}"
        
        try:
            raw_response, thinking_trace, _ = await llm.complete(
                model=os.getenv("STAGE1_MODEL"),
                system_prompt=prompt_template,
                user_prompt=user_prompt,
                thinking=True,
                num_gpu=num_gpu,
            )
            ideas_in_batch = parse_idea_list(raw_response)
            
            # Attach trace data for dashboard
            for raw in ideas_in_batch:
                if isinstance(raw, dict):
                    raw["input_prompt"] = user_prompt
                    raw["thinking_trace"] = thinking_trace
                    
                    if raw.get("ticker") and raw.get("headline"):
                        dash_state.ideas_data.append(raw)
                        dash_state.ideas_extracted += 1
            
            all_raw_ideas.extend(ideas_in_batch)
            dash_state.articles_summarized += len(batch)
            
            log.info("stage1.batch_done", batch=batch_idx+1, ideas=len(ideas_in_batch))
            
            # Pacing delay to avoid 429 Quota errors on Gemini
            await asyncio.sleep(15)

        except Exception as e:
            log.error("stage1.batch_failed", batch=batch_idx+1, error=str(e))
            continue

    # ── 4. Validate and re-index ideas ───────────────────────────────────────
    valid_ideas: list[IdeaSummary] = []
    for raw in all_raw_ideas:
        try:
            # Ensure ID exists or generate one
            if not raw.get("id"):
                import uuid
                raw["id"] = f"idea_{run_date}_{str(uuid.uuid4())[:8]}"
            
            # Map legacy or common fields if necessary
            if "rationale" in raw and "thesis_1sentence" not in raw:
                raw["thesis_1sentence"] = raw["rationale"]
            
            # Filter out items with no ticker
            if not raw.get("ticker"):
                continue

            # Validate against IdeaSummary model
            idea = IdeaSummary.model_validate(raw)
            valid_ideas.append(idea)
        except Exception as ve:
            log.warning("stage1.validation_error", ticker=raw.get("ticker"), error=str(ve))

    log.info("stage1.ideas_extracted", total=len(valid_ideas))

    # ── 4.5 Inject Synthetic Ideas for Portfolio and Watchlist ────────────────
    try:
        import yaml
        portfolio = live_portfolio or {}
        if not portfolio:
            with open("config/portfolio.yaml", "r") as f:
                portfolio = yaml.safe_load(f)
                
        port_tickers = []
        for holding in portfolio.get("holdings", []):
            ticker = holding.get("ticker", holding.get("isin", ""))
            if not ticker: continue
            port_tickers.append(ticker)
            
            synthetic_sell = IdeaSummary(
                id=f"idea_{run_date}_PORTFOLIO_{ticker}",
                ticker=ticker,
                company=holding.get("name", ticker),
                market="US",
                direction=Direction.SHORT,
                time_horizon=TimeHorizon.SHORT_TERM,
                conviction_from_sources=8,
                headline=f"Periodic Portfolio Review: Sell-side research for {ticker}",
                thesis_1sentence=f"Actively searching for bear-case arguments and reasons to close our position in {ticker}.",
                key_facts=["Currently held in portfolio", "Seeking opposing views to prevent confirmation bias"],
                source_quality_score=5,
                sources=[],
                tags=["Portfolio Maintenance", "Sell-Side Research"]
            )
            valid_ideas.append(synthetic_sell)
            
        import json
        watchlist_path = Path("watchlist.json")
        if watchlist_path.exists():
            watchlist = json.loads(watchlist_path.read_text(encoding="utf-8"))
            
            # Remove anything already in portfolio
            watchlist = [w for w in watchlist if w.get("ticker") not in port_tickers]
            
            if watchlist:
                dash_state.current_task = "Evaluating Watchlist"
                wl_prompt = f"""
### CURRENT DATE: {run_date}

You are an expert portfolio manager. Review the following watchlist of stocks.
For each stock, based on its original reason for being on the watchlist and the current date, decide:
1. "REMOVE": The catalyst has passed, the reason is stale, or it's no longer interesting.
2. "KEEP": Keep watching but do not actively research today.
3. "RESEARCH": Add to today's active research pipeline to actively find reasons to BUY.

Return a JSON list of objects EXACTLY matching this schema:
[
  {{"ticker": "TICKER", "decision": "REMOVE|KEEP|RESEARCH", "rationale": "Why you made this decision"}}
]

Watchlist:
{json.dumps(watchlist, indent=2)}
"""
                try:
                    raw_wl_resp, _, _ = await llm.complete(
                        model=os.getenv("STAGE1_MODEL"),
                        system_prompt="You are a strict JSON-only AI. Output only valid JSON.",
                        user_prompt=wl_prompt,
                        require_json=True,
                        num_gpu=num_gpu
                    )
                    from src.llm.structured import extract_json_from_response
                    wl_json = extract_json_from_response(raw_wl_resp)
                    if wl_json:
                        wl_decisions = json.loads(wl_json)
                        if isinstance(wl_decisions, dict):
                            for k in wl_decisions.keys():
                                if isinstance(wl_decisions[k], list):
                                    wl_decisions = wl_decisions[k]
                                    break
                                    
                        new_watchlist = []
                        for dec in wl_decisions:
                            if not isinstance(dec, dict): continue
                            t = dec.get("ticker")
                            d = dec.get("decision", "KEEP").upper()
                            
                            orig_w = next((w for w in watchlist if w.get("ticker") == t), None)
                            if not orig_w: continue
                            
                            if d == "REMOVE":
                                log.info("stage1.watchlist_remove", ticker=t, rationale=dec.get("rationale"))
                                continue
                                
                            new_watchlist.append(orig_w)
                            
                            if d == "RESEARCH":
                                synthetic_buy = IdeaSummary(
                                    id=f"idea_{run_date}_WATCHLIST_{t}",
                                    ticker=t,
                                    company=orig_w.get("company_name", t),
                                    market="US",
                                    direction=Direction.LONG,
                                    time_horizon=TimeHorizon.SHORT_TERM,
                                    conviction_from_sources=8,
                                    headline=f"Watchlist Activation: Buy-side research for {t}",
                                    thesis_1sentence=f"Actively searching for catalysts and arguments to initiate a position in {t}. Previous reason: {orig_w.get('reason', '')}",
                                    key_facts=["Currently on watchlist", "Seeking entry points"],
                                    source_quality_score=5,
                                    sources=[],
                                    tags=["Watchlist", "Buy-Side Research"]
                                )
                                valid_ideas.append(synthetic_buy)
                                
                        watchlist_path.write_text(json.dumps(new_watchlist, indent=2), encoding="utf-8")
                except Exception as e:
                    log.error("stage1.watchlist_eval_error", error=str(e))
                    # fallback: just add them all to research
                    for w in watchlist:
                        t = w.get("ticker")
                        synthetic_buy = IdeaSummary(
                            id=f"idea_{run_date}_WATCHLIST_{t}",
                            ticker=t,
                            company=w.get("company_name", t),
                            market="US",
                            direction=Direction.LONG,
                            time_horizon=TimeHorizon.SHORT_TERM,
                            conviction_from_sources=8,
                            headline=f"Watchlist Activation: Buy-side research for {t}",
                            thesis_1sentence=f"Actively searching for catalysts and arguments to initiate a position in {t}. Previous reason: {w.get('reason', '')}",
                            key_facts=["Currently on watchlist", "Seeking entry points"],
                            source_quality_score=5,
                            sources=[],
                            tags=["Watchlist", "Buy-Side Research"]
                        )
                        valid_ideas.append(synthetic_buy)
                
    except Exception as e:
        log.error("stage1.synthetic_injection_error", error=str(e))

    # ── 5. Save output ────────────────────────────────────────────────────────
    duration = time.time() - start_time
    podcasts_count = len([a for a in all_articles if a.source_type == SourceType.PODCAST])
    output = Stage1Output(
        run_date=run_date,
        ideas=valid_ideas,
        total_articles_processed=len(all_articles),
        total_podcasts_processed=podcasts_count,
        total_podcast_minutes=podcast_minutes,
        processing_duration_seconds=round(duration, 1),
    )

    if valid_ideas or not force:
        save_json(output, "02_ideas.json", run_date)
        log.info("stage1.complete", ideas=len(valid_ideas), duration_s=round(duration, 1))

    # Update seen media cache
    for a in all_articles:
        identifier = a.url or a.title
        if identifier:
            seen_media.add(identifier)
    save_seen_media(seen_media)

    return output
