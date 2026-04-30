"""
Stage 3 — Portfolio Fitting + Report Generation.

Takes all scored ideas and current portfolio state, sends to Qwen3 32B
(same model, already warm in Ollama) for portfolio synthesis.
Generates portfolio_update.json and daily_report.md.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
import structlog

from src.data.models import ResearchReport, Stage1Output, Stage2Output, Stage3Output, Recommendation
from src.data.storage import save_json, save_markdown, load_json
from src.llm.client import get_client
from src.llm.structured import extract_thinking_trace, extract_json_from_response
from src.utils.dashboard import state as dash_state

log = structlog.get_logger()


def _load_prompt() -> str:
    return Path("config/prompts/stage3_portfolio.md").read_text(encoding="utf-8")


def _load_portfolio() -> dict:
    """Load portfolio.yaml."""
    with open("config/portfolio.yaml", "r") as f:
        return yaml.safe_load(f)


def _load_session_memory() -> str:
    """Load the persistent session memory file."""
    path = Path("config/session_memory.md")
    if not path.exists():
        return "No previous session memory available. This is the first run or memory has been cleared."
    return path.read_text(encoding="utf-8")


def _update_session_memory(new_summary: str, run_date: str):
    """Append the latest summary to the session memory."""
    path = Path("config/session_memory.md")
    # We keep the last 5 sessions to avoid prompt bloat while maintaining context
    entry = f"\n### Session: {run_date}\n{new_summary}\n---\n"
    
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    
    # Split by the separator and keep the most recent 5
    sessions = [s for s in existing.split("---") if s.strip()]
    sessions.append(entry)
    
    updated_content = "---".join(sessions[-5:])
    path.write_text(updated_content, encoding="utf-8")


def _format_portfolio(portfolio: dict, live_portfolio: dict | None = None) -> str:
    """Render current portfolio as a text block for LLM context."""
    mode = portfolio.get("mode", "paper")
    constraints = portfolio.get("constraints", {})
    style = portfolio.get("style", {})

    # Use live Wikifolio data if available, else fall back to portfolio.yaml
    if live_portfolio and live_portfolio.get("holdings"):
        holdings = live_portfolio["holdings"]
        source_label = f"LIVE (Wikifolio: {live_portfolio.get('wikifolio_name', '?')})"
        perf = live_portfolio.get("performance", {})
        perf_str = ""
        if perf:
            perf_str = (
                f"\n\nWikifolio Performance:\n"
                f"  Portfolio value:    {perf.get('value', 'N/A')}\n"
                f"  YTD performance:    {perf.get('performance_ytd', 'N/A')}\n"
                f"  1Y performance:     {perf.get('performance_1y', 'N/A')}\n"
                f"  Max drawdown ever:  {perf.get('max_drawdown', 'N/A')}"
            )
    else:
        holdings = portfolio.get("holdings", [])
        source_label = mode.upper()
        perf_str = ""

    holdings_str = "\n".join(
        f"  • {h.get('ticker', h.get('isin', '?'))}: {h.get('allocation_pct', 0):.1f}%"
        + (f" ({h.get('name', '')})" if h.get('name') else "")
        + (f" @ buy {h.get('buy_price', '')}" if h.get('buy_price') else "")
        for h in holdings
    ) or "  (No holdings — 100% cash)"

    return f"""
CURRENT PORTFOLIO ({source_label})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Holdings:
{holdings_str}{perf_str}

Constraints:
  Max single position:  {constraints.get('max_single_position_pct', 15)}%
  Max sector:           {constraints.get('max_sector_pct', 40)}%
  Min cash reserve:     {constraints.get('min_cash_reserve_pct', 5)}%
  Max positions:        {constraints.get('max_positions', 20)}
  Min idea score:       {constraints.get('min_idea_score', 6.5)}
  Max daily deploy:     {constraints.get('max_daily_deploy_pct', 20)}%

Style:
  Primary horizon:      {style.get('primary_horizon', 'MEDIUM_TERM')}
  Allow short:          {style.get('allow_short', False)}
  Allow derivatives:    {style.get('allow_derivatives', False)}
  Preferred markets:    {', '.join(style.get('preferred_markets', ['US']))}
