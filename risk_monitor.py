from __future__ import annotations

import argparse
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import config

# Defaults sourced from config.py (single source of truth).
DEFAULT_DATA = config.DATA_PATH
CORR_THRESHOLD = config.CORR_THRESHOLD
VOL_WINDOW = config.VOL_WINDOW
VOL_THRESHOLD = config.VOL_THRESHOLD  # daily rolling stdev = regime shift
OUTLIER_Z = config.OUTLIER_Z          # |z-score| above which a return is an outlier


# --------------------------------------------------------------------------- #
# Loading / reshaping
# --------------------------------------------------------------------------- #
def load_portfolio(path: str = DEFAULT_DATA) -> pd.DataFrame:
    """Load the long-format portfolio data (Date, Ticker, PositionSize, Return).

    Supports Parquet (preferred, fast) or CSV based on the file extension.
    """
    if str(path).endswith(".parquet"):
        df = pd.read_parquet(path)
        df["Date"] = pd.to_datetime(df["Date"])
    else:
        df = pd.read_csv(path, parse_dates=["Date"])
    return df


def returns_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """Pivot long-format returns into a Date x Ticker wide matrix.

    Duplicate (Date, Ticker) rows are averaged so the pivot is well defined.
    """
    return df.pivot_table(index="Date", columns="Ticker", values="Return", aggfunc="mean")


# --------------------------------------------------------------------------- #
# 1. Correlation / crowded trades
# --------------------------------------------------------------------------- #
def correlation_matrix(wide: pd.DataFrame) -> pd.DataFrame:
    return wide.corr()


def crowded_pairs(wide: pd.DataFrame, threshold: float = CORR_THRESHOLD) -> pd.DataFrame:
    """Return ticker pairs whose return correlation exceeds ``threshold``."""
    corr = correlation_matrix(wide)
    pairs = []
    cols = corr.columns
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            c = corr.iloc[i, j]
            if pd.notna(c) and abs(c) >= threshold:
                pairs.append({"PairA": cols[i], "PairB": cols[j], "Correlation": round(float(c), 3)})
    return pd.DataFrame(pairs).sort_values("Correlation", ascending=False).reset_index(drop=True) \
        if pairs else pd.DataFrame(columns=["PairA", "PairB", "Correlation"])


