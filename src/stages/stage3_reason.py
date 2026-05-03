"""
Stage 3 — Analytical Reasoning (The Analyst).

For each research dossier from Stage 2:
  1. Build synthesis context (idea + fundamentals + pre-fetched search results)
  2. Send to Analyst LLM for deep research + scoring
  3. Validate and save results as 04_scored_ideas.json
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from src.data.models import (
    IdeaSummary, Stage1Output, Stage2Output, ResearchPlan,
    ResearchReport, Stage3Output, Recommendation, FundamentalData
)
from src.data.storage import save_json, load_json
from src.fetchers.fundamentals import fetch_fundamentals
from src.llm.client import get_client, OllamaClient
from src.llm.structured import extract_thinking_trace, extract_json_from_response
from src.utils.dashboard import state as dash_state

log = structlog.get_logger()


def _load_reasoning_prompt() -> str:
    return Path("config/prompts/stage3_reason.md").read_text(encoding="utf-8")


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
  Operating Margin:   {fmt(fund.operating_margin, pct=True)}
  EBITDA Margin:      {fmt(fund.ebitda_margin, pct=True)}
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


def _calculate_required_context(prompt: str, max_output: int = 2000) -> int:
    """Estimate the required num_ctx based on prompt length."""
    token_est = len(prompt) // 3
    required = token_est + max_output + 500
    for size in [4096, 8192, 12288, 16384, 24576, 32768, 65536, 128000]:
        if required <= size:
            return size
    return 128000


def _parse_research_response(
    raw_response: str,
    idea_id: str,
    ticker: str,
    thinking_trace: Optional[str] = None,
    input_prompt: Optional[str] = None,
) -> Optional[ResearchReport]:
    """Parse Analyst response into a ResearchReport."""
    from src.llm.structured import extract_json_from_response, _recursive_strip_nulls, unwrap_json
    cleaned = extract_json_from_response(raw_response)
    if not cleaned: return None

    try:
        data = json.loads(cleaned)
        data = unwrap_json(data, root_keys=["research_report", "analysis", "report"])
        data = _recursive_strip_nulls(data)
        data["id"] = idea_id
        data["ticker"] = ticker
        data["thinking_trace"] = thinking_trace
        data["input_prompt"] = input_prompt
        return ResearchReport.model_validate(data)
    except Exception as e:
        log.warning("stage3.validation_error", ticker=ticker, error=str(e))
        return None


async def run(
    stage2_output: Optional[Stage2Output] = None,
    run_date: Optional[str] = None,
    score_threshold: float = 6.5,
    num_gpu: Optional[int] = None,
) -> Stage3Output:
    """Execute Stage 3 — returns Stage3Output."""
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    start_time = time.time()

    if stage2_output is None:
        raw = load_json("03_research_plans.json", run_date)
        stage2_output = Stage2Output.model_validate(raw)

    # We also need Stage 1 for the core idea details
    raw_s1 = load_json("02_ideas.json", run_date)
    stage1_output = Stage1Output.model_validate(raw_s1)
    ideas_map = {i.id: i for i in stage1_output.ideas}

    plans = stage2_output.plans
    log.info("stage3.start_reasoning", dossiers=len(plans), date=run_date)

    if not plans:
        return Stage3Output(run_date=run_date, scored_ideas=[], ideas_processed=0, ideas_passing=0, processing_duration_seconds=0.0)

    model = os.getenv("STAGE3_MODEL", "qwen3:32b")
    llm = get_client()
    system_prompt = _load_reasoning_prompt()

    scored_ideas: list[ResearchReport] = []
    
    # ── 3. Processing Toggle ──────────────────────────────────────────────────
    use_search = os.getenv("STAGE3_USE_SEARCH", "false").lower() == "true"
    
    dash_state.total_items = len(plans)
    processed_count = 0

    for idx, plan in enumerate(plans):
        log.info("stage3.processing_idea", idx=idx+1, total=len(plans), ticker=plan.ticker, use_search=use_search)
        
        idea = ideas_map.get(plan.id)
        if not idea: continue
        
        # 1. Reconstruct Research Results
        results_block = ""
        for res_item in plan.search_results:
            results_block += f"### RESULTS FOR: {res_item.get('query')}\n{res_item.get('content')}\n\n"

        # 2. Build Analyst Context for this specific idea
        fund = await fetch_fundamentals(plan.ticker) if plan.ticker != "UNKNOWN" else None
        idea_context = "\n\n".join([
            f"--- DOSSIER FOR {plan.ticker} ({plan.id}) ---",
            _format_idea(idea),
            _format_fundamentals(fund),
            f"RESEARCH RESULTS:\n{results_block}"
        ])

        full_user_prompt = idea_context + "\n\n" + (
            "INSTRUCTIONS: Analyze the dossier above. "
            "Provide a full 'research_report' object following the schema. "
            "Return the final output as a SINGLE JSON object."
        )
        
        dash_state.llm_prompt = f"Processing Idea {idx+1}: {plan.ticker} (Search: {use_search})"

        try:
            # 3. Final Reasoning synthesis
            raw_response, trace, _ = await llm.complete(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": full_user_prompt}
                ],
                temperature=0.2,
                thinking=True,
                use_tools=use_search,
                require_json=True,
                num_ctx=int(os.getenv("STAGE3_NUM_CTX", "8192")), 
                num_gpu=num_gpu,
                thinking_level="medium",
                thinking_summaries="auto"
            )

            # Parse the response
            from src.llm.structured import extract_json_from_response
            cleaned = extract_json_from_response(raw_response)
            if cleaned:
                try:
                    data = json.loads(cleaned)
                    # Handle cases where model wraps it
                    if isinstance(data, dict):
                        if "report" in data and len(data) == 1:
                            data = data["report"]
                        elif "research_report" in data and len(data) == 1:
                            data = data["research_report"]
                    
                    if isinstance(data, list) and len(data) > 0:
                        data = data[0]
                    
                    try:
                        # Validation
                        report = ResearchReport.model_validate(data)
                        scored_ideas.append(report)
                        dash_state.reports_data.append(report.model_dump(mode="json"))
                        processed_count += 1
                        dash_state.current_item_index = processed_count
                        dash_state.ideas_scored += 1
                    except Exception as ve:
                        log.warning("stage3.item_validation_error", ticker=plan.ticker, error=str(ve))
                except Exception as je:
                    log.error("stage3.json_error", ticker=plan.ticker, error=je)
            else:
                log.warning("stage3.no_json", ticker=plan.ticker)

        except Exception as e:
            log.error("stage3.llm_error", ticker=plan.ticker, error=str(e))

    passing = [r for r in scored_ideas if r.scores.get("overall", 0) >= score_threshold]
    duration = time.time() - start_time

    output = Stage3Output(
        run_date=run_date,
        scored_ideas=scored_ideas,
        ideas_processed=len(plans),
        ideas_passing=len(passing),
        processing_duration_seconds=round(duration, 1),
    )

    save_json(output, "04_scored_ideas.json", run_date)
    log.info("stage3.complete", scored=len(scored_ideas), passing=len(passing), duration_s=round(duration, 1))
    return output
