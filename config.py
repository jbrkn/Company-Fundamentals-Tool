"""
Central configuration for the Company Fundamental Analysis Tool.

Nothing in this file pulls data. It's just constants, so the rest of the
codebase has one place to look for cache TTLs, sampling sizes, etc.

REPOSITIONED (final consolidated spec): this tool no longer ranks/screens a
fixed 150-company universe. It's a single-company fundamental analysis page
with live free-text search, sector-average benchmarking via a lightweight
sample, and automatic same-sub-industry competitor discovery. Removed
entirely: UNIVERSE_SIZE, UNIVERSE_FILE, CATEGORY_WEIGHTS, LOWER_IS_BETTER,
WINSORIZE_LIMIT -- all scoring-engine config, since scoring.py itself is
deleted.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).parent
DATA_DIR = ROOT_DIR / "data"
CACHE_DIR = ROOT_DIR / "cache"
# one small file per ticker, not one big blob
TICKER_CACHE_DIR = CACHE_DIR / "tickers"

# Static classification file: Ticker, Sector (broad GICS), Sub-Industry
# (narrow GICS). Built once via scripts/build_classification.py. Holds NO
# financial data -- classification only, used for sector-average sampling
# and same-sub-industry competitor discovery. Replaces the old
# universe_top150.csv (which no longer exists -- there's no fixed "top 150"
# concept anymore; any valid US ticker can be searched).
CLASSIFICATION_FILE = DATA_DIR / "sp500_classification.csv"

for _d in (DATA_DIR, CACHE_DIR, TICKER_CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Caching / refresh policy
# ---------------------------------------------------------------------------
# Per-ticker fundamentals are cached individually (one small file per
# ticker, keyed by ticker+date) rather than one giant universe-wide blob --
# this fits the new usage pattern (single searched company + a handful of
# sector-sample peers + a handful of competitors on demand), not "load all
# 150 up front."
CACHE_TTL_HOURS = 20  # effectively "once per day"; below is a safety margin

# ---------------------------------------------------------------------------
# Rate limiting / retry (yfinance is semi-official/free-tier and
# intermittently throttles or drops fields with no clean error signal)
# ---------------------------------------------------------------------------
YF_MAX_RETRIES = 4
YF_BACKOFF_BASE_SECONDS = 2.0        # exponential backoff: base * 2**attempt
YF_INTER_TICKER_SLEEP_SECONDS = 0.15  # small pause between sequential calls

# ---------------------------------------------------------------------------
# Price history window
# ---------------------------------------------------------------------------
# The full price history fetched per ticker is 3 years (see data_pipeline.py
# _pull_ticker_frames) -- a single yfinance call, extended from the
# original 450 days once volatility needed a genuinely multi-year window.
# These two windows both slice that same 3-year pull -- no extra fetches.
VOLATILITY_LONG_WINDOW_DAYS = 3 * 365   # "historical" volatility (~3yr)
VOLATILITY_RECENT_WINDOW_DAYS = 90       # "current/recent" volatility (~90d)
TRADING_DAYS_PER_YEAR = 252              # for annualizing daily-return stdev

# ---------------------------------------------------------------------------
# Sector-average benchmarking (new infrastructure, replaces the old
# 150-universe groupby-mean sector math)
# ---------------------------------------------------------------------------
# How many same-broad-sector peers to sample (live/cached) when computing a
# sector-average benchmark for the viewed company. Not every company in a
# sector is pulled -- that would reintroduce the old "precompute the whole
# universe" cost this redesign is explicitly avoiding. A sample is a
# deliberate accuracy/cost tradeoff: flagged here, not hidden.
SECTOR_BENCHMARK_SAMPLE_SIZE = 12

# ---------------------------------------------------------------------------
# Competitor discovery (new)
# ---------------------------------------------------------------------------
COMPETITOR_MIN_TARGET = 3
COMPETITOR_MAX_TARGET = 5
COMPETITOR_CANDIDATE_THRESHOLD = 5  # if same-sub-industry matches exceed this,
# rank by market-cap proximity and trim

# ---------------------------------------------------------------------------
# Value Creation / WACC assumptions (Section 3.7) -- FLAT, STATED assumptions,
# not fitted or unilaterally chosen without disclosure. Every one of these is
# displayed directly next to the calculation that uses it in the UI, per the
# spec's explicit "nothing here should be a hidden constant" instruction.
# ---------------------------------------------------------------------------
# Flat corporate tax rate assumption for NOPAT (EBIT x (1 - tax_rate)) and
# the after-tax cost of debt in WACC. A flat rate is used instead of each
# company's effective tax rate, which is noisy (one-time items, credits,
# jurisdiction mix) -- exactly the same "flag rather than guess" reasoning
# already applied to the separate *simple* ROIC figure in Profitability,
# which is why that one stays "not computed" while this one is computable:
# the assumption here is visible and stated, not hidden.
WACC_TAX_RATE_ASSUMPTION = 0.21  # US federal corporate rate

# Equity risk premium for CAPM (Cost of Equity = risk-free rate + Beta x ERP).
# 5% is a commonly cited long-run US equity risk premium estimate; reasonable
# analysts differ on this by a percentage point or two either way -- shown
# explicitly in the UI as an assumption, not presented as a precise figure.
EQUITY_RISK_PREMIUM_ASSUMPTION = 0.05

# Risk-free rate: attempt a LIVE fetch of the 10-Year Treasury yield
# (^TNX) first; if that fails (rate limit, ticker unavailable, etc.), fall
# back to this static assumption. Whichever was actually used is labeled
# in the UI ("Live" vs "Static assumption") -- this is the "fetch or use a
# flat assumption, state which" tradeoff the spec asked to be flagged
# rather than decided silently.
# Static fallback sourced from a point-in-time market check (Trading
# Economics, early September 2026: 10yr UST yield ~4.75-4.79%) -- this WILL
# drift out of date; it exists only as a last-resort fallback when the live
# fetch fails, not as the primary source of truth.
RISK_FREE_RATE_STATIC_FALLBACK = 0.0475

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
TEXT_MISSING_LABEL = "N/A"
