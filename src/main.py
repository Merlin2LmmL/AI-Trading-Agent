"""
Main entry point — daily pipeline orchestrator.

Usage:
    python -m src.main              # Run all 3 stages
    python -m src.main --stage 1   # Stage 1 only (fetch + extract)
    python -m src.main --stage 2   # Stage 2 only (research + score)
    python -m src.main --stage 3   # Stage 3 only (portfolio + report)
    python -m src.main --date 2026-04-28  # Reprocess a past date
    python -m src.main --skip-podcasts    # Skip podcast transcription
    python -m src.main --no-email         # Skip email notification
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

# ── Load both config files before any other imports ──────────────────────────
# .env  → API keys (Finnhub, NewsAPI, Ollama settings)
# secrets.env → Wikifolio credentials + SMTP settings
load_dotenv(".env")
load_dotenv("secrets.env")   # Overrides nothing in .env; adds new vars

from src.utils.logger import configure_logging
configure_logging()

import structlog
log = structlog.get_logger()

from src.llm.client import get_client
from src.stages import stage1_ingest, stage2_reason, stage3_portfolio


# ── Startup checks ────────────────────────────────────────────────────────────

async def _check_ollama(required_models: list[str]) -> bool:
    """Verify Ollama is running and required models are available."""
    client = get_client()
    missing = await client.check_required_models(required_models)
    if missing:
        log.error(
            "startup.models_missing",
            missing=missing,
            hint="Run: ollama pull " + " && ollama pull ".join(missing),
        )
        return False
    log.info("startup.models_ok", models=required_models)
    return True


def _check_api_keys() -> None:
    """Warn about missing optional API keys."""
    if not os.getenv("FINNHUB_API_KEY") or os.getenv("FINNHUB_API_KEY") == "your_finnhub_api_key_here":
        log.warning(
            "startup.finnhub_key_missing",
            hint="Register free at https://finnhub.io/register — set FINNHUB_API_KEY in .env",
        )
    if not os.getenv("NEWSAPI_API_KEY") or os.getenv("NEWSAPI_API_KEY") == "your_newsapi_api_key_here":
        log.warning(
            "startup.newsapi_key_missing",
            hint="Register free at https://newsapi.org/register — set NEWSAPI_API_KEY in .env",
        )


def _load_sources_config() -> dict:
    with open("config/sources.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _fetch_live_portfolio() -> dict | None:
    """
    Attempt to fetch the live Wikifolio portfolio.
    Returns None if not configured or fetch fails (pipeline continues with portfolio.yaml).
    """
    from src.fetchers.wikifolio import fetch_wikifolio_portfolio, holdings_to_portfolio_yaml_format

    wikifolio_name = os.getenv("WIKIFOLIO_NAME", "").strip()
    if not wikifolio_name:
        log.info("wikifolio.skipped",
                 reason="WIKIFOLIO_NAME not set in secrets.env — using portfolio.yaml instead")
        return None

    log.info("wikifolio.fetching", name=wikifolio_name)
    result = fetch_wikifolio_portfolio()

    if result.get("error"):
        log.warning("wikifolio.fetch_failed", error=result["error"],
                    fallback="Continuing with portfolio.yaml")
        return None

    holdings = result["holdings"]
    if not holdings:
        log.warning("wikifolio.empty_portfolio", fallback="Using portfolio.yaml")
        return None

    # Convert to the format Stage 3 understands
    formatted = holdings_to_portfolio_yaml_format(holdings)
    log.info("wikifolio.live_portfolio_loaded",
             positions=len([h for h in formatted if h.get("ticker") != "CASH"]),
             performance=result.get("performance", {}))

    return {
        "holdings": formatted,
        "performance": result["performance"],
        "wikifolio_name": wikifolio_name,
        "source": "wikifolio_live",
    }


# ── Main pipeline ─────────────────────────────────────────────────────────────

async def run_pipeline(
    stage: int | None,
    run_date: str,
    skip_podcasts: bool,
    score_threshold: float,
    send_email: bool,
    resume: bool = False,
    dashboard: bool = False,
    no_model_check: bool = False,
    force: bool = False,
) -> None:
    """Execute the pipeline stages in sequence."""
    total_start = time.time()

    # ── Model availability check ──────────────────────────────────────────────
    if not no_model_check:
        required = []
        if stage is None or stage == 1:
            required.append(os.getenv("STAGE1_MODEL", "qwen3:32b"))
        if stage is None or stage in (2, 3):
            # Also check STAGE2_MODEL and STAGE3_MODEL
            required.append(os.getenv("STAGE2_MODEL", "deepseek-r1:70b"))
            required.append(os.getenv("STAGE3_MODEL", "deepseek-r1:70b"))
        
        if required:
            # Filter out duplicates and None
            required = list(set([m for m in required if m]))
            if not await _check_ollama(required):
                sys.exit(1)

    if dashboard:
        from src.utils.dashboard import start_dashboard
        await start_dashboard(8080)

    from src.utils.dashboard import state as dash_state
    dash_state.stage = "Initializing"

    log.info(
        "pipeline.start",
        date=run_date,
        stage=stage or "all",
        skip_podcasts=skip_podcasts,
        force=force,
    )

    sources_config = _load_sources_config()

    # ── Fetch live Wikifolio portfolio (before any stage) ─────────────────────
    live_portfolio = None
    if stage is None or stage == 3:
        live_portfolio = _fetch_live_portfolio()

    stage1_out = None
    stage2_out = None
    stage3_out = None

    # ── Stage 1 ───────────────────────────────────────────────────────────────
    if stage is None or stage == 1:
        dash_state.stage = "Stage 1: Media Ingest & Extraction"
        stage1_output_file = Path(f"output/{run_date}/02_ideas.json")
        
        # If force is True, we never resume Stage 1
        if not force and resume and stage1_output_file.exists():
            log.info("pipeline.stage1_resume_found", file=str(stage1_output_file))
            from src.data.storage import load_json
            from src.data.models import Stage1Output
            raw_s1 = load_json("02_ideas.json", run_date)
            stage1_out = Stage1Output.model_validate(raw_s1)
        else:
            log.info("pipeline.stage1_begin", force=force)
            s1_gpu = os.getenv("STAGE1_NUM_GPU_LAYERS")
            stage1_out = await stage1_ingest.run(
                sources_config=sources_config,
                run_date=run_date,
                skip_podcasts=skip_podcasts,
                force=force,
                num_gpu=int(s1_gpu) if s1_gpu else None,
            )
            log.info(
                "pipeline.stage1_done",
                ideas=len(stage1_out.ideas),
                articles=stage1_out.total_articles_processed,
                duration_s=stage1_out.processing_duration_seconds,
            )
        
        if not stage1_out.ideas:
            log.warning("pipeline.no_ideas_extracted",
                        hint="Check RSS feeds and API keys")
            return

    # ── Stage 2 ───────────────────────────────────────────────────────────────
    if stage is None or stage == 2:
        dash_state.stage = "Stage 2: Deep Research"
        log.info("pipeline.stage2_begin")
        s2_gpu = os.getenv("STAGE2_NUM_GPU_LAYERS")
        stage2_out = await stage2_reason.run(
            stage1_output=stage1_out,
            run_date=run_date,
            score_threshold=score_threshold,
            num_gpu=int(s2_gpu) if s2_gpu else None,
        )
        log.info(
            "pipeline.stage2_done",
            processed=stage2_out.ideas_processed,
            passing=stage2_out.ideas_passing,
            duration_s=stage2_out.processing_duration_seconds,
        )

    # ── Stage 3 ───────────────────────────────────────────────────────────────
    if stage is None or stage == 3:
        dash_state.stage = "Stage 3: Portfolio Actions"
        log.info("pipeline.stage3_begin")
        s3_gpu = os.getenv("STAGE3_NUM_GPU_LAYERS")
        stage3_out = await stage3_portfolio.run(
            stage2_output=stage2_out,
            run_date=run_date,
            score_threshold=score_threshold,
            live_portfolio=live_portfolio,   # Inject Wikifolio data
            num_gpu=int(s3_gpu) if s3_gpu else None,
        )
        log.info(
            "pipeline.stage3_done",
            actions=len(stage3_out.actions),
            watchlist=len(stage3_out.watchlist_additions),
            duration_s=stage3_out.processing_duration_seconds,
        )

    # ── Send email notification ───────────────────────────────────────────────
    if send_email and stage3_out is not None:
        report_path = Path(f"output/{run_date}/daily_report.md")
        if report_path.exists():
            from src.utils.email_notifier import send_daily_report
            report_md = report_path.read_text(encoding="utf-8")
            wf_name = live_portfolio.get("wikifolio_name") if live_portfolio else None
            send_daily_report(
                report_md=report_md,
                run_date=run_date,
                wikifolio_name=wf_name,
                actions_count=len(stage3_out.actions),
            )
        else:
            log.warning("email.no_report_to_send", path=str(report_path))

    # ── Summary ───────────────────────────────────────────────────────────────
    total_elapsed = time.time() - total_start
    total_min = total_elapsed / 60

    log.info(
        "pipeline.complete",
        total_minutes=round(total_min, 1),
        output_dir=f"output/{run_date}/",
    )

    report_path = Path(f"output/{run_date}/daily_report.md")
    if report_path.exists():
        print(f"\n{'='*60}")
        print(f"Daily report ready: {report_path.resolve()}")
        print(f"Total runtime:       {total_min:.1f} minutes")
        print(f"{'='*60}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Trading Insider Processor — daily pipeline"
    )
    parser.add_argument(
        "--stage", type=int, choices=[1, 2, 3], default=None,
        help="Run only a specific stage (default: run all)",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Override run date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--skip-podcasts", action="store_true",
        help="Skip podcast transcription (faster, no whisper.cpp needed)",
    )
    parser.add_argument(
        "--score-threshold", type=float, default=6.5,
        help="Minimum score for an idea to pass Stage 2 (default: 6.5)",
    )
    parser.add_argument(
        "--no-model-check", action="store_true",
        help="Skip Ollama model availability check",
    )
    parser.add_argument(
        "--no-email", action="store_true",
        help="Skip email notification after completion",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip Stage 1 if 02_ideas.json already exists for the run date",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite today's output and ignore seen media (rerun everything)",
    )
    parser.add_argument(
        "--dashboard", action="store_true",
        help="Open a real-time web dashboard in the browser to monitor progress",
    )
    args = parser.parse_args()

    run_date = args.date or datetime.now().strftime("%Y-%m-%d")

    # ── Pre-flight checks ─────────────────────────────────────────────────────
    _check_api_keys()

    # ── Run ───────────────────────────────────────────────────────────────────
    asyncio.run(run_pipeline(
        stage=args.stage,
        run_date=run_date,
        skip_podcasts=args.skip_podcasts,
        score_threshold=args.score_threshold,
        send_email=not args.no_email,
        resume=args.resume,
        dashboard=args.dashboard,
        no_model_check=args.no_model_check,
        force=args.force,
    ))


if __name__ == "__main__":
    main()
