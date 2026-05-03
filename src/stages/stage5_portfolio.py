"""
Stage 5 — Portfolio Fitting + Report Generation.

Takes all scored ideas from Stage 3 and current portfolio state,
generates final trading decisions and the daily Markdown report.
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

from src.data.models import ResearchReport, Stage1Output, Stage3Output, Stage5Output, Recommendation, ActionType
from src.data.storage import save_json, save_markdown, load_json
from src.llm.client import get_client
from src.llm.structured import extract_thinking_trace, extract_json_from_response
from src.utils.dashboard import state as dash_state

log = structlog.get_logger()


def _load_prompt() -> str:
    return Path("config/prompts/stage5_portfolio.md").read_text(encoding="utf-8")


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


def _update_session_memory(stage5: Stage5Output, stage3: Stage3Output, run_date: str):
    """Append the latest summary to the session memory."""
    path = Path("config/session_memory.md")
    entry = f"\n### Session: {run_date}\n{stage5.summary}\n---\n"
    
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    
    sessions = [s for s in existing.split("---") if s.strip()]
    sessions.append(entry)
    updated_content = "---".join(sessions[-5:])
    path.write_text(updated_content, encoding="utf-8")


def _format_portfolio(portfolio: dict, live_portfolio: dict | None = None) -> str:
    """Render current portfolio as a JSON block for LLM context."""
    constraints = portfolio.get("constraints", {})
    style = portfolio.get("style", {})

    if live_portfolio and live_portfolio.get("holdings"):
        holdings = live_portfolio["holdings"]
        source_label = f"LIVE (Wikifolio: {live_portfolio.get('wikifolio_name', '?')})"
    else:
        holdings = portfolio.get("holdings", [])
        source_label = portfolio.get("mode", "paper").upper()

    ctx = {
        "source": source_label,
        "holdings": [
            {
                "ticker": h.get("ticker", h.get("isin", "?")),
                "allocation_pct": h.get("allocation_pct", 0),
                "name": h.get("name", "")
            }
            for h in holdings
        ],
        "constraints": constraints,
        "investment_style": style
    }
    
    return f"## CURRENT_PORTFOLIO_STATE\n{json.dumps(ctx, indent=2)}"


def _format_scored_ideas(ideas: list[ResearchReport], threshold: float) -> str:
    """Format passing scored ideas and watchlist candidates as JSON for context."""
    passing = []
    for r in ideas:
        if r.scores.get("overall", 0) >= threshold:
            passing.append({
                "id": r.id,
                "ticker": r.ticker,
                "overall_score": r.scores.get("overall", 0),
                "suggested_size_pct": r.suggested_position_size_pct,
                "recommendation": r.recommendation,
                "analysis_summary": r.research.get("fundamental_assessment", "")[:500],
                "geopolitical_context": r.research.get("geopolitical_assessment", "")[:300],
                "target_rationale": r.price_target_rationale
            })

    watchlist = [
        {"id": r.id, "ticker": r.ticker, "score": r.scores.get("overall", 0)}
        for r in ideas if 5.0 <= r.scores.get("overall", 0) < threshold
    ]

    ctx = {
        "threshold": threshold,
        "passing_ideas": passing,
        "watchlist_candidates": watchlist
    }
    
    return f"## RESEARCHED_IDEAS_DATA\n{json.dumps(ctx, indent=2)}"


def _parse_portfolio_response(raw_response: str, run_date: str, input_prompt: Optional[str] = None) -> Optional[Stage5Output]:
    """Parse Stage 5 response into Stage5Output."""
    from src.llm.structured import extract_json_from_response, _recursive_strip_nulls, unwrap_json
    thinking_trace = extract_thinking_trace(raw_response)
    cleaned = extract_json_from_response(raw_response)
    if not cleaned: return None

    try:
        data = json.loads(cleaned)
        data = unwrap_json(data, root_keys=["portfolio_update", "report", "synthesis", "result"])
        data = _recursive_strip_nulls(data)
        if isinstance(data, list): data = {"recommendations": data}
        
        data["date"] = run_date
        data["portfolio_name"] = os.getenv("WIKIFOLIO_NAME", "GeoPoTech Capital")
        data["execution_mode"] = "ADVISORY_ONLY"
        data["disclaimer"] = "Informational only; requires manual review and execution."
        data["thinking_trace"] = thinking_trace
        data["input_prompt"] = input_prompt

        for key in ("recommendations", "skipped_ideas", "watchlist", "geopolitical_themes_today"):
            if key not in data or not isinstance(data[key], list): data[key] = []
        for key in ("portfolio_after", "risk_snapshot"):
            if key not in data or not isinstance(data[key], dict): data[key] = {}
        
        # Inject input_prompt/trace into recommendations too
        for rec in data["recommendations"]:
            rec["input_prompt"] = input_prompt
            rec["thinking_trace"] = thinking_trace

        return Stage5Output.model_validate(data)
    except Exception as e:
        log.error("stage5.validation_error", error=str(e))
        return None


def _generate_markdown_report(stage5: Stage5Output, stage3: Stage3Output, stage1: Stage1Output, run_date: str) -> str:
    """Generate the human-readable daily Markdown report."""
    lines = [
        f"# 📊 Daily Trading Report — {run_date}",
        "",
        f"> Generated by {stage5.portfolio_name} | {stage5.execution_mode}",
        "",
        "---",
        "",
        "## 🎯 Executive Summary",
        "",
        stage5.summary,
        "",
        "---",
        "",
    ]

    if stage5.geopolitical_themes_today:
        lines += ["## 🌍 Global Themes Today", ""]
        for theme in stage5.geopolitical_themes_today: lines.append(f"- {theme}")
        lines += ["", "---", ""]

    if stage5.thinking_trace:
        lines += ["## 🧠 Global Thinking Process", "", "<details><summary>View Global Thinking Process</summary>", "", "```text", stage5.thinking_trace.strip(), "```", "", "</details>", "", "---", ""]

    lines.append("## 📋 Portfolio Recommendations")
    lines.append("")

    if stage5.recommendations:
        for rec in stage5.recommendations:
            emoji = {"ADD": "🟢", "BUY": "🟢", "SELL": "🔴", "REDUCE": "🟠", "HOLD": "⚪", "WATCH": "👀", "AVOID": "🚫"}.get(rec.action.value, "⚪")
            qty_str = f" | Target Qty: {rec.target_quantity:,.0f}" if rec.target_quantity > 0 else ""
            lines.append(f"### {emoji} **{rec.action.value}** `{rec.ticker}` ({rec.company_name or ''})")
            lines.append(f"**Target Allocation:** {rec.target_allocation_pct:.1f}% ({'+' if rec.change_pct >= 0 else ''}{rec.change_pct:.1f}%){qty_str}")
            if rec.geopolitical_angle: lines.append(f"**Geopolitical Angle:** {rec.geopolitical_angle}")
            lines.append("")
            
            research = next((r for r in stage3.scored_ideas if r.id == rec.idea_id), None) if rec.idea_id else None
            idea = next((i for i in stage1.ideas if i.id == rec.idea_id), None) if rec.idea_id else None

            lines.append("<details><summary>📝 View Decision Rationale & Research</summary>")
            lines.append(f"\n#### Investment Reasoning\n{rec.reasoning}\n")
            if research:
                lines.append("<details><summary>🧠 View Full AI Thought Process & Raw Data</summary>")
                if research.thinking_trace: lines.append(f"#### Thinking Process\n```text\n{research.thinking_trace.strip()}\n```\n")
                lines.append("#### Raw Research Data (JSON)\n```json")
                jd = research.model_dump(mode="json")
                if "thinking_trace" in jd: del jd["thinking_trace"]
                lines.append(json.dumps(jd, indent=2))
                lines.append("```\n</details>")
            if idea and idea.sources:
                lines.append("#### Media Sources")
                for s in idea.sources:
                    u = f" — [Read Source]({s.url})" if s.url else ""
                    lines.append(f"- **{s.name}** ({s.date}){u}")
            lines.append("</details>\n---\n")
    else:
        lines.append("*No recommendations today — portfolio remains unchanged.*")

    # Skipped Ideas
    if stage5.skipped_ideas:
        lines += ["", "## 🚫 Skipped Ideas (Overall ≥ 6.5)", ""]
        for skipped in stage5.skipped_ideas:
            lines.append(f"- **{skipped.ticker}**: {skipped.reason_skipped} (ID: {skipped.idea_id})")
        lines += ["", "---", ""]

    # Watchlist
    if stage5.watchlist:
        lines += ["", "## 👀 Watchlist (Triggers)", ""]
        for w in stage5.watchlist:
            lines.append(f"- **{w.ticker}** ({w.company_name or ''}): {w.reason}")
        lines += ["", "---", ""]

    # Risk snapshot
    risk = stage5.risk_snapshot
    lines += ["", "## 📊 Portfolio After Actions", "", f"- **Positions**: {risk.get('total_stock_positions', 0)}", f"- **Largest position**: {risk.get('largest_position_pct', 0):.1f}%", f"- **Cash reserve**: {risk.get('cash_pct', 0):.1f}%", "", "**Sector Breakdown:**", ""]
    for sector, pct in (risk.get("sector_breakdown") or {}).items(): lines.append(f"- {sector}: {pct:.1f}%")

    # Scored ideas table
    passing = [r for r in stage3.scored_ideas if r.scores.get("overall", 0) >= 6.5]
    if passing:
        passing.sort(key=lambda r: r.scores.get("overall", 0), reverse=True)
        lines += ["", "---", "", "## 🔬 Today's Researched Ideas", "", "| Ticker | Score | Recommendation | Conviction | Risk/Reward | Timeliness |", "|--------|-------|----------------|------------|-------------|------------|"]
        for r in passing:
            s = r.scores
            lines.append(f"| `{r.ticker}` | **{s.get('overall', 0):.1f}** | {r.recommendation.value} | {s.get('conviction', 0):.1f} | {s.get('risk_reward', 0):.1f} | {s.get('timeliness', 0):.1f} |")

    lines += ["", f"*Pipeline stats: {stage3.ideas_processed} ideas processed, {stage3.ideas_passing} passed threshold, {len(stage5.recommendations)} recommendations generated.*"]
    return "\n".join(lines)


async def run(
    stage3_output: Optional[Stage3Output] = None,
    run_date: Optional[str] = None,
    score_threshold: float = 6.5,
    live_portfolio: Optional[dict] = None,
    num_gpu: Optional[int] = None,
) -> Stage5Output:
    """Execute Stage 5 — portfolio fitting and report generation."""
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    start_time = time.time()

    if stage3_output is None:
        raw = load_json("05_filtered_ideas.json", run_date)
        stage3_output = Stage3Output.model_validate(raw)

    raw_s1 = load_json("02_ideas.json", run_date)
    stage1_output = Stage1Output.model_validate(raw_s1)

    log.info("stage5.start", ideas=len(stage3_output.scored_ideas), date=run_date)

    portfolio = _load_portfolio()
    session_memory = _load_session_memory()
    model = os.getenv("STAGE5_MODEL", "deepseek-r1:70b")
    llm = get_client()
    system_prompt = _load_prompt()

    user_prompt = "\n\n".join([
        "## PREVIOUS SESSION MEMORY\n" + session_memory,
        _format_portfolio(portfolio, live_portfolio),
        _format_scored_ideas(stage3_output.scored_ideas, score_threshold),
        f"Today's Date: {run_date}",
        "IMPORTANT: Be concise. Synthesize quickly and output the JSON report."
    ])

    dash_state.total_items = 1
    dash_state.current_item_index = 1
    dash_state.llm_prompt = user_prompt

    raw_response, trace, _ = await llm.complete(
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.3,
        max_tokens=16384,
        thinking=True,
        require_json=True,
        num_ctx=int(os.getenv("STAGE5_NUM_CTX", "8192")),
        num_gpu=num_gpu,
        use_tools=False,
        thinking_level="medium",
        thinking_summaries="auto",
    )

    stage5 = _parse_portfolio_response(raw_response, run_date, input_prompt=user_prompt)
    if stage5 is None:
        stage5 = Stage5Output(date=run_date, summary="Stage 5 parsing failed.", recommendations=[], portfolio_after={}, risk_snapshot={}, skipped_ideas=[], watchlist=[])

    stage5.processing_duration_seconds = round(time.time() - start_time, 1)
    save_json(stage5, "06_portfolio_update.json", run_date)
    _update_session_memory(stage5, stage3_output, run_date)
    report_md = _generate_markdown_report(stage5, stage3_output, stage1_output, run_date)
    save_markdown(report_md, "daily_report.md", run_date)

    log.info("stage5.complete", recommendations=len(stage5.recommendations), duration_s=stage5.processing_duration_seconds)
    dash_state.actions_data = [r.model_dump(mode="json") for r in stage5.recommendations]
    dash_state.stage = "Complete!"
    return stage5
