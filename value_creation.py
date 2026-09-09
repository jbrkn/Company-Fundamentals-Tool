"""
Value Creation (ROIC vs. WACC) -- build spec Section 3.7.

The most involved new section in the final consolidated spec. Every
assumption is a disclosed, flat, stated constant (see config.py) rather
than a hidden or unilaterally-chosen number -- per the spec's explicit
"nothing here should be a hidden constant" instruction, and the "flag the
tradeoff rather than deciding unilaterally" instruction on live-vs-flat
inputs (risk-free rate: live-with-fallback; equity risk premium and tax
rate: flat, stated).

This module only combines already-fetched scalar fields (EBIT, total debt,
total equity, cash, market cap, beta, interest expense) into the ROIC/WACC
framework -- it makes no new network calls itself. The one live input
(risk-free rate) is fetched separately by data_pipeline.fetch_risk_free_rate
and passed in, so this module stays pure/testable.
"""

from __future__ import annotations

import config


def compute_nopat(ebit: float | None, tax_rate: float = config.WACC_TAX_RATE_ASSUMPTION) -> float | None:
    """NOPAT = EBIT x (1 - assumed tax rate). Flat stated assumption, not
    each company's noisy effective tax rate -- see config.py docstring for
    why this is defensible where the separate *simple* ROIC figure
    (Profitability section) is not: the assumption here is visible.
    """
    if ebit is None:
        return None
    return ebit * (1 - tax_rate)


def compute_invested_capital(total_debt: float | None, total_equity: float | None,
                             cash: float | None) -> float | None:
    """Invested Capital = Total Debt + Total Equity - Cash."""
    if total_debt is None or total_equity is None or cash is None:
        return None
    return total_debt + total_equity - cash


def compute_roic(nopat: float | None, invested_capital: float | None) -> float | None:
    if nopat is None or invested_capital in (None, 0):
        return None
    return nopat / invested_capital


def compute_cost_of_equity(risk_free_rate: float, beta: float | None,
                           equity_risk_premium: float = config.EQUITY_RISK_PREMIUM_ASSUMPTION) -> float | None:
    """CAPM: Cost of Equity = risk-free rate + Beta x equity risk premium."""
    if beta is None:
        return None
    return risk_free_rate + beta * equity_risk_premium


def compute_cost_of_debt(interest_expense: float | None, total_debt: float | None) -> float | None:
    """Cost of Debt ~ Interest Expense / Total Debt (pre-tax)."""
    if interest_expense is None or total_debt in (None, 0):
        return None
    return interest_expense / total_debt


def compute_wacc(market_cap: float | None, total_debt: float | None,
                 cost_of_equity: float | None, cost_of_debt: float | None,
                 tax_rate: float = config.WACC_TAX_RATE_ASSUMPTION) -> float | None:
    """WACC, weighted by market cap (equity weight) vs. total debt (debt
    weight), using the after-tax cost of debt (the standard WACC
    convention -- debt's tax shield). Same flat tax_rate assumption as
    NOPAT, for consistency.
    """
    if market_cap is None or total_debt is None or cost_of_equity is None or cost_of_debt is None:
        return None
    total_capital = market_cap + total_debt
    if total_capital == 0:
        return None
    equity_weight = market_cap / total_capital
    debt_weight = total_debt / total_capital
    after_tax_cost_of_debt = cost_of_debt * (1 - tax_rate)
    return equity_weight * cost_of_equity + debt_weight * after_tax_cost_of_debt


def compute_value_creation(row: dict, risk_free_rate: float, risk_free_rate_is_live: bool) -> dict:
    """Full Value Creation calculation from a single company's raw data
    dict (as produced by data_pipeline.fetch_one_ticker / load_or_fetch_ticker).

    Returns every intermediate input alongside the final spread, so the UI
    can display each one explicitly -- this section is "most likely to be
    challenged in an interview," per the spec, so nothing here should be a
    black box.
    """
    ebit = row.get("ebit_raw")
    total_debt = row.get("total_debt_raw")
    total_equity = row.get("total_equity")
    cash = row.get("cash_raw")
    market_cap = row.get("market_cap")
    beta = row.get("beta")
    interest_expense = row.get("interest_expense_raw")

    nopat = compute_nopat(ebit)
    invested_capital = compute_invested_capital(total_debt, total_equity, cash)
    roic = compute_roic(nopat, invested_capital)

    cost_of_equity = compute_cost_of_equity(risk_free_rate, beta)
    cost_of_debt = compute_cost_of_debt(interest_expense, total_debt)
    wacc = compute_wacc(market_cap, total_debt, cost_of_equity, cost_of_debt)

    spread = (roic - wacc) if roic is not None and wacc is not None else None

    return {
        "ebit": ebit,
        "tax_rate_assumption": config.WACC_TAX_RATE_ASSUMPTION,
        "nopat": nopat,
        "total_debt": total_debt,
        "total_equity": total_equity,
        "cash": cash,
        "invested_capital": invested_capital,
        "roic": roic,
        "risk_free_rate": risk_free_rate,
        "risk_free_rate_is_live": risk_free_rate_is_live,
        "beta": beta,
        "equity_risk_premium_assumption": config.EQUITY_RISK_PREMIUM_ASSUMPTION,
        "cost_of_equity": cost_of_equity,
        "interest_expense": interest_expense,
        "cost_of_debt": cost_of_debt,
        "market_cap": market_cap,
        "wacc": wacc,
        "spread": spread,
    }
