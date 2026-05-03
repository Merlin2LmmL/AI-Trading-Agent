"""
yfinance wrapper — fetches fundamental data for each idea's ticker.
"""
from __future__ import annotations

import structlog
import yfinance as yf

from src.data.models import FundamentalData

log = structlog.get_logger()


async def fetch_fundamentals(ticker: str) -> FundamentalData | None:
    """
    Pull fundamental data for a ticker using yfinance (ASYNC).
    Returns None if the ticker is invalid or data is unavailable.
    """
    if not ticker:
        return None

    try:
        import asyncio
        stock = yf.Ticker(ticker)
        # yfinance.info is a blocking network call
        info = await asyncio.to_thread(lambda: stock.info)

        if not info or info.get("quoteType") is None:
            log.warning("fundamentals.no_data", ticker=ticker)
            return None

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        high_52w = info.get("fiftyTwoWeekHigh")
        pct_from_high = None
        if price and high_52w and high_52w > 0:
            pct_from_high = round((price - high_52w) / high_52w * 100, 2)

        fund = FundamentalData(
            ticker=ticker,
            company_name=info.get("longName") or info.get("shortName"),
            current_price=price,
            market_cap_usd=info.get("marketCap"),
            pe_ratio_ttm=info.get("trailingPE"),
            pe_ratio_forward=info.get("forwardPE"),
            revenue_growth_yoy=info.get("revenueGrowth"),
            gross_margin=info.get("grossMargins"),
            operating_margin=info.get("operatingMargins"),
            ebitda_margin=info.get("ebitdaMargins"),
            net_margin=info.get("profitMargins"),
            debt_to_equity=info.get("debtToEquity"),
            roe=info.get("returnOnEquity"),
            price_52w_high=high_52w,
            price_52w_low=info.get("fiftyTwoWeekLow"),
            pct_from_52w_high=pct_from_high,
            short_float_pct=info.get("shortPercentOfFloat"),
            analyst_target_price=info.get("targetMeanPrice"),
            analyst_recommendation=info.get("recommendationKey"),
            sector=info.get("sector"),
            industry=info.get("industry"),
        )

        log.info("fundamentals.fetched", ticker=ticker,
                 price=price, pe=fund.pe_ratio_ttm)
        return fund

    except Exception as e:
        log.warning("fundamentals.error", ticker=ticker, error=str(e))
        return None


async def fetch_fundamentals_batch(tickers: list[str]) -> dict[str, FundamentalData | None]:
    """Fetch fundamentals for a list of tickers (ASYNC). Returns dict keyed by ticker."""
    results = {}
    for ticker in tickers:
        results[ticker] = await fetch_fundamentals(ticker)
    return results
