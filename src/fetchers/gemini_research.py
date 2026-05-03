"""
Gemini Research Agent — specialized fetcher that uses Gemini's deep-research capabilities
to find and summarize recent media articles affecting the current portfolio.
"""
import asyncio
import os
from datetime import datetime, timezone
from typing import List, Optional, Dict

import structlog
from src.data.models import RawArticle, SourceType, Credibility
from src.llm.gemini import GeminiClient
from src.fetchers.wikifolio import fetch_wikifolio_portfolio, holdings_to_portfolio_yaml_format

log = structlog.get_logger()

async def fetch_gemini_research_news(tickers: List[str]) -> List[RawArticle]:
    """
    Uses Gemini's native search/research tools to find impactful news for the given tickers.
    """
    if not tickers:
        return []

    if os.getenv("LLM_PROVIDER", "local").lower() != "api":
        log.info("gemini_research.skipped", reason="local_mode_active")
        return []

    client = GeminiClient()
    articles = []
    
    # We use a research-capable model
    model = os.getenv("API_STAGE1_MODEL", "gemini-3.1-flash-lite-preview")
    
    log.info("gemini_research.start", count=len(tickers))
    
    # Process in small chunks to avoid prompt bloat or timeouts
    chunk_size = 3
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i+chunk_size]
        log.info("gemini_research.batch", tickers=chunk)
        
        user_prompt = (
            f"Perform deep research on the following tickers: {', '.join(chunk)}.\n"
            "Find recent high-impact media articles, news, or analyst reports from the last 7 days.\n"
            "For each significant piece of news found, provide a separate entry with:\n"
            "1. Headline\n"
            "2. Source Name\n"
            "3. Summary of the impact\n"
            "4. URL (if available)\n\n"
            "Format your response as a clear list of findings."
        )
        
        try:
            # We enable tools (Google Search) and thinking
            raw_response, thinking, _ = await client.complete(
                model=model,
                user_prompt=user_prompt,
                thinking=True,
                use_tools=True
            )
            
            # Since Gemini returns a text summary, we'll treat the whole response as one 'Research Report' article 
            # for each ticker, or try to split it.
            # For simplicity and robust integration, we'll create one 'Gemini Research Report' per ticker 
            # if they were mentioned in the response.
            
            for ticker in chunk:
                # Basic check if the ticker was mentioned in the research results
                if ticker.upper() in raw_response.upper():
                    # Extract the part relevant to this ticker (rough heuristic)
                    article = RawArticle(
                        source_name="Gemini Deep Research",
                        source_type=SourceType.API,
                        language="en",
                        credibility=Credibility.HIGH,
                        title=f"Deep Research Report: {ticker}",
                        url="https://gemini.google.com/search",
                        published=datetime.now(timezone.utc).isoformat(),
                        full_text=raw_response, # We store the full response; Stage 1 extraction will pick out the parts
                    )
                    articles.append(article)
                    
        except Exception as e:
            log.error("gemini_research.error", batch=chunk, error=str(e))
            
        await asyncio.sleep(10) # Pacing to avoid 429 errors
            
    return articles

async def fetch_portfolio_research(portfolio_data: Optional[dict] = None) -> List[RawArticle]:
    """Helper to fetch research specifically for current portfolio holdings."""
    try:
        if not portfolio_data:
            portfolio_data = fetch_wikifolio_portfolio()
        
        holdings = portfolio_data.get("holdings", [])
        if not holdings:
            return []
            
        # Handle both raw holdings and formatted portfolio data
        if holdings and "ticker" in holdings[0]:
            # Already formatted
            holdings_yaml = holdings
        else:
            holdings_yaml = holdings_to_portfolio_yaml_format(holdings)

        tickers = [h.get("ticker") for h in holdings_yaml if h.get("ticker") and h.get("ticker") != "CASH"]
        return await fetch_gemini_research_news(tickers)
    except Exception as e:
        log.error("gemini_research.portfolio_fetch_failed", error=str(e))
        return []
