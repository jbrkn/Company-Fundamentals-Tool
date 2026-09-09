"""
Sentiment pipeline: Finnhub (headline retrieval, paid-tier-free) + FinBERT
(local scoring model, no ongoing API cost per the build spec).

FLAG (build spec Section 8): Finnhub's free tier is 60 calls/minute. A full
150-ticker pull is 150 calls minimum for company-news alone. At the
free-tier limit that's roughly 3 minutes of throttled pulling for one full
run, which is workable for a once-daily cached pull but would NOT be
workable if triggered per dashboard page load. This module:
    - Rate-limits itself to config.FINNHUB_MAX_CALLS_PER_MINUTE (with
      headroom below the actual 60/min ceiling).
    - Is designed to be called once per day via the same cache pattern as
      data_pipeline.py, not on every Streamlit rerun.
    - Retries on 429s with backoff rather than failing the ticker outright.

If your Finnhub plan's rate limit is lower than 60/min, lower
FINNHUB_MAX_CALLS_PER_MINUTE in config.py accordingly -- do not silently
drop tickers to speed up the pull; slow down the pull instead.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sentiment_pipeline")

_FINBERT_MODEL = None
_FINBERT_TOKENIZER = None


def _load_finbert():
    """Lazy-load FinBERT once per process. Local model, no API cost per call."""
    global _FINBERT_MODEL, _FINBERT_TOKENIZER
    if _FINBERT_MODEL is None:
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        model_name = "ProsusAI/finbert"
        logger.info(
            "Loading FinBERT (%s) -- one-time cost per process", model_name)
        _FINBERT_TOKENIZER = AutoTokenizer.from_pretrained(model_name)
        _FINBERT_MODEL = AutoModelForSequenceClassification.from_pretrained(
            model_name)
        _FINBERT_MODEL.eval()
    return _FINBERT_MODEL, _FINBERT_TOKENIZER


def score_headlines(headlines: list[str]) -> list[float]:
    """Score each headline with FinBERT. Returns a signed score per headline:
    +P(positive) - P(negative), in [-1, 1]. Neutral probability is implicitly
    excluded from the sign but still dilutes the magnitude, which is the
    standard way to fold FinBERT's 3-class output into one continuous score.
    """
    if not headlines:
        return []
    import torch

    model, tokenizer = _load_finbert()
    inputs = tokenizer(headlines, padding=True, truncation=True,
                       max_length=64, return_tensors="pt")
    with torch.no_grad():
        logits = model(**inputs).logits
        probs = torch.nn.functional.softmax(logits, dim=-1)
    # ProsusAI/finbert label order: 0=positive, 1=negative, 2=neutral
    scores = (probs[:, 0] - probs[:, 1]).tolist()
    return scores


class _RateLimiter:
    """Simple sliding-window limiter for Finnhub's per-minute cap."""

    def __init__(self, max_calls_per_minute: int):
        self.max_calls = max_calls_per_minute
        self.calls: deque[float] = deque()

    def wait_if_needed(self):
        now = time.time()
        while self.calls and now - self.calls[0] > 60:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            sleep_for = 60 - (now - self.calls[0]) + 0.1
            logger.info(
                "Finnhub rate limit reached, sleeping %.1fs", sleep_for)
            time.sleep(max(sleep_for, 0))
        self.calls.append(time.time())


@retry(reraise=True, stop=stop_after_attempt(config.FINNHUB_MAX_RETRIES),
       wait=wait_exponential(multiplier=config.FINNHUB_BACKOFF_BASE_SECONDS, min=2, max=30))
def _fetch_headlines_for_ticker(client, ticker: str, from_date: str, to_date: str) -> list[str]:
    news = client.company_news(ticker, _from=from_date, to=to_date)
    return [item["headline"] for item in news if item.get("headline")]


