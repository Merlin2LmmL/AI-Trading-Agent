from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, Any
from pydantic import BaseModel, Field, field_validator, model_validator


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
    REGULATORY_FILING = "regulatory_filing"


class Recommendation(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    WATCH = "WATCH"
    SKIP = "SKIP"
    AVOID = "AVOID"


class ActionType(str, Enum):
    ADD = "ADD"
    REDUCE = "REDUCE"
    SELL = "SELL"
    HOLD = "HOLD"
    WATCH = "WATCH"
    AVOID = "AVOID"


# ── Stage 1 Models ────────────────────────────────────────────────────────────

class SourceRef(BaseModel):
    name: str
    url: Optional[str] = None
    credibility: Credibility
    date: str                       # YYYY-MM-DD
    type: SourceType
    headline: str
    original_language: Optional[str] = None


class IdeaSummary(BaseModel):
    """Output of Stage 1 — one extracted trading idea."""
    id: str                         # idea_YYYY-MM-DD_NNN
    ticker: Optional[str] = None    # None = no clear ticker, idea is skipped
    company: Optional[str] = None
    market: str = "US"              # US | KR | DE | JP | TW | HK | CN | EU | OTHER
    direction: Direction
    time_horizon: TimeHorizon
    conviction_from_sources: int = Field(ge=0, le=10)
    headline: str
    thesis_1sentence: str
    key_facts: list[str] = Field(default_factory=list, max_length=10)
    counter_signals: list[str] = Field(default_factory=list)
    catalyst: Optional[str] = None
    source_quality_score: int = Field(default=5, ge=0, le=10)
    sources: list[SourceRef] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    
    # Dashboard Traceability
    input_prompt: Optional[str] = None
    thinking_trace: Optional[str] = None

    @field_validator("key_facts", "counter_signals", "tags", mode="before")
    @classmethod
    def filter_nulls(cls, v: Any) -> list[str]:
        """Remove nulls or non-string garbage often returned by smaller LLMs."""
        if not isinstance(v, list):
            return []
        return [str(item) for item in v if item is not None and str(item).strip()]


class Stage1Output(BaseModel):
    run_date: str                   # YYYY-MM-DD
    ideas: list[IdeaSummary]
    total_articles_processed: int
    total_podcasts_processed: int = 0
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
    operating_margin: Optional[float] = None
    ebitda_margin: Optional[float] = None
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


class ResearchPlan(BaseModel):
    """Output of Stage 2 (Planning) for a single idea."""
    id: str
    ticker: str
    thought: str
    queries: list[str]
    search_results: list[dict] = Field(default_factory=list)
    
    # Dashboard Traceability
    input_prompt: Optional[str] = None
    thinking_trace: Optional[str] = None


class Stage2Output(BaseModel):
    """Output of Stage 2 (Planning) — all research plans."""
    run_date: str
    plans: list[ResearchPlan]
    ideas_processed: int
    processing_duration_seconds: float


class ResearchReport(BaseModel):
    """Output of Stage 3 (Reasoning) for a single idea."""
    id: str = Field(default_factory=lambda: str(__import__('uuid').uuid4()))
    ticker: Optional[str] = None

    research: dict = Field(default_factory=dict)
    scores: dict = Field(default_factory=dict)

    recommendation: Recommendation = Field(default=Recommendation.WATCH)
    instrument_type: str = "STOCK"
    suggested_position_size_pct: float = 0.0
    stop_loss_level: Optional[float] = None
    price_target_rationale: Optional[str] = None

    # Dashboard Traceability
    input_prompt: Optional[str] = None
    thinking_trace: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def handle_missing_sections(cls, data: Any) -> Any:
        """
        DeepSeek-R1 sometimes hallucinates keys or skips 'research'/'scores' 
        wrappers. This validator ensures basic dictionary structures exist.
        """
        if not isinstance(data, dict):
            return data
        
        # 1. Broad Unwrapping: Look for common report wrappers even if not the only key
        report_wrappers = ["research_report", "report", "output", "data", "analysis", "research"]
        for wrapper in report_wrappers:
            if wrapper in data and isinstance(data[wrapper], dict):
                nested = data[wrapper]
                # Pull useful keys from the nested wrapper to the top level if they are missing
                for k in ["scores", "research", "recommendation", "instrument_type", "suggested_position_size_pct", "stop_loss_level", "price_target_rationale"]:
                    if k in nested and (k not in data or not data[k]):
                        data[k] = nested[k]

        # 2. If 'research' is missing but other detailed keys are present at top level
        if "research" not in data or not data["research"]:
            detailed_keys = [
                "fundamental_assessment", "geopolitical_assessment", "technological_assessment", 
                "bull_case", "bear_case", "catalysts", "key_risks", "fundamental_data", "thesis", "analysis",
                "fundamental_analysis", "geopolitical_analysis", "technological_analysis", "regional_comparison"
            ]
            extracted_research = {k: data[k] for k in detailed_keys if k in data}
            if extracted_research:
                data["research"] = extracted_research
        
        # 3. If 'scores' is missing but 'overall' or 'conviction' is at top level
        if "scores" not in data or not data["scores"]:
            score_keys = ["overall", "conviction", "risk_reward", "timeliness", "fundamentals", "fundamental", "sentiment"]
            extracted_scores = {k: data[k] for k in score_keys if k in data}
            if extracted_scores:
                data["scores"] = extracted_scores
        
        # 4. Standardize recommendation (if nested inside research or analysis)
        if "recommendation" not in data or data["recommendation"] == "WATCH":
            # Search in research or analysis blocks
            for root in ["research", "analysis"]:
                if root in data and isinstance(data[root], dict):
                    if "recommendation" in data[root]:
                        rec = data[root]["recommendation"]
                        data["recommendation"] = rec
                        break
        
        # 5. Fallback for overall score calculation if missing
        if "scores" in data and isinstance(data["scores"], dict):
            scores = data["scores"]
            # Handle 'fundamental' vs 'fundamentals'
            if "fundamental" in scores and "fundamentals" not in scores:
                scores["fundamentals"] = scores.pop("fundamental")
                
            if "overall" not in scores:
                # Simple average of available scores as fallback
                val_scores = [v for k, v in scores.items() if isinstance(v, (int, float)) and k != "overall"]
                if val_scores:
                    scores["overall"] = round(sum(val_scores) / len(val_scores), 1)
                else:
                    scores["overall"] = 0.0

        # Default empty dicts if still missing
        if "research" not in data: data["research"] = {}
        if "scores" not in data: data["scores"] = {}
            
        return data



class Stage3Output(BaseModel):
    """Output of Stage 3 (Reasoning) — all scored reports."""
    run_date: str
    scored_ideas: list[ResearchReport]
    ideas_processed: int
    ideas_passing: int               # score >= threshold
    processing_duration_seconds: float


# ── Stage 5 Models ────────────────────────────────────────────────────────────

class PortfolioRecommendation(BaseModel):
    action: ActionType
    ticker: str
    company_name: str
    market: str
    instrument_type: str = "STOCK"
    current_allocation_pct: float = 0.0
    target_allocation_pct: float
    change_pct: float
    target_quantity: int = 0
    idea_id: Optional[str] = None
    geopolitical_angle: Optional[str] = None
    reasoning: str
    
    # Dashboard Traceability
    input_prompt: Optional[str] = None
    thinking_trace: Optional[str] = None


class SkippedIdea(BaseModel):
    idea_id: str
    ticker: Optional[str]
    reason_skipped: str


class WatchlistEntry(BaseModel):
    ticker: str
    company_name: Optional[str] = None
    reason: str
    source_ideas: list[str] = []


class Stage5Output(BaseModel):
    """Output of Stage 5 (Portfolio) — final decisions."""
    date: str
    portfolio_name: str = "GeoPoTech Capital"
    execution_mode: str = "ADVISORY_ONLY"
    disclaimer: str = ""
    summary: str
    recommendations: list[PortfolioRecommendation]
    portfolio_after: dict            # {ticker: {allocation_pct, is_new_position}}
    risk_snapshot: dict              # {total_stock_positions, largest_position_pct, cash_pct, ...}
    skipped_ideas: list[SkippedIdea]
    watchlist: list[WatchlistEntry]
    geopolitical_themes_today: list[str] = []

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
