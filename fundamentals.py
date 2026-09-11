"""
Fundamentals computation layer.

Pure calculation only -- no network calls, no yfinance dependency. Every
function here takes plain scalar inputs (floats, strings, None) already
extracted by data_pipeline.py and returns a plain dict. This module can be
imported and unit-tested with zero external dependencies beyond Python
itself, and data_pipeline.py imports it (not the other way around) to
assemble each ticker's full raw dict during fetch_one_ticker().

Covers:
    - DuPont decomposition (ROE = Net Margin x Asset Turnover x Equity Multiplier)
    - FCF margin and FCF/Net Income (quality-of-earnings check)
    - Altman Z-Score, with standard interpretation bands
    - Projection growth rate: a 3-year revenue CAGR (falling back to 2yr,
      then 1yr YoY, only as a last resort) used as the compounding input
      for forward projections -- see compute_projection_growth_rate.
    - Forward projections at 1yr/5yr/10yr for revenue, net income, and
      Net Debt/EBITDA (the above growth rate compounded, margin/debt
      assumption held constant), including the strengthened long-horizon
      caveat framing (the caveat TEXT lives in the dashboard/deep_dive UI
      layer -- this module only marks which horizons are the "long" ones
      via FORWARD_PROJECTION_HORIZONS, it doesn't render any caveat itself).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Projection growth rate: 3yr revenue CAGR, falling back to 2yr then 1yr
# ---------------------------------------------------------------------------
# BUG FIX: forward projections used to compound a single year's YoY revenue
# growth rate. This made a 10yr projection extremely sensitive to one-off
# noise in a single base year -- confirmed in testing, where one unusual
# year for NVDA got mechanically compounded into a ~$33 trillion 10yr
# revenue figure. A multi-year CAGR smooths over a single anomalous year
# and is far less likely to produce that kind of blowup, though it doesn't
# eliminate the underlying "naive compounding" limitation -- see the
# strengthened long-horizon caveat in deep_dive.py.
PROJECTION_GROWTH_FALLBACK_2YR_WARNING = (
    "Limited history: growth rate based on 2 years of data, not the usual 3."
)
PROJECTION_GROWTH_FALLBACK_1YR_WARNING = (
    "Limited history: growth rate based on a single year — forward "
    "projections for this company are less reliable than usual and should "
    "be treated with extra caution."
)
PROJECTION_GROWTH_INSUFFICIENT_WARNING = (
    "Insufficient revenue history to compute any growth rate — forward "
    "projections cannot be produced for this company."
)


def compute_projection_growth_rate(revenues: list[float | None]) -> dict:
    """Compute the growth rate used as the compounding input for forward
    projections, preferring a 3-year revenue CAGR and falling back to
    shorter windows only when full history isn't available.

    `revenues` must be ordered MOST RECENT FIRST (matching yfinance's own
    income_stmt column order): [current_year, 1yr_ago, 2yr_ago, 3yr_ago, ...].

    Fallback order (never silently skipped -- the basis actually used is
    always returned so the caller can display it):
        1. 3yr CAGR = (revenue[0] / revenue[3]) ** (1/3) - 1  -- needs 4 data points
        2. 2yr CAGR = (revenue[0] / revenue[2]) ** (1/2) - 1  -- needs 3 data points
        3. 1yr YoY  = (revenue[0] / revenue[1]) - 1           -- needs 2 data points,
           used ONLY as a last resort (this is the old, noise-sensitive method)
        4. None, with an "insufficient data" flag, if fewer than 2 usable
           data points exist at all.

    A candidate window is skipped (not silently accepted) if either
    endpoint is missing, zero, or negative -- revenue shouldn't realistically
    be <= 0, and a CAGR computed against such a value would be nonsensical
    or produce a complex/undefined result.

    Returns: {"rate": float | None, "basis": "3yr" | "2yr" | "1yr" | "none",
              "warning": str | None}
    """
    def _valid(v) -> bool:
        return v is not None and v > 0

    if len(revenues) >= 4 and _valid(revenues[0]) and _valid(revenues[3]):
        rate = (revenues[0] / revenues[3]) ** (1 / 3) - 1
        return {"rate": rate, "basis": "3yr", "warning": None}

    if len(revenues) >= 3 and _valid(revenues[0]) and _valid(revenues[2]):
        rate = (revenues[0] / revenues[2]) ** (1 / 2) - 1
        return {"rate": rate, "basis": "2yr", "warning": PROJECTION_GROWTH_FALLBACK_2YR_WARNING}

    if len(revenues) >= 2 and _valid(revenues[0]) and _valid(revenues[1]):
        rate = (revenues[0] / revenues[1]) - 1
        return {"rate": rate, "basis": "1yr", "warning": PROJECTION_GROWTH_FALLBACK_1YR_WARNING}

    return {"rate": None, "basis": "none", "warning": PROJECTION_GROWTH_INSUFFICIENT_WARNING}


# ---------------------------------------------------------------------------
# DuPont decomposition
# ---------------------------------------------------------------------------
def compute_dupont(net_margin: float | None, revenue: float | None,
                   total_assets: float | None, total_equity: float | None,
                   missing: list[str]) -> dict[str, float | None]:
    """DuPont decomposition: ROE = Net Margin x Asset Turnover x Equity
    Multiplier. Meant to be shown alongside the reported ROE (a separate
    .info field, fetched in data_pipeline.py) as a cross-check -- the
    DuPont-derived figure should be close to, but won't always exactly
    match, the reported ROE due to differing period conventions between
    yfinance's .info ROE and the balance-sheet/income-statement columns
    used here.
    """
    out = {"asset_turnover": None, "equity_multiplier": None, "dupont_roe": None}

    if revenue is None or total_assets in (None, 0):
        missing.append("asset_turnover")
    else:
        out["asset_turnover"] = revenue / total_assets

    if total_assets is None or total_equity in (None, 0):
        missing.append("equity_multiplier")
    else:
        out["equity_multiplier"] = total_assets / total_equity

    if net_margin is not None and out["asset_turnover"] is not None and out["equity_multiplier"] is not None:
        out["dupont_roe"] = net_margin * \
            out["asset_turnover"] * out["equity_multiplier"]
    else:
        missing.append("dupont_roe")

    return out


# ---------------------------------------------------------------------------
# Free Cash Flow margin / quality-of-earnings
# ---------------------------------------------------------------------------
def compute_fcf_quality(fcf: float | None, revenue: float | None,
                        net_income: float | None, missing: list[str]) -> dict[str, float | None]:
    """FCF Margin = FCF / Revenue. FCF/Net Income = quality-of-earnings
    check (does reported profit convert to cash; a ratio well below 1.0x
    can flag earnings that aren't cash-backed). `fcf` here is the raw FCF
    dollar figure already extracted by data_pipeline.py's cashflow-
    statement parsing -- this function does no statement parsing itself.
    """
    out = {"fcf_margin": None, "fcf_to_net_income": None}

    if revenue not in (None, 0) and fcf is not None:
        out["fcf_margin"] = fcf / revenue
    else:
        missing.append("fcf_margin")

    if net_income not in (None, 0) and fcf is not None:
        out["fcf_to_net_income"] = fcf / net_income
    else:
        missing.append("fcf_to_net_income")

    return out


# ---------------------------------------------------------------------------
# Altman Z-Score
# ---------------------------------------------------------------------------
# Standard interpretation bands (1968 model, public manufacturing/industrial
# companies).
ALTMAN_SAFE_THRESHOLD = 2.99
ALTMAN_DISTRESS_THRESHOLD = 1.81

# Sectors where this model is widely considered a poor structural fit
# (limited/no conventional current-assets/liabilities distinction,
# intentionally heavy leverage as a normal part of the business).
ALTMAN_LESS_APPLICABLE_SECTORS = ("Financials", "Real Estate")


def compute_altman_z_score(current_assets: float | None, current_liabilities: float | None,
                           total_assets: float | None, retained_earnings: float | None,
                           ebit: float | None, market_cap: float | None,
                           total_liabilities: float | None, revenue: float | None,
                           sector: str | None,
                           missing: list[str]) -> dict:
    """Standard Altman Z-Score: Z = 1.2A + 1.4B + 3.3C + 0.6D + 1.0E, where
        A = Working Capital / Total Assets
        B = Retained Earnings / Total Assets
        C = EBIT / Total Assets
        D = Market Value of Equity / Total Liabilities
        E = Sales / Total Assets

    FLAG: this model was designed for public manufacturing/industrial
    companies. It is widely considered LESS APPLICABLE to financials and
    REITs -- see ALTMAN_LESS_APPLICABLE_SECTORS. Rather than hide or force
    the number for those sectors, this function still computes it but
    returns an explicit `less_applicable_sector` flag the caller should
    surface prominently, never suppress.
    """
    out = {"working_capital": None, "altman_z_score": None, "altman_zone": None,
           "less_applicable_sector": sector in ALTMAN_LESS_APPLICABLE_SECTORS if sector else False}

    if current_assets is None or current_liabilities is None:
        missing.append("altman_z_score")
        return out
    working_capital = current_assets - current_liabilities
    out["working_capital"] = working_capital

    required = [total_assets, retained_earnings,
                ebit, market_cap, total_liabilities, revenue]
    if total_assets in (None, 0) or any(v is None for v in required) or total_liabilities == 0:
        missing.append("altman_z_score")
        return out

    a = working_capital / total_assets
    b = retained_earnings / total_assets
    c = ebit / total_assets
    d = market_cap / total_liabilities
    e = revenue / total_assets

    z = 1.2 * a + 1.4 * b + 3.3 * c + 0.6 * d + 1.0 * e
    out["altman_z_score"] = z

    if z > ALTMAN_SAFE_THRESHOLD:
        out["altman_zone"] = "Safe Zone"
    elif z > ALTMAN_DISTRESS_THRESHOLD:
        out["altman_zone"] = "Grey Zone"
    else:
        out["altman_zone"] = "Distress Zone"

    return out


# ---------------------------------------------------------------------------
# Forward projections (1yr / 5yr / 10yr)
# ---------------------------------------------------------------------------
FORWARD_PROJECTION_HORIZONS = (1, 5, 10)
LONG_HORIZONS = (5, 10)  # horizons that need the strengthened UI caveat


def horizon_key(h: int) -> str:
    """String key for a projection horizon (e.g. 1 -> '1yr'). Integer keys
    would silently break after a JSON cache round-trip -- JSON object keys
    must be strings, so json.dump/json.load turns {1: ...} into {"1": ...},
    and a caller doing projections.get(1) after a cache reload would get
    None instead of the data, with no error raised. Using string keys from
    the start avoids this entirely.
    """
    return f"{h}yr"


def compute_forward_projections(revenue: float | None, ebitda: float | None,
                                net_debt: float | None, net_margin: float | None,
                                growth_rate: float | None,
                                missing: list[str],
                                horizons: tuple[int, ...] = FORWARD_PROJECTION_HORIZONS) -> dict:
    """General N-horizon forward projection: revenue, EBITDA, net income,
    net debt, and Net Debt/EBITDA at each horizon in `horizons` (default
    1/5/10yr per the final consolidated spec).

    `growth_rate` should be produced by compute_projection_growth_rate
    (a 3-year revenue CAGR, falling back to 2yr then 1yr YoY only when full
    history isn't available) -- NOT a raw single-year YoY figure. This
    function itself is agnostic to which basis produced the rate; it just
    compounds whatever it's given. Renamed from the earlier
    `revenue_growth_yoy` parameter name, which became misleading once the
    3yr-CAGR fix meant callers were no longer always passing a single-year
    rate here.

    Assumptions (stated explicitly here; the UI layer must display these
    next to the projection table, never bury them):
        1. Net debt is held CONSTANT at every horizon (no modeled paydown,
           refinancing, or new issuance).
        2. EBITDA margin AND net margin are assumed constant as revenue
           grows (no modeled margin expansion or compression).
        3. The growth rate continues unchanged at every horizon (no
           deceleration/acceleration/business-model-transition modeled).

    The 5yr/10yr figures need a visually strengthened caveat in the UI:
    naive extrapolation for illustrative comparison only, increasingly
    unrealistic the further out it's compounded -- a company growing 15%
    a year today implies an absurd 4x revenue figure at 10yr under this
    model. This module marks which horizons are "long" via LONG_HORIZONS
    so the UI layer can apply that stronger caveat consistently; the
    caveat text itself is a UI-layer concern, not rendered here.

    Returns a dict: {"current": {...}, "1yr": {...}, "5yr": {...}, "10yr": {...}}.
    Keys are always STRINGS (see horizon_key) so this survives a JSON cache
    round-trip intact.
    """
    current = {
        "revenue": revenue, "ebitda": ebitda, "net_debt": net_debt,
        "net_income": (revenue * net_margin) if revenue is not None and net_margin is not None else None,
        "net_debt_ebitda": (net_debt / ebitda) if net_debt is not None and ebitda not in (None, 0) else None,
    }

    result = {"current": current}

    if net_debt is None or ebitda in (None, 0) or growth_rate is None:
        missing.append("forward_projections")
        for h in horizons:
            result[horizon_key(h)] = {"revenue": None, "ebitda": None, "net_debt": None,
                                      "net_income": None, "net_debt_ebitda": None}
        return result

    for h in horizons:
        growth_factor = (1 + growth_rate) ** h
        proj_revenue = revenue * growth_factor if revenue is not None else None
        proj_ebitda = ebitda * growth_factor
        proj_net_income = (
            proj_revenue * net_margin) if proj_revenue is not None and net_margin is not None else None
        proj_net_debt = net_debt  # held flat -- assumption 1
        proj_ratio = (proj_net_debt /
                      proj_ebitda) if proj_ebitda != 0 else None

        result[horizon_key(h)] = {
            "revenue": proj_revenue,
            "ebitda": proj_ebitda,
            "net_debt": proj_net_debt,
            "net_income": proj_net_income,
            "net_debt_ebitda": proj_ratio,
        }
        if proj_ratio is None:
            missing.append(f"projected_net_debt_ebitda_{h}yr")

    return result
