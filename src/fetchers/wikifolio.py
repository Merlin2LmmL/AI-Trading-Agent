"""
Wikifolio portfolio fetcher — uses the unofficial Wikifolio web API.
Logs in with a session cookie and fetches current holdings.

API reference: https://github.com/henrydatei/wikifolio-api
"""
from __future__ import annotations

import os
import time
from typing import Optional

import requests
import structlog

log = structlog.get_logger()

BASE = "https://www.wikifolio.com"


class WikifolioClient:
    """
    Thin session-based client for the unofficial Wikifolio API.
    Uses cookies for auth — no official API key required.
    """

    def __init__(self, email: str, password: str, wikifolio_name: str) -> None:
        self.email = email
        self.wikifolio_name = wikifolio_name
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; TradingInsiderBot/1.0)",
            "Accept": "application/json",
        })
        self._login(email, password)

    def _login(self, email: str, password: str) -> None:
        """POST to Wikifolio login endpoint and store session cookie."""
        resp = self._session.post(
            f"{BASE}/api/login?country=de&language=de",
            data={"email": email, "password": password, "keepLoggedIn": True},
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Wikifolio login failed — HTTP {resp.status_code}. "
                "Check WIKIFOLIO_EMAIL and WIKIFOLIO_PASSWORD in secrets.env."
            )
        log.info("wikifolio.logged_in", email=email)

    def get_portfolio_holdings(self) -> list[dict]:
        """
        Fetch current portfolio holdings.

        Returns a list of dicts, each representing one position:
          - name: str          Company / security name
          - isin: str          ISIN code
          - weight: float      Portfolio weight in % (0–100)
          - quantity: float    Number of units held
          - value: float       Current position value (EUR)
          - buyPrice: float    Average entry price
          - currentPrice: float Current market price
          - currency: str      Currency code
          - changeToday: float Intraday change %
        """
        url = f"{BASE}/api/wikifolio/{self.wikifolio_name}/portfolio"
        params = {"country": "de", "language": "de"}

        resp = self._session.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Wikifolio portfolio fetch failed — HTTP {resp.status_code}. "
                f"Check WIKIFOLIO_NAME in secrets.env (current: '{self.wikifolio_name}')."
            )

        data = resp.json()

        # The API returns groups[0].items for equities, cash is often separate
        holdings = []
        for group in data.get("groups", []):
            for item in group.get("items", []):
                holdings.append({
                    "name":          item.get("name", "Unknown"),
                    "isin":          item.get("isin", ""),
                    # percentage is already 0–1 (e.g. 0.124 = 12.4%)
                    "weight":        round(item.get("percentage", 0.0) * 100, 2),
                    "quantity":      item.get("quantity", 0.0),
                    # No totalValue field — compute from quantity × mid price
                    "value":         round(item.get("quantity", 0) * item.get("mid", item.get("close", 0)), 2),
                    "buy_price":     item.get("averagePurchasePrice", 0.0),
                    "current_price": item.get("mid", item.get("close", 0.0)),
                    "ask":           item.get("ask", 0.0),
                    "bid":           item.get("bid", 0.0),
                    "currency":      "EUR",   # Wikifolio.de is always EUR
                    "is_leveraged":  item.get("isLeveraged", False),
                })

        # Also pull performance metrics
        time.sleep(0.5)  # Be polite to the API
        log.info("wikifolio.holdings_fetched", count=len(holdings),
                 portfolio=self.wikifolio_name)
        return holdings

    def get_performance(self) -> dict:
        """Fetch key performance indicators for the wikifolio."""
        url = f"{BASE}/api/chart/{self.wikifolio_name}/data"
        params = {"includeportfolio": True, "country": "de", "language": "de"}

        resp = self._session.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            return {}

        data = resp.json()
        return {
            "value": data.get("latestValue"),
            "performance_ytd": data.get("performanceYtd"),
            "performance_1y": data.get("performanceOneYear"),
            "max_drawdown": data.get("maxLossEver"),
        }


# ── Public helpers ────────────────────────────────────────────────────────────

def fetch_wikifolio_portfolio() -> dict:
    """
    Load credentials from secrets.env and return current portfolio state.

    Returns:
        {
            "holdings": [...],     # list of position dicts
            "performance": {...},  # key metrics
            "wikifolio_name": str,
            "error": str | None    # set if fetch failed, holdings will be []
        }
    """
    email    = os.getenv("WIKIFOLIO_EMAIL", "")
    password = os.getenv("WIKIFOLIO_PASSWORD", "")
    name     = os.getenv("WIKIFOLIO_NAME", "").strip()

    if not email or not password:
        log.warning("wikifolio.no_credentials",
                    hint="Set WIKIFOLIO_EMAIL and WIKIFOLIO_PASSWORD in secrets.env")
        return {"holdings": [], "performance": {}, "wikifolio_name": name, "error": "No credentials"}

    if not name:
        log.warning("wikifolio.no_name",
                    hint="Set WIKIFOLIO_NAME in secrets.env (the short name from your wikifolio URL)")
        return {"holdings": [], "performance": {}, "wikifolio_name": "", "error": "WIKIFOLIO_NAME not set"}

    try:
        client = WikifolioClient(email, password, name)
        holdings = client.get_portfolio_holdings()
        performance = client.get_performance()
        return {
            "holdings": holdings,
            "performance": performance,
            "wikifolio_name": name,
            "error": None,
        }
    except Exception as e:
        log.error("wikifolio.fetch_failed", error=str(e))
        return {"holdings": [], "performance": {}, "wikifolio_name": name, "error": str(e)}


def holdings_to_portfolio_yaml_format(holdings: list[dict]) -> list[dict]:
    """
    Convert Wikifolio holdings to the format expected by Stage 3's portfolio context.
    Converts weights to allocation_pct and maps field names.
    """
    result = []
    for h in holdings:
        result.append({
            "ticker":           h.get("isin", "?"),   # Use ISIN as identifier
            "name":             h.get("name", ""),
            "allocation_pct":   h.get("weight", 0.0), # already in % (0–100)
            "quantity":         h.get("quantity", 0),
            "current_price":    h.get("current_price", 0.0),
            "buy_price":        h.get("buy_price", 0.0),
            "currency":         h.get("currency", "EUR"),
        })

    # Add implied cash if weights don't sum to 100
    total_weight = sum(h.get("weight", 0) for h in holdings)
    # Normalize weight (API sometimes returns 0–1 fractions, sometimes 0–100)
    if total_weight <= 1.5:
        total_weight *= 100

    cash_pct = max(0.0, round(100.0 - total_weight, 2))
    if cash_pct > 0:
        result.append({
            "ticker": "CASH",
            "name": "Cash",
            "allocation_pct": cash_pct,
            "currency": "EUR",
        })

    return result
