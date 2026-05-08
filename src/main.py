"""
Main entry point — daily pipeline orchestrator.

Usage:
    python -m src.main              # Run all 4 stages
    python -m src.main --stage 1   # Stage 1 only (fetch + extract)
    python -m src.main --stage 2   # Stage 2 only (research planning)
    python -m src.main --stage 3   # Stage 3 only (research reasoning)
    python -m src.main --stage 4   # Stage 4 only (portfolio + report)
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

load_dotenv(".env")
load_dotenv("secrets.env")

from src.utils.logger import configure_logging
configure_logging()

import structlog
import warnings
import logging
warnings.filterwarnings("ignore", category=UserWarning, module="google.genai")
warnings.filterwarnings("ignore", message="Async interactions client cannot use aiohttp")

# Silence noisy HTTP logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google.genai").setLevel(logging.WARNING)

log = structlog.get_logger()
from src.data.storage import save_json

from src.llm.client import get_client
from src.stages import stage1_ingest, stage2_plan, stage3_reason, stage4_filter, stage5_portfolio


def _resolve_models() -> None:
    """
    Resolve STAGE_MODEL variables based on LLM_PROVIDER.
    Allows switching between local and API models by changing one setting.
    """
    provider = os.getenv("LLM_PROVIDER", "local").lower()
    prefix = "API_" if provider == "api" else "LOCAL_"
    
    log.debug("pipeline.resolving_models", provider=provider, prefix=prefix)
    
    for i in range(1, 6):
        key = f"STAGE{i}_MODEL"
        # Only overwrite if the prefixed version exists and the base key is empty or not set correctly
        prefixed_val = os.getenv(f"{prefix}{key}")
        if prefixed_val:
            os.environ[key] = prefixed_val
            log.debug("pipeline.model_resolved", stage=i, model=prefixed_val)


async def _check_models(required_models: list[str]) -> bool:
    client = get_client()
    missing = await client.check_required_models(required_models)
    if missing:
        log.error("startup.models_missing", missing=missing)
        return False
    
    provider = os.getenv("LLM_PROVIDER", "local").lower()
    log.info("startup.models_ok", models=required_models, provider=provider)
    return True


def _check_api_keys() -> None:
    if not os.getenv("FINNHUB_API_KEY") or os.getenv("FINNHUB_API_KEY") == "your_finnhub_api_key_here":
        log.warning("startup.finnhub_key_missing")
    if not os.getenv("NEWSAPI_API_KEY") or os.getenv("NEWSAPI_API_KEY") == "your_newsapi_api_key_here":
        log.warning("startup.newsapi_key_missing")
    
    if os.getenv("LLM_PROVIDER") == "api":
        if not os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") == "your_gemini_api_key_here":
            log.error("startup.gemini_key_missing", hint="Set GEMINI_API_KEY in secrets.env")
            sys.exit(1)


def _load_sources_config() -> dict:
    with open("config/sources.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _fetch_live_portfolio() -> dict | None:
    from src.fetchers.wikifolio import fetch_wikifolio_portfolio, holdings_to_portfolio_yaml_format
    wikifolio_name = os.getenv("WIKIFOLIO_NAME", "").strip()
    if not wikifolio_name: return None
    log.info("wikifolio.fetching", name=wikifolio_name)
    result = fetch_wikifolio_portfolio()
    if result.get("error"): return None
    holdings = result["holdings"]
    if not holdings: return None
    formatted = holdings_to_portfolio_yaml_format(holdings)
    return {"holdings": formatted, "performance": result["performance"], "wikifolio_name": wikifolio_name, "source": "wikifolio_live"}


async def run_pipeline(
    stage: int | None,
    from_stage: int | None,
    run_date: str,
    skip_podcasts: bool,
    score_threshold: float,
    send_email: bool,
    resume: bool = False,
    dashboard: bool = False,
    no_model_check: bool = False,
    force: bool = False,
) -> None:
    total_start = time.time()

    def should_run(s_idx: int) -> bool:
        if stage is not None:
            return stage == s_idx
        if from_stage is not None:
            return s_idx >= from_stage
        return True

    # ── Model availability check ──────────────────────────────────────────────
    if not no_model_check:
        required = []
        if should_run(1): required.append(os.getenv("STAGE1_MODEL"))
        if should_run(2): required.append(os.getenv("STAGE2_MODEL"))
        if should_run(3): required.append(os.getenv("STAGE3_MODEL"))
        if should_run(5): required.append(os.getenv("STAGE5_MODEL"))
        
        required = list(set([m for m in required if m]))
        if required:
            if not await _check_models(required): sys.exit(1)

    if dashboard:
        from src.utils.dashboard import start_dashboard
        await start_dashboard(8080)

    from src.utils.dashboard import state as dash_state
    dash_state.stage = "Initializing"
    from src.data.models import Stage1Output, Stage2Output, Stage3Output
    log.info("pipeline.start", date=run_date, stage=stage, from_stage=from_stage, force=force)

    sources_config = _load_sources_config()
    # Fetch live portfolio once if needed for Stage 1 (research) or Stage 4 (allocation)
    live_portfolio = _fetch_live_portfolio() if (should_run(1) or should_run(4)) else None

    stage1_out = None
    stage2_out = None
    stage3_out = None
    stage4_out = None

    # ── Stage 1: Ingest & Extract ──
    if should_run(1):
        dash_state.stage = "Stage 1: Media Ingest"
        s1_file = Path(f"output/{run_date}/02_ideas.json")
        if not force and resume and s1_file.exists():
            from src.data.storage import load_json
            from src.data.models import Stage1Output
            stage1_out = Stage1Output.model_validate(load_json("02_ideas.json", run_date))
            if stage1_out.ideas:
                log.info("stage1.resume", ideas=len(stage1_out.ideas))
                # Populate dashboard with historical data
                dash_state.ideas_data = [i.model_dump(mode="json") for i in stage1_out.ideas]
                dash_state.ideas_extracted = len(stage1_out.ideas)
                dash_state.articles_fetched = stage1_out.total_articles_processed
                dash_state.podcasts_transcribed = getattr(stage1_out, 'total_podcasts_processed', 0)
            else:
                log.info("stage1.resume_empty_retry")
                s1_gpu = os.getenv("STAGE1_NUM_GPU_LAYERS")
                stage1_out = await stage1_ingest.run(sources_config, run_date, skip_podcasts, force, int(s1_gpu) if s1_gpu else None, live_portfolio)
        else:
            s1_gpu = os.getenv("STAGE1_NUM_GPU_LAYERS")
            stage1_out = await stage1_ingest.run(sources_config, run_date, skip_podcasts, force, int(s1_gpu) if s1_gpu else None, live_portfolio)
    else:
        # Load historical Stage 1 data for dashboard visibility
        s1_file = Path(f"output/{run_date}/02_ideas.json")
        if s1_file.exists():
            from src.data.storage import load_json
            from src.data.models import Stage1Output
            stage1_out = Stage1Output.model_validate(load_json("02_ideas.json", run_date))
            dash_state.ideas_data = [i.model_dump(mode="json") for i in stage1_out.ideas]
            dash_state.ideas_extracted = len(stage1_out.ideas)
            dash_state.articles_fetched = stage1_out.total_articles_processed
            dash_state.podcasts_transcribed = getattr(stage1_out, 'total_podcasts_processed', 0)

    # ── Stage 2: Planning (Librarian) ──
    if should_run(2):
        if stage1_out and stage1_out.ideas:
            s2_file = Path(f"output/{run_date}/03_research_plans.json")
            if not force and resume and s2_file.exists():
                from src.data.storage import load_json
                from src.data.models import Stage2Output
                stage2_out = Stage2Output.model_validate(load_json("03_research_plans.json", run_date))
                # Populate dashboard
                dash_state.plans_data = [p.model_dump(mode="json") for p in stage2_out.plans]
            else:
                s2_gpu = os.getenv("STAGE2_NUM_GPU_LAYERS")
                stage2_out = await stage2_plan.run(stage1_out, run_date, int(s2_gpu) if s2_gpu else None)
        else:
            log.info("stage2.skip_no_ideas")
    else:
        # Load historical Stage 2 data
        s2_file = Path(f"output/{run_date}/03_research_plans.json")
        if s2_file.exists():
            from src.data.storage import load_json
            from src.data.models import Stage2Output
            stage2_out = Stage2Output.model_validate(load_json("03_research_plans.json", run_date))
            dash_state.plans_data = [p.model_dump(mode="json") for p in stage2_out.plans]

    # ── Stage 3: Reasoning (Analyst) ──
    if should_run(3):
        if stage2_out and stage2_out.plans:
            s3_file = Path(f"output/{run_date}/04_scored_ideas.json")
            if not force and resume and s3_file.exists():
                from src.data.storage import load_json
                from src.data.models import Stage3Output
                stage3_out = Stage3Output.model_validate(load_json("04_scored_ideas.json", run_date))
                # Populate dashboard
                dash_state.reports_data = [r.model_dump(mode="json") for r in stage3_out.scored_ideas]
                dash_state.ideas_scored = len(stage3_out.scored_ideas)
            else:
                s3_gpu = os.getenv("STAGE3_NUM_GPU_LAYERS")
                stage3_out = await stage3_reason.run(stage2_out, run_date, score_threshold, int(s3_gpu) if s3_gpu else None)
        else:
            log.info("stage3.skip_no_plans")
            # If we don't have Stage 3 output, we might still want to run Stage 4 with an empty scored list
            # but only if Stage 4 is supposed to run.
            stage3_out = Stage3Output(run_date=run_date, scored_ideas=[], ideas_processed=0, ideas_passing=0, processing_duration_seconds=0.0)
    else:
        # Load historical Stage 3 data
        s3_file = Path(f"output/{run_date}/04_scored_ideas.json")
        if s3_file.exists():
            from src.data.storage import load_json
            from src.data.models import Stage3Output
            stage3_out = Stage3Output.model_validate(load_json("04_scored_ideas.json", run_date))
            dash_state.reports_data = [r.model_dump(mode="json") for r in stage3_out.scored_ideas]
            dash_state.ideas_scored = len(stage3_out.scored_ideas)

    # ── Stage 4: Filter ──
    if should_run(4):
        dash_state.stage = "Stage 4: Filter"
        s4_file = Path(f"output/{run_date}/05_filtered_ideas.json")
        if not force and resume and s4_file.exists():
            from src.data.storage import load_json
            from src.data.models import Stage3Output
            stage3_out = Stage3Output.model_validate(load_json("05_filtered_ideas.json", run_date))
        else:
            # Ensure stage3_out exists, load historical if missing
            if stage3_out is None:
                s3_file = Path(f"output/{run_date}/04_scored_ideas.json")
                if s3_file.exists():
                    from src.data.storage import load_json
                    from src.data.models import Stage3Output
                    stage3_out = Stage3Output.model_validate(load_json("04_scored_ideas.json", run_date))
            if stage3_out is None:
                raise RuntimeError("Stage 4 requires Stage 3 output. Ensure Stage 3 has been run or data available.")

    # ── Stage 5: Portfolio ──
    if should_run(5):
        dash_state.stage = "Stage 5: Portfolio Actions"
        s5_gpu = os.getenv("STAGE5_NUM_GPU_LAYERS")
        stage5_out = await stage5_portfolio.run(stage3_out, run_date, score_threshold, live_portfolio, int(s5_gpu) if s5_gpu else None)
        if stage5_out:
            dash_state.actions_data = [r.model_dump(mode="json") for r in stage5_out.recommendations]
    else:
        # Load historical Stage 5 data
        s5_file = Path(f"output/{run_date}/06_portfolio_update.json")
        if s5_file.exists():
            from src.data.storage import load_json
            from src.data.models import Stage5Output

            stage5_out = Stage5Output.model_validate(load_json("06_portfolio_update.json", run_date))
            dash_state.actions_data = [r.model_dump(mode="json") for r in stage5_out.recommendations]

    # Email
    if send_email and stage5_out:
        report_path = Path(f"output/{run_date}/daily_report.md")
        if report_path.exists():
            from src.utils.email_notifier import send_daily_report
            send_daily_report(report_path.read_text(encoding="utf-8"), run_date, stage5_out)


    total_min = (time.time() - total_start) / 60
    log.info("pipeline.complete", total_minutes=round(total_min, 1))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GeoPoTech Autonomous Trading Pipeline — Professional CLI Interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Hardware Priority:
  --force overrides --resume. Use --force if you want to re-run a stage even if output exists.
  GPU layers are pulled from .env (STAGE[1-5]_NUM_GPU_LAYERS).

Example Commands:
  Full Run:        python -m src.main --dashboard --resume
  Specific Stage:  python -m src.main --stage 3 --date ${EXAMPLE_DATE}
  Reset Today:     python -m src.main --force
        """
    )
    parser.add_argument(
        "--stage", type=int, choices=[1, 2, 3, 4, 5], default=None,
        help="Run ONLY this specific stage (1: Extract, 2: Plan, 3: Reason, 4: Filter, 5: Portfolio)."
    )

    parser.add_argument(
        "--from-stage", type=int, choices=[1, 2, 3, 4, 5], default=None,
        help="Start from this stage and run everything through Stage 5."
    )

    parser.add_argument(
        "--date", type=str, default=None,
        help="Override run date (YYYY-MM-DD). Default: Today. Used for backfilling or re-running reports."
    )
    parser.add_argument(
        "--skip-podcasts", action="store_true",
        help="Bypass podcast downloading and transcription. Significantly faster if only news is needed."
    )
    parser.add_argument(
        "--portfolio-name", type=str, default=None,
        help="Override portfolio name from config or env var."
    )
    parser.add_argument(
        "--score-threshold", type=float, default=6.5,
        help="Minimum overall score (1-10) for an idea to pass Stage 3 and be considered for the portfolio."
    )
    parser.add_argument(
        "--no-model-check", action="store_true",
        help="Skip the Ollama model availability check at startup. Use if models are managed externally."
    )
    parser.add_argument(
        "--no-email", action="store_true",
        help="Disable sending the daily summary email after Stage 4 completion."
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume pipeline using existing JSON files in the output directory. Skips stages already finished."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="CRITICAL: Overwrite existing outputs and ignore --resume. Re-fetches news and re-runs all LLM logic."
    )
    parser.add_argument(
        "--dashboard", action="store_true",
        help="Launch the real-time web dashboard (localhost:8080) to monitor AI thinking and progress."
    )
    args = parser.parse_args()
    if args.portfolio_name:
        os.environ["WIKIFOLIO_NAME"] = args.portfolio_name
    else:
        # Keep existing env value from .env or .secrets
        os.environ.setdefault("WIKIFOLIO_NAME", os.getenv("WIKIFOLIO_NAME", "GeoPoTech Capital"))

    run_date = args.date or datetime.now().strftime("%Y-%m-%d")
    _resolve_models()
    _check_api_keys()
    asyncio.run(run_pipeline(
        stage=args.stage, from_stage=args.from_stage, run_date=run_date, skip_podcasts=args.skip_podcasts,
        score_threshold=args.score_threshold, send_email=not args.no_email,
        resume=args.resume, dashboard=args.dashboard, no_model_check=args.no_model_check, force=args.force
    ))

if __name__ == "__main__":
    main()
