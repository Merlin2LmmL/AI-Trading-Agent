"""
Email notifier — sends the daily report to all recipients in secrets.env.
Uses Python's built-in smtplib with Gmail SMTP (App Password required).
"""
from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path
from typing import Any, Optional

import structlog

log = structlog.get_logger()


def _markdown_to_html(markdown: str) -> str:
    """
    Minimal Markdown → HTML conversion for the email body.
    Handles: headings, bold, code, tables, bullet lists, horizontal rules, and raw HTML/code blocks.
    """
    import re
    lines = markdown.split("\n")
    html_lines = []
    in_table = False
    in_list = False
    in_code_block = False

    for line in lines:
        # Code blocks
        if line.strip().startswith("```"):
            if not in_code_block:
                html_lines.append('<pre style="background:#f4f4f4;padding:10px;border-radius:5px;overflow-x:auto;"><code>')
                in_code_block = True
            else:
                html_lines.append("</code></pre>")
                in_code_block = False
            continue
            
        if in_code_block:
            # Escape HTML characters in code blocks
            safe_line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            html_lines.append(f"{safe_line}")
            continue

        # Horizontal rule
        if re.match(r"^---+$", line.strip()):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("<hr>")
            continue

        # Raw HTML tags (like <details>, <summary>)
        if re.match(r"^\s*</?(details|summary)>", line.strip()):
            html_lines.append(line.strip())
            continue

        # Headings
        h_match = re.match(r"^(#{1,4})\s+(.*)", line)
        if h_match:
            level = len(h_match.group(1))
            text = h_match.group(2)
            text = _inline_format(text)
            html_lines.append(f"<h{level}>{text}</h{level}>")
            continue

        # Table rows
        if line.strip().startswith("|"):
            if not in_table:
                html_lines.append('<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">')
                in_table = True
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            # Skip separator rows (---|---|---)
            if all(re.match(r"^[-:]+$", c) for c in cells if c):
                continue
            is_header = all(c.startswith("**") or c.startswith("`") for c in cells if c) or html_lines[-1] == '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">'
            tag = "th" if is_header else "td"
            row_html = "".join(f"<{tag}>{_inline_format(c)}</{tag}>" for c in cells)
            html_lines.append(f"<tr>{row_html}</tr>")
            continue
        elif in_table:
            html_lines.append("</table>")
            in_table = False

        # Bullet list items
        if re.match(r"^[-*]\s+", line):
            if not in_table and not in_list:
                html_lines.append("<ul>")
                in_list = True
            content = re.sub(r"^[-*]\s+", "", line)
            html_lines.append(f"<li>{_inline_format(content)}</li>")
            continue
        elif in_list and line.strip():
            html_lines.append("</ul>")
            in_list = False

        # Blockquote (used for > summaries)
        if line.startswith("> "):
            html_lines.append(f"<blockquote>{_inline_format(line[2:])}</blockquote>")
            continue

        # Empty line → paragraph break
        if not line.strip():
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("<br>")
            continue

        html_lines.append(f"<p>{_inline_format(line)}</p>")

    if in_table:
        html_lines.append("</table>")
    if in_list:
        html_lines.append("</ul>")
    if in_code_block:
        html_lines.append("</code></pre>")

    return "\n".join(html_lines)


def _inline_format(text: str) -> str:
    """Apply inline markdown: **bold**, `code`, *italic*."""
    import re
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r'<code style="background:#f4f4f4;padding:2px 4px">\1</code>', text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    return text


