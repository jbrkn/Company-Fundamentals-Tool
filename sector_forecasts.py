"""
Static, manually-sourced 10-year sector growth forecast reference.

THIS IS NOT LIVE DATA. Genuine 10-year sector growth forecasts require paid
research subscriptions (Statista, IBISWorld, McKinsey/Deloitte/Gartner
reports, etc.) that are not accessible via free APIs and cannot be
responsibly fabricated. Every entry below MUST be manually researched and
filled in by a human, with a real, named, checkable public source.

Until a sector's entry is filled in, the dashboard displays "Not yet
sourced -- see README" rather than a blank, a zero, or a guessed number.
A zero or blank would be silently misread as "no growth expected" or
"missing data" -- neither is true; it simply hasn't been researched yet.

HOW TO FILL THIS IN:
    1. Find a real, publicly citable long-term (~10yr) growth forecast for
       the sector (a market-research report abstract, an industry
       association publication, a bank/consultancy research note, etc. --
       doesn't need to be paywalled, just needs to be real and attributable).
    2. Set "cagr" to the compound annual growth rate as a decimal (e.g. 0.08
       for 8%).
    3. Set "source" to a specific, named citation -- publisher, report title
       (or topic), and year at minimum. "TODO" placeholders are treated as
       "not yet sourced" by get_sector_forecast() below and will NOT display
       a number even if you're tempted to fill in just the cagr without a
       real source.

Sector names below match the 11 standard GICS sectors, which is also what
yfinance's `.info['sector']` field returns.
"""

from __future__ import annotations

SECTOR_10YR_GROWTH_FORECAST: dict[str, dict[str, float | str | None]] = {
    "Information Technology": {"cagr": None, "source": "TODO: cite a real public report"},
    "Health Care": {"cagr": None, "source": "TODO: cite a real public report"},
    "Financials": {"cagr": None, "source": "TODO: cite a real public report"},
    "Consumer Discretionary": {"cagr": None, "source": "TODO: cite a real public report"},
    "Communication Services": {"cagr": None, "source": "TODO: cite a real public report"},
    "Industrials": {"cagr": None, "source": "TODO: cite a real public report"},
    "Consumer Staples": {"cagr": None, "source": "TODO: cite a real public report"},
    "Energy": {"cagr": None, "source": "TODO: cite a real public report"},
    "Utilities": {"cagr": None, "source": "TODO: cite a real public report"},
    "Real Estate": {"cagr": None, "source": "TODO: cite a real public report"},
    "Materials": {"cagr": None, "source": "TODO: cite a real public report"},
}

_NOT_YET_SOURCED = "Not yet sourced — see README"


def get_sector_forecast(sector: str | None) -> dict[str, str]:
    """Look up the static 10yr forecast for a sector. Returns a dict with
    'display' (what to show in the UI) and 'source' (citation or a
    "not sourced" message) -- never a fabricated number.
    """
    if not sector or sector not in SECTOR_10YR_GROWTH_FORECAST:
        return {"display": _NOT_YET_SOURCED, "source": _NOT_YET_SOURCED}

    entry = SECTOR_10YR_GROWTH_FORECAST[sector]
    cagr = entry.get("cagr")
    source = entry.get("source") or ""

    if cagr is None or not source or source.strip().upper().startswith("TODO"):
        return {"display": _NOT_YET_SOURCED, "source": _NOT_YET_SOURCED}

    return {"display": f"{cagr:.1%} CAGR (10yr forecast)", "source": source}
