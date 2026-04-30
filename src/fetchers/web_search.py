"""
DuckDuckGo web search fetcher for Stage 2.
Provides recent, general web and news context for a given company/ticker without requiring an API key.
"""
from typing import Optional
from ddgs import DDGS
import structlog

log = structlog.get_logger()

async def fetch_recent_news(company_name: Optional[str], ticker: str, max_results: int = 4) -> str:
    """
    Search DuckDuckGo News for the latest articles about the company (ASYNC).
    Returns a formatted string of recent news snippets.
    """
    import asyncio
    # Build a robust search query
    target = company_name if company_name and company_name.strip() else ticker
    query = f'"{target}" (stock OR geopolitics OR news)'
    
    log.info("web_search.ddg", query=query)
    
    results_text = []
    try:
        def _sync_search():
            with DDGS() as ddgs:
                # timelimit='w' means past week, keeping info highly relevant
                return list(ddgs.news(query, timelimit='w', max_results=max_results))
        
        results = await asyncio.to_thread(_sync_search)
        
        for i, r in enumerate(results):
                title = r.get("title", "No Title")
                body = r.get("body", "No Summary")
                date = r.get("date", "Recent")
                source = r.get("source", "Web")
                
                # Clean up date string if possible
                if "T" in date:
                    date = date.split("T")[0]
                    
                results_text.append(f"[{i+1}] {date} | {source} | {title}\n    Snippet: {body}")
                
    except Exception as e:
        log.warning("web_search.failed", query=query, error=str(e))
        return "⚠️ Web search failed or was rate-limited."
        
    if not results_text:
        return f"No significant news found on the web for '{target}' in the past week."
        
    return "\n\n".join(results_text)