def _build_email(
    report_md: str,
    run_date: str,
    portfolio_name: Optional[str],
    actions_count: int,
) -> tuple[str, str]:
    """Return (subject, html_body)."""
    subject = f"📊 Daily Trading Report — {run_date}"
    if portfolio_name:
        subject += f" | {portfolio_name}"
    if actions_count > 0:
        subject += f" | {actions_count} action{'s' if actions_count != 1 else ''}"

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          max-width: 800px; margin: 0 auto; padding: 20px; color: #1a1a1a; }}
  h1 {{ color: #1a1a1a; border-bottom: 3px solid #0066cc; padding-bottom: 8px; }}
  h2 {{ color: #333; border-bottom: 1px solid #ddd; padding-bottom: 4px; margin-top: 24px; }}
  h3 {{ color: #444; }}
  table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 0.9em; }}
  th {{ background: #0066cc; color: white; padding: 8px; text-align: left; }}
  td {{ padding: 7px; border-bottom: 1px solid #eee; }}
  tr:nth-child(even) {{ background: #f8f9fa; }}
  code {{ background: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-family: monospace; }}
  blockquote {{ border-left: 4px solid #0066cc; margin: 0; padding: 8px 16px;
                background: #f0f6ff; color: #333; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 16px 0; }}
  .footer {{ margin-top: 32px; padding-top: 12px; border-top: 1px solid #ddd;
             font-size: 0.8em; color: #888; }}
</style>
</head>
<body>
{_markdown_to_html(report_md)}
<div class="footer">
  Generated by AI Trading Insider Processor &bull; {run_date} &bull;
  Powered by Gemma 4 + DeepSeek-R1 (local, free)
</div>
</body>
</html>
"""
    return subject, html_body


def _extract_summary(report_md: str) -> str:
    """Extract the Executive Summary section from the markdown report."""
    import re
    # Match text between "## 🎯 Executive Summary" and the next "---"
    m = re.search(r'##\s+🎯\s*Executive Summary\s*\n\s*\n(.*?)(?=\n\s*---)', report_md, re.DOTALL)
    if m:
        return re.sub(r'\n+', ' ', m.group(1).strip())[:800]
    # Fallback: first paragraph after Executive Summary
    m = re.search(r'Executive Summary\s*\n\s*(.*)', report_md)
    if m:
        return re.sub(r'\n+', ' ', m.group(1).strip())[:800]
    return report_md[:500]


def _build_training_report(report_md: str, run_date: str) -> str:
    """Build a detailed training report by cross-referencing all pipeline output files.

    Loads the full reasoning chain for each accepted idea by linking:
    - Stage 1 ideas (raw media + sources) via plan ID lookup
    - Stage 2 plans (thought + search queries + search results)
    - Stage 3 research (fundamental, geopolitical, technological analysis + scores)
    """
    from pathlib import Path as _P
    import json as _json

    out_dir = _P(f"output/{run_date}")

    # Load outputs
    s1 = _json.loads((_P(f"output/{run_date}/02_ideas.json").read_text()) if out_dir.joinpath(f"02_ideas.json").exists() else "{}")
    s2 = _json.loads((_P(f"output/{run_date}/03_research_plans.json").read_text()) if out_dir.joinpath(f"03_research_plans.json").exists() else "{}")
    s3 = _json.loads((_P(f"output/{run_date}/04_scored_ideas.json").read_text()) if out_dir.joinpath(f"04_scored_ideas.json").exists() else "{}")
    s5 = _json.loads((_P(f"output/{run_date}/06_portfolio_update.json").read_text()) if out_dir.joinpath(f"06_portfolio_update.json").exists() else "{}")

    # Build lookup maps
    # Stage 2 plans keyed by plan.id (links directly to a Stage 3 ResearchReport.id)
    plans_by_id: dict[str, dict] = {p["id"]: p for p in s2.get("plans", [])}

    # Stage 1 ideas keyed by idea.id (links to plan.id)
    ideas_by_id: dict[str, dict] = {}
    for idea in s1.get("ideas", []):
        ideas_by_id[idea.get("id")] = idea
        # Also index by ticker for tickers that appear
        ideas_by_id.setdefault(idea.get("ticker", ""), []).append(idea)

    # Stage 3 scored ideas keyed by research_report.id
    scored_by_id: dict[str, dict] = {r["id"]: r for r in s3.get("scored_ideas", [])}

    lines = []
    lines.append("# Daily Trading Report — " + run_date)
    lines.append("")
    lines.append(f"> Generated by {s5.get('portfolio_name', 'GeoPoTech Capital')} | {s5.get('execution_mode', 'ADVISORY_ONLY')}")
    lines.append("")

    # Executive summary
    lines.append("---")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    summary = _extract_summary(report_md)
    lines.append(summary)
    lines.append("")
    lines.append("---")
    lines.append("")

    # Global thinking
    tt = s5.get("thinking_trace", "")
    if tt:
        lines.append("## Global Thinking Process")
        lines.append("")
        lines.append("<details><summary>View Global Thinking Process</summary>")
        lines.append("")
        lines.append("```text")
        lines.append(tt.strip())
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Pipeline overview
    lines.append("## Pipeline Overview")
    lines.append("")
    lines.append("| Stage | Purpose | Key Metrics |")
    lines.append("|--     |-----    |--         --|")
    lines.append("| 1 | **Ingest & Extraction** | " + str(s1.get("total_articles_processed", 0)) + " articles, " + str(s1.get("total_podcasts_processed", 0)) + " podcasts, " + str(len(s1.get("ideas", []))) + " ideas extracted |")
    lines.append("| 2 | **Research Planning** | " + str(len(s2.get("plans", []))) + " research plans, " + str(sum(len(p.get("queries", [])) for p in s2.get("plans", []))) + " total search queries |")
    lines.append("| 3 | **Researching & Reasoning** | " + str(s3.get("ideas_processed", 0)) + " scored ideas, " + str(s3.get("ideas_passing", 0)) + " passed threshold |")
    lines.append("| 4 | **Filter & Deduplication** | " + str(len(s3.get("scored_ideas", []))) + " ideas after filter |")
    lines.append("| 5 | **Portfolio & Report Generation** | " + str(len(s5.get("recommendations", []))) + " portfolio actions |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Per-idea deep reasoning
    recs = s5.get("recommendations", [])
    if recs:
        lines.append("## Deep Reasoning — Per-Idea Analysis")
        lines.append("")

        for i, rec in enumerate(recs, 1):
            action = rec.get("action", {}).get("value", rec.get("action", "HOLD")) if isinstance(rec.get("action"), dict) else rec.get("action", "HOLD")
            ticker = rec.get("ticker", "?")
            idea_id = rec.get("idea_id", "")
            reasoning = rec.get("reasoning", "")
            geo_angle = rec.get("geopolitical_angle", "")
            target_alloc = rec.get("target_allocation_pct", 0)
            change_pct = rec.get("change_pct", 0)
            target_qty = rec.get("target_quantity", 0)
            company_name = rec.get("company_name", "")

            # Emoji for action
            emoji_map = {"ADD": "🟢", "BUY": "🟢", "SELL": "🔴", "REDUCE": "🟡", "HOLD": "⚪", "WATCH": "👀", "AVOID": "🚫"}
            emoji = emoji_map.get(action, "⚪")

            lines.append(f"### {i}. {emoji} **{action}** `{ticker}` ({company_name or ''})")
            lines.append(f"**Target Allocation:** {target_alloc:.1f}% ({'+' if change_pct >= 0 else ''}{change_pct:.1f}%)  |  **Target Qty:** {target_qty:,}")
            lines.append("")

            # --- Original Idea (Stage 1) ---
            lines.append("#### Original Idea & Media Sources (Stage 1)")
            lines.append("")
            s1_idea = ideas_by_id.get(idea_id)
            if not s1_idea:
                s1_idea = ideas_by_id.get(ticker)
                if isinstance(s1_idea, list):
                    s1_idea = s1_idea[0]

            if s1_idea:
                dir_val = s1_idea.get("direction", "")
                if isinstance(dir_val, dict):
                    dir_val = dir_val.get("value", dir_val)
                hor = s1_idea.get("time_horizon", "")
                if isinstance(hor, dict):
                    hor = hor.get("value", hor)
                lines.append(f"- **Direction:** {dir_val}  •  **Time Horizon:** {hor}")
                lines.append(f"- **Media Conviction:** {s1_idea.get('conviction_from_sources', 'N/A')}/10")
                lines.append(f"- **Headline:** {s1_idea.get('headline', 'N/A')}")
                lines.append("")
                thesis = s1_idea.get('thesis_1sentence', '')
                if thesis:
                    lines.append(f"> **Thesis:** {thesis}")
                    lines.append("")
                if s1_idea.get("key_facts"):
                    lines.append("**Key Facts:**")
                    for f in s1_idea["key_facts"][:5]:
                        lines.append(f"  - {f}")
                    lines.append("")
            else:
                lines.append("*Original idea not found in Stage 1.*")
                lines.append("")

            # --- Sources ---
            if s1_idea and s1_idea.get("sources"):
                lines.append("**Media Sources:**")
                for s in s1_idea["sources"]:
                    src_name = s.get("name", "Unknown")
                    src_type = s.get("type", "unknown")
                    src_url = s.get("url", "")
                    src_date = s.get("date", "N/A")
                    src_cred = s.get("credibility", "N/A")
                    url_str = f" ({src_url})" if src_url and src_url != "N/A" else ""
                    lines.append(f"  - **{src_name}** [{src_type}] {src_date} [{src_cred}]{url_str}")
                lines.append("")

            # --- Research Plan (Stage 2) ---
            plan = plans_by_id.get(idea_id)
            lines.append("#### Research Plan (Stage 2)")
            lines.append("")
            if plan:
                thought = plan.get("thought", "")
                if thought:
                    lines.append("<details><summary>View Planning Thought</summary>")
                    lines.append("")
                    lines.append(thought.strip())
                    lines.append("")
                    lines.append("</details>")
                    lines.append("")

                queries = plan.get("queries", [])
                if queries:
                    lines.append("**Generated Search Queries:**")
                    for qi, q in enumerate(queries, 1):
                        lines.append(f"  {qi}. {q}")
                    lines.append("")

                search_results = plan.get("search_results", [])
                if search_results:
                    lines.append("**Search Results Summary:**")
                    for sr in search_results:
                        q = sr.get("query", "Unknown Query")
                        content = sr.get("content", "")
                        # Show first 200 chars of content
                        lines.append(f"  - Query: `{q}`")
                        lines.append(f"    Result preview: {content[:150]}{'...' if len(content) > 150 else ''}")
                        lines.append("")
            else:
                lines.append("*No research plan found.*")
                lines.append("")

            # --- Research Report (Stage 3) ---
            rr = scored_by_id.get(idea_id)
            lines.append("#### Research Report & Scoring (Stage 3)")
            lines.append("")
            if rr:
                research = rr.get("research", {})
                scores = rr.get("scores", {})
                lines.append("<details><summary>View Full Research Analysis</summary>")
                lines.append("")

                # Fundamental
                fa = research.get("fundamental_assessment", "")
                if fa:
                    lines.append("**⚡ Fundamental Assessment**")
                    lines.append(fa)
                    lines.append("")

                # Geopolitical
                ga = research.get("geopolitical_assessment", "")
                if ga:
                    lines.append("**🌍 Geopolitical Assessment**")
                    lines.append(ga)
                    lines.append("")

                # Technological
                ta = research.get("technological_assessment", "")
                if ta:
                    lines.append("**🔬 Technological Assessment**")
                    lines.append(ta)
                    lines.append("")

                # Bull/Bear
                bc = research.get("bull_case", "")
                if bc:
                    lines.append(f"**🟢 Bull Case:** {bc}")
                    lines.append("")
                bear = research.get("bear_case", "")
                if bear:
                    lines.append(f"**🔴 Bear Case:** {bear}")
                    lines.append("")

                risks = research.get("key_risks", [])
                if risks:
                    lines.append("**Key Risks:**")
                    for r in risks:
                        lines.append(f"  - {r}")
                    lines.append("")

                wm = research.get("what_media_missed", "")
                if wm:
                    lines.append(f"> **💡 What Media Missed:** {wm}")
                    lines.append("")

                # Scores table
                if scores:
                    lines.append("**Scoring Breakdown:**")
                    lines.append("| Criterion | Score |")
                    lines.append("|-----------|-------|")
                    for k, v in scores.items():
                        if k == "overall":
                            lines.append(f"| **Overall** | **{v}** |")
                        else:
                            lines.append(f"| {k} | {v} |")
                    lines.append("")

                # Price target rationale
                ptr = rr.get("price_target_rationale", "")
                if ptr:
                    lines.append(f"**Price Target Rationale:** {ptr}")
                    lines.append("")

                lines.append("</details>")
                lines.append("")
            else:
                lines.append("*No research report found.*")
                lines.append("")

            # --- Recommendation Reasoning (Stage 5) ---
            if reasoning or geo_angle:
                lines.append("#### Final Recommendation Reasoning (Stage 5)")
                lines.append("")
                if geo_angle:
                    lines.append(f"**Geopolitical Angle:** {geo_angle}")
                    lines.append("")
                if reasoning:
                    lines.append(f"**Investment Reasoning:**")
                    lines.append(reasoning)
                    lines.append("")

            lines.append("---")
            lines.append("")

    lines.append("")
    lines.append(f"Pipeline stats: {s3.get('ideas_processed', 0)} ideas processed, {s3.get('ideas_passing', 0)} passed threshold, {len(recs)} recommendations generated.")
    lines.append("")
    lines.append("Generated by AI Trading Insider Processor • " + run_date + " • Powered by Gemma 4 + DeepSeek-R1 (local, free)")

    return "\n".join(lines)


def send_daily_report(
    report_md: str,
    run_date: str,
    stage5_out: Any = None,
) -> bool:
    """
    Send the daily Markdown report as a formatted HTML email.

    Reads SMTP settings and recipient list from environment (loaded from secrets.env).

    Returns True on success, False on failure.
    """

    # Extract portfolio info from Stage 5 output
    portfolio_name = None
    actions_count = 0
    if stage5_out is not None:
        if isinstance(stage5_out, dict):
            portfolio_name = stage5_out.get("portfolio_name")
            actions_count = len(stage5_out.get("recommendations", []))
        else:
            portfolio_name = getattr(stage5_out, "portfolio_name", None)
            actions_count = len(getattr(stage5_out, "recommendations", []))

    # Generate full HTML for attachment
    _, full_html = _build_email(report_md, run_date, portfolio_name, actions_count)

    # Create version for email body
    # Now includes deep reasoning, sources, queries, and search results from all pipeline stages
    short_md = _build_training_report(report_md, run_date)
    # Load recipients
    raw_recipients = os.getenv("NOTIFY_EMAILS", "").strip()
    if not raw_recipients:
        log.warning("email.no_recipients",
                    hint="Set NOTIFY_EMAILS in secrets.env")
        return False

    recipients = [e.strip() for e in raw_recipients.split(",") if e.strip()]
    if not recipients:
        log.warning("email.no_valid_recipients")
        return False

    # Load SMTP settings
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASSWORD", "")
    smtp_from = os.getenv("SMTP_FROM", smtp_user)

    if not smtp_user or not smtp_pass or smtp_pass == "your_gmail_app_password_here":
        log.warning(
            "email.smtp_not_configured",
            hint="Set SMTP_USER and SMTP_PASSWORD (Gmail App Password) in secrets.env",
        )
        return False

    subject, html_body = _build_email(short_md, run_date, portfolio_name, actions_count)

    # Build MIME message (mixed to support attachments)
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = ", ".join(recipients)

    # Email body (alternative for plain/html)
    body_part = MIMEMultipart("alternative")
    
    # Plain-text fallback (strip markdown)
    import re
    plain = re.sub(r"[#*`>|]", "", short_md)
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    body_part.attach(MIMEText(plain, "plain", "utf-8"))
    body_part.attach(MIMEText(html_body, "html", "utf-8"))
    
    msg.attach(body_part)

    # Attach the full HTML report
    attachment = MIMEApplication(full_html.encode('utf-8'), _subtype="html")
    attachment.add_header('Content-Disposition', 'attachment', filename=f"Deep_Research_Report_{run_date}.html")
    msg.attach(attachment)

    # Send
    try:
        log.info("email.sending", recipients=recipients, subject=subject)
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, recipients, msg.as_string())
        log.info("email.sent_ok", count=len(recipients))
        return True
    except smtplib.SMTPAuthenticationError:
        log.error(
            "email.auth_failed",
            hint="Gmail requires an App Password, not your login password. "
                 "Generate one at https://myaccount.google.com/apppasswords",
        )
        return False
    except Exception as e:
        log.error("email.send_failed", error=str(e))
        return False