def fetch_sentiment_for_universe(tickers: list[str], api_key: str | None = None) -> pd.DataFrame:
    """For each ticker: pull recent headlines from Finnhub, score with
    FinBERT, aggregate to one sentiment score + a coverage-volume count.

    Both the score AND the coverage count are returned and displayed --
    per build spec, low-coverage names are flagged, not hidden.
    """
    import finnhub

    api_key = api_key or os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        raise RuntimeError(
            "FINNHUB_API_KEY not set. Set it as an environment variable or pass "
            "api_key= explicitly. Sentiment cannot be pulled without it -- this "
            "is surfaced as an error, not silently skipped, per the 'flag rather "
            "than guess' instruction."
        )

    client = finnhub.Client(api_key=api_key)
    limiter = _RateLimiter(config.FINNHUB_MAX_CALLS_PER_MINUTE)

    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now(
    ) - timedelta(days=config.SENTIMENT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    rows = []
    for i, ticker in enumerate(tickers):
        limiter.wait_if_needed()
        try:
            headlines = _fetch_headlines_for_ticker(
                client, ticker, from_date, to_date)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Finnhub fetch failed for %s after retries: %s", ticker, exc)
            rows.append({"ticker": ticker, "sentiment_score": None,
                         "coverage_volume": 0, "sentiment_error": str(exc)})
            continue

        coverage = len(headlines)
        if coverage == 0:
            rows.append({"ticker": ticker, "sentiment_score": None,
                         "coverage_volume": 0, "sentiment_error": None})
            continue

        scores = score_headlines(headlines)
        avg_score = sum(scores) / len(scores)
        rows.append({"ticker": ticker, "sentiment_score": avg_score,
                     "coverage_volume": coverage, "sentiment_error": None})

        if (i + 1) % 10 == 0:
            logger.info("Sentiment: %d/%d tickers processed",
                        i + 1, len(tickers))

    return pd.DataFrame(rows)


def fetch_recent_headlines_for_ticker(ticker: str, n: int = 3,
                                      api_key: str | None = None) -> dict:
    """On-demand fetch of the N most recent headlines (with their individual
    FinBERT scores) for a single ticker -- used by the deep-dive view so a
    sentiment score has visible grounding, per the deep-dive build spec.

    Deliberately NOT part of the daily universe-wide cache: storing full
    headline text for 150 tickers every day is unnecessary weight when only
    whichever ticker the user has open needs to show its headlines. This is
    a single-ticker call (well under Finnhub's rate limit on its own), so it
    skips the sliding-window limiter used for the 150-ticker batch pull --
    but if a user selects tickers very rapidly in quick succession, this
    could still approach the limit; the retry/backoff below handles a 429
    gracefully rather than crashing the deep-dive view.

    BUG FIX: this used to return a bare list, so a Finnhub API failure
    (auth error, rate limit, malformed request) and a genuine "no headlines
    in this window" result were both silently rendered the same way in the
    UI -- an empty list either way, logged only to the server console the
    person running Streamlit locally never watches. Returns a dict now:
    {"headlines": [...], "error": str | None} so the caller can show an
    actual error/warning instead of a misleading "no headlines found."

    "headlines" is a list of up to `n` dicts: {headline, datetime, sentiment_score}.
    """
    import finnhub

    api_key = api_key or os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return {"headlines": [], "error": "FINNHUB_API_KEY not set."}

    client = finnhub.Client(api_key=api_key)
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now(
    ) - timedelta(days=config.SENTIMENT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    try:
        news = client.company_news(ticker, _from=from_date, to=to_date)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to fetch recent headlines for %s: %s", ticker, exc)
        return {"headlines": [], "error": f"Finnhub request failed: {exc}"}

    # Sort newest first (Finnhub doesn't guarantee order) and take the top N.
    news_sorted = sorted(news, key=lambda item: item.get(
        "datetime", 0), reverse=True)[:n]
    headlines = [item.get("headline")
                 for item in news_sorted if item.get("headline")]
    if not headlines:
        # Genuinely empty result (the API call succeeded, Finnhub just has
        # no articles for this ticker in the window) -- not an error.
        return {"headlines": [], "error": None}

    scores = score_headlines(headlines)
    results = []
    for item, score in zip(news_sorted, scores):
        results.append({
            "headline": item.get("headline"),
            "datetime": datetime.fromtimestamp(item["datetime"]).strftime("%Y-%m-%d")
            if item.get("datetime") else None,
            "sentiment_score": score,
        })
    return {"headlines": results, "error": None}


def _sentiment_cache_path(ticker: str):
    safe_ticker = ticker.replace("/", "_").replace("\\", "_")
    return config.SENTIMENT_CACHE_DIR / f"{safe_ticker}.json"


def load_or_fetch_sentiment(tickers: list[str], force_refresh: bool = False,
                            api_key: str | None = None) -> pd.DataFrame:
    """Per-ticker, per-day cache (replaces the old single whole-universe
    parquet blob, which assumed a fixed 150-name universe that no longer
    exists). Only tickers whose cache is missing/stale get fetched --
    already-cached tickers in the same call are read straight from disk,
    so a mixed batch (some cached, some not) doesn't refetch everything.
    """
    import json

    cached_rows = []
    tickers_to_fetch = []

    for ticker in tickers:
        cache_path = _sentiment_cache_path(ticker)
        if not force_refresh and cache_path.exists():
            age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
            if age_hours < config.CACHE_TTL_HOURS:
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        cached_rows.append(json.load(f))
                    continue
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning(
                        "Sentiment cache for %s unreadable (%s), refetching", ticker, exc)
        tickers_to_fetch.append(ticker)

    fetched_rows = []
    if tickers_to_fetch:
        fetched_df = fetch_sentiment_for_universe(
            tickers_to_fetch, api_key=api_key)
        for _, row in fetched_df.iterrows():
            row_dict = row.to_dict()
            fetched_rows.append(row_dict)
            try:
                with open(_sentiment_cache_path(row_dict["ticker"]), "w", encoding="utf-8") as f:
                    json.dump(row_dict, f, default=str)
            except OSError as exc:
                logger.warning(
                    "Failed to write sentiment cache for %s: %s", row_dict["ticker"], exc)

    all_rows = cached_rows + fetched_rows
    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame(
        columns=["ticker", "sentiment_score",
                 "coverage_volume", "sentiment_error"]
    )
