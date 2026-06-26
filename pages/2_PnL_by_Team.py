"""Real-time P&L by Team — Streamlit page.

The COO's P&L cockpit across all desks: realized vs unrealized, today and
month-to-date, color-coded red/green. This is the JD's "real-time P&L across
equities, futures, options."

Lives in pages/ so Streamlit shows it beside the home dashboard.
Run from the repo root:  streamlit run dashboard.py
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

import config
import pnl

st.set_page_config(page_title="P&L by Team", layout="wide")

GREEN, RED, GREY = "#1a7f37", "#a40010", "#888888"
PNL_COLS = pnl._DAY_COLS + pnl._MTD_COLS + ["Open Unrealized"]


def _money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


# Table cells: tint the background and force dark text so numbers stay
# readable in both light and dark themes (colored text alone vanishes on a
# dark background).
def _color(v) -> str:
    if not isinstance(v, (int, float)) or v == 0:
        return ""
    if v > 0:
        return "background-color:#cdeed6; color:#0f5a2a; font-weight:600"
    return "background-color:#f6cccc; color:#8a0010; font-weight:600"


st.title("💸 Real-time P&L by Team")
st.caption(
    f"Realized vs unrealized · daily & month-to-date · as of **{config.AS_OF_DATE}** · "
    "marked against latest EOD prices"
)

# --- Load + compute -------------------------------------------------------- #
try:
    internal, marks = pnl.load_inputs()
except FileNotFoundError:
    st.error("P&L inputs not found. Run `python generate_data.py` first.")
    st.stop()

daily, positions = pnl.daily_pnl(internal, marks)
team = pnl.summary_by_team(daily, positions)
firm = pnl.firm_totals(team)

# --- Firm headline, color-coded -------------------------------------------- #
st.subheader("Firm-wide")


def _pnl_block(label: str, value: float, sub: str = "") -> str:
    color = GREEN if value >= 0 else RED
    sub_html = f"<div style='font-size:0.8rem;color:{GREY}'>{sub}</div>" if sub else ""
    return (
        f"<div style='font-size:0.85rem;color:{GREY}'>{label}</div>"
        f"<div style='font-size:1.7rem;font-weight:700;color:{color}'>{_money(value)}</div>"
        f"{sub_html}"
    )


c1, c2, c3, c4 = st.columns(4)
c1.markdown(_pnl_block("P&L Today", firm["Day Total"],
                       f"R {_money(firm['Day Realized'])} · U {_money(firm['Day Unrealized'])}"),
            unsafe_allow_html=True)
c2.markdown(_pnl_block("P&L Month-to-date", firm["MTD Total"],
                       f"R {_money(firm['MTD Realized'])} · U {_money(firm['MTD Unrealized'])}"),
            unsafe_allow_html=True)
c3.markdown(_pnl_block("Realized MTD", firm["MTD Realized"]), unsafe_allow_html=True)
c4.markdown(_pnl_block("Open unrealized", firm["Open Unrealized"],
                       "current mark-to-market"), unsafe_allow_html=True)

st.divider()

# --- Per-team scorecard ---------------------------------------------------- #
st.subheader("Per-team scorecard")
st.caption("Realized = locked in on closed trades · Unrealized = mark-to-market change. "
           "Green = profit, red = loss.")

firm_row = {"Team": "FIRM", "Segment": "All desks", **firm}
disp = pd.concat([team, pd.DataFrame([firm_row])], ignore_index=True)


def _bold_firm(row: pd.Series):
    if row["Team"] == "FIRM":
        return ["border-top: 2px solid #444; font-weight: 700"] * len(row)
    return [""] * len(row)


styled = (
    disp.style
    .apply(_bold_firm, axis=1)
    .map(_color, subset=PNL_COLS)
    .format({c: "${:,.0f}" for c in PNL_COLS})
)
st.dataframe(styled, width="stretch", hide_index=True)

# --- MTD P&L by team chart (red/green) ------------------------------------- #
col_a, col_b = st.columns([3, 2])
with col_a:
    st.subheader("MTD P&L by team")
    vals = team.set_index("Team")["MTD Total"]
    fig, ax = plt.subplots(figsize=(7, 3.2))
    ax.bar(vals.index, vals.values,
           color=[GREEN if v >= 0 else RED for v in vals.values])
    ax.axhline(0, color="#888", linewidth=0.8)
    ax.set_ylabel("MTD P&L ($)")
    ax.spines[["top", "right"]].set_visible(False)
    ax.ticklabel_format(axis="y", style="plain")
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    st.pyplot(fig)
with col_b:
    st.subheader("Realized vs unrealized (MTD)")
    ru = team.set_index("Team")[["MTD Realized", "MTD Unrealized"]]
    st.bar_chart(ru)

st.divider()

# --- Open positions blotter ------------------------------------------------ #
st.subheader("Open positions — current mark-to-market")
st.caption("Live book: signed position, average cost vs latest mark, and open P&L.")

blotter = positions.copy()
blotter = blotter[blotter["Position"].round(0) != 0]
blotter = blotter.sort_values("OpenUnrealized")
blotter = blotter[["Team", "Segment", "Symbol", "Position", "AvgCost", "Mark", "OpenUnrealized"]]

f1, f2 = st.columns(2)
team_filter = f1.multiselect("Team", sorted(blotter["Team"].unique()))
side_filter = f2.selectbox("Side", ["All", "Long only", "Short only"])

view = blotter.copy()
if team_filter:
    view = view[view["Team"].isin(team_filter)]
if side_filter == "Long only":
    view = view[view["Position"] > 0]
elif side_filter == "Short only":
    view = view[view["Position"] < 0]

st.dataframe(
    view.style
    .map(_color, subset=["OpenUnrealized"])
    .format({"Position": "{:,.0f}", "AvgCost": "${:,.2f}",
             "Mark": "${:,.2f}", "OpenUnrealized": "${:,.0f}"}),
    width="stretch", hide_index=True, height=380,
)
st.caption(f"{len(view)} open positions · net open unrealized "
           f"{_money(view['OpenUnrealized'].sum())}")
