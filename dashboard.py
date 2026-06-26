from __future__ import annotations

import matplotlib.pyplot as plt
import seaborn as sns
import streamlit as st

import config
import pnl
import reconciliation as recon
import risk_monitor as rm

GREEN, RED, GREY = "#1a7f37", "#a40010", "#888888"


def _money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def _pnl_block(label: str, value: float, sub: str = "") -> str:
    color = GREEN if value >= 0 else RED
    sub_html = f"<div style='font-size:0.8rem;color:{GREY}'>{sub}</div>" if sub else ""
    return (
        f"<div style='font-size:0.85rem;color:{GREY}'>{label}</div>"
        f"<div style='font-size:1.7rem;font-weight:700;color:{color}'>{_money(value)}</div>"
        f"{sub_html}"
    )

st.set_page_config(page_title="Operational Risk Dashboard", layout="wide")

st.title("Operational Risk Dashboard")
st.caption("A COO's lens into quant strategy health")

try:
    _int, _brk = recon.load_trades()
    _breaks = recon.reconcile(_int, _brk)
    _critical = int((_breaks["Severity"] == "Critical").sum()) if len(_breaks) else 0
    if _critical:
        st.error(
            f"🔴 **Needs attention:** {_critical} reconciliation break(s) unresolved "
            f"≥ {config.BREAK_AGING_CRITICAL_DAYS} days. See **Reconciliation Board** →"
        )
except FileNotFoundError:
    pass

# --- Firm P&L strip (the COO's most-watched number) ------------------------ #
try:
    _intr, _marks = pnl.load_inputs()
    _daily, _pos = pnl.daily_pnl(_intr, _marks)
    _firm = pnl.firm_totals(pnl.summary_by_team(_daily, _pos))
    st.subheader("Firm P&L")
    p1, p2, p3 = st.columns(3)
    p1.markdown(_pnl_block("Today", _firm["Day Total"],
                           f"R {_money(_firm['Day Realized'])} · U {_money(_firm['Day Unrealized'])}"),
                unsafe_allow_html=True)
    p2.markdown(_pnl_block("Month-to-date", _firm["MTD Total"],
                           f"R {_money(_firm['MTD Realized'])} · U {_money(_firm['MTD Unrealized'])}"),
                unsafe_allow_html=True)
    p3.markdown(_pnl_block("Open unrealized", _firm["Open Unrealized"],
                           "current mark-to-market"), unsafe_allow_html=True)
    st.caption("Per-desk breakdown on the **P&L by Team** page →")
    st.divider()
except FileNotFoundError:
    pass

st.caption(
    f"Settings (config.py): correlation ≥ {config.CORR_THRESHOLD} · "
    f"vol window {config.VOL_WINDOW}d · vol ≥ {config.VOL_THRESHOLD} · "
    f"outlier z ≥ {config.OUTLIER_Z}"
)

try:
    df = rm.load_portfolio(config.DATA_PATH)
except FileNotFoundError:
    st.error(f"Data file not found: {config.DATA_PATH}. Run `python generate_data.py` first.")
    st.stop()

wide = rm.returns_matrix(df)

# --- Headline metrics ------------------------------------------------------ #
pairs = rm.crowded_pairs(wide, config.CORR_THRESHOLD)
vol_alerts = rm.volatility_alerts(wide, config.VOL_WINDOW, config.VOL_THRESHOLD)
dq = rm.data_quality_check(df, config.OUTLIER_Z)
dq_issues = (
    sum(len(v) for v in dq.missing_dates.values()) + dq.duplicates + len(dq.outliers)
)

c1, c2, c3 = st.columns(3)
c1.metric("Crowded trades", len(pairs))
c2.metric("Volatility alerts", len(vol_alerts))
c3.metric("Data-quality issues", dq_issues)

st.divider()

# --- Correlation heatmap --------------------------------------------------- #
st.subheader("Correlation matrix — crowded trades")
corr = rm.correlation_matrix(wide)
fig, ax = plt.subplots(figsize=(6, 4))
sns.heatmap(corr, annot=True, cmap="coolwarm", vmin=-1, vmax=1, center=0, ax=ax)
st.pyplot(fig)
if pairs.empty:
    st.success(f"No pairs above correlation {config.CORR_THRESHOLD}.")
else:
    for _, r in pairs.iterrows():
        st.warning(f"Crowded trade: **{r.PairA}/{r.PairB}** correlation = {r.Correlation}")

st.divider()

# --- Rolling volatility ---------------------------------------------------- #
st.subheader("Rolling volatility — regime shifts")
vol = rm.rolling_volatility(wide, config.VOL_WINDOW)
st.line_chart(vol)
if vol_alerts.empty:
    st.success(f"No tickers above rolling-vol threshold {config.VOL_THRESHOLD}.")
else:
    for _, r in vol_alerts.iterrows():
        st.warning(f"Volatility spike in **{r.Ticker}**: rolling vol = {r.RollingVol}")

st.divider()

# --- Data-quality report --------------------------------------------------- #
st.subheader("Data-quality report")
col_a, col_b = st.columns(2)
with col_a:
    st.markdown("**Missing business days**")
    missing = {t: len(d) for t, d in dq.missing_dates.items() if d}
    st.write(missing or "None")
    st.markdown(f"**Duplicate rows:** {dq.duplicates}")
with col_b:
    st.markdown("**Outlier returns**")
    st.write(dq.outliers if not dq.outliers.empty else "None")