""".strip()


def _format_scored_ideas(ideas: list[ResearchReport], threshold: float) -> str:
    """Format passing scored ideas for Stage 3 context."""
    passing = [r for r in ideas if r.scores.get("overall", 0) >= threshold]
    passing.sort(key=lambda r: r.scores.get("overall", 0), reverse=True)

    if not passing:
        return "No ideas passed the minimum score threshold today."

    lines = [f"SCORED IDEAS PASSING THRESHOLD ({threshold}+) — {len(passing)} ideas\n"]
    for i, r in enumerate(passing):
        research = r.research or {}
        lines.append(f"{'━'*40}")
        lines.append(f"#{i+1}. {r.ticker} — {r.recommendation.value}")
        lines.append(f"   Overall Score: {r.scores.get('overall', 0):.1f}/10")
        lines.append(f"   Breakdown: conviction={r.scores.get('conviction', 0):.1f}, "
                     f"risk_reward={r.scores.get('risk_reward', 0):.1f}, "
                     f"timeliness={r.scores.get('timeliness', 0):.1f}, "
                     f"fundamentals={r.scores.get('fundamentals', 0):.1f}")
        lines.append(f"   Suggested size: {r.suggested_position_size_pct:.1f}%")
        lines.append(f"   ID: {r.id}")
        if research.get("fundamental_assessment"):
            lines.append(f"   Assessment: {research['fundamental_assessment']}")
        if r.price_target_rationale:
            lines.append(f"   Price target: {r.price_target_rationale}")
        lines.append("")

    return "\n".join(lines)


def _parse_portfolio_response(
    raw_response: str,
    run_date: str,
) -> Optional[Stage3Output]:
    """Parse DeepSeek-R1 Stage 3 response into Stage3Output."""
    thinking_trace = extract_thinking_trace(raw_response)
    cleaned = extract_json_from_response(raw_response)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.error("stage3.json_parse_error", error=str(e))
        return None

    try:
        data["date"] = run_date
        data["thinking_trace"] = thinking_trace

        # Ensure list fields exist
        for key in ("actions", "skipped_ideas", "watchlist_additions"):
            if key not in data:
                data[key] = []

        return Stage3Output.model_validate(data)
    except Exception as e:
        log.error("stage3.validation_error", error=str(e))
        return None


def _generate_markdown_report(
    stage3: Stage3Output,
    stage2: Stage2Output,
    stage1: Stage1Output,
    run_date: str,
) -> str:
    """Generate the human-readable daily Markdown report."""
    lines = [
        f"# 📊 Daily Trading Report — {run_date}",
        "",
        f"> Generated by AI Trading Insider Processor | {datetime.now().strftime('%H:%M')} local time",
        "",
        "---",
        "",
        "## 🎯 Executive Summary",
        "",
        stage3.summary,
        "",
        "---",
        "",
        "## 📋 Portfolio Actions",
        "",
    ]

    if stage3.actions:
        for action in stage3.actions:
            emoji = {
                "BUY": "🟢", "ADD": "🟢",
                "SELL": "🔴", "TRIM": "🔴",
                "HOLD": "⚪", "SKIP": "⚫",
            }.get(action.action.value, "⚪")

            qty_str = f" | Target Qty: {action.target_quantity:,.0f}" if getattr(action, 'target_quantity', None) is not None else ""
            comp_name = getattr(action, 'company_name', '')
            name_str = f" ({comp_name})" if comp_name else ""

            lines.append(f"### {emoji} **{action.action.value}** `{action.ticker}`{name_str}")
            lines.append(f"**Target:** {action.target_allocation_pct:.1f}% ({'+'if action.change_pct >= 0 else ''}{action.change_pct:.1f}%){qty_str}")
            lines.append("")
            
            # Find research and sources for expandable details
            research = next((r for r in stage2.scored_ideas if r.id == action.idea_id), None) if action.idea_id else None
            idea = next((i for i in stage1.ideas if i.id == action.idea_id), None) if action.idea_id else None

            # NESTED STRUCTURE
            lines.append("<details>")
            lines.append("<summary>📝 View Description & Investment Thesis</summary>")
            lines.append("")
            lines.append(f"{action.reasoning}")
            lines.append("")

            if research or idea:
                lines.append("<details>")
                lines.append("<summary>🧠 View Full AI Thought Process & Raw Data</summary>")
                lines.append("")
                
                if research and research.thinking_trace:
                    lines.append("#### AI Internal Reasoning (DeepSeek-R1)")
                    lines.append("```text")
                    lines.append(research.thinking_trace.strip())
                    lines.append("```")
                    lines.append("")

                if research:
                    lines.append("#### Raw Research Data (JSON)")
                    lines.append("```json")
                    json_data = research.model_dump(mode="json")
                    if "thinking_trace" in json_data: del json_data["thinking_trace"]
                    lines.append(json.dumps(json_data, indent=2))
                    lines.append("```")
                    lines.append("")
                    
                if idea and idea.sources:
                    lines.append("#### Media Sources & URLs")
                    for s in idea.sources:
                        url_str = f" — [Read Source]({s.url})" if s.url else ""
                        lines.append(f"- **{s.name}** ({s.date}){url_str}")
                    lines.append("")
                
                lines.append("</details>")
                lines.append("")
            
            lines.append("</details>")
            lines.append("")
            lines.append("---")
    else:
        lines.append("*No actions today — portfolio remains unchanged.*")
        lines.append("")

    # --- DISCARDED / REJECTED IDEAS ---
    # Ideas from Stage 2 that had low scores OR were skipped by Stage 3
    all_scored = stage2.scored_ideas
    acted_on_tickers = [a.ticker for a in stage3.actions]
    discarded = [r for r in all_scored if r.ticker not in acted_on_tickers]
    
    if discarded:
        lines += [
            "## 🗑️ Discarded & Rejected Research",
            "The following ideas were analyzed but rejected for the portfolio.",
            "",
        ]
        for r in discarded:
            lines.append(f"### ⚪ **REJECTED** `{r.ticker}`")
            lines.append(f"**Overall Score:** {r.scores.get('overall', 0):.1f}/10")
            
            # Find the "reason skipped" from Stage 3 if it exists
            skip_reason = next((s.reason_skipped for s in stage3.skipped_ideas if s.ticker == r.ticker), "Did not meet conviction or risk/reward thresholds for active trading.")
            
            lines.append("<details>")
            lines.append("<summary>📝 View Rejection Rationale</summary>")
            lines.append("")
            lines.append(f"**AI Decision:** {skip_reason}")
            lines.append("")
            
            # Nested thought process for rejected ideas too
            idea = next((i for i in stage1.ideas if i.id == r.id), None)
            lines.append("<details>")
            lines.append("<summary>🧠 View Rejected Thought Process & JSON</summary>")
            lines.append("")

            if r.thinking_trace:
                lines.append("#### AI Internal Reasoning")
                lines.append("```text")
                lines.append(r.thinking_trace.strip())
                lines.append("```")
                lines.append("")
            
            lines.append("#### Raw Research Data (JSON)")
            lines.append("```json")
            json_data = r.model_dump(mode="json")
            if "thinking_trace" in json_data: del json_data["thinking_trace"]
            lines.append(json.dumps(json_data, indent=2))
            lines.append("```")
            lines.append("")
            
            if idea and idea.sources:
                lines.append("#### Media Sources")
                for s in idea.sources:
                    url_str = f" — [Link]({s.url})" if s.url else ""
                    lines.append(f"- **{s.name}** ({s.date}){url_str}")
            
            lines.append("</details>")
            lines.append("</details>")
            lines.append("")
            lines.append("---")
        lines.append("")


    # Risk snapshot
    risk = stage3.risk_snapshot
    lines += [
        "---",
        "",
        "## 📊 Portfolio After Actions",
        "",
        f"- **Positions**: {risk.get('total_positions', 0)}",
        f"- **Largest position**: {risk.get('largest_position_pct', 0):.1f}%",
        f"- **Cash reserve**: {risk.get('cash_pct', 0):.1f}%",
        "",
        "**Sector Breakdown:**",
        "",
    ]
    for sector, pct in (risk.get("sector_breakdown") or {}).items():
        lines.append(f"- {sector}: {pct:.1f}%")
    lines.append("")

    # Scored ideas table
    passing = [r for r in stage2.scored_ideas if r.scores.get("overall", 0) >= 6.5]
    passing.sort(key=lambda r: r.scores.get("overall", 0), reverse=True)

    if passing:
        lines += [
            "---",
            "",
            "## 🔬 Today's Researched Ideas",
            "",
            "| Ticker | Score | Recommendation | Conviction | Risk/Reward | Timeliness |",
            "|--------|-------|----------------|------------|-------------|------------|",
        ]
        for r in passing:
            s = r.scores
            lines.append(
                f"| `{r.ticker}` "
                f"| **{s.get('overall', 0):.1f}** "
                f"| {r.recommendation.value} "
                f"| {s.get('conviction', 0):.1f} "
                f"| {s.get('risk_reward', 0):.1f} "
                f"| {s.get('timeliness', 0):.1f} |"
            )
        lines.append("")

    # Watchlist
    if stage3.watchlist_additions:
        lines += [
            "---",
            "",
            "## 👁️ Watchlist Additions",
            "",
        ]
        for w in stage3.watchlist_additions:
            lines.append(f"- **{w.ticker}**: {w.reason}")
        lines.append("")

    # Skipped ideas
    if stage3.skipped_ideas:
        lines += [
            "---",
            "",
            "## ⏭️ Skipped (Passed Score, Not Acted On)",
            "",
        ]
        for s in stage3.skipped_ideas:
            lines.append(f"- **{s.ticker or s.idea_id}**: {s.reason_skipped}")
        lines.append("")

    lines += [
        "---",
        "",
        f"*Pipeline stats: {stage2.ideas_processed} ideas processed, "
        f"{stage2.ideas_passing} passed threshold, "
        f"{len(stage3.actions)} actions generated.*",
    ]

    return "\n".join(lines)


async def run(
    stage2_output: Optional[Stage2Output] = None,
    run_date: Optional[str] = None,
    score_threshold: float = 6.5,
    live_portfolio: Optional[dict] = None,  # Live data from Wikifolio fetcher
    num_gpu: Optional[int] = None,
) -> Stage3Output:
    """Execute Stage 3 — portfolio fitting and report generation."""
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    start_time = time.time()

    # Load Stage 2 output if not passed
    if stage2_output is None:
        raw = load_json("03_scored_ideas.json", run_date)
        stage2_output = Stage2Output.model_validate(raw)

    # Load Stage 1 output (needed for source URLs in the report)
    raw_s1 = load_json("02_ideas.json", run_date)
    stage1_output = Stage1Output.model_validate(raw_s1)

    log.info("stage3.start", ideas=len(stage2_output.scored_ideas), date=run_date)

    portfolio = _load_portfolio()
    session_memory = _load_session_memory()
    model = os.getenv("STAGE3_MODEL", "deepseek-r1:32b")
    llm = get_client()
    system_prompt = _load_prompt()

    user_prompt = "\n\n".join([
        "## PREVIOUS SESSION MEMORY\n" + session_memory,
        _format_portfolio(portfolio, live_portfolio),  # Use live data if available
        _format_scored_ideas(stage2_output.scored_ideas, score_threshold),
        f"Today's Date: {run_date}",
        "Produce the portfolio update JSON following your instructions.",
    ])

    log.info("stage3.llm_call", model=model)
    raw_response, _ = await llm.complete(
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.3,
        max_tokens=2500,
        thinking=True,    # Full reasoning for portfolio decisions
        require_json=True,
        num_ctx=3072,     # Session memory + portfolio + ideas = long prompt
        num_gpu=num_gpu,
    )

    stage3 = _parse_portfolio_response(raw_response, run_date)
    if stage3 is None:
        log.error("stage3.failed_to_parse_response")
        # Return minimal safe output
        stage3 = Stage3Output(
            date=run_date,
            summary="Stage 3 parsing failed — review raw LLM output.",
            actions=[],
            portfolio_after={},
            risk_snapshot={},
            skipped_ideas=[],
            watchlist_additions=[],
        )

    duration = time.time() - start_time
    stage3.processing_duration_seconds = round(duration, 1)

    # Save JSON
    save_json(stage3, "04_portfolio_update.json", run_date)

    # Update persistent session memory
    _update_session_memory(stage3.summary, run_date)

    # Generate and save Markdown report
    report_md = _generate_markdown_report(stage3, stage2_output, stage1_output, run_date)
    save_markdown(report_md, "daily_report.md", run_date)

    log.info("stage3.complete",
             actions=len(stage3.actions),
             watchlist=len(stage3.watchlist_additions),
             duration_s=round(duration, 1))

    # Dashboard live update
    dash_state.actions_data = [a.model_dump(mode="json") for a in stage3.actions]
    dash_state.stage = "Complete!"

    return stage3
