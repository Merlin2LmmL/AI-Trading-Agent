"""
DuckDuckGo web search fetcher for Stage 2.
Provides recent, general web and news context for a given company/ticker without requiring an API key.
"""
from typing import Optional
from ddgs import DDGS
import structlog

log = structlog.get_logger()

async def fetch_recent_news(
    company_name: Optional[str] = None, 
    ticker: Optional[str] = None, 
    max_results: int = 4,
    custom_query: Optional[str] = None
) -> str:
    """
    Search DuckDuckGo News for the latest articles.
    If custom_query is provided, it uses that. Otherwise, it builds a generic one.
    Returns a formatted string of recent news snippets.
    """
    import asyncio
    
    if custom_query:
        query = custom_query
        target = custom_query 
    else:
        target = company_name if company_name and company_name.strip() else ticker
        query = f'"{target}" (stock OR geopolitics OR news)'
    
    log.info("web_search.ddg", query=query)
    
    results_text = []
    try:
        def _sync_search(q: str, is_news: bool):
            with DDGS() as ddgs:
                if is_news:
                    return list(ddgs.news(q, timelimit='w', max_results=max_results))
                else:
                    return list(ddgs.text(q, timelimit='w', max_results=max_results))
        
        # Try News first
        try:
            results = await asyncio.to_thread(_sync_search, query, True)
        except Exception as news_err:
            log.warning("web_search.news_failed", error=str(news_err))
            # Fallback to general text search if news is blocked (403)
            results = await asyncio.to_thread(_sync_search, query, False)
        
        for i, r in enumerate(results):
                title = r.get("title", "No Title")
                body = r.get("body") or r.get("snippet") or "No Summary"
                date = r.get("date", "Recent")
                source = r.get("source") or r.get("href", "Web")
                
                if isinstance(date, str) and "T" in date:
                    date = date.split("T")[0]
                    
                results_text.append(f"[{i+1}] {date} | {source} | {title}\n    Snippet: {body}")
                
    except Exception as e:
        log.warning("web_search.all_failed", query=query, error=str(e))
        return "⚠️ Web search failed. This is usually due to rate-limiting by DuckDuckGo."
        
    if not results_text:
        return f"No results found for '{query}'."
        
    return "\n\n".join(results_text)
        
    return "\n\n".join(results_text)
        
    return "\n\n".join(results_text)