# --------------------------------------------------------------------------- #
# 2. Volatility monitor
# --------------------------------------------------------------------------- #
def rolling_volatility(wide: pd.DataFrame, window: int = VOL_WINDOW) -> pd.DataFrame:
    """Rolling standard deviation of returns per ticker."""
    return wide.rolling(window=window, min_periods=max(2, window // 2)).std()


def volatility_alerts(wide: pd.DataFrame, window: int = VOL_WINDOW,
                      threshold: float = VOL_THRESHOLD) -> pd.DataFrame:
    """Flag tickers whose latest rolling vol exceeds ``threshold``."""
    vol = rolling_volatility(wide, window)
    latest = vol.iloc[-1]
    alerts = [
        {"Ticker": t, "RollingVol": round(float(v), 4), "Threshold": threshold}
        for t, v in latest.items()
        if pd.notna(v) and v >= threshold
    ]
    return pd.DataFrame(alerts).sort_values("RollingVol", ascending=False).reset_index(drop=True) \
        if alerts else pd.DataFrame(columns=["Ticker", "RollingVol", "Threshold"])


# --------------------------------------------------------------------------- #
# 3. Data-quality check
# --------------------------------------------------------------------------- #
@dataclass
class DataQualityReport:
    missing_dates: dict = field(default_factory=dict)   # ticker -> [dates]
    duplicates: int = 0
    outliers: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def is_clean(self) -> bool:
        return (
            not any(self.missing_dates.values())
            and self.duplicates == 0
            and self.outliers.empty
        )


def data_quality_check(df: pd.DataFrame, outlier_z: float = OUTLIER_Z) -> DataQualityReport:
    """Detect missing business days, duplicate rows, and outlier returns."""
    report = DataQualityReport()

    # Missing business days per ticker, relative to the full date span.
    full_range = pd.bdate_range(df["Date"].min(), df["Date"].max())
    for ticker, grp in df.groupby("Ticker"):
        present = set(grp["Date"])
        missing = [d.date().isoformat() for d in full_range if d not in present]
        report.missing_dates[ticker] = missing

    # Duplicate (Date, Ticker) rows.
    report.duplicates = int(df.duplicated(subset=["Date", "Ticker"]).sum())

    # Outlier returns via per-ticker z-score.
    outlier_rows = []
    for ticker, grp in df.groupby("Ticker"):
        mu, sigma = grp["Return"].mean(), grp["Return"].std()
        if sigma == 0 or pd.isna(sigma):
            continue
        z = (grp["Return"] - mu) / sigma
        flagged = grp[z.abs() >= outlier_z]
        for _, row in flagged.iterrows():
            outlier_rows.append({
                "Date": row["Date"].date().isoformat(),
                "Ticker": ticker,
                "Return": round(float(row["Return"]), 4),
                "ZScore": round(float((row["Return"] - mu) / sigma), 1),
            })
    report.outliers = pd.DataFrame(outlier_rows)
    return report


# --------------------------------------------------------------------------- #
# CLI reporting
# --------------------------------------------------------------------------- #
def run_report(path: str = DEFAULT_DATA, corr_threshold: float = CORR_THRESHOLD,
               vol_threshold: float = VOL_THRESHOLD, vol_window: int = VOL_WINDOW) -> None:
    df = load_portfolio(path)
    wide = returns_matrix(df)

    print("=" * 64)
    print(" OPERATIONAL RISK DASHBOARD — a COO's lens into quant health")
    print("=" * 64)
    print(f" Source: {path}   |   {df['Ticker'].nunique()} tickers, "
          f"{wide.shape[0]} trading days\n")

    # 1. Crowded trades
    print("-- Crowded Trades (correlation) " + "-" * 31)
    pairs = crowded_pairs(wide, corr_threshold)
    if pairs.empty:
        print("  OK  No pairs above correlation threshold "
              f"({corr_threshold}).")
    else:
        for _, r in pairs.iterrows():
            print(f"  WARN  Crowded trade: {r.PairA}/{r.PairB} "
                  f"correlation = {r.Correlation}")
    print()

    # 2. Volatility
    print("-- Volatility Monitor " + "-" * 41)
    vol = volatility_alerts(wide, vol_window, vol_threshold)
    if vol.empty:
        print(f"  OK  No tickers above rolling-vol threshold "
              f"({vol_threshold}).")
    else:
        for _, r in vol.iterrows():
            print(f"  WARN  Volatility spike in {r.Ticker}: "
                  f"rolling vol = {r.RollingVol} (> {r.Threshold})")
    print()

    # 3. Data quality
    print("-- Data Quality " + "-" * 47)
    dq = data_quality_check(df)
    any_missing = {t: d for t, d in dq.missing_dates.items() if d}
    if any_missing:
        for t, d in any_missing.items():
            print(f"  WARN  {t}: {len(d)} missing day(s), e.g. {d[:3]}")
    else:
        print("  OK  No missing business days.")
    if dq.duplicates:
        print(f"  WARN  {dq.duplicates} duplicate (Date, Ticker) row(s).")
    else:
        print("  OK  No duplicate rows.")
    if not dq.outliers.empty:
        for _, r in dq.outliers.iterrows():
            print(f"  WARN  Outlier return: {r.Ticker} {r.Date} "
                  f"= {r.Return} (z={r.ZScore})")
    else:
        print("  OK  No outlier returns.")
    print()
    print("=" * 64)


def main() -> None:
    p = argparse.ArgumentParser(description="Operational Risk Dashboard (CLI).")
    p.add_argument("--data", default=DEFAULT_DATA, help="Path to portfolio CSV.")
    p.add_argument("--corr-threshold", type=float, default=CORR_THRESHOLD)
    p.add_argument("--vol-threshold", type=float, default=VOL_THRESHOLD)
    p.add_argument("--vol-window", type=int, default=VOL_WINDOW)
    args = p.parse_args()
    run_report(args.data, args.corr_threshold, args.vol_threshold, args.vol_window)


if __name__ == "__main__":
    main()
