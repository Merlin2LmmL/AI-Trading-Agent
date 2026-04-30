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
from typing import Optional

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
    wikifolio_name: Optional[str],
    actions_count: int,
) -> tuple[str, str]:
    """Return (subject, html_body)."""
    subject = f"📊 Daily Trading Report — {run_date}"
    if wikifolio_name:
        subject += f" | {wikifolio_name}"
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


def send_daily_report(
    report_md: str,
    run_date: str,
    wikifolio_name: Optional[str] = None,
    actions_count: int = 0,
) -> bool:
    """
    Send the daily Markdown report as a formatted HTML email.

    Reads SMTP settings and recipient list from environment (loaded from secrets.env).

    Returns True on success, False on failure.
    """
    
    # Generate full HTML for attachment
    _, full_html = _build_email(report_md, run_date, wikifolio_name, actions_count)
    
    # Create shortened version for email body
    short_lines = []
    depth = 0
    for line in report_md.split('\n'):
        if '<details>' in line:
            depth += 1
            continue
        if '</details>' in line:
            depth -= 1
            continue
        if depth == 0:
            short_lines.append(line)
            
    short_lines.append("")
    short_lines.append("> 📎 **Note:** The full research report containing the AI's internal reasoning, source links, and raw data is attached to this email.")
    short_md = '\n'.join(short_lines)
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

    subject, html_body = _build_email(short_md, run_date, wikifolio_name, actions_count)

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
