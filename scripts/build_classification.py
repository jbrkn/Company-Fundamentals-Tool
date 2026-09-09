"""
ONE-TIME SETUP SCRIPT -- run this manually, once, to produce
data/sp500_classification.csv.

Usage:
    python3 scripts/build_classification.py

Re-run manually (e.g. quarterly) to refresh classifications as S&P 500
membership or GICS sub-industry assignments change -- this is a deliberate
manual step, not automated, consistent with the original build's
"do not scrape on every run" instruction.

WHAT CHANGED FROM THE OLD build_universe.py:
    The old script fetched market cap for all ~500 tickers via yfinance
    (one API call per ticker) specifically to rank and cut down to a fixed
    "top 150 by market cap" universe. That whole concept is gone -- this
    tool now supports searching ANY valid US ticker live, not just a fixed
    list. This file exists purely as a CLASSIFICATION reference (Ticker,
    Sector, Sub-Industry) used for two things: sampling sector-average
    benchmark peers, and discovering same-sub-industry competitors. It
    holds NO financial data and requires NO yfinance calls at all -- the
    Wikipedia table already has GICS Sector and GICS Sub-Industry columns,
    so this script is now just a scrape + column rename, no per-ticker
    market-cap loop. Meaningfully faster and simpler than before.

FLAG (unchanged from before): Wikipedia's S&P 500 page is not an official
index feed -- it lags real constituent changes by days to weeks around
index reconstitutions. If you have a paid index membership feed available,
swap the source in `_fetch_sp500_classification` below for that instead.
"""

import io
import sys
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import CLASSIFICATION_FILE  # noqa: E402


def _fetch_sp500_classification() -> pd.DataFrame:
    """Pull current S&P 500 Ticker/Sector/Sub-Industry from Wikipedia.

    Returns a DataFrame with columns: ticker, name, sector, sub_industry.
    """
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    # Wikipedia blocks pandas' default request (no User-Agent header) with a
    # 403 -- fetch with a browser-like User-Agent first, then hand the HTML
    # to pd.read_html via io.StringIO (passing the raw string directly makes
    # pandas try to open it AS a filename, not parse it as HTML content).
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    tables = pd.read_html(io.StringIO(response.text))

    raw = tables[0]
    sp500 = raw[["Symbol", "Security", "GICS Sector", "GICS Sub-Industry"]].rename(
        columns={
            "Symbol": "ticker",
            "Security": "name",
            "GICS Sector": "sector",
            "GICS Sub-Industry": "sub_industry",
        }
    )
    # yfinance uses '-' where Wikipedia uses '.' for share classes (e.g. BRK.B -> BRK-B)
    sp500["ticker"] = sp500["ticker"].str.replace(".", "-", regex=False)
    return sp500


def main() -> None:
    print("Fetching S&P 500 classification (Ticker/Sector/Sub-Industry) from Wikipedia...")
    classification = _fetch_sp500_classification()
    print(f"  Found {len(classification)} constituents.")

    n_sectors = classification["sector"].nunique()
    n_sub_industries = classification["sub_industry"].nunique()
    print(
        f"  {n_sectors} distinct sectors, {n_sub_industries} distinct sub-industries.")

    missing_sector = classification["sector"].isna().sum()
    missing_sub = classification["sub_industry"].isna().sum()
    if missing_sector or missing_sub:
        print(f"  [FLAG] {missing_sector} rows missing sector, {missing_sub} rows missing "
              f"sub-industry -- these companies won't be usable for sector-benchmark "
              f"sampling or competitor discovery until this is investigated.")

    classification["pull_date"] = pd.Timestamp.today().strftime("%Y-%m-%d")
    classification.to_csv(CLASSIFICATION_FILE, index=False)
    print(f"\nWrote {len(classification)} rows to {CLASSIFICATION_FILE}")
    print("This file is classification-only (no financial data). The dashboard reads it "
          "for sector-average sampling and competitor discovery, and re-fetches actual "
          "financials live/on-demand for whichever tickers are actually needed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 -- top-level guard: clean failure, non-zero exit
        print(f"\n[FAILED] build_classification.py did not complete: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        sys.exit(1)
