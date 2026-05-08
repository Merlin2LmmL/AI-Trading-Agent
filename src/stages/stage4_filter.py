"""
Stage 4 — Filter and Deduplication.

This stage consumes the output of Stage 3 (scored ideas) and produces a cleaned list for Stage 4.

Rules:
1. Skip ideas with recommendation `SKIP`.
2. Collect ideas with recommendation `WATCH` into a global JSON watchlist file (`watchlist.json`) in the repo root.
   Existing entries are preserved and new ones appended.
3. Deduplicate remaining ideas by ticker – keep the one with the highest overall score.
"""

from __future__ import annotations

import json

from pathlib import Path
from src.data.models import (
    Stage3Output,
    ResearchReport,
    Recommendation,
)

WATCHLIST_PATH = Path("watchlist.json")


def _load_watchlist() -> list[dict]:
    if WATCHLIST_PATH.exists():
        return json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    return []


def _append_watchlist(entries: list[dict]) -> None:
    existing = _load_watchlist()
    
    # Clean up watchlist: remove items already in portfolio
    import yaml
    try:
        with open("config/portfolio.yaml", "r") as f:
            port = yaml.safe_load(f)
            port_tickers = {h.get("ticker", h.get("isin", "")) for h in port.get("holdings", [])}
    except Exception:
        port_tickers = set()

    existing = [e for e in existing if e.get("ticker") not in port_tickers]
    
    existing_tickers = {e.get("ticker") for e in existing if e.get("ticker")}
    for e in entries:
        t = e.get("ticker")
        if t and t not in existing_tickers and t not in port_tickers:
            existing.append(e)
            existing_tickers.add(t)
            
    # Cap size to last 15 items to prevent infinite growth
    if len(existing) > 15:
        existing = existing[-15:]
        
    WATCHLIST_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")


async def run(
    stage3_output: Stage3Output,
    run_date: str,
) -> Stage3Output:
    """Filter and deduplicate scored ideas.

    Returns a new Stage3Output with the cleaned list.
    """
    ideas = stage3_output.scored_ideas
    watch_entries: list[dict] = []
    ticker_to_report: dict[str, ResearchReport] = {}

    for r in ideas:
        if r.recommendation == Recommendation.SKIP:
            continue
        if r.recommendation == Recommendation.WATCH:
            watch_entries.append({"ticker": r.ticker, "reason": r.research.get("fundamental_assessment", "")})
            continue
        # Skip sell/ reduce recommendations if the ticker is not in the current portfolio holdings
        if r.recommendation in (Recommendation.SELL, Recommendation.REDUCE):
            # Load portfolio holdings once for efficiency
            if 'portfolio_holdings' not in globals():
                import yaml
                try:
                    with open("config/portfolio.yaml", "r") as f:
                        port = yaml.safe_load(f)
                        globals()['portfolio_holdings'] = {h.get("ticker", h.get("isin", "")) for h in port.get("holdings", [])}
                except Exception:
                    globals()['portfolio_holdings'] = set()
            if r.ticker not in globals()['portfolio_holdings']:
                continue
        if not r.ticker:
            continue
        key = r.ticker
        curr = ticker_to_report.get(key)
        if curr is None or r.scores.get("overall", 0) > curr.scores.get("overall", 0):
            ticker_to_report[key] = r
    filtered = list(ticker_to_report.values())

    if watch_entries:
        _append_watchlist(watch_entries)

    return Stage3Output(
        run_date=stage3_output.run_date,
        scored_ideas=filtered,
        ideas_processed=len(filtered),
            ideas_passing=len(filtered),
        processing_duration_seconds=stage3_output.processing_duration_seconds,
    )
