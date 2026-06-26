"""Breaks & Reconciliation Board — Streamlit page.

The COO's reconciliation cockpit: internal book vs clearing broker, per team.
Answers "what's broken, how much, and how long has it sat?" — with the 3-day
aging fear front and centre.

This file lives in pages/ so Streamlit shows it as a sibling page to the
home dashboard. Run from the repo root:  streamlit run dashboard.py
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import config
import reconciliation as recon

st.set_page_config(page_title="Reconciliation Board", layout="wide")

st.title("🔄 Breaks & Reconciliation Board")
st.caption(
    f"Internal book vs clearing broker · as of **{config.AS_OF_DATE}** · "
    f"critical when unresolved ≥ **{config.BREAK_AGING_CRITICAL_DAYS} days**"
)

# --- Load + reconcile ------------------------------------------------------ #
try:
    internal, broker = recon.load_trades()
except FileNotFoundError:
    st.error("Trade files not found. Run `python generate_data.py` first.")
    st.stop()

breaks = recon.reconcile(internal, broker)
by_team = recon.summary_by_team(internal, breaks)
by_type = recon.summary_by_type(breaks)

total_breaks = len(breaks)
critical = int((breaks["Severity"] == "Critical").sum()) if total_breaks else 0
# Match rate = internal trades that reconciled cleanly. Breaks that touch an
# internal trade are everything except broker-only "Missing internally" rows.
internal_breaks = int((breaks["BreakType"] != "Missing internally").sum()) if total_breaks else 0
match_rate = round((len(internal) - internal_breaks) / max(len(internal), 1) * 100, 1)

# --- The COO's #1 fear, loud and at the top -------------------------------- #
if critical:
    st.error(
        f"⚠️ **{critical} break(s) unresolved for ≥ {config.BREAK_AGING_CRITICAL_DAYS} days.** "
        "These are the COO's #1 fear — escalate before they age further."
    )
else:
    st.success("✅ No breaks aged past the critical threshold.")

# --- Headline metrics ------------------------------------------------------ #
c1, c2, c3, c4 = st.columns(4)
c1.metric("Open breaks", total_breaks)
c2.metric("Critical (3+ days)", critical, delta=None,
          delta_color="inverse")
c3.metric("Match rate", f"{match_rate}%")
c4.metric("Oldest break", f"{int(breaks['AgeDays'].max()) if total_breaks else 0} d")

st.divider()

# --- Per-team scorecard ---------------------------------------------------- #
st.subheader("Per-team scorecard")
st.caption("Sorted worst-first by critical breaks. This is the COO's roll-call.")


def _highlight_critical(row: pd.Series):
    # Dark text on the tint so numbers stay readable in light & dark themes.
    color = "background-color:#f6cccc; color:#8a0010" if row["Critical (3+ days)"] > 0 else ""
    return [color] * len(row)


styled = (
    by_team.style
    .apply(_highlight_critical, axis=1)
    .format({"Break Notional": "${:,.0f}", "Break %": "{:.1f}%"})
)
st.dataframe(styled, width="stretch", hide_index=True)

# --- Two charts: breaks by team, breaks by aging bucket -------------------- #
col_a, col_b = st.columns(2)
with col_a:
    st.subheader("Breaks by team")
    st.bar_chart(by_team.set_index("Team")["Breaks"])
with col_b:
    st.subheader("Break aging")
    if total_breaks:
        order = ["Today", "1-2 days", f"{config.BREAK_AGING_CRITICAL_DAYS}+ days"]
        aging = (breaks["AgingBucket"].value_counts()
                 .reindex(order).fillna(0).astype(int))
        st.bar_chart(aging)
    else:
        st.info("No breaks to age.")

st.divider()

# --- Break type breakdown -------------------------------------------------- #
st.subheader("Breaks by type")
st.caption(
    "Field mismatch = booked on both sides but a field disagrees · "
    "Missing at broker = we booked it, broker never confirmed · "
    "Missing internally = broker alleges a trade we have no record of."
)
st.dataframe(by_type, width="stretch", hide_index=True)

st.divider()

# --- The break blotter (filterable) ---------------------------------------- #
st.subheader("Break blotter")

f1, f2, f3 = st.columns(3)
team_filter = f1.multiselect("Team", sorted(breaks["Team"].unique()) if total_breaks else [])
sev_filter = f2.multiselect("Severity", ["Critical", "Warning", "New"])
type_filter = f3.multiselect("Break type", sorted(breaks["BreakType"].unique()) if total_breaks else [])

view = breaks.copy()
if team_filter:
    view = view[view["Team"].isin(team_filter)]
if sev_filter:
    view = view[view["Severity"].isin(sev_filter)]
if type_filter:
    view = view[view["BreakType"].isin(type_filter)]


def _color_severity(val: str) -> str:
    # Tint + forced dark text so the label is readable in light & dark themes.
    return {
        "Critical": "background-color:#f6cccc; color:#8a0010; font-weight:600",
        "Warning": "background-color:#fce8c8; color:#7a4a00; font-weight:600",
        "New": "background-color:#cdeed6; color:#0f5a2a; font-weight:600",
    }.get(val, "")


blotter = view[["TradeID", "Team", "Symbol", "Broker", "BreakType",
                "Detail", "TradeDate", "AgeDays", "Severity", "Notional"]].copy()
blotter["TradeDate"] = pd.to_datetime(blotter["TradeDate"]).dt.date

st.dataframe(
    blotter.style
    .map(_color_severity, subset=["Severity"])
    .format({"Notional": "${:,.0f}"}),
    width="stretch", hide_index=True, height=420,
)
st.caption(f"Showing {len(view)} of {total_breaks} breaks.")
