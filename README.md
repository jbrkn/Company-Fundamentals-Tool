# Company Fundamentals Tool

Live, on-demand fundamental analysis for any US-listed company — profitability,
leverage, valuation, and value-creation metrics, with automatically discovered
peer comparables.

**Live app:** [company-fundamentals-tool-ve8itidd6qikp2jw4hkgv9.streamlit.app](https://company-fundamentals-tool-ve8itidd6qikp2jw4hkgv9.streamlit.app)

---

## What this tool is

A single-company deep-dive tool. Search any valid US ticker and get a full
fundamental profile: profitability, leverage, valuation, risk, value creation
(ROIC vs. WACC), auto-discovered competitors, ownership/dilution trends, and
sector growth context — all built from live data, computed on demand.

## What this tool is NOT

- **Not a ranking or screening tool.** There is no composite score, no
  universe-wide ranking, and no percentile system. An earlier version of this
  project did include a 150-company ranking mechanism; it was deliberately
  removed (see *Design Evolution* below) once the project's actual purpose —
  deep single-company analysis — became clear.
- **Not a backtest.** Nothing here is tested for historical predictive power.
  All figures are a live or recently-cached snapshot as of the date shown.
- **Not investment advice.** This is a research and portfolio-project tool,
  not a recommendation engine.

---

## Full Methodology

### Company Size
Market cap, enterprise value, trailing-twelve-month revenue, and total
assets — shown as plain dollar figures deliberately separate from every
ratio elsewhere on the page, so scale is never hidden behind a percentage.

### Profitability
**Current metrics**: gross margin, operating margin, net margin, EBITDA
margin, ROE, ROA — each compared against a live sample of same-sector peers
(see *Sector Benchmarking*, below), not a percentile across a fixed universe.

**5-year historical trend**: the same five metrics shown across the last five
fiscal years, to distinguish a consistently strong business from a single
good year.

**DuPont decomposition**: ROE broken into Net Margin × Asset Turnover ×
Equity Multiplier, so a high ROE driven by genuine profitability can be
told apart from one driven mainly by leverage.

**Free Cash Flow**: FCF (TTM), FCF margin, and FCF/Net Income — the last of
these is a quality-of-earnings check; a ratio below 1.0x can indicate
reported profit that isn't fully converting to cash.

**Forward projections (1yr / 5yr / 10yr) — two independent, clearly
separated sources:**
- *(a) Analyst Consensus* — near-term aggregated analyst revenue and
  earnings estimates (source: Yahoo Finance). No 5-year or 10-year analyst
  consensus is shown, because standard analyst consensus data does not
  extend that far for individual companies — it would have to be
  fabricated, so it isn't included.
- *(b) Model Extrapolation* — this project's own simple projection: a
  3-year revenue CAGR compounded forward (falling back to a 2-year or,
  only if necessary, 1-year growth rate when full history isn't available
  — the basis actually used is always shown in-app for the specific
  company), with net margin held constant, out to 1/5/10 years. Explicitly
  labelled as illustrative, not a forecast — see *Key Design Assumptions*.

**ROIC (simple)**: intentionally shown as "Not computed" for most companies.
Effective tax rate and invested capital aren't reliably computable from free
yfinance data for a clean, simple ROIC figure across arbitrary companies. A
separate, defensible ROIC appears in the *Value Creation* section using a
disclosed flat tax-rate assumption instead — that version is trustworthy
because the assumption is visible, not because the underlying data is any
more reliable.

### Gearing / Debt
**Current metrics**: current ratio, quick ratio, Debt/Equity, Net
Debt/EBITDA, interest coverage — plus two additions chosen specifically for
cross-company comparability regardless of company size: **Net Debt/Market
Cap (%)** and **Total Debt/Total Assets (%)**.

**Altman Z-Score**: the standard academic bankruptcy-risk composite
(working capital/assets, retained earnings/assets, EBIT/assets, market
cap/total liabilities, sales/assets), shown with its standard interpretation
bands (Safe / Grey / Distress zones).

**Forward projections (1yr / 5yr / 10yr)**: projected Net Debt/EBITDA under
the same stated assumptions as the profitability projections (see *Key
Design Assumptions*), visualised as a Revenue vs. Debt Trajectory chart —
the debt line is flat by construction under the net-debt-held-constant
assumption; it is not a claim that debt won't change, only a simplification
that isolates the growth side of the picture.

### Valuation
P/E (trailing and forward), EV/EBITDA, P/B, FCF yield, and PEG ratio —
compared against both the live sector-average sample and the auto-discovered
competitor set (see below) in a single side-by-side table.

### Risk
Beta, historical volatility (annualised, ~3yr), and recent volatility
(annualised, ~90-day). This section answers "how much does this stock move,"
not "which direction is it going" — it is deliberately not a momentum or
market-timing signal.

### Value Creation — ROIC vs. WACC
The single most involved calculation in the tool, and the one where every
input is shown rather than hidden:
- **NOPAT** = EBIT × (1 − assumed tax rate)
- **Invested Capital** = Total Debt + Total Equity − Cash
- **ROIC** = NOPAT / Invested Capital
- **Cost of Equity (CAPM)** = risk-free rate + Beta × equity risk premium
- **Cost of Debt** ≈ Interest Expense / Total Debt
- **WACC** = the above two, weighted by market cap vs. total debt
- **Spread** = ROIC − WACC — a positive spread indicates the business is
  generating returns above its cost of capital; a negative spread indicates
  the opposite, regardless of how strong headline profitability looks.

Where interest expense or other required inputs are missing for a given
company, the spread is shown as "N/A — insufficient data," never estimated
or silently defaulted.

### Competitors
Same-GICS-Sub-Industry companies (a narrower classification than the broad
sector), ranked by market cap proximity to the searched company, with the
top 3–5 shown as a comps table (ticker, market cap, revenue, EV, P/E,
EV/EBITDA). Fetched live, on demand, for this short list only.

### Ownership & Dilution
Diluted shares outstanding over the last 5 years (rising = dilution,
typically from stock-based compensation; falling = net buybacks), insider
ownership %, institutional ownership %, and analyst price target vs. current
price (as a % upside/downside).

### Sector / Market
**(a) Trailing sector growth (live, computed)**: the average revenue growth
rate of a live sample of same-sector peers, for direct comparison against
the company's own growth rate.

**(b) Published 10-year sector growth forecast (static, manually sourced)**:
*(Not yet sourced — see Limitations below.)*

---

## Sector Benchmarking & Competitor Discovery

Both the sector-average figures (Profitability, Sector/Market) and the
Competitors section draw on one lightweight, static classification file —
Ticker, Sector, and Sub-Industry for the S&P 500, built once from Wikipedia's
S&P 500 constituent table. This file holds no financial data; it exists
purely to answer "which other companies belong to the same group as this
one." Full financials are then fetched live, on demand, only for the small
number of companies actually needed for a given comparison (a sector sample,
or 3–5 competitors) — never precomputed for the full S&P 500.

---

## Key Design Assumptions

- **Forward projections** (both profitability and leverage) hold net margin
  and net debt constant, compounding a **3-year revenue CAGR** forward
  (falling back to a 2-year or, only if necessary, 1-year growth rate when
  full history isn't available — the basis used is always shown in-app for
  the specific company). This meaningfully reduces sensitivity to a single
  unusual year compared to a pure year-over-year rate, but does not
  eliminate the underlying limitation: no deceleration, acceleration, or
  business-model transition is modelled. The 5-year figure should be read
  as a rough illustration; the 10-year figure should be read as an
  order-of-magnitude illustration only — a decade of compounding a recent
  growth rate at constant margins can still imply an unrealistic multiple of
  a company's current size for high-growth names, which is a mechanical
  consequence of the projection method, not a considered forecast.
- **ROIC/WACC uses flat, stated assumptions**, not each company's actual
  effective tax rate or a live-fitted equity risk premium: a flat corporate
  tax rate and a flat equity risk premium are used throughout, disclosed
  next to every calculation. The one live input is the risk-free rate
  (current Treasury yield).
- **Sector and competitor benchmarks use a live sample, not the full
  universe.** Sample sizes are shown next to every benchmark figure.
- **This is a live/cached snapshot tool, not a point-in-time historical
  system.** Figures reflect the most recent available data at the time of
  the search, not a specific fixed historical date.

## Limitations

- **10-year published sector growth forecasts are not yet sourced.** Genuine
  long-term sector forecasts require paid research subscriptions (Statista,
  IBISWorld, McKinsey/Deloitte industry reports) not accessible via free
  APIs; this section will be manually populated with real, cited figures
  over time and shows "Not yet sourced" honestly until then, rather than a
  fabricated number.
- **ROIC/WACC and interest-coverage figures are frequently unavailable**
  for companies where yfinance's reported interest expense data is missing,
  inconsistent (net vs. gross), or absent — shown as "N/A," never estimated.
- **GICS Sub-Industry-based competitor matching is a proxy for true
  business comparability**, not a guarantee of it — some sub-industry
  groupings (e.g. companies grouped under broad hardware/storage
  categories) can produce peers that are officially correct but not
  intuitively obvious as direct competitors.
- **All data is sourced from free APIs (yfinance, Yahoo Finance aggregated
  estimates)** — not institutional-grade data. Occasional gaps, delays, or
  inconsistencies versus a paid terminal should be expected.

## Methodology Defence

Every formula used in this tool — DuPont decomposition, Altman Z-Score,
CAPM-based cost of equity, WACC — is a standard, widely published corporate
finance method, not something invented or fitted to produce a particular
result. Flat assumptions (tax rate, equity risk premium) were chosen for
transparency and consistency across companies, not tuned to flatter any
specific result. This tool makes no predictive or return claim of any kind;
it is a structured presentation of a company's current and recent financial
position, not a signal-generating system.

## Design Evolution

This project originally included a composite scoring and ranking mechanism
across the top 150 S&P 500 constituents by market cap — a systematic
"quant screen." During development it became clear that a ranking mechanism
answers a different question ("how does this stock compare to 150 others
right now") than the one this project actually set out to answer ("give me
a genuinely deep, well-organised analysis of one company"). The ranking
engine, composite score, and percentile system were removed; the
single-company deep-dive became the entire tool. The lightweight
classification file originally built to support the ranking table was kept
and repurposed for sector benchmarking and competitor discovery, which are
the two comparative features that remained genuinely useful after the
repositioning.

## Bridge to Stock Pitches

This tool supplies the quantitative and structural picture — profitability,
leverage, valuation relative to peers, and whether a business is creating
value above its cost of capital. It does not supply investment judgement:
competitive positioning, the credibility of a growth catalyst, or a view on
timing. That judgement is the job of the two accompanying stock pitches
(Broadcom, Oracle), which use this tool's output as their quantitative
starting point and build the qualitative case on top of it.