"""
Classification-based competitor discovery and sector-average benchmarking.

FILE NAMING NOTE: the file checklist for this rebuild specified
`competitors.py` for competitor discovery, but didn't list a separate file
for sector-average benchmarking (the "sample same-SECTOR peers and average
their metrics" mechanism used throughout the Profitability/Gearing/
Valuation sections). Both mechanisms share the same static classification
file and nearly all their supporting code (load_classification,
get_classification_for_ticker), so rather than either dropping sector
benchmarking or inventing an undocumented extra file, this module keeps
both under the `competitors.py` name. Flagged here explicitly rather than
silently deciding -- split this into two files if you'd prefer a stricter
one-file-one-responsibility split.

Both use the static classification file (data/sp500_classification.csv --
Ticker/Sector/Sub-Industry ONLY, no financial data) to decide WHICH tickers
to look at, then fetch those tickers' actual financials live/on-demand via
data_pipeline.load_or_fetch_many -- never precomputed for the whole S&P 500.

Two responsibilities:
    1. Sector-average benchmark: sample a handful of companies sharing the
       viewed company's broad GICS Sector, fetch lightweight metrics, and
       average them for comparison context.
    2. Competitor discovery: find companies sharing the viewed company's
       narrow GICS Sub-Industry (excluding itself). If more than
       COMPETITOR_CANDIDATE_THRESHOLD matches exist, rank by market-cap
       proximity and take the closest COMPETITOR_MIN_TARGET-
       COMPETITOR_MAX_TARGET. If fewer, show whatever's available.

FLAG: if a searched ticker isn't in the classification file (e.g. it's not
an S&P 500 constituent), competitor discovery and sector-benchmark sampling
both fall back to fetching that ticker's own sector directly from yfinance
(so the page doesn't break), but competitor matching in particular may not
find good matches without classification data for the *rest* of that
sector/sub-industry -- flagged in the UI, not silently degraded.
"""

from __future__ import annotations

import logging

import pandas as pd

import config
from data_pipeline import load_or_fetch_many, load_or_fetch_ticker

logger = logging.getLogger("competitors")

_classification_cache: pd.DataFrame | None = None


def load_classification() -> pd.DataFrame:
    """Load the static classification file. Cached in-process (it's small
    and doesn't change during a session) -- re-read from disk only once per
    process, not once per function call.
    """
    global _classification_cache
    if _classification_cache is None:
        if not config.CLASSIFICATION_FILE.exists():
            raise FileNotFoundError(
                f"Classification file not found at {config.CLASSIFICATION_FILE}. "
                "Run scripts/build_classification.py once to generate it."
            )
        _classification_cache = pd.read_csv(config.CLASSIFICATION_FILE)
    return _classification_cache


def get_classification_for_ticker(ticker: str) -> dict:
    """Look up a ticker's sector/sub_industry from the static file. If not
    found (e.g. a non-S&P name), fall back to a live yfinance lookup for
    that one ticker only -- flagged so the caller knows competitor
    matching may be degraded.
    """
    classification = load_classification()
    match = classification[classification["ticker"] == ticker]
    if not match.empty:
        row = match.iloc[0]
        return {
            "ticker": ticker,
            "sector": row.get("sector"),
            "sub_industry": row.get("sub_industry"),
            "in_classification_file": True,
        }

    logger.info(
        "%s not found in classification file -- fetching sector directly", ticker)
    fetched = load_or_fetch_ticker(ticker)
    return {
        "ticker": ticker,
        "sector": fetched.get("sector"),
        "sub_industry": None,  # yfinance .info doesn't provide GICS sub-industry
        "in_classification_file": False,
    }


def sample_sector_peers(sector: str, exclude_ticker: str,
                        sample_size: int | None = None) -> list[str]:
    """Sample up to `sample_size` tickers sharing `sector` (excluding
    `exclude_ticker`) from the classification file. Sampling, not a full
    sector pull -- see config.SECTOR_BENCHMARK_SAMPLE_SIZE for the
    accuracy/cost tradeoff this represents.
    """
    sample_size = sample_size or config.SECTOR_BENCHMARK_SAMPLE_SIZE
    classification = load_classification()
    peers = classification[
        (classification["sector"] == sector) & (
            classification["ticker"] != exclude_ticker)
    ]
    if peers.empty:
        return []
    # Deterministic sample (not random) so repeated views of the same
    # sector are cache-friendly -- random sampling would mean a different
    # set of peer tickers (and therefore cache misses) on every rerun.
    sampled = peers.sort_values("ticker").head(sample_size)
    return sampled["ticker"].tolist()


