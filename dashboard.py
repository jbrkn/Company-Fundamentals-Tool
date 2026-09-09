"""
Top-level Streamlit app shell for the Company Fundamental Analysis Tool.

Deliberately thin: page config/style, the README link, the free-text
ticker search box, and the VIX header card (page-level chrome, not
company-specific) live here. Everything company-specific is delegated to
deep_dive.render(selected_ticker) -- this file does not fetch or compute
anything about the selected company itself; it only fetches VIX (also via
data_pipeline.py, the single source of truth for all yfinance calls).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import streamlit as st

import config
import deep_dive
from data_pipeline import fetch_vix

# ---------------------------------------------------------------------------
# STYLE (placeholder until Systematic Trading Lab's real theme is provided).
# Shares its color constants with deep_dive.py so charts/cards rendered
# there match this page-level CSS.
# ---------------------------------------------------------------------------
PAGE_BG = deep_dive.PAGE_BG
CARD_BG = deep_dive.CARD_BG
ACCENT = deep_dive.ACCENT

st.set_page_config(
    page_title="Company Fundamental Analysis Tool", layout="wide")

st.markdown(
    f"""
    <style>
        .stApp {{ background-color: {PAGE_BG}; }}
        .metric-card {{
            background-color: {CARD_BG}; border-radius: 8px; padding: 1rem 1.5rem;
            display: inline-block; margin-bottom: 1rem;
        }}
        .section-card {{
            background-color: {CARD_BG}; border-radius: 8px; padding: 1.2rem 1.5rem;
            margin-bottom: 1rem;
        }}
        .assumption-note {{
            font-size: 0.85rem; color: #aaa; border-left: 3px solid {ACCENT};
            padding-left: 0.75rem; margin-top: 0.5rem;
        }}
    </style>
    """,
    unsafe_allow_html=True,
)

# README button: in-page toggle (session_state + expander) -- confirmed
# still pointing at the real README.md content, not a placeholder link.
if "show_readme" not in st.session_state:
    st.session_state["show_readme"] = False

if st.button("📄 View full README →"):
    st.session_state["show_readme"] = not st.session_state["show_readme"]

if st.session_state["show_readme"]:
    with st.expander("README", expanded=True):
        _readme_path = Path(__file__).parent / "README.md"
        st.info(
            "📌 **This README is being rewritten in stages following the "
            "Company Fundamental Analysis Tool repositioning.** Most of the "
            "content below still describes the old scoring/ranking engine, "
            "which has been removed. See the repositioning notice at the top "
            "of the README itself for what's current vs. pending rewrite."
        )
        if _readme_path.exists():
            st.markdown(_readme_path.read_text(encoding="utf-8"))
        else:
            st.warning(f"README.md not found at {_readme_path}.")

st.title("Company Fundamental Analysis Tool")
st.caption("Live, on-demand fundamental analysis for any US-listed company. "
           "Not a ranking or screening tool, and not a historical backtest.")


@st.cache_data(ttl=config.CACHE_TTL_HOURS * 3600)
def _cached_vix(_cache_date: str):
    return fetch_vix()


TODAY_STR = date.today().isoformat()

# ---------------------------------------------------------------------------
# Header metric card: VIX (market-regime context only, page-level chrome)
# ---------------------------------------------------------------------------
vix_level, vix_date = _cached_vix(TODAY_STR)
col1, col2 = st.columns([1, 3])
with col1:
    vix_display = f"{vix_level:.2f}" if vix_level is not None else "N/A"
    st.markdown(
        f"""<div class="metric-card">
                <div style="font-size:0.85rem;color:#999;">VIX (as of {vix_date})</div>
                <div style="font-size:1.8rem;font-weight:700;">{vix_display}</div>
            </div>""",
        unsafe_allow_html=True,
    )
with col2:
    st.caption("VIX is shown for market-regime context only. It is not a per-company "
               "metric and does not feed into any calculation on this page.")

# ---------------------------------------------------------------------------
# Ticker search -- free text, any valid US ticker, fetched live on demand
# ---------------------------------------------------------------------------
if "selected_ticker" not in st.session_state:
    st.session_state["selected_ticker"] = "AAPL"

ticker_input = st.text_input(
    "Search for a company (any valid US ticker)",
    value=st.session_state["selected_ticker"],
    key="ticker_search_input",
).strip().upper()

if ticker_input and ticker_input != st.session_state["selected_ticker"]:
    st.session_state["selected_ticker"] = ticker_input

selected_ticker = st.session_state["selected_ticker"]

if not selected_ticker:
    st.info("Enter a ticker to begin.")
    st.stop()

# ---------------------------------------------------------------------------
# Route to the deep dive -- all company-specific fetching, computation
# lookup, and rendering happens inside deep_dive.py.
# ---------------------------------------------------------------------------
deep_dive.render(selected_ticker)
