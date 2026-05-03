"""
Stage 2 — Research Planning (The Librarian).

For each idea from Stage 1:
  1. Build context (idea + fundamentals)
  2. Ask Librarian LLM to identify data gaps
  3. Generate 3-5 surgical search queries
  4. EXECUTE those searches immediately
  5. Save results into 03_research_plans.json
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from src.data.models import (
    IdeaSummary, Stage1Output, ResearchPlan, Stage2Output
)
from src.data.storage import save_json, load_json
from src.fetchers.fundamentals import fetch_fundamentals
from src.fetchers.web_search import fetch_recent_news
from src.llm.client import get_client
from src.llm.structured import extract_json_from_response
from src.utils.dashboard import state as dash_state

log = structlog.get_logger()


def _load_plan_prompt() -> str:
    return Path("config/prompts/stage2_plan.md").read_text(encoding="utf-8")


def _format_context(idea: IdeaSummary, fund: Optional[any]) -> str:
    """Minimal context for the Librarian."""
    from src.stages.stage3_reason import _format_idea, _format_fundamentals
    return f"{_format_idea(idea)}\n\n{_format_fundamentals(fund)}"


async def run(
    stage1_output: Optional[Stage1Output] = None,
    run_date: Optional[str] = None,
    num_gpu: Optional[int] = None,
) -> Stage2Output:
    """Execute Stage 2 — returns Stage2Output."""
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    start_time = time.time()

    if stage1_output is None:
        raw = load_json("02_ideas.json", run_date)
        stage1_output = Stage1Output.model_validate(raw)

    ideas = stage1_output.ideas
    log.info("stage2.start_planning", ideas=len(ideas), date=run_date)

    # PORTFOLIO MONITORING: Load current holdings to ensure they are also researched
    from src.fetchers.wikifolio import fetch_wikifolio_portfolio, holdings_to_portfolio_yaml_format
    portfolio_data = fetch_wikifolio_portfolio()
    raw_holdings = portfolio_data.get("holdings", [])
    formatted_holdings = holdings_to_portfolio_yaml_format(raw_holdings)
    current_tickers = [h.get("ticker") for h in formatted_holdings if h.get("ticker") and h.get("ticker") != "CASH"]
    
    # Create "Synthetic Ideas" for current holdings so they get researched too
    for ticker in current_tickers:
        if not any(i.ticker == ticker for i in ideas):
            ideas.append(IdeaSummary(
                id=f"monitor-{ticker}",
                ticker=ticker,
                headline=f"Portfolio Monitor: {ticker}",
                thesis_1sentence=f"Active monitoring of current portfolio holding {ticker}.",
                direction="WATCH",
                time_horizon="LONG_TERM",
                conviction_from_sources=5,
                source_quality_score=10,
                sources=[]
            ))
            log.info("stage2.portfolio_monitor.added", ticker=ticker)

    if not ideas:
        return Stage2Output(run_date=run_date, plans=[], ideas_processed=0, processing_duration_seconds=0.0)

    model = os.getenv("STAGE2_MODEL", "gemini-3.1-flash-lite-preview")
    llm = get_client()
    system_prompt = _load_plan_prompt()

    plans: list[ResearchPlan] = []

    dash_state.total_items = len(ideas)
    for idx, idea in enumerate(ideas):
        # Skip low conviction or low quality ideas
        if idea.conviction_from_sources < 8 or idea.source_quality_score < 7:
            log.info("stage2.filter_low_confidence", ticker=idea.ticker, conviction=idea.conviction_from_sources, quality=idea.source_quality_score)
            continue
        dash_state.current_item_index = idx + 1
        log.info("stage2.planning_idea", idx=idx+1, total=len(ideas), ticker=idea.ticker)
        
        fund = await fetch_fundamentals(idea.ticker) if idea.ticker else None
        context = _format_context(idea, fund)
        dash_state.llm_prompt = context

        try:
            # 1. Determine if we use Gemini Research or standard planning
            is_gemini = os.getenv("LLM_PROVIDER") == "api"
            active_prompt = context
            if is_gemini:
                active_prompt += "\n\nUse your built-in Google Search tools to perform this research right now. Provide a detailed summary of your findings including specific data points and news."
            else:
                active_prompt += "\n\nCRITICAL: You MUST return ONLY a valid JSON object. Do NOT include any conversational preamble, markdown wrappers, or explanations."

            raw_response, trace, _ = await llm.complete(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": active_prompt}
                ],
                temperature=0.1,
                require_json=not is_gemini, 
                num_gpu=num_gpu,
                use_tools=True
            )

            results_dossier = []
            queries = []
            thought = ""

            if is_gemini:
                # 2. For Gemini, the research results are already in the response
                queries = ["Native Gemini Research"]
                results_dossier = [{
                    "query": "Grounding/Agent Research",
                    "content": raw_response
                }]
                thought = "Performed native research via Google tools."
            else:
                # Standard path for local models: Parse queries and execute manually
                cleaned = extract_json_from_response(raw_response)
                if cleaned:
                    import json
                    data = json.loads(cleaned)
                    queries = data.get("queries", [])
                    thought = data.get("thought", "")
                    
                    log.info("stage2.executing_research", ticker=idea.ticker, queries=len(queries))
                    search_tasks = [fetch_recent_news(custom_query=q) for q in queries]
                    raw_results = await asyncio.gather(*search_tasks)
                    
                    for q, res in zip(queries, raw_results):
                        results_dossier.append({"query": q, "content": res})
                else:
                    log.warning("stage2.plan_parse_failed", ticker=idea.ticker)
                    continue

            # 3. Save the Research Plan (from either path)
            plan = ResearchPlan(
                id=idea.id,
                ticker=idea.ticker or "UNKNOWN",
                thought=thought,
                queries=queries,
                search_results=results_dossier,
                input_prompt=context,
                thinking_trace=trace
            )
            plans.append(plan)
            dash_state.plans_data.append(plan.model_dump(mode="json"))

            # Incremental Save
            incremental_output = Stage2Output(
                run_date=run_date,
                plans=plans,
                ideas_processed=len(ideas),
                processing_duration_seconds=round(time.time() - start_time, 1)
            )
            save_json(incremental_output, "03_research_plans.json", run_date)

        except Exception as e:
            log.error("stage2.plan_error", ticker=idea.ticker, error=str(e))

    duration = time.time() - start_time
    output = Stage2Output(
        run_date=run_date,
        plans=plans,
        ideas_processed=len(ideas),
        processing_duration_seconds=round(duration, 1)
    )

    save_json(output, "03_research_plans.json", run_date)
    log.info("stage2.planning_complete", plans=len(plans), duration_s=round(duration, 1))
    return output