def compute_sector_benchmark(sector: str, exclude_ticker: str) -> dict:
    """Fetch a sample of sector peers and average a handful of lightweight
    metrics for comparison context. Returns a dict of {metric: average}
    plus the actual sample tickers used and how many had valid data per
    metric (transparency: an average of 3 valid values out of 12 sampled
    should read differently than an average of 12/12).
    """
    peer_tickers = sample_sector_peers(sector, exclude_ticker)
    if not peer_tickers:
        return {"sample_tickers": [], "n_sampled": 0, "metrics": {}}

    peer_df = load_or_fetch_many(peer_tickers)

    metrics_to_average = [
        "trailing_pe", "forward_pe", "price_to_book", "ev_to_ebitda",
        "fcf_yield", "gross_margin", "operating_margin", "net_margin",
        "ebitda_margin", "roe", "roa", "debt_to_equity",
        "current_net_debt_ebitda", "revenue_growth_yoy",
    ]
    metrics = {}
    for m in metrics_to_average:
        if m not in peer_df.columns:
            metrics[m] = {"average": None, "n_valid": 0}
            continue
        valid = pd.to_numeric(peer_df[m], errors="coerce").dropna()
        metrics[m] = {
            "average": float(valid.mean()) if not valid.empty else None,
            "n_valid": int(len(valid)),
        }

    return {
        "sample_tickers": peer_tickers,
        "n_sampled": len(peer_tickers),
        "metrics": metrics,
    }


def discover_competitors(sub_industry: str | None, exclude_ticker: str,
                         target_market_cap: float | None) -> dict:
    """Find same-sub-industry competitors. If sub_industry is None (ticker
    wasn't in the classification file), returns an empty result with a flag
    rather than guessing.

    Ranking: if candidate count exceeds COMPETITOR_CANDIDATE_THRESHOLD, rank
    by market-cap proximity to `target_market_cap` and take the closest
    COMPETITOR_MIN_TARGET-COMPETITOR_MAX_TARGET. If fewer candidates exist
    than the threshold, show all available candidates (up to
    COMPETITOR_MAX_TARGET) rather than padding with irrelevant matches.
    """
    if not sub_industry:
        return {
            "competitors": [],
            "candidate_count": 0,
            "flag": "No sub-industry classification available for this ticker "
                    "(likely not an S&P 500 constituent) -- automatic competitor "
                    "matching isn't possible without it.",
        }

    classification = load_classification()
    candidates = classification[
        (classification["sub_industry"] == sub_industry)
        & (classification["ticker"] != exclude_ticker)
    ]
    candidate_tickers = candidates["ticker"].tolist()
    candidate_count = len(candidate_tickers)

    if candidate_count == 0:
        return {
            "competitors": [],
            "candidate_count": 0,
            "flag": f"No other companies found in sub-industry '{sub_industry}' "
            "within the S&P 500 classification file.",
        }

    if candidate_count <= config.COMPETITOR_CANDIDATE_THRESHOLD:
        chosen = candidate_tickers[: config.COMPETITOR_MAX_TARGET]
        flag = (f"Only {candidate_count} same-sub-industry candidate(s) found "
                f"within the S&P 500 -- showing all available." if candidate_count < config.COMPETITOR_MIN_TARGET
                else None)
    else:
        # Rank by market-cap proximity -- fetch market caps for all
        # candidates, then take the closest N to the target's market cap.
        if target_market_cap is None:
            # No target market cap to rank against -- fall back to the
            # first N candidates alphabetically rather than guessing a
            # ranking, and flag this explicitly.
            chosen = sorted(candidate_tickers)[: config.COMPETITOR_MAX_TARGET]
            flag = ("Target company's market cap unavailable -- competitors "
                    "shown are the first available matches, not ranked by "
                    "market-cap proximity.")
        else:
            candidate_df = load_or_fetch_many(candidate_tickers)
            candidate_df["market_cap"] = pd.to_numeric(
                candidate_df.get("market_cap"), errors="coerce")
            candidate_df["cap_distance"] = (
                candidate_df["market_cap"] - target_market_cap).abs()
            candidate_df = candidate_df.dropna(
                subset=["market_cap"]).sort_values("cap_distance")
            chosen = candidate_df["ticker"].head(
                config.COMPETITOR_MAX_TARGET).tolist()
            flag = None

    competitor_df = load_or_fetch_many(chosen) if chosen else pd.DataFrame()

    return {
        "competitors": competitor_df.to_dict("records") if not competitor_df.empty else [],
        "candidate_count": candidate_count,
        "flag": flag,
    }
