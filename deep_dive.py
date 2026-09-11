"""
Company Deep Dive -- assembles and renders the full company analysis page.

This module PULLS FROM every other module (data_pipeline, competitors,
value_creation, sector_forecasts) but does not contain its own
data-fetching or calculation logic that duplicates what's in those files.
Its only jobs are: (1) thin caching wrappers around each module's fetch
functions (so a Streamlit rerun doesn't refetch/recompute needlessly), and
(2) Streamlit rendering calls that assemble those results into the page.

Public entry point: render(selected_ticker). dashboard.py calls this one
function for whichever ticker is currently selected; it does not pass in
any pre-fetched data -- this module does its own fetching via the cached
loaders below, each of which delegates to the appropriate module.

Section order matches the final consolidated spec:
    3.1 Header
    3.2 Company Size
    3.3 Profitability (current, 5yr trend, DuPont, FCF, 1/5/10yr projections)
    3.4 Gearing/Debt (current, Altman Z-Score, 1/5/10yr projections, trajectory chart)
    3.5 Valuation (vs. sector-average and competitor comps)
    3.6 Risk (Beta, historical/recent volatility)
    3.7 Value Creation (ROIC vs. WACC)
    3.8 Competitors
    3.9 Ownership & Dilution
    3.10 Sector/Market

SENTIMENT REMOVED (Finnhub/FinBERT): sentiment was cut entirely due to
ongoing API key friction -- not a bug fix, a deliberate removal. This
includes the Sentiment tab, _cached_sentiment(), _cached_recent_headlines(),
and the sentiment_pipeline.py module itself (deleted). If you see any
reference to sentiment, Finnhub, or sentiment_pipeline anywhere in this
codebase going forward, that's a regression -- it should all be gone.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
import value_creation as vc
from competitors import compute_sector_benchmark, discover_competitors, get_classification_for_ticker
from data_pipeline import load_or_fetch_ticker
from sector_forecasts import get_sector_forecast

# ---------------------------------------------------------------------------
# Style constants -- shared with dashboard.py's global CSS block so the
# page chrome and this module's charts/cards use one consistent palette.
# ---------------------------------------------------------------------------
PAGE_BG = "#0e1117"
CARD_BG = "#1a1d24"
ACCENT = "#4da3ff"


# ---------------------------------------------------------------------------
# Analyst consensus estimates
# ---------------------------------------------------------------------------
def fetch_analyst_estimates(ticker: str) -> dict:
    """On-demand fetch of forward analyst consensus estimates for a single
    ticker via yfinance. Returns a dict with 'revenue', 'earnings', and
    'long_term_growth_rate' keys ('revenue'/'earnings' are small DataFrames
    indexed by period, e.g. '0y'/'+1y'; 'long_term_growth_rate' is a single
    float or None), plus 'error'.

    Labeled in the UI as "Analyst Consensus (source: aggregated analyst
    estimates via Yahoo Finance)" -- kept clearly separate from this
    project's own Model Extrapolation, never blended into one number.

    FLAG: yfinance's analyst-estimate endpoints are a newer, less
    battle-tested part of its API than `.info` or `.history()`. Treat an
    empty/missing result as normal (flagged in the UI), not an error -- but
    if these are empty for a LARGE share of tickers you try (not just a
    handful of low-coverage names), that's worth reporting back.

    long_term_growth_rate BUG HISTORY: this used to be read from
    data_pipeline.py via .info['longTermPotentialGrowthRate'], falling back
    to .info['earningsGrowth'] when that was absent -- which, in practice,
    was ALWAYS, because longTermPotentialGrowthRate doesn't appear to be a
    real populated field. earningsGrowth is a TRAILING earnings-growth
    figure (e.g. a recent quarter's YoY EPS growth), not a long-term
    forward estimate -- mixing the two produced real but wrongly-labeled
    numbers (NVDA ~127.8%, AAPL ~28.7%, both plausible as trailing growth,
    implausible as decade-scale consensus growth). Replaced with an
    attempt at yfinance's get_growth_estimates() table, which (if it
    behaves as documented) has a '+5y' row representing the standard
    "Next 5 Years (per annum)" analyst consensus figure shown on Yahoo
    Finance's own Analysis tab -- an actual forward-looking estimate, not
    a trailing number in a forward-looking costume.

    UNVERIFIED: this method/shape has not been tested against live data in
    this environment (no network access here). Sanity-check the resulting
    number against Yahoo Finance's own Analysis tab for one ticker before
    trusting it; if get_growth_estimates() doesn't exist or doesn't behave
    as expected, this degrades to "not available" rather than guessing.
    """
    import yfinance as yf

    result = {"revenue": None, "earnings": None,
              "long_term_growth_rate": None, "error": None}
    try:
        tobj = yf.Ticker(ticker)

        try:
            rev_est = tobj.get_revenue_estimate()
            if rev_est is not None and not rev_est.empty:
                result["revenue"] = rev_est
        except Exception:  # noqa: BLE001
            pass

        try:
            earn_est = tobj.get_earnings_estimate()
            if earn_est is not None and not earn_est.empty:
                result["earnings"] = earn_est
        except Exception:  # noqa: BLE001
            pass

        try:
            growth_est = tobj.get_growth_estimates()
            if growth_est is not None and not growth_est.empty and "+5y" in growth_est.index:
                row_5y = growth_est.loc["+5y"]
                # Column name isn't verified -- try the plausible candidates,
                # then fall back to the first numeric value in the row
                # rather than guessing a specific column name blindly.
                value = None
                for col_candidate in ("stockTrend", "growth", ticker):
                    if col_candidate in row_5y.index:
                        value = row_5y[col_candidate]
                        break
                if value is None:
                    numeric_vals = pd.to_numeric(
                        row_5y, errors="coerce").dropna()
                    if not numeric_vals.empty:
                        value = numeric_vals.iloc[0]
                if value is not None:
                    result["long_term_growth_rate"] = float(value)
        except Exception:  # noqa: BLE001
            pass

    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)

    if result["revenue"] is None and result["earnings"] is None and result["error"] is None:
        result["error"] = "No analyst consensus data available for this ticker."

    return result


# ---------------------------------------------------------------------------
# Cached loaders -- one thin wrapper per data source this page needs.
# Each delegates entirely to the owning module; no fetch/calc logic here.
# ---------------------------------------------------------------------------
@st.cache_data(ttl=config.CACHE_TTL_HOURS * 3600)
def _cached_ticker_fetch(ticker: str, _cache_date: str) -> dict:
    return load_or_fetch_ticker(ticker)


@st.cache_data(ttl=config.CACHE_TTL_HOURS * 3600)
def _cached_analyst_estimates(ticker: str, _cache_date: str):
    return fetch_analyst_estimates(ticker)


@st.cache_data(ttl=config.CACHE_TTL_HOURS * 3600)
def _cached_sector_benchmark(sector: str, exclude_ticker: str, _cache_date: str):
    return compute_sector_benchmark(sector, exclude_ticker)


@st.cache_data(ttl=config.CACHE_TTL_HOURS * 3600)
def _cached_risk_free_rate(_cache_date: str):
    from data_pipeline import fetch_risk_free_rate
    return fetch_risk_free_rate()


@st.cache_data(ttl=config.CACHE_TTL_HOURS * 3600)
def _cached_competitors(sub_industry: str | None, exclude_ticker: str,
                        target_market_cap: float | None, _cache_date: str):
    return discover_competitors(sub_industry, exclude_ticker, target_market_cap)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _to_list(val) -> list:
    """Normalize a possibly-numpy-array, possibly-None value to a plain
    list (JSON cache round-trips can occasionally hand back arrays instead
    of lists depending on how a value was produced upstream)."""
    if val is None:
        return []
    if isinstance(val, float) and pd.isna(val):
        return []
    if isinstance(val, np.ndarray):
        return val.tolist()
    if isinstance(val, (list, tuple, set)):
        return list(val)
    return []


def _fmt(val, kind: str = "num", decimals: int = 2) -> str:
    """Consistent N/A-safe formatting for display values."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "N/A"
    if kind == "pct":
        return f"{val:.1%}"
    if kind == "money":
        abs_val = abs(val)
        sign = "-" if val < 0 else ""
        if abs_val >= 1e9:
            return f"{sign}${abs_val / 1e9:.2f}B"
        if abs_val >= 1e6:
            return f"{sign}${abs_val / 1e6:.2f}M"
        return f"{sign}${abs_val:,.0f}"
    if kind == "price":
        return f"${val:,.2f}"
    return f"{val:.{decimals}f}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def render(selected_ticker: str) -> None:
    """Render the full company deep-dive page for `selected_ticker`. Does
    its own fetching via the cached loaders above -- dashboard.py should
    call this with just the ticker string, nothing pre-fetched.

    LAYOUT (revised): Header and Company Size stay always visible above a
    row of tabs -- one tab per remaining section (Profitability,
    Gearing/Debt, Valuation, Risk, Value Creation, Competitors, Ownership &
    Dilution, Sector/Market, Sentiment). Chose st.tabs() over a custom
    sidebar/sticky-nav: Streamlit has no native scroll-to-anchor jump-link
    support, and a custom HTML/JS nav bar would carry the same
    version-fragility that broke the earlier README-button attempt in this
    project. Tabs are a native, robust primitive that directly solves
    "don't know where to look" -- the section list is always visible as
    the tab bar, and only one section's content is on screen at a time.

    Within each tab: headline numbers stay visible; supporting calculation
    detail (5yr trends, full projection tables, ROIC/WACC inputs) collapses
    behind st.expander(), matching the pattern from the Systematic Trading
    Lab project. No metric, section, or calculation was removed -- only
    reorganized. See the build prompt's Part 2 for the specific rationale
    per section.
    """
    today_str = date.today().isoformat()

    with st.spinner(f"Fetching {selected_ticker}..."):
        raw = _cached_ticker_fetch(selected_ticker, today_str)

    if raw.get("fetch_error"):
        st.error(f"Couldn't fetch data for '{selected_ticker}': {raw['fetch_error']}. "
                 "Check the ticker is valid and try again.")
        st.stop()

    row = pd.Series(raw)

    st.markdown("---")

    # --- 3.1 Header (always visible, above tabs) ---
    # BUG FIX: yfinance's own .info['sector'] uses Yahoo Finance's internal
    # sector taxonomy (e.g. "Technology"), which does NOT match official
    # GICS sector names (e.g. "Information Technology") used in the
    # Wikipedia-scraped classification file. Previously this used
    # row.get("sector") -- Yahoo's label -- to filter the classification
    # file's "sector" column (GICS labels) when sampling sector-average
    # peers, silently matching zero rows for every sector whose Yahoo name
    # differs from its GICS name (Technology vs Information Technology,
    # Consumer Cyclical vs Consumer Discretionary, Financial Services vs
    # Financials, Healthcare vs Health Care, Consumer Defensive vs
    # Consumer Staples). Competitor discovery was unaffected because it
    # only ever used sub_industry, sourced directly from the classification
    # file -- consistent vocabulary throughout, hence why it worked while
    # sector-benchmark sampling silently returned zero.
    #
    # FIX: use the classification file's own sector value (GICS-labeled,
    # consistent with what sample_sector_peers filters against) everywhere
    # on this page. Falls back to Yahoo's live .info sector only if the
    # ticker isn't in the classification file at all.
    classification = get_classification_for_ticker(selected_ticker)
    sector = classification.get("sector") or row.get("sector")
    sub_industry = classification.get("sub_industry")

    hcol1, hcol2, hcol3, hcol4 = st.columns(4)
    hcol1.metric("Ticker", selected_ticker)
    hcol2.metric("Sector", sector or "N/A")
    hcol3.metric("Sub-Industry", sub_industry or "N/A")
    hcol4.metric("Price", _fmt(row.get("current_price"), "price"))

    company_name = row.get("company_name") or selected_ticker
    st.markdown(f"### {company_name}")

    if not classification.get("in_classification_file"):
        st.caption("⚠️ Not found in the S&P 500 classification file — sub-industry unavailable, "
                   "and sector-average benchmarking below uses this company's own sector "
                   "(fetched directly), but competitor discovery may not find good matches "
                   "without full classification data.")

    st.markdown("")

    # --- 3.2 Company Size (always visible, above tabs) ---
    st.markdown("#### Company Size")
    st.caption(
        "Plain dollar figures — scale should never be hidden behind ratios alone.")
    size_cols = st.columns(4)
    size_cols[0].metric("Market Cap", _fmt(row.get("market_cap"), "money"))
    size_cols[1].metric("Enterprise Value", _fmt(
        row.get("enterprise_value"), "money"))
    size_cols[2].metric("Total Revenue (TTM)", _fmt(
        row.get("revenue_ttm"), "money"))
    size_cols[3].metric("Total Assets", _fmt(row.get("total_assets"), "money"))

    st.markdown("---")

    # Pre-fetch shared data used by multiple tabs (sector benchmark feeds
    # Profitability, Valuation, and Sector/Market; competitors feeds
    # Valuation and Competitors) so each tab doesn't redundantly trigger it.
    with st.spinner(f"Sampling {sector} sector peers for benchmark comparison..."):
        benchmark = _cached_sector_benchmark(
            sector, selected_ticker, today_str) if sector else {"metrics": {}, "n_sampled": 0}

    with st.spinner("Discovering competitors..."):
        competitor_result = _cached_competitors(
            sub_industry, selected_ticker, row.get("market_cap"), today_str
        )

    projections = row.get("projections") or {}

    (tab_profit, tab_gearing, tab_valuation, tab_risk, tab_value_creation,
     tab_competitors, tab_ownership, tab_sector) = st.tabs([
         "💰 Profitability", "🏦 Gearing/Debt", "🏷️ Valuation", "⚠️ Risk",
         "💎 Value Creation", "🏢 Competitors", "📊 Ownership & Dilution",
         "🌐 Sector/Market",
     ])

    # =========================================================================
    # TAB: Profitability
    # =========================================================================
    with tab_profit:
        st.markdown("#### Current")
        st.caption("Compared against a live sample of same-sector peers — not a percentile "
                   "across a fixed universe, which no longer exists in this tool.")

        margin_metrics = [
            ("Gross margin", "gross_margin", "pct"),
            ("Operating margin", "operating_margin", "pct"),
            ("Net margin", "net_margin", "pct"),
            ("EBITDA margin", "ebitda_margin", "pct"),
            ("ROE", "roe", "pct"),
            ("ROA", "roa", "pct"),
        ]
        margin_cols = st.columns(len(margin_metrics))
        for i, (label, metric, kind) in enumerate(margin_metrics):
            bench = benchmark["metrics"].get(metric, {})
            avg = bench.get("average")
            delta_str = None
            if avg is not None and pd.notna(row.get(metric)):
                delta = row.get(metric) - avg
                delta_str = f"{delta:+.1%} vs sector avg" if kind == "pct" else f"{delta:+.2f} vs sector avg"
            margin_cols[i].metric(label, _fmt(
                row.get(metric), kind), delta_str)

        # LAYOUT FIX: ROIC's explanatory note used to be crammed into a 7th
        # column alongside the 6 margin cards (1/7th page width), forcing a
        # normal-length caption to wrap into a tall, narrow sliver with
        # dead whitespace beside it. Moved to a full-width caption below
        # the row instead.
        st.caption("**ROIC (simple): Not computed.** Effective tax rate and invested capital "
                   "aren't reliably computable from free yfinance data for a *simple* ROIC. "
                   "A separate ROIC using a disclosed flat tax-rate assumption appears in the "
                   "Value Creation tab — that one is defensible because the assumption is "
                   "visible, not because the data got more reliable.")

        if benchmark.get("n_sampled"):
            st.caption(f"Sector benchmark based on a live sample of {benchmark['n_sampled']} "
                       f"{sector} peers (not the full sector) — see config.SECTOR_BENCHMARK_SAMPLE_SIZE.")
        elif sector:
            st.caption(
                f"No sector peers found to benchmark against for '{sector}'.")

        st.markdown("")

        st.markdown("#### Free Cash Flow")
        st.caption(
            "FCF/Net Income below 1.0x can flag earnings that aren't converting to cash.")
        fcol1, fcol2, fcol3 = st.columns(3)
        fcol1.metric("FCF (TTM)", _fmt(row.get("fcf_raw"), "money"))
        fcol2.metric("FCF Margin", _fmt(row.get("fcf_margin"), "pct"))
        fcol3.metric("FCF / Net Income", _fmt(row.get("fcf_to_net_income")))

        st.markdown("")

        # --- Forward projections headline only; full detail in expander ---
        st.markdown("#### Forward Projections — Revenue & Net Income")
        pcol1, pcol2, pcol3, pcol4 = st.columns(4)
        pcol1.metric("Revenue (Current)", _fmt(
            projections.get("current", {}).get("revenue"), "money"))
        pcol2.metric("Revenue (+10yr)",
                     _fmt(projections.get("10yr", {}).get("revenue"), "money"))
        pcol3.metric("Net Income (Current)", _fmt(
            projections.get("current", {}).get("net_income"), "money"))
        pcol4.metric("Net Income (+10yr)",
                     _fmt(projections.get("10yr", {}).get("net_income"), "money"))

        # BUG FIX: projections used to compound a single year's YoY revenue
        # growth, which was extremely sensitive to one anomalous base year
        # (a single unusual year for NVDA mechanically compounded into a
        # ~$33 trillion 10yr figure). Now uses a 3-year revenue CAGR,
        # falling back to 2yr then 1yr YoY only when full history isn't
        # available -- the basis actually used for THIS company is always
        # shown here, never silently hidden.
        growth_basis = row.get("projection_growth_basis")
        growth_warning = row.get("projection_growth_warning")
        basis_label = {"3yr": "3-year revenue CAGR", "2yr": "2-year revenue CAGR",
                       "1yr": "single-year YoY growth", "none": "no growth rate available"}.get(growth_basis, "unknown")
        if growth_warning:
            st.warning(
                f"⚠️ Growth basis used for this company: **{basis_label}**. {growth_warning}")
        else:
            st.caption(
                f"Growth basis used for this company: **{basis_label}**.")

        st.caption("⚠️ +10yr figures are naive extrapolation for illustrative comparison only, "
                   "increasingly unrealistic at longer horizons — see full detail below for "
                   "the +1yr/+5yr intermediate values and every stated assumption.")

        with st.expander("Show 5-year trend & DuPont breakdown"):
            st.markdown("**5-Year Historical Trend**")
            trend = row.get("historical_trend") or []
            if trend:
                if len(trend) < 5:
                    st.caption(f"⚠️ Only {len(trend)} fiscal year(s) available from yfinance for "
                               "this ticker (it typically provides ~4 annual periods, not always "
                               "5) — showing what's available rather than padding or guessing.")
                trend_df = pd.DataFrame(trend).set_index("year")
                trend_df_display = trend_df.rename(columns={
                    "gross_margin": "Gross Margin", "operating_margin": "Operating Margin",
                    "net_margin": "Net Margin", "ebitda_margin": "EBITDA Margin",
                    "roe": "ROE", "roa": "ROA",
                })
                for col in trend_df_display.columns:
                    trend_df_display[col] = trend_df_display[col].apply(
                        lambda v: _fmt(v, "pct"))
                st.dataframe(trend_df_display, use_container_width=True)
            else:
                st.info("No historical trend data available for this ticker.")

            st.markdown("**DuPont Decomposition**")
            st.caption("ROE = Net Margin × Asset Turnover × Equity Multiplier")
            dcol1, dcol2, dcol3, dcol4 = st.columns(4)
            dcol1.metric("Net Margin", _fmt(row.get("net_margin"), "pct"))
            dcol2.metric("Asset Turnover", _fmt(row.get("asset_turnover")))
            dcol3.metric("Equity Multiplier", _fmt(
                row.get("equity_multiplier")))
            dcol4.metric("= DuPont ROE", _fmt(row.get("dupont_roe"), "pct"),
                         help="Cross-check against reported ROE above — won't always match "
                              "exactly due to differing period conventions between yfinance's "
                              ".info ROE and the balance-sheet/income-statement columns used here.")

        with st.expander("Show full projection & analyst estimate detail"):
            st.markdown("**Model Extrapolation — all horizons**")
            if projections:
                proj_table = pd.DataFrame({
                    "Horizon": ["Current", "+1yr", "+5yr", "+10yr"],
                    "Revenue": [_fmt(projections.get(h, {}).get("revenue"), "money") for h in ["current", "1yr", "5yr", "10yr"]],
                    "Net Income": [_fmt(projections.get(h, {}).get("net_income"), "money") for h in ["current", "1yr", "5yr", "10yr"]],
                })
                st.dataframe(proj_table, use_container_width=True,
                             hide_index=True)
            else:
                st.info(
                    "Insufficient data to project revenue/net income for this company.")

            st.markdown(
                f"""<div class="assumption-note">
                <b>Assumptions (stated explicitly):</b><br>
                1. Revenue growth continues at the <b>{basis_label}</b> at every horizon —
                no deceleration/acceleration/business-model-transition modeled. A multi-year
                CAGR (preferred over a single year's YoY rate) meaningfully reduces sensitivity
                to one anomalous base year, but does not eliminate the underlying limitation.<br>
                2. Net margin is assumed <b>constant</b> as revenue grows — no modeled margin
                expansion or compression.
                </div>""",
                unsafe_allow_html=True,
            )

            st.markdown("")
            st.markdown("**Analyst Consensus**")
            st.caption("Source: aggregated analyst estimates via Yahoo Finance — near-term "
                       "only. No 5yr/10yr analyst consensus is fabricated; it doesn't exist "
                       "in standard form.")
            estimates = _cached_analyst_estimates(selected_ticker, today_str)
            if estimates.get("revenue") is not None:
                st.markdown("Revenue estimates:")
                st.dataframe(estimates["revenue"], use_container_width=True)
            if estimates.get("earnings") is not None:
                st.markdown("Earnings estimates:")
                st.dataframe(estimates["earnings"], use_container_width=True)
            if estimates.get("revenue") is None and estimates.get("earnings") is None:
                st.info(estimates.get("error")
                        or "No analyst consensus data available for this ticker.")

            ltg = estimates.get("long_term_growth_rate")
            if ltg is not None and pd.notna(ltg):
                st.metric(
                    "Long-term growth rate estimate (Next 5 Years, per annum)", _fmt(ltg, "pct"))
                st.caption("⚠️ Sourced from yfinance's get_growth_estimates() '+5y' row — "
                           "UNVERIFIED against live data in development. Sanity-check against "
                           "Yahoo Finance's own Analysis tab before trusting this figure.")
            else:
                st.caption("Long-term growth rate estimate: not available from yfinance for "
                           "this ticker.")

    # =========================================================================
    # TAB: Gearing / Debt
    # =========================================================================
    with tab_gearing:
        st.markdown("#### Current")
        liq_cols = st.columns(4)
        liq_cols[0].metric("Current ratio", _fmt(row.get("current_ratio")))
        liq_cols[1].metric("Quick ratio", _fmt(row.get("quick_ratio")))
        liq_cols[2].metric("Debt/Equity", _fmt(row.get("debt_to_equity")))
        liq_cols[3].metric("Interest coverage", _fmt(
            row.get("interest_coverage")))
        if row.get("inventory") is None or pd.isna(row.get("inventory")):
            st.caption("No inventory on the balance sheet (common for software, financials, "
                       "REITs) — quick ratio computed treating inventory as $0, not flagged missing.")

        # LAYOUT FIX: this used to declare st.columns(4) but only assign 3
        # metrics into it, leaving an empty 4th column stretching the other
        # three wider than necessary. Now declares exactly 3.
        liq_cols2 = st.columns(3)
        liq_cols2[0].metric("Net Debt/EBITDA (current)",
                            _fmt(row.get("current_net_debt_ebitda")))
        liq_cols2[1].metric("Net Debt/Market Cap",
                            _fmt(row.get("net_debt_to_market_cap"), "pct"))
        liq_cols2[2].metric("Total Debt/Total Assets",
                            _fmt(row.get("total_debt_to_total_assets"), "pct"))

        st.markdown("")

        st.markdown("#### Altman Z-Score")
        if row.get("less_applicable_sector"):
            st.warning(f"⚠️ {sector} companies are generally considered a poor fit for the "
                       "Altman Z-Score model — it was designed for public manufacturing/"
                       "industrial companies, and financials/REITs have fundamentally "
                       "different balance-sheet structures (limited conventional current "
                       "assets/liabilities, intentionally heavy leverage as a normal part "
                       "of the business). The number below is still computed and shown, "
                       "not hidden, but should be weighted accordingly.")

        zcol1, zcol2 = st.columns(2)
        zcol1.metric("Altman Z-Score", _fmt(row.get("altman_z_score")))
        zcol2.metric("Zone", row.get("altman_zone") or "N/A")
        st.caption(
            "Safe Zone: Z > 2.99 · Grey Zone: 1.81 < Z ≤ 2.99 · Distress Zone: Z ≤ 1.81")

        with st.expander("Show forward projection detail & trajectory chart"):
            st.markdown("**Forward Projections — Leverage**")

            # Same growth basis as the Profitability tab (one growth rate,
            # used consistently everywhere -- never a different rate in
            # different sections). Always shown, never hidden.
            if growth_warning:
                st.warning(
                    f"⚠️ Growth basis used for this company: **{basis_label}**. {growth_warning}")
            else:
                st.caption(
                    f"Growth basis used for this company: **{basis_label}**.")

            st.markdown(
                f"""<div class="assumption-note">
                ⚠️ <b>5yr/10yr figures are naive extrapolation for illustrative comparison
                only, increasingly unrealistic at longer horizons.</b><br><br>
                <b>Assumptions:</b> Net debt held <b>constant</b> at every horizon (no modeled
                paydown, refinancing, or new issuance) · EBITDA margin assumed <b>constant</b>
                as revenue grows · revenue growth continues at the <b>{basis_label}</b>.
                </div>""",
                unsafe_allow_html=True,
            )

            if projections:
                gearing_proj_table = pd.DataFrame({
                    "Horizon": ["Current", "+1yr", "+5yr", "+10yr"],
                    "EBITDA": [_fmt(projections.get(h, {}).get("ebitda"), "money") for h in ["current", "1yr", "5yr", "10yr"]],
                    "Net Debt": [_fmt(projections.get(h, {}).get("net_debt"), "money") for h in ["current", "1yr", "5yr", "10yr"]],
                    "Net Debt/EBITDA": [_fmt(projections.get(h, {}).get("net_debt_ebitda")) for h in ["current", "1yr", "5yr", "10yr"]],
                })
                st.dataframe(gearing_proj_table,
                             use_container_width=True, hide_index=True)
            else:
                st.info("Insufficient data to project leverage for this company.")

            st.markdown("")
            st.markdown(
                "**Revenue vs. Debt Trajectory** (indexed to 100 at Current)")

            rev_ttm = row.get("revenue_ttm")
            net_debt_raw = row.get("net_debt_raw")
            if pd.notna(rev_ttm) and rev_ttm != 0 and pd.notna(net_debt_raw) and projections:
                horizons_labels = ["Current", "+1yr", "+5yr", "+10yr"]
                horizons_keys = ["current", "1yr", "5yr", "10yr"]
                rev_index = []
                for h in horizons_keys:
                    h_rev = projections.get(h, {}).get("revenue")
                    rev_index.append(100.0 * (h_rev / rev_ttm)
                                     if h_rev is not None else None)
                debt_index = [100.0] * len(horizons_keys)

                fig_traj = go.Figure()
                fig_traj.add_trace(go.Scatter(x=horizons_labels, y=rev_index,
                                              mode="lines+markers", name="Revenue (indexed)",
                                              line=dict(color="#4da3ff", width=3)))
                fig_traj.add_trace(go.Scatter(x=horizons_labels, y=debt_index,
                                              mode="lines+markers", name="Net Debt (indexed, held flat)",
                                              line=dict(color="#e0a030", width=3, dash="dash")))
                fig_traj.update_layout(
                    paper_bgcolor=PAGE_BG, plot_bgcolor=PAGE_BG, font=dict(
                        color="white"),
                    yaxis_title="Index (Current = 100)", height=350,
                    legend=dict(orientation="h", yanchor="bottom", y=1.02),
                )
                st.plotly_chart(fig_traj, use_container_width=True)
                st.caption("The debt line is flat by construction under this model's "
                           "net-debt-held-constant assumption — it is not a forecast that "
                           "debt won't change, only a simplification for isolating the "
                           "growth side of the picture.")
            else:
                st.info("Insufficient data to plot the revenue/debt trajectory for this "
                        "company (missing trailing revenue or net debt).")

    # =========================================================================
    # TAB: Valuation
    # =========================================================================
    with tab_valuation:
        if competitor_result.get("flag"):
            st.caption(f"⚠️ {competitor_result['flag']}")

        valuation_metrics = [
            ("P/E (trailing)", "trailing_pe"),
            ("P/E (forward)", "forward_pe"),
            ("EV/EBITDA", "ev_to_ebitda"),
            ("P/B", "price_to_book"),
            ("FCF Yield", "fcf_yield"),
            ("PEG Ratio", "peg_ratio"),
        ]

        val_rows = [{"Metric": label, selected_ticker: _fmt(row.get(key)) if key != "fcf_yield"
                     else _fmt(row.get(key), "pct")} for label, key in valuation_metrics]
        val_df = pd.DataFrame(val_rows).set_index("Metric")

        sector_avg_col = []
        for label, key in valuation_metrics:
            bench = benchmark["metrics"].get(key, {}) if benchmark else {}
            avg = bench.get("average")
            sector_avg_col.append(_fmt(avg, "pct") if key ==
                                  "fcf_yield" else _fmt(avg))
        val_df[f"{sector} Avg (sample)"] = sector_avg_col

        for comp in competitor_result.get("competitors", []):
            comp_ticker = comp.get("ticker", "?")
            comp_col = []
            for label, key in valuation_metrics:
                val = comp.get(key)
                comp_col.append(_fmt(val, "pct") if key ==
                                "fcf_yield" else _fmt(val))
            val_df[comp_ticker] = comp_col

        st.dataframe(val_df, use_container_width=True)
        st.caption("Compared against a live sector-average sample and auto-discovered "
                   "same-sub-industry competitors (see Competitors tab).")

    # =========================================================================
    # TAB: Risk
    # =========================================================================
    with tab_risk:
        st.caption("Volatility/risk context only — this answers \"how much does it move,\" "
                   "never \"is it going up.\" Not a momentum or market-timing signal.")
        rcol1, rcol2, rcol3 = st.columns(3)
        rcol1.metric("Beta", _fmt(row.get("beta")))
        rcol2.metric("Historical Volatility (~3yr, annualized)",
                     _fmt(row.get("volatility_long"), "pct"))
        rcol3.metric("Recent Volatility (~90d, annualized)",
                     _fmt(row.get("volatility_recent"), "pct"))

    # =========================================================================
    # TAB: Value Creation (ROIC vs. WACC)
    # =========================================================================
    with tab_value_creation:
        st.caption("Every input is a visibly disclosed assumption, not a hidden constant "
                   "(see the expander below for the full calculation).")

        with st.spinner("Fetching risk-free rate..."):
            risk_free_rate, rf_is_live = _cached_risk_free_rate(today_str)

        vcr = vc.compute_value_creation(raw, risk_free_rate, rf_is_live)

        # LAYOUT FIX: a single metric card used to be forced into a
        # 1-of-3-width column (st.columns([1,2])) alongside the detail
        # bullets, which is unnecessary now that the detail moved into an
        # expander -- render the headline directly, letting it size
        # naturally instead of stretching an isolated card across a
        # partial-width column.
        st.metric("ROIC − WACC Spread", _fmt(vcr["spread"], "pct"))
        if vcr["spread"] is not None:
            verdict = "creating value above its cost of capital" if vcr["spread"] > 0 else \
                      "not covering its cost of capital"
            st.caption(f"Positive spread = creating value above cost of capital. "
                       f"This company is currently **{verdict}**.")
        else:
            st.caption(
                "Insufficient data to compute the spread for this company.")

        with st.expander("Show ROIC/WACC calculation detail"):
            detail_col1, detail_col2 = st.columns(2)
            with detail_col1:
                st.markdown("**ROIC inputs**")
                st.markdown(
                    f"- EBIT: {_fmt(vcr['ebit'], 'money')}\n"
                    f"- Tax rate assumption: {_fmt(vcr['tax_rate_assumption'], 'pct')} (flat, stated)\n"
                    f"- NOPAT = EBIT × (1 − tax rate): {_fmt(vcr['nopat'], 'money')}\n"
                    f"- Invested Capital = Total Debt + Total Equity − Cash: {_fmt(vcr['invested_capital'], 'money')}\n"
                    f"- **ROIC = NOPAT / Invested Capital: {_fmt(vcr['roic'], 'pct')}**"
                )
            with detail_col2:
                st.markdown("**WACC inputs**")
                rf_label = "live" if vcr[
                    "risk_free_rate_is_live"] else "static assumption (live fetch failed)"
                st.markdown(
                    f"- Risk-free rate ({rf_label}): {_fmt(vcr['risk_free_rate'], 'pct')}\n"
                    f"- Beta: {_fmt(vcr['beta'])}\n"
                    f"- Equity risk premium assumption: {_fmt(vcr['equity_risk_premium_assumption'], 'pct')} (flat, stated)\n"
                    f"- Cost of Equity (CAPM) = risk-free + Beta × ERP: {_fmt(vcr['cost_of_equity'], 'pct')}\n"
                    f"- Interest expense: {_fmt(vcr['interest_expense'], 'money')} / Total Debt: {_fmt(vcr['total_debt'], 'money')}\n"
                    f"- Cost of Debt ≈ Interest Expense / Total Debt: {_fmt(vcr['cost_of_debt'], 'pct')}\n"
                    f"- Weighted by Market Cap ({_fmt(vcr['market_cap'], 'money')}) vs. Total Debt, "
                    f"after-tax cost of debt (same {_fmt(vcr['tax_rate_assumption'], 'pct')} tax assumption)\n"
                    f"- **WACC: {_fmt(vcr['wacc'], 'pct')}**"
                )

    # =========================================================================
    # TAB: Competitors
    # =========================================================================
    with tab_competitors:
        if competitor_result.get("competitors"):
            st.caption(f"{competitor_result['candidate_count']} same-sub-industry candidate(s) "
                       f"found within the S&P 500 classification file; showing "
                       f"{len(competitor_result['competitors'])}.")
            comp_rows = []
            for comp in competitor_result["competitors"]:
                comp_rows.append({
                    "Ticker": comp.get("ticker"),
                    "Company Name": comp.get("company_name") or "N/A",
                    "Market Cap": _fmt(comp.get("market_cap"), "money"),
                    "Revenue": _fmt(comp.get("revenue_ttm"), "money"),
                    "EV": _fmt(comp.get("enterprise_value"), "money"),
                    "P/E": _fmt(comp.get("trailing_pe")),
                    "EV/EBITDA": _fmt(comp.get("ev_to_ebitda")),
                })
            st.dataframe(pd.DataFrame(comp_rows),
                         use_container_width=True, hide_index=True)
        else:
            st.info(competitor_result.get("flag")
                    or "No competitors found for this ticker.")

    # =========================================================================
    # TAB: Ownership & Dilution
    # =========================================================================
    with tab_ownership:
        ocol1, ocol2, ocol3 = st.columns(3)
        ocol1.metric("Insider Ownership", _fmt(
            row.get("insider_ownership_pct"), "pct"))
        ocol2.metric("Institutional Ownership", _fmt(
            row.get("institutional_ownership_pct"), "pct"))

        target_price = row.get("analyst_target_mean_price")
        current_price = row.get("current_price")
        if target_price is not None and current_price:
            upside = (target_price / current_price) - 1
            ocol3.metric("Analyst Price Target", _fmt(
                target_price, "price"), _fmt(upside, "pct"))
        else:
            ocol3.metric("Analyst Price Target", "N/A")

        st.markdown(
            "**Diluted Shares Outstanding — 5yr trend** (rising = dilution, falling = buybacks)")
        shares_trend = row.get("diluted_shares_trend") or []
        if shares_trend:
            if len(shares_trend) < 5:
                st.caption(
                    f"⚠️ Only {len(shares_trend)} fiscal year(s) available from yfinance.")
            shares_df = pd.DataFrame(shares_trend).set_index("year")
            shares_df["diluted_shares"] = shares_df["diluted_shares"].apply(
                lambda v: _fmt(v, "num", 0))
            st.dataframe(shares_df.rename(
                columns={"diluted_shares": "Diluted Shares"}), use_container_width=True)
        else:
            st.info("No diluted shares trend data available for this ticker.")

    # =========================================================================
    # TAB: Sector/Market
    # =========================================================================
    with tab_sector:
        scol_a, scol_b = st.columns(2)

        with scol_a:
            st.markdown("**Trailing sector growth (live, sampled)**")
            growth_bench = benchmark["metrics"].get(
                "revenue_growth_yoy", {}) if sector else {}
            sector_avg_growth = growth_bench.get("average")
            n_valid_growth = growth_bench.get("n_valid", 0)
            st.metric("This company's revenue growth (YoY)",
                      _fmt(row.get("revenue_growth_yoy"), "pct"))
            st.metric(f"{sector} sector avg (sample of {n_valid_growth})", _fmt(
                sector_avg_growth, "pct"))
            if sector_avg_growth is not None and pd.notna(row.get("revenue_growth_yoy")):
                st.metric("Sector-relative growth",
                          _fmt(row.get("revenue_growth_yoy") - sector_avg_growth, "pct"))
            else:
                st.metric("Sector-relative growth", "N/A")

        with scol_b:
            st.markdown(
                "**Published 10-year sector growth forecast (static, manually sourced)**")
            forecast = get_sector_forecast(sector)
            st.metric("10yr forecast CAGR", forecast["display"])
            st.caption(f"Source: {forecast['source']}")

    st.markdown("---")

    missing = _to_list(row.get("missing_fields"))
    if missing:
        st.warning(
            f"Missing/flagged metrics for {selected_ticker}: " + ", ".join(missing))
    else:
        st.success("No missing metrics for this company.")
