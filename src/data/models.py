from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enumerations ──────────────────────────────────────────────────────────────

class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    WATCH = "WATCH"


class TimeHorizon(str, Enum):
    INTRADAY = "INTRADAY"
    SHORT_TERM = "SHORT_TERM"
    MEDIUM_TERM = "MEDIUM_TERM"
    LONG_TERM = "LONG_TERM"


class Credibility(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class SourceType(str, Enum):
    NEWS = "news"
    ANALYSIS = "analysis"
    PODCAST = "podcast"
    ANALYST_REPORT = "analyst_report"


class Recommendation(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    WATCH = "WATCH"
    SKIP = "SKIP"
    AVOID = "AVOID"


class ActionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    TRIM = "TRIM"
    ADD = "ADD"
    HOLD = "HOLD"
    SKIP = "SKIP"


# ── Stage 1 Models ────────────────────────────────────────────────────────────

class SourceRef(BaseModel):
    name: str
    credibility: Credibility
    date: str                       # YYYY-MM-DD
    type: SourceType
    url: Optional[str] = None
    original_language: Optional[str] = None


class IdeaSummary(BaseModel):
    """Output of Stage 1 — one extracted trading idea."""
    id: str                         # idea_YYYY-MM-DD_NNN
    ticker: Optional[str] = None    # None = no clear ticker, idea is skipped
    company: Optional[str] = None
    market: str = "US"              # US, DE, EU, CRYPTO, COMMODITY
    direction: Direction
    time_horizon: TimeHorizon
    conviction_from_sources: int = Field(ge=0, le=10)
    headline: str
    thesis_1sentence: str
    key_facts: list[str] = Field(max_length=5)
    counter_signals: list[str]
    catalyst: Optional[str] = None
    source_quality_score: int = Field(ge=0, le=10)
    sources: list[SourceRef]
    tags: list[str] = []


class Stage1Output(BaseModel):
    run_date: str                   # YYYY-MM-DD
    ideas: list[IdeaSummary]
    total_articles_processed: int
    total_podcast_minutes: float
    processing_duration_seconds: float


# ── Stage 2 Models ────────────────────────────────────────────────────────────

class FundamentalData(BaseModel):
    """Stock fundamentals pulled from yfinance."""
    ticker: str
    company_name: Optional[str] = None
    current_price: Optional[float] = None
    market_cap_usd: Optional[float] = None
    pe_ratio_ttm: Optional[float] = None
    pe_ratio_forward: Optional[float] = None
    revenue_growth_yoy: Optional[float] = None   # as decimal e.g. 0.22 = 22%
    gross_margin: Optional[float] = None
    net_margin: Optional[float] = None
    debt_to_equity: Optional[float] = None
    roe: Optional[float] = None
    price_52w_high: Optional[float] = None
    price_52w_low: Optional[float] = None
    pct_from_52w_high: Optional[float] = None
    short_float_pct: Optional[float] = None
    analyst_target_price: Optional[float] = None
    analyst_recommendation: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    data_timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class ResearchReport(BaseModel):
    """Output of Stage 2 for a single idea."""
    id: str
    ticker: Optional[str]

    research: dict                  # fundamental_assessment, bull_case, bear_case, etc.

    scores: dict = Field(
        description="conviction, risk_reward, timeliness, fundamentals, sentiment, overall"
    )

    recommendation: Recommendation
    suggested_position_size_pct: float = 0.0
    stop_loss_level: Optional[float] = None
    price_target_rationale: Optional[str] = None

    # Raw thinking trace from DeepSeek-R1 (preserved for auditability)
    thinking_trace: Optional[str] = None


class Stage2Output(BaseModel):
    run_date: str
    scored_ideas: list[ResearchReport]
    ideas_processed: int
    ideas_passing: int               # score >= threshold
    processing_duration_seconds: float


# ── Stage 3 Models ────────────────────────────────────────────────────────────

class PortfolioAction(BaseModel):
    action: ActionType
    ticker: str
    company_name: str
    target_allocation_pct: float
    change_pct: float
    target_quantity: Optional[float] = None
    idea_id: Optional[str] = None
    reasoning: str


class SkippedIdea(BaseModel):
    idea_id: str
    ticker: Optional[str]
    reason_skipped: str


class WatchlistEntry(BaseModel):
    ticker: str
    reason: str


class Stage3Output(BaseModel):
    date: str
    summary: str
    actions: list[PortfolioAction]
    portfolio_after: dict            # {ticker: {allocation_pct, is_new_position}}
    risk_snapshot: dict              # {total_positions, largest_position_pct, cash_pct, sector_breakdown}
    skipped_ideas: list[SkippedIdea]
    watchlist_additions: list[WatchlistEntry]

    # Raw thinking trace
    thinking_trace: Optional[str] = None
    processing_duration_seconds: float = 0.0


# ── Raw Article Model (pre-LLM) ───────────────────────────────────────────────

class RawArticle(BaseModel):
    """A single piece of fetched content before LLM processing."""
    source_name: str
    source_type: SourceType
    language: str                   # ISO 639-1 e.g. 'en', 'de'
    credibility: Credibility
    title: str
    url: Optional[str] = None
    published: Optional[str] = None  # ISO datetime string
    summary: Optional[str] = None   # RSS description field
    full_text: Optional[str] = None  # Full article or transcript
    fetch_timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
