"""
yfinance data pipeline -- the SINGLE source of truth for all live financial
data in this app. No other module calls yfinance directly.

Responsibilities:
    - Pull raw fields from .info, .cashflow, .balance_sheet, .income_stmt,
      .history() for a given ticker (or a small on-demand list of tickers --
      e.g. sector-benchmark sample peers, competitor discovery matches).
    - Compute the metrics that are essentially one step removed from a raw
      statement line item: FCF (raw + yield), Net Debt/EBITDA, interest
      coverage, earnings stability, price-return/volatility context,
      EPS/revenue growth, debt growth, margins, liquidity ratios,
      cross-comparability gearing ratios, enterprise value, PEG ratio,
      diluted shares outstanding history, ownership %, analyst target price.
    - Delegate the more composite financial FRAMEWORKS -- DuPont
      decomposition, Altman Z-Score, FCF quality-of-earnings, and 1/5/10yr
      forward projections -- to fundamentals.py (pure calc, no network
      dependency), passing in the scalars already extracted here. This
      module still assembles the final complete raw dict per ticker; it
      just doesn't own those specific calculations' logic anymore.
    - Cache each ticker's fetch to its own small JSON file (see
      load_or_fetch_ticker), refreshed at most once per CACHE_TTL_HOURS.

Also fetches the live risk-free rate (^TNX) for value_creation.py's CAPM
calculation (fetch_risk_free_rate), and VIX for the dashboard's header
metric card (fetch_vix) -- both market-context fetches, not per-ticker.

Known field reliability issues (flagged, not silently substituted or dropped):
    - `enterpriseToEbitda`, `enterpriseToRevenue`: frequently None for
      financials/REITs/utilities where EBITDA isn't a standard reported
      metric, or for companies with negative/near-zero EBITDA.
    - `debtToEquity`: occasionally None or reported on an inconsistent
      basis (some tickers include operating leases, some don't).
    - Interest coverage (EBIT / interest expense, computed from
      .income_stmt): "Interest Expense" is inconsistently reported --
      sometimes net of interest income, sometimes gross, sometimes absent
      entirely for companies with minimal debt. This is the single
      flakiest calculated metric in this pipeline; expect it to be flagged
      missing more often than any other field.
    - `.info` overall: this is a scraped/semi-official yfinance endpoint,
      not a guaranteed-stable API. It intermittently omits fields for no
      documented reason and can rate-limit without a clean HTTP error.
      Every .info access below goes through a retrying wrapper for this
      reason.
    - `beta`, `heldPercentInsiders`, `heldPercentInstitutions`,
      `targetMeanPrice`, `pegRatio`, `longTermPotentialGrowthRate`: all
      plausible standard .info field names, but UNVERIFIED against live
      data in this development environment (no network access here). If
      any of these are consistently None/missing in practice, that's worth
      confirming rather than assuming the code path works as intended.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import config
import fundamentals

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("data_pipeline")


# ---------------------------------------------------------------------------
# Retry wrapper for all yfinance calls
# ---------------------------------------------------------------------------
def _yf_retry():
    return retry(
        reraise=True,
        stop=stop_after_attempt(config.YF_MAX_RETRIES),
        wait=wait_exponential(
            multiplier=config.YF_BACKOFF_BASE_SECONDS, min=1, max=30),
        retry=retry_if_exception_type(Exception),
    )


@dataclass
class TickerFetchResult:
    ticker: str
    sector: str | None = None
    raw: dict = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    fetch_error: str | None = None


@_yf_retry()
def _get_ticker_object(ticker: str):
    import yfinance as yf
    return yf.Ticker(ticker)


def _safe_get(info: dict, key: str, missing_list: list[str]):
    """Pull a field from .info, tracking it as missing if absent/None."""
    val = info.get(key)
    if val is None:
        missing_list.append(key)
    return val


def _compute_fcf(cashflow: pd.DataFrame, market_cap: float | None,
                 missing: list[str]) -> dict[str, float | None]:
    """Free Cash Flow: raw dollar figure AND FCF yield (FCF / market cap).
    Returns both -- the raw figure is needed for FCF margin (FCF/Revenue)
    and FCF/Net Income (quality-of-earnings check), while yield is the
    original Value-category metric.
    """
    out = {"fcf_raw": None, "fcf_yield": None}
    if cashflow is None or cashflow.empty:
        missing.extend(["fcf_raw", "fcf_yield"])
        return out

    fcf = None
    for label in ("Free Cash Flow", "FreeCashFlow"):
        if label in cashflow.index:
            val = cashflow.loc[label].iloc[0]
            if pd.notna(val):
                fcf = float(val)
                break
    if fcf is None:
        # Fallback: Operating Cash Flow - Capital Expenditure
        try:
            ocf = cashflow.loc["Operating Cash Flow"].iloc[0]
            capex = cashflow.loc["Capital Expenditure"].iloc[0]
            if pd.notna(ocf) and pd.notna(capex):
                fcf = float(ocf - abs(capex))
        except (KeyError, IndexError):
            pass

    if fcf is None:
        missing.extend(["fcf_raw", "fcf_yield"])
        return out

    out["fcf_raw"] = fcf
    if market_cap:
        out["fcf_yield"] = fcf / market_cap
    else:
        missing.append("fcf_yield")
    return out


def _compute_total_debt(balance_sheet: pd.DataFrame, col_idx: int = 0) -> float | None:
    """GROSS total debt (not net of cash) from a given .balance_sheet
    column -- needed for Total Debt/Total Assets (%) and as the WACC debt
    weight, distinct from net_debt (which subtracts cash, used for
    Net Debt/EBITDA and Net Debt/Market Cap).
    """
    if balance_sheet is None or balance_sheet.empty or col_idx >= balance_sheet.shape[1]:
        return None
    try:
        if "Total Debt" in balance_sheet.index:
            val = balance_sheet.loc["Total Debt"].iloc[col_idx]
            if pd.notna(val):
                return float(val)
        parts = []
        for label in ("Current Debt", "Long Term Debt"):
            if label in balance_sheet.index:
                v = balance_sheet.loc[label].iloc[col_idx]
                if pd.notna(v):
                    parts.append(v)
        return float(sum(parts)) if parts else None
    except (KeyError, IndexError):
        return None


def _compute_cash(balance_sheet: pd.DataFrame, col_idx: int = 0) -> float | None:
    """Cash & equivalents from a given .balance_sheet column -- extracted
    separately (rather than only inline inside _compute_net_debt) because
    Invested Capital (Value Creation / WACC section) needs it directly:
    Invested Capital = Total Debt + Total Equity - Cash.
    """
    if balance_sheet is None or balance_sheet.empty or col_idx >= balance_sheet.shape[1]:
        return None
    for label in ("Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"):
        if label in balance_sheet.index:
            val = balance_sheet.loc[label].iloc[col_idx]
            if pd.notna(val):
                return float(val)
    return None


def _compute_net_debt(balance_sheet: pd.DataFrame, col_idx: int = 0) -> float | None:
    """Net debt = total debt - cash & equivalents, from a given .balance_sheet column."""
    if balance_sheet is None or balance_sheet.empty or col_idx >= balance_sheet.shape[1]:
        return None
    try:
        total_debt = None
        for label in ("Total Debt",):
            if label in balance_sheet.index:
                total_debt = balance_sheet.loc[label].iloc[col_idx]
        if total_debt is None:
            # Fallback: sum short + long term debt
            parts = []
            for label in ("Current Debt", "Long Term Debt"):
                if label in balance_sheet.index:
                    v = balance_sheet.loc[label].iloc[col_idx]
                    if pd.notna(v):
                        parts.append(v)
            total_debt = sum(parts) if parts else None
        cash = None
        for label in ("Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"):
            if label in balance_sheet.index:
                cash = balance_sheet.loc[label].iloc[col_idx]
                break
        if total_debt is None or cash is None or pd.isna(total_debt) or pd.isna(cash):
            return None
        return float(total_debt - cash)
    except (KeyError, IndexError):
        return None


def _compute_ebitda(income_stmt: pd.DataFrame, col_idx: int = 0) -> float | None:
    if income_stmt is None or income_stmt.empty or col_idx >= income_stmt.shape[1]:
        return None
    for label in ("EBITDA", "Normalized EBITDA"):
        if label in income_stmt.index:
            v = income_stmt.loc[label].iloc[col_idx]
            if pd.notna(v):
                return float(v)
    return None


def _compute_ebit(income_stmt: pd.DataFrame, col_idx: int = 0) -> float | None:
    """EBIT from a given .income_stmt column -- used for Altman Z-Score
    (EBIT/Total Assets) and NOPAT in the upcoming Value Creation/WACC
    section. Extracted as its own reusable function since the existing
    interest-coverage calc only used EBIT inline without storing it.
    """
    if income_stmt is None or income_stmt.empty or col_idx >= income_stmt.shape[1]:
        return None
    for label in ("EBIT", "Operating Income"):
        if label in income_stmt.index:
            v = income_stmt.loc[label].iloc[col_idx]
            if pd.notna(v):
                return float(v)
    return None


def _compute_balance_sheet_items(balance_sheet: pd.DataFrame, col_idx: int = 0) -> dict[str, float | None]:
    """Total Assets, Total Equity, Retained Earnings, Total Liabilities from
    a given .balance_sheet column. All free extractions from a frame
    already fetched -- needed for Company Size (3.2), DuPont decomposition
    (3.3), and Altman Z-Score (3.4).
    """
    out = {"total_assets": None, "total_equity": None,
           "retained_earnings": None, "total_liabilities": None}
    if balance_sheet is None or balance_sheet.empty or col_idx >= balance_sheet.shape[1]:
        return out

    if "Total Assets" in balance_sheet.index:
        val = balance_sheet.loc["Total Assets"].iloc[col_idx]
        if pd.notna(val):
            out["total_assets"] = float(val)

    for label in ("Stockholders Equity", "Total Equity Gross Minority Interest"):
        if label in balance_sheet.index:
            val = balance_sheet.loc[label].iloc[col_idx]
            if pd.notna(val):
                out["total_equity"] = float(val)
                break

    if "Retained Earnings" in balance_sheet.index:
        val = balance_sheet.loc["Retained Earnings"].iloc[col_idx]
        if pd.notna(val):
            out["retained_earnings"] = float(val)

    if "Total Liabilities Net Minority Interest" in balance_sheet.index:
        val = balance_sheet.loc["Total Liabilities Net Minority Interest"].iloc[col_idx]
        if pd.notna(val):
            out["total_liabilities"] = float(val)
    elif out["total_assets"] is not None and out["total_equity"] is not None:
        # Fallback: Total Liabilities = Total Assets - Total Equity
        out["total_liabilities"] = out["total_assets"] - out["total_equity"]

    return out


def _compute_interest_expense(income_stmt: pd.DataFrame) -> float | None:
    """Interest expense from a given .income_stmt column -- extracted as
    its own function (rather than only inline inside interest coverage)
    because Cost of Debt (Value Creation / WACC section) needs it directly:
    Cost of Debt ~ Interest Expense / Total Debt.
    """
    if income_stmt is None or income_stmt.empty:
        return None
    for label in ("Interest Expense", "Interest Expense Non Operating"):
        if label in income_stmt.index:
            val = income_stmt.loc[label].iloc[0]
            if pd.notna(val):
                return abs(float(val))
    return None


def _compute_interest_coverage(income_stmt: pd.DataFrame, missing: list[str]) -> float | None:
    """EBIT / interest expense. Flagged as unreliable per module docstring."""
    if income_stmt is None or income_stmt.empty:
        missing.append("interest_coverage")
        return None
    try:
        ebit = None
        for label in ("EBIT", "Operating Income"):
            if label in income_stmt.index:
                ebit = income_stmt.loc[label].iloc[0]
                if pd.notna(ebit):
                    break
        interest_expense = _compute_interest_expense(income_stmt)
        if ebit is None or interest_expense in (None, 0) or pd.isna(ebit):
            missing.append("interest_coverage")
            return None
        return float(ebit) / interest_expense
    except (KeyError, IndexError):
        missing.append("interest_coverage")
        return None


def _compute_earnings_stability(income_stmt: pd.DataFrame, missing: list[str]) -> float | None:
    """Stdev of YoY EPS growth across however many years .income_stmt provides
    (yfinance typically gives ~4 annual columns). Needs at least 3 years of
    EPS to compute at least 2 YoY growth figures and a meaningful stdev."""
    if income_stmt is None or income_stmt.empty:
        missing.append("earnings_stability_stdev")
        return None
    eps_row = None
    for label in ("Diluted EPS", "Basic EPS"):
        if label in income_stmt.index:
            eps_row = income_stmt.loc[label]
            break
    if eps_row is None:
        missing.append("earnings_stability_stdev")
        return None
    eps_series = eps_row.dropna().iloc[::-1]  # oldest -> newest
    if len(eps_series) < 3:
        missing.append("earnings_stability_stdev")
        return None
    growth = eps_series.pct_change().dropna()
    if growth.empty:
        missing.append("earnings_stability_stdev")
        return None
    return float(growth.std())


def _compute_debt_growth_yoy(balance_sheet: pd.DataFrame, missing: list[str]) -> float | None:
    """YoY % change in total/net debt, most recent year vs prior year column."""
    if balance_sheet is None or balance_sheet.empty or balance_sheet.shape[1] < 2:
        missing.append("debt_growth_yoy")
        return None
    debt_now = _compute_net_debt(balance_sheet, col_idx=0)
    debt_prior = _compute_net_debt(balance_sheet, col_idx=1)
    if debt_now is None or debt_prior is None or debt_prior == 0:
        missing.append("debt_growth_yoy")
        return None
    return (debt_now - debt_prior) / abs(debt_prior)


def _compute_revenue_and_net_income(income_stmt: pd.DataFrame, missing: list[str]) -> dict[str, float | None]:
    """Trailing revenue and net income (most recent annual column). These
    are extracted purely for display/percentile purposes (net margin,
    EBITDA margin, deep-dive dollar figures) -- NOT fed into any z-score or
    the composite/category scores, which are unchanged per the build spec.
    """
    out = {"revenue_ttm": None, "net_income_ttm": None}
    if income_stmt is None or income_stmt.empty:
        missing.extend(["revenue_ttm", "net_income_ttm"])
        return out
    if "Total Revenue" in income_stmt.index:
        val = income_stmt.loc["Total Revenue"].iloc[0]
        if pd.notna(val):
            out["revenue_ttm"] = float(val)
    if out["revenue_ttm"] is None:
        missing.append("revenue_ttm")

    for label in ("Net Income", "Net Income Common Stockholders"):
        if label in income_stmt.index:
            val = income_stmt.loc[label].iloc[0]
            if pd.notna(val):
                out["net_income_ttm"] = float(val)
                break
    if out["net_income_ttm"] is None:
        missing.append("net_income_ttm")
    return out


def _compute_liquidity_ratios(balance_sheet: pd.DataFrame, missing: list[str]) -> dict[str, float | None]:
    """Current ratio and quick ratio from the most recent balance sheet
    column. Extracted for display in the company deep-dive (build spec
    Section 3.3) -- a genuine gap in the original build. Computed here
    (universe-wide, for all 150 tickers) rather than on-demand per selected
    ticker, because the balance sheet is already fetched for every ticker
    regardless -- this costs zero additional network calls and lets the
    deep dive show these instantly without a fresh per-click yfinance hit.
    """
    out = {"current_assets": None, "current_liabilities": None,
           "inventory": None, "current_ratio": None, "quick_ratio": None}
    if balance_sheet is None or balance_sheet.empty:
        missing.extend(["current_ratio", "quick_ratio"])
        return out

    for label in ("Current Assets",):
        if label in balance_sheet.index:
            val = balance_sheet.loc[label].iloc[0]
            if pd.notna(val):
                out["current_assets"] = float(val)

    for label in ("Current Liabilities",):
        if label in balance_sheet.index:
            val = balance_sheet.loc[label].iloc[0]
            if pd.notna(val):
                out["current_liabilities"] = float(val)

    for label in ("Inventory",):
        if label in balance_sheet.index:
            val = balance_sheet.loc[label].iloc[0]
            if pd.notna(val):
                out["inventory"] = float(val)

    if out["current_assets"] is not None and out["current_liabilities"] not in (None, 0):
        out["current_ratio"] = out["current_assets"] / \
            out["current_liabilities"]
        # Quick ratio = (current assets - inventory) / current liabilities.
        # Inventory absence is common and valid for many sectors (software,
        # financials, REITs carry no inventory) -- treat missing inventory
        # as zero for the quick ratio rather than flagging the whole ratio
        # missing, since "no inventory" is a legitimate business reality,
        # not a data gap, for those sectors.
        inv = out["inventory"] if out["inventory"] is not None else 0.0
        out["quick_ratio"] = (out["current_assets"] - inv) / \
            out["current_liabilities"]
    else:
        missing.extend(["current_ratio", "quick_ratio"])

    return out


def _compute_historical_trend(balance_sheet: pd.DataFrame, income_stmt: pd.DataFrame,
                              missing: list[str], max_years: int = 5) -> list[dict]:
    """Margins (gross/operating/net/EBITDA) and ROE/ROA for each available
    fiscal year, most recent first. yfinance's .balance_sheet/.income_stmt
    typically provide ~4 annual columns, not always 5 -- FLAG: if fewer
    than `max_years` are available, this returns however many exist rather
    than padding or guessing; the caller/UI should note the actual count
    shown rather than assuming a full 5 years.
    """
    if income_stmt is None or income_stmt.empty:
        missing.append("historical_trend")
        return []

    n_years = min(max_years, income_stmt.shape[1])
    trend = []
    for col_idx in range(n_years):
        year_label = None
        try:
            year_label = str(income_stmt.columns[col_idx].year)
        except (AttributeError, IndexError):
            year_label = f"Year -{col_idx}"

        revenue = None
        if "Total Revenue" in income_stmt.index:
            v = income_stmt.loc["Total Revenue"].iloc[col_idx]
            if pd.notna(v):
                revenue = float(v)

        net_income = None
        for label in ("Net Income", "Net Income Common Stockholders"):
            if label in income_stmt.index:
                v = income_stmt.loc[label].iloc[col_idx]
                if pd.notna(v):
                    net_income = float(v)
                    break

        gross_profit = None
        if "Gross Profit" in income_stmt.index:
            v = income_stmt.loc["Gross Profit"].iloc[col_idx]
            if pd.notna(v):
                gross_profit = float(v)

        operating_income = None
        if "Operating Income" in income_stmt.index:
            v = income_stmt.loc["Operating Income"].iloc[col_idx]
            if pd.notna(v):
                operating_income = float(v)

        ebitda_year = _compute_ebitda(income_stmt, col_idx=col_idx)
        bs_items = _compute_balance_sheet_items(balance_sheet, col_idx=col_idx)

        year_data = {
            "year": year_label,
            "gross_margin": (gross_profit / revenue) if gross_profit is not None and revenue else None,
            "operating_margin": (operating_income / revenue) if operating_income is not None and revenue else None,
            "net_margin": (net_income / revenue) if net_income is not None and revenue else None,
            "ebitda_margin": (ebitda_year / revenue) if ebitda_year is not None and revenue else None,
            "roe": (net_income / bs_items["total_equity"])
            if net_income is not None and bs_items["total_equity"] else None,
            "roa": (net_income / bs_items["total_assets"])
            if net_income is not None and bs_items["total_assets"] else None,
        }
        trend.append(year_data)

    if len(trend) < max_years:
        missing.append(f"historical_trend_only_{len(trend)}_years")

    return trend


def _compute_momentum(history: pd.DataFrame, missing: list[str]) -> dict[str, float | None]:
    """12-1 month, 3-month, 6-month raw price returns from daily close
    history. NOT a scored "momentum factor" anymore (the scoring engine
    that combined this into a composite score has been removed) -- these
    raw return figures are kept as-is for potential use as volatility/risk
    context (build spec Section 3.6), never reframed as a timing signal.
    """
    out = {"return_12_1m": None, "return_3m": None, "return_6m": None}
    if history is None or history.empty or "Close" not in history.columns:
        missing.extend(["return_12_1m", "return_3m", "return_6m"])
        return out

    closes = history["Close"].dropna()
    if len(closes) < 30:
        missing.extend(["return_12_1m", "return_3m", "return_6m"])
        return out

    idx = closes.index
    last_date = idx[-1]

    def _price_on_or_before(target_date) -> float | None:
        eligible = closes[idx <= target_date]
        return float(eligible.iloc[-1]) if not eligible.empty else None

    price_now = float(closes.iloc[-1])
    price_1m_ago = _price_on_or_before(last_date - pd.Timedelta(days=30))
    price_3m_ago = _price_on_or_before(last_date - pd.Timedelta(days=91))
    price_6m_ago = _price_on_or_before(last_date - pd.Timedelta(days=182))
    price_12m_ago = _price_on_or_before(last_date - pd.Timedelta(days=365))

    if price_12m_ago and price_1m_ago:
        out["return_12_1m"] = (price_1m_ago / price_12m_ago) - 1
    else:
        missing.append("return_12_1m")
    if price_3m_ago:
        out["return_3m"] = (price_now / price_3m_ago) - 1
    else:
        missing.append("return_3m")
    if price_6m_ago:
        out["return_6m"] = (price_now / price_6m_ago) - 1
    else:
        missing.append("return_6m")
    return out


def _compute_volatility(history: pd.DataFrame, missing: list[str]) -> dict[str, float | None]:
    """Annualized historical volatility (stdev of daily returns x
    sqrt(252)) over two windows: a long/"historical" window (~3yr, the
    full price history now fetched -- see config.VOLATILITY_LONG_WINDOW_DAYS)
    and a short/"current-recent" window (~90 days). Both slice the SAME
    already-fetched 3yr history -- no extra network calls.

    This is risk/volatility CONTEXT ONLY, explicitly not a momentum or
    market-timing signal -- it answers "how much does this stock move,"
    never "is it going up." Framing that distinction is a dashboard-layer
    responsibility; this function only computes the numbers.
    """
    out = {"volatility_long": None, "volatility_recent": None}
    if history is None or history.empty or "Close" not in history.columns:
        missing.extend(["volatility_long", "volatility_recent"])
        return out

    closes = history["Close"].dropna()
    if len(closes) < 20:
        missing.extend(["volatility_long", "volatility_recent"])
        return out

    daily_returns = closes.pct_change().dropna()

    if len(daily_returns) >= 20:
        out["volatility_long"] = float(
            daily_returns.std() * (config.TRADING_DAYS_PER_YEAR ** 0.5))
    else:
        missing.append("volatility_long")

    recent_cutoff = closes.index[-1] - \
        pd.Timedelta(days=config.VOLATILITY_RECENT_WINDOW_DAYS)
    recent_returns = daily_returns[daily_returns.index >= recent_cutoff]
    if len(recent_returns) >= 10:
        out["volatility_recent"] = float(
            recent_returns.std() * (config.TRADING_DAYS_PER_YEAR ** 0.5))
    else:
        missing.append("volatility_recent")

    return out


def _compute_diluted_shares_trend(income_stmt: pd.DataFrame, missing: list[str],
                                  max_years: int = 5) -> list[dict]:
    """Diluted shares outstanding for each available fiscal year, most
    recent first -- rising = dilution, falling = buybacks (build spec
    3.9). Same "however many years yfinance actually gives us" caveat as
    the profitability historical trend.
    """
    if income_stmt is None or income_stmt.empty:
        missing.append("diluted_shares_trend")
        return []

    label = None
    for candidate in ("Diluted Average Shares", "Basic Average Shares"):
        if candidate in income_stmt.index:
            label = candidate
            break
    if label is None:
        missing.append("diluted_shares_trend")
        return []

    n_years = min(max_years, income_stmt.shape[1])
    trend = []
    for col_idx in range(n_years):
        try:
            year_label = str(income_stmt.columns[col_idx].year)
        except (AttributeError, IndexError):
            year_label = f"Year -{col_idx}"
        val = income_stmt.loc[label].iloc[col_idx]
        trend.append({"year": year_label, "diluted_shares": float(
            val) if pd.notna(val) else None})

    if len(trend) < max_years:
        missing.append(f"diluted_shares_trend_only_{len(trend)}_years")

    return trend


def _compute_peg_ratio(info: dict, trailing_pe: float | None,
                       eps_growth_yoy: float | None, missing: list[str]) -> float | None:
    """PEG = P/E / (EPS growth rate x 100). Prefer yfinance's own
    'pegRatio' field when available (it may use forward estimates, which
    is the more standard PEG convention); fall back to a computed version
    using trailing P/E and trailing EPS growth, which is what this pipeline
    reliably has -- flagged as a less standard variant when used, not
    presented as equivalent to a forward-looking PEG.
    """
    info_peg = info.get("pegRatio") if info else None
    if info_peg is not None:
        return float(info_peg)

    if trailing_pe is None or not eps_growth_yoy or eps_growth_yoy <= 0:
        missing.append("peg_ratio")
        return None
    return trailing_pe / (eps_growth_yoy * 100)


def _extract_revenue_history(income_stmt: pd.DataFrame, max_years: int = 4) -> list[float | None]:
    """Raw 'Total Revenue' values for up to `max_years` columns, ordered
    MOST RECENT FIRST (matching yfinance's own column order), with None for
    any missing/NaN year -- positional integrity matters here (index 0 =
    current year, index 3 = 3 years ago) for fundamentals.
    compute_projection_growth_rate's fallback logic, so this does NOT
    .dropna() (which would shift positions and silently corrupt the
    year-to-year alignment).
    """
    if income_stmt is None or income_stmt.empty or "Total Revenue" not in income_stmt.index:
        return []
    n = min(max_years, income_stmt.shape[1])
    values = []
    for col_idx in range(n):
        val = income_stmt.loc["Total Revenue"].iloc[col_idx]
        values.append(float(val) if pd.notna(val) else None)
    return values


def _compute_revenue_and_eps_growth(income_stmt: pd.DataFrame, info: dict,
                                    missing: list[str]) -> dict[str, float | None]:
    out = {"revenue_growth_yoy": None, "eps_growth_yoy": None}
    if income_stmt is not None and not income_stmt.empty and "Total Revenue" in income_stmt.index:
        rev = income_stmt.loc["Total Revenue"].dropna()
        if len(rev) >= 2:
            out["revenue_growth_yoy"] = float((rev.iloc[0] / rev.iloc[1]) - 1)
    if out["revenue_growth_yoy"] is None:
        # Fallback to .info's own figure if the calculated version isn't available
        val = info.get("revenueGrowth")
        if val is not None:
            out["revenue_growth_yoy"] = float(val)
        else:
            missing.append("revenue_growth_yoy")

    if income_stmt is not None and not income_stmt.empty:
        for label in ("Diluted EPS", "Basic EPS"):
            if label in income_stmt.index:
                eps = income_stmt.loc[label].dropna()
                if len(eps) >= 2 and eps.iloc[1] != 0:
                    out["eps_growth_yoy"] = float(
                        (eps.iloc[0] / eps.iloc[1]) - 1)
                    break
    if out["eps_growth_yoy"] is None:
        missing.append("eps_growth_yoy")
    return out


@_yf_retry()
def _pull_ticker_frames(ticker_obj) -> dict:
    """Single retrying call site that grabs every yfinance sub-object needed.
    Wrapped together (rather than per-field) so a transient failure retries
    the whole small batch rather than leaving a ticker half-populated.

    History period extended from 450d to 3y (single API call, same cost --
    yfinance doesn't charge per-day) to additionally support the Risk
    section's historical volatility calc (an annualized stdev over a
    genuinely multi-year window), not just the existing momentum returns
    (12-1m/3m/6m), which only ever needed ~13 months.
    """
    return {
        "info": ticker_obj.info or {},
        "cashflow": ticker_obj.cashflow,
        "balance_sheet": ticker_obj.balance_sheet,
        "income_stmt": ticker_obj.income_stmt,
        "history": ticker_obj.history(period="3y", interval="1d"),
    }


def fetch_one_ticker(ticker: str) -> TickerFetchResult:
    """Fetch and compute every metric for a single ticker. Never raises --
    on unrecoverable failure, returns a result with fetch_error set so the
    caller can flag it in the UI rather than the whole run crashing."""
    result = TickerFetchResult(ticker=ticker)
    missing: list[str] = []
    try:
        tobj = _get_ticker_object(ticker)
        frames = _pull_ticker_frames(tobj)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch %s after retries: %s", ticker, exc)
        result.fetch_error = str(exc)
        return result

    info = frames["info"]
    result.sector = info.get("sector")
    if result.sector is None:
        missing.append("sector")

    raw: dict = {}
    raw["trailing_pe"] = _safe_get(info, "trailingPE", missing)
    raw["forward_pe"] = _safe_get(info, "forwardPE", missing)
    raw["price_to_book"] = _safe_get(info, "priceToBook", missing)
    raw["ev_to_ebitda"] = _safe_get(info, "enterpriseToEbitda", missing)
    raw["ev_to_sales"] = _safe_get(info, "enterpriseToRevenue", missing)
    raw["roe"] = _safe_get(info, "returnOnEquity", missing)
    raw["roa"] = _safe_get(info, "returnOnAssets", missing)
    raw["gross_margin"] = _safe_get(info, "grossMargins", missing)
    raw["operating_margin"] = _safe_get(info, "operatingMargins", missing)
    raw["debt_to_equity"] = _safe_get(info, "debtToEquity", missing)
    raw["market_cap"] = _safe_get(info, "marketCap", missing)
    raw["company_name"] = info.get("longName") or info.get("shortName")
    if raw["company_name"] is None:
        missing.append("company_name")
    raw["current_price"] = info.get(
        "currentPrice") or info.get("regularMarketPrice")
    if raw["current_price"] is None:
        missing.append("current_price")

    # Risk section (3.6) -- Beta from .info, plus ownership/price-target
    # fields (3.9) which are also simple single .info fields.
    raw["beta"] = _safe_get(info, "beta", missing)
    raw["insider_ownership_pct"] = _safe_get(
        info, "heldPercentInsiders", missing)
    raw["institutional_ownership_pct"] = _safe_get(
        info, "heldPercentInstitutions", missing)
    raw["analyst_target_mean_price"] = info.get(
        "targetMeanPrice") or info.get("targetMedianPrice")
    if raw["analyst_target_mean_price"] is None:
        missing.append("analyst_target_mean_price")

    raw["fcf"] = _compute_fcf(frames["cashflow"], raw["market_cap"], missing)
    raw["fcf_raw"] = raw["fcf"]["fcf_raw"]
    raw["fcf_yield"] = raw["fcf"]["fcf_yield"]
    del raw["fcf"]

    net_debt = _compute_net_debt(frames["balance_sheet"])
    total_debt = _compute_total_debt(frames["balance_sheet"])
    cash = _compute_cash(frames["balance_sheet"])
    ebit = _compute_ebit(frames["income_stmt"])
    ebitda = _compute_ebitda(frames["income_stmt"])
    interest_expense = _compute_interest_expense(frames["income_stmt"])
    bs_items = _compute_balance_sheet_items(frames["balance_sheet"])
    # total_assets, total_equity, retained_earnings, total_liabilities
    raw.update(bs_items)
    raw["total_debt_raw"] = total_debt
    raw["cash_raw"] = cash
    raw["ebit_raw"] = ebit
    raw["interest_expense_raw"] = interest_expense
    if ebit is None:
        missing.append("ebit_raw")
    if cash is None:
        missing.append("cash_raw")
    if interest_expense is None:
        missing.append("interest_expense_raw")
    for k in ("total_assets", "total_equity", "retained_earnings", "total_liabilities"):
        if raw.get(k) is None:
            missing.append(k)

    if net_debt is None or ebitda in (None, 0):
        raw["net_debt_to_ebitda"] = None
        missing.append("net_debt_to_ebitda")
    else:
        raw["net_debt_to_ebitda"] = net_debt / ebitda

    # Company Size (3.2) -- Enterprise Value = Market Cap + Net Debt (net
    # debt already excludes cash, so this is the standard simplified EV;
    # it does not add minority interest or preferred stock, which yfinance
    # doesn't reliably expose as separate fields).
    if raw["market_cap"] is not None and net_debt is not None:
        raw["enterprise_value"] = raw["market_cap"] + net_debt
    else:
        raw["enterprise_value"] = None
        missing.append("enterprise_value")

    raw["interest_coverage"] = _compute_interest_coverage(
        frames["income_stmt"], missing)
    raw["earnings_stability_stdev"] = _compute_earnings_stability(
        frames["income_stmt"], missing)
    raw["debt_growth_yoy"] = _compute_debt_growth_yoy(
        frames["balance_sheet"], missing)

    raw.update(_compute_momentum(frames["history"], missing))
    raw.update(_compute_volatility(frames["history"], missing))
    raw["diluted_shares_trend"] = _compute_diluted_shares_trend(
        frames["income_stmt"], missing)

    growth = _compute_revenue_and_eps_growth(
        frames["income_stmt"], info, missing)
    raw.update(growth)

    raw["peg_ratio"] = _compute_peg_ratio(
        info, raw["trailing_pe"], growth["eps_growth_yoy"], missing)

    rev_income = _compute_revenue_and_net_income(
        frames["income_stmt"], missing)
    raw.update(rev_income)

    revenue_ttm = rev_income["revenue_ttm"]
    net_income_ttm = rev_income["net_income_ttm"]

    # Net margin: prefer calculating for consistency, fall back to .info's
    # own figure (same pattern already used for revenue_growth_yoy).
    if revenue_ttm not in (None, 0) and net_income_ttm is not None:
        raw["net_margin"] = net_income_ttm / revenue_ttm
    else:
        info_margin = info.get("profitMargins")
        if info_margin is not None:
            raw["net_margin"] = float(info_margin)
        else:
            raw["net_margin"] = None
            missing.append("net_margin")

    if revenue_ttm not in (None, 0) and ebitda is not None:
        raw["ebitda_margin"] = ebitda / revenue_ttm
    else:
        raw["ebitda_margin"] = None
        missing.append("ebitda_margin")

    # FCF margin and FCF/Net Income (quality-of-earnings check) -- pure
    # calc, delegated to fundamentals.py.
    fcf_quality = fundamentals.compute_fcf_quality(
        raw["fcf_raw"], revenue_ttm, net_income_ttm, missing)
    raw.update(fcf_quality)

    # Simple ROIC deliberately NOT computed here (unchanged, per instruction
    # to keep this existing honest flag as-is). NOPAT requires a reliable
    # effective tax rate and invested capital requires a clean debt+equity-
    # cash figure -- both are inconsistent enough in free yfinance data
    # that a computed simple ROIC here would be more misleading than
    # useful. This is DISTINCT from the ROIC used in the upcoming Value
    # Creation / WACC section, which instead uses a disclosed FLAT tax-rate
    # assumption (e.g. 21%) rather than an unreliable effective rate --
    # that version is defensible precisely because the assumption is
    # visible, not because the underlying data got more reliable.
    missing.append("roic")

    # DuPont decomposition -- ROE = Net Margin x Asset Turnover x Equity
    # Multiplier, using the raw scalars already extracted above. Pure calc,
    # delegated to fundamentals.py.
    dupont = fundamentals.compute_dupont(raw["net_margin"], revenue_ttm, raw["total_assets"],
                                         raw["total_equity"], missing)
    raw.update(dupont)

    liquidity = _compute_liquidity_ratios(frames["balance_sheet"], missing)
    raw.update(liquidity)

    # Cross-comparability gearing ratios (3.4).
    if net_debt is not None and raw["market_cap"]:
        raw["net_debt_to_market_cap"] = net_debt / raw["market_cap"]
    else:
        raw["net_debt_to_market_cap"] = None
        missing.append("net_debt_to_market_cap")

    if total_debt is not None and raw["total_assets"]:
        raw["total_debt_to_total_assets"] = total_debt / raw["total_assets"]
    else:
        raw["total_debt_to_total_assets"] = None
        missing.append("total_debt_to_total_assets")

    # Altman Z-Score (3.4) -- flagged less-applicable for Financials/Real
    # Estate rather than hidden or forced. Pure calc, delegated to
    # fundamentals.py.
    altman = fundamentals.compute_altman_z_score(
        liquidity.get("current_assets"), liquidity.get("current_liabilities"),
        raw["total_assets"], raw["retained_earnings"], ebit, raw["market_cap"],
        raw["total_liabilities"], revenue_ttm, result.sector, missing,
    )
    raw.update(altman)

    # 5-year historical trend (3.3) -- however many years yfinance actually
    # provides (flagged if fewer than 5). Needs the multi-column
    # balance_sheet/income_stmt frames directly, so this stays in
    # data_pipeline.py rather than fundamentals.py (which only ever sees
    # already-extracted scalars, never raw multi-column frames).
    raw["historical_trend"] = _compute_historical_trend(
        frames["balance_sheet"], frames["income_stmt"], missing
    )

    # NOTE: long_term_growth_rate used to be computed here from .info
    # fields (longTermPotentialGrowthRate, falling back to earningsGrowth)
    # -- REMOVED. Bug found in practice: longTermPotentialGrowthRate
    # appears to never actually be populated by yfinance, so the
    # earningsGrowth fallback was ALWAYS silently activating -- and
    # earningsGrowth is a TRAILING/short-term earnings growth figure (e.g.
    # a recent quarter's or year's YoY EPS growth), not a long-term
    # forward estimate. This produced real but wrongly-labeled numbers:
    # NVDA's ~127.8% and AAPL's ~28.7% are both plausible trailing
    # earnings-growth figures (NVDA's AI-driven earnings surge, a specific
    # AAPL quarter's YoY EPS growth) masquerading as "long-term growth"
    # when they're nothing of the sort. Mixing two genuinely different
    # metrics under one label is worse than showing "not available", so
    # this field is no longer computed via .info fallback logic at all.
    # See deep_dive.py's fetch_analyst_estimates() for the replacement
    # attempt via yfinance's get_growth_estimates() -- an actual forward-
    # looking analyst consensus figure, not a trailing growth number
    # relabeled. That replacement is itself UNVERIFIED against live data
    # (no network access here) and should be sanity-checked against a
    # known source before being trusted.

    # BUG FIX: forward projections used to compound raw['revenue_growth_yoy']
    # (a single year's YoY rate) forward -- extremely sensitive to a single
    # anomalous base year (confirmed: one exceptional year for NVDA
    # mechanically compounded into a ~$33 trillion 10yr revenue figure).
    # Replaced with a 3-year revenue CAGR (falling back to 2yr, then 1yr
    # YoY only as a last resort) as the projection input specifically --
    # revenue_growth_yoy itself is UNCHANGED and still used as-is for
    # Sector Growth Context display and sector-average benchmarking
    # elsewhere; this fix only touches what feeds the Model Extrapolation
    # projections.
    revenue_history = _extract_revenue_history(frames["income_stmt"])
    projection_growth = fundamentals.compute_projection_growth_rate(
        revenue_history)
    raw["projection_growth_rate"] = projection_growth["rate"]
    raw["projection_growth_basis"] = projection_growth["basis"]
    raw["projection_growth_warning"] = projection_growth["warning"]
    if projection_growth["warning"]:
        missing.append(f"projection_growth_basis_{projection_growth['basis']}")

    # Forward projections (1yr/5yr/10yr) -- pure calc, delegated to
    # fundamentals.py. Uses the 3yr-CAGR-with-fallback rate above, NOT
    # growth["revenue_growth_yoy"] (see bug-fix note above).
    projections = fundamentals.compute_forward_projections(
        revenue_ttm, ebitda, net_debt, raw["net_margin"], projection_growth["rate"], missing
    )
    raw["projections"] = projections
    # Flat scalar kept for convenience/backward-compat with existing display code.
    raw["current_net_debt_ebitda"] = projections["current"]["net_debt_ebitda"]
    raw["net_debt_raw"] = net_debt
    raw["ebitda_raw"] = ebitda

    result.raw = raw
    result.missing_fields = missing
    return result


def fetch_universe(tickers: list[str], sleep_between: float | None = None) -> pd.DataFrame:
    """Fetch a LIST of tickers sequentially and return one DataFrame, one row
    per ticker. Missing fields are tracked in a `missing_fields` column
    (list) per row rather than silently dropping rows or columns.

    Retained (renamed usage, not removed) for the new sector-benchmark
    sampling and competitor-discovery flows, which each need a *small*,
    on-demand list of tickers fetched together -- NOT the old "fetch the
    whole 150-company universe up front" pattern. Callers should pass a
    short list (a dozen or so sector-sample peers, or 3-5 competitors),
    never a large fixed universe.
    """
    sleep_between = sleep_between if sleep_between is not None else config.YF_INTER_TICKER_SLEEP_SECONDS
    rows = []
    for i, ticker in enumerate(tickers):
        res = fetch_one_ticker(ticker)
        row = {"ticker": res.ticker, "sector": res.sector,
               "fetch_error": res.fetch_error, "missing_fields": res.missing_fields}
        row.update(res.raw)
        rows.append(row)
        if (i + 1) % 10 == 0:
            logger.info("Fetched %d/%d tickers", i + 1, len(tickers))
        time.sleep(sleep_between)
    df = pd.DataFrame(rows)
    df["pulled_at"] = datetime.now().isoformat()
    return df


# ---------------------------------------------------------------------------
# Per-ticker daily cache (replaces the old whole-universe parquet blob)
# ---------------------------------------------------------------------------
def _ticker_cache_path(ticker: str) -> Path:
    safe_ticker = ticker.replace("/", "_").replace("\\", "_")
    return Path(config.TICKER_CACHE_DIR) / f"{safe_ticker}.json"


def load_or_fetch_ticker(ticker: str, force_refresh: bool = False) -> dict:
    """Cache a single ticker's fetch_one_ticker() result to its own small
    JSON file, refreshed at most once per CACHE_TTL_HOURS. This replaces the
    old load_or_fetch_universe() (one big parquet blob for a fixed 150-name
    universe) -- the new usage pattern is "fetch whichever single company is
    searched, plus a handful of sector-sample peers and competitors on
    demand," so a per-ticker cache file is both simpler and a better fit
    than a single shared universe-wide cache that every page load rewrites.

    Returns a plain dict (ticker, sector, raw fields, missing_fields,
    fetch_error) -- the same shape as one row of the old universe DataFrame,
    so callers combining several tickers can still build a small DataFrame
    with pd.DataFrame([load_or_fetch_ticker(t) for t in tickers]).
    """
    import json

    cache_path = _ticker_cache_path(ticker)
    if not force_refresh and cache_path.exists():
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours < config.CACHE_TTL_HOURS:
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Cache file for %s unreadable (%s), refetching", ticker, exc)

    res = fetch_one_ticker(ticker)
    row = {"ticker": res.ticker, "sector": res.sector,
           "fetch_error": res.fetch_error, "missing_fields": res.missing_fields}
    row.update(res.raw)
    row["pulled_at"] = datetime.now().isoformat()

    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(row, f, default=str)
    except OSError as exc:
        logger.warning("Failed to write cache for %s: %s", ticker, exc)

    return row


def load_or_fetch_many(tickers: list[str], force_refresh: bool = False) -> pd.DataFrame:
    """Convenience wrapper: load/fetch several tickers via the per-ticker
    cache and combine into one DataFrame. Used by sector-benchmark sampling
    and competitor discovery, which both need a handful of tickers' data at
    once but should never precompute a large fixed universe up front.
    """
    rows = [load_or_fetch_ticker(t, force_refresh=force_refresh)
            for t in tickers]
    return pd.DataFrame(rows)


def fetch_vix() -> tuple[float | None, str]:
    """VIX level for the header card. Market-regime context only -- not
    used in any calculation."""
    try:
        tobj = _get_ticker_object("^VIX")
        hist = tobj.history(period="5d")
        if hist.empty:
            return None, "unavailable"
        return float(hist["Close"].iloc[-1]), hist.index[-1].strftime("%Y-%m-%d")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch VIX: %s", exc)
        return None, "unavailable"


def fetch_risk_free_rate() -> tuple[float, bool]:
    """10-Year Treasury yield (^TNX) for CAPM's risk-free rate, live. yfinance
    quotes ^TNX in yield-percent terms (e.g. 4.75 means 4.75%), so this
    divides by 100 to return a decimal rate.

    Returns (rate, is_live): if the live fetch fails for any reason, falls
    back to config.RISK_FREE_RATE_STATIC_FALLBACK and returns is_live=False
    so the UI can label which source was actually used -- never silently
    presenting a static fallback as if it were live.
    """
    try:
        tobj = _get_ticker_object("^TNX")
        hist = tobj.history(period="5d")
        if hist.empty:
            raise ValueError("empty history for ^TNX")
        rate = float(hist["Close"].iloc[-1]) / 100
        return rate, True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to fetch live risk-free rate (^TNX): %s -- using static fallback", exc)
        return config.RISK_FREE_RATE_STATIC_FALLBACK, False
