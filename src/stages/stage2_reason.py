"""
Stage 2 — Deep Research + Evaluation.

For each idea from Stage 1:
  1. Fetch fundamental data (yfinance)
  2. Build enriched context (idea + fundamentals + web news)
  3. Send to Qwen3 32B for deep research + scoring in ONE pass
  4. Validate and save results
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from src.data.models import (
    IdeaSummary, Stage1Output, FundamentalData,
    ResearchReport, Stage2Output, Recommendation,
)
from src.data.storage import save_json, load_json
from src.fetchers.fundamentals import fetch_fundamentals
from src.fetchers.web_search import fetch_recent_news
from src.llm.client import get_client
from src.llm.structured import extract_thinking_trace, extract_json_from_response
from src.utils.dashboard import state as dash_state

log = structlog.get_logger()


def _load_prompt() -> str:
    return Path("config/prompts/stage2_reason.md").read_text(encoding="utf-8")


def _format_fundamentals(fund: Optional[FundamentalData]) -> str:
    """Render fundamental data as a clean text block for LLM context."""
    if not fund:
        return "⚠️ No fundamental data available for this ticker."

    def fmt(val, pct=False, money=False):
        if val is None:
            return "N/A"
        if pct:
            return f"{val * 100:.1f}%"
        if money:
            if val >= 1e12:
                return f"${val/1e12:.2f}T"
            if val >= 1e9:
                return f"${val/1e9:.1f}B"
            if val >= 1e6:
                return f"${val/1e6:.0f}M"
            return f"${val:,.0f}"
        return str(round(val, 2))

    return f"""
FUNDAMENTAL DATA — {fund.ticker} ({fund.company_name or "Unknown"})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Current Price:        {fmt(fund.current_price, money=True)}
Market Cap:           {fmt(fund.market_cap_usd, money=True)}
Sector / Industry:    {fund.sector or "N/A"} / {fund.industry or "N/A"}

Valuation:
  P/E (TTM):          {fmt(fund.pe_ratio_ttm)}x
  P/E (Forward):      {fmt(fund.pe_ratio_forward)}x
  Analyst Target:     {fmt(fund.analyst_target_price, money=True)}
  Analyst Rating:     {fund.analyst_recommendation or "N/A"}

Growth & Margins:
  Revenue Growth YoY: {fmt(fund.revenue_growth_yoy, pct=True)}
  Gross Margin:       {fmt(fund.gross_margin, pct=True)}
  Net Margin:         {fmt(fund.net_margin, pct=True)}
  ROE:                {fmt(fund.roe, pct=True)}

Risk Metrics:
  Debt/Equity:        {fmt(fund.debt_to_equity)}
  Short Float %:      {fmt(fund.short_float_pct, pct=True)}

Price Position:
  52W High:           {fmt(fund.price_52w_high, money=True)}
  52W Low:            {fmt(fund.price_52w_low, money=True)}
  % from 52W High:    {fmt(fund.pct_from_52w_high)}%
""".strip()


def _format_web_news(news_text: str) -> str:
    """Render recent web search results for LLM context."""
    return f"""
LIVE WEB SEARCH (Past 7 Days)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{news_text}
""".strip()


def _format_idea(idea: IdeaSummary) -> str:
    """Render an IdeaSummary as structured text for LLM context."""
    sources_str = "\n".join(
        f"  • {s.name} [{s.credibility.value}] — {s.date} ({s.type.value})"
        for s in idea.sources
    )
    facts_str = "\n".join(f"  • {f}" for f in idea.key_facts)
    risks_str = "\n".join(f"  • {r}" for r in idea.counter_signals)

    return f"""
TRADING IDEA — {idea.ticker} ({idea.company or "Unknown"})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Direction:        {idea.direction.value}
Time Horizon:     {idea.time_horizon.value}
Media Conviction: {idea.conviction_from_sources}/10
Source Quality:   {idea.source_quality_score}/10

Headline: {idea.headline}

Thesis: {idea.thesis_1sentence}

Catalyst: {idea.catalyst or "None specified"}

Key Facts:
{facts_str}

Counter-Signals:
{risks_str}

Sources:
{sources_str}

Tags: {", ".join(idea.tags)}
""".strip()


def _parse_research_response(
    raw_response: str,
    idea: IdeaSummary,
) -> Optional[ResearchReport]:
    """Parse Qwen3 response into a ResearchReport."""
    thinking_trace = extract_thinking_trace(raw_response)
    cleaned = extract_json_from_response(raw_response)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning("stage2.json_parse_error", ticker=idea.ticker, error=str(e))
        return None

    try:
        # Ensure id and ticker are set
        data["id"] = idea.id
        data["ticker"] = idea.ticker
        data["thinking_trace"] = thinking_trace

        # Map recommendation string to enum
        rec_str = data.get("recommendation", "SKIP")
        try:
            data["recommendation"] = Recommendation(rec_str)
        except ValueError:
            data["recommendation"] = Recommendation.SKIP

        return ResearchReport.model_validate(data)

    except Exception as e:
        log.warning("stage2.validation_error", ticker=idea.ticker, error=str(e))
        return None


async def run(
    stage1_output: Optional[Stage1Output] = None,
    run_date: Optional[str] = None,
    score_threshold: float = 6.5,
    num_gpu: Optional[int] = None,
) -> Stage2Output:
    """Execute Stage 2 — returns Stage2Output and saves scored_ideas.json."""
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    start_time = time.time()

    # Load Stage 1 output if not passed directly
    if stage1_output is None:
        raw = load_json("02_ideas.json", run_date)
        stage1_output = Stage1Output.model_validate(raw)

    ideas = stage1_output.ideas
    log.info("stage2.start", ideas=len(ideas), date=run_date)

    if not ideas:
        log.warning("stage2.no_ideas")
        return Stage2Output(
            run_date=run_date,
            scored_ideas=[],
            ideas_processed=0,
            ideas_passing=0,
            processing_duration_seconds=0.0,
        )

    model = os.getenv("STAGE2_MODEL", "deepseek-r1:32b")
    llm = get_client()
    system_prompt = _load_prompt()

    scored_ideas: list[ResearchReport] = []

    for idx, idea in enumerate(ideas):
        log.info("stage2.processing_idea",
                 idx=idx+1, total=len(ideas),
                 ticker=idea.ticker, horizon=idea.time_horizon.value)

        # Fetch fundamentals and web context
        fund = await fetch_fundamentals(idea.ticker) if idea.ticker else None
        web_news = await fetch_recent_news(idea.company, idea.ticker)

        # Build enriched user prompt
        user_prompt = "\n\n".join([
            _format_idea(idea),
            _format_fundamentals(fund),
            _format_web_news(web_news),
            f"\nToday's Date: {run_date}",
            "\nProvide your deep research report as JSON conforming to the ResearchReport schema.",
        ])

        try:
            raw_response, _ = await llm.complete(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.35,     # Slight creativity for nuanced analysis
                max_tokens=2000,
                thinking=True,        # Full chain-of-thought reasoning
                require_json=True,
                num_ctx=3072,         # Larger ctx for idea+fundamentals+news prompt
                num_gpu=num_gpu,
            )

            report = _parse_research_response(raw_response, idea)
            if report:
                overall = report.scores.get("overall", 0)
                log.info("stage2.idea_scored",
                         ticker=idea.ticker,
                         overall=overall,
                         recommendation=report.recommendation.value)
                scored_ideas.append(report)
                
                # Dashboard live update
                dash_state.reports_data.append(report.model_dump(mode="json"))
                dash_state.ideas_scored += 1
            else:
                log.warning("stage2.parse_failed", ticker=idea.ticker)

        except Exception as e:
            log.error("stage2.llm_error", ticker=idea.ticker, error=str(e))
            continue

    passing = [r for r in scored_ideas if r.scores.get("overall", 0) >= score_threshold]
    duration = time.time() - start_time

    output = Stage2Output(
        run_date=run_date,
        scored_ideas=scored_ideas,
        ideas_processed=len(ideas),
        ideas_passing=len(passing),
        processing_duration_seconds=round(duration, 1),
    )

    save_json(output, "03_scored_ideas.json", run_date)
    log.info("stage2.complete",
             processed=len(ideas),
             scored=len(scored_ideas),
             passing=len(passing),
             duration_s=round(duration, 1))

    return output
