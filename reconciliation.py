"""Trade reconciliation: internal book vs clearing broker.

This is the engine behind the Breaks & Reconciliation board. It answers the
three questions a quant COO actually asks every morning:

  1. What doesn't match?   (break detection + classification)
  2. How many, per desk?   (break count)
  3. How long has it sat?  (break aging — the #1 fear above 3 days)

Reconciliation == matching two trade files on TradeID and explaining every
row that doesn't agree. A clean trade appears identically on both sides; a
"break" is anything else.

Mirrors the style of risk_monitor.py: pure functions + a small CLI.
"""

from __future__ import annotations

import argparse

import pandas as pd

import config

AS_OF = pd.Timestamp(config.AS_OF_DATE)

# Fields that must agree for two matched trades to be considered clean.
_COMPARE_FIELDS = ["Side", "Quantity", "Price"]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_trades(internal_path: str = config.INTERNAL_TRADES_PATH,
                broker_path: str = config.BROKER_TRADES_PATH
                ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load internal and broker trade Parquet files."""
    internal = pd.read_parquet(internal_path)
    broker = pd.read_parquet(broker_path)
    return internal, broker


# --------------------------------------------------------------------------- #
# Aging helpers
# --------------------------------------------------------------------------- #
def _age_days(trade_date: pd.Series, as_of: pd.Timestamp = AS_OF) -> pd.Series:
    """Calendar days a trade (hence its break) has been unresolved."""
    return (as_of.normalize() - pd.to_datetime(trade_date).dt.normalize()).dt.days


def severity(age: int) -> str:
    """Map break age to a COO-facing severity label."""
    if age >= config.BREAK_AGING_CRITICAL_DAYS:
        return "Critical"
    if age >= config.BREAK_AGING_WARNING_DAYS:
        return "Warning"
    return "New"


def aging_bucket(age: int) -> str:
    """Human-readable aging bucket for grouping/charts."""
    if age >= config.BREAK_AGING_CRITICAL_DAYS:
        return f"{config.BREAK_AGING_CRITICAL_DAYS}+ days"
    if age >= config.BREAK_AGING_WARNING_DAYS:
        return "1-2 days"
    return "Today"


# --------------------------------------------------------------------------- #
# Core reconciliation
# --------------------------------------------------------------------------- #
def reconcile(internal: pd.DataFrame, broker: pd.DataFrame,
              as_of: pd.Timestamp = AS_OF) -> pd.DataFrame:
    """Return one row per break.

    Columns: TradeID, Team, Segment, Broker, Symbol, BreakType, Detail,
             TradeDate, AgeDays, AgingBucket, Severity, Notional.

    A break is any TradeID that is not present-and-identical on both sides:
      * Missing at broker   — in internal only (booked, unconfirmed)
      * Missing internally  — in broker only   (alleged by broker)
      * Field mismatch      — present on both, but Side/Quantity/Price differ
    """
    merged = internal.merge(
        broker, on="TradeID", how="outer",
        suffixes=("_int", "_brk"), indicator=True,
    )

    breaks: list[dict] = []

    for _, row in merged.iterrows():
        side = row["_merge"]

        # Identity columns: prefer the internal side, fall back to broker.
        def pick(col: str):
            v = row.get(f"{col}_int")
            return v if pd.notna(v) else row.get(f"{col}_brk")

        base = {
            "TradeID": row["TradeID"],
            "Team": pick("Team"),
            "Segment": pick("Segment"),
            "Broker": pick("Broker"),
            "Symbol": pick("Symbol"),
            "TradeDate": pick("TradeDate"),
            "Notional": pick("Notional"),
        }

        if side == "left_only":
            base.update(BreakType="Missing at broker",
                        Detail="Booked internally; no broker confirmation")
            breaks.append(base)
        elif side == "right_only":
            base.update(BreakType="Missing internally",
                        Detail="Broker alleges trade; no internal record")
            breaks.append(base)
        else:  # both sides present — compare the economic fields
            diffs = []
            for f in _COMPARE_FIELDS:
                iv, bv = row[f"{f}_int"], row[f"{f}_brk"]
                if f == "Price":
                    if abs(float(iv) - float(bv)) > config.PRICE_BREAK_TOLERANCE:
                        diffs.append(f"Price {iv} vs {bv}")
                elif iv != bv:
                    diffs.append(f"{f} {iv} vs {bv}")
            if diffs:
                kinds = ", ".join(d.split()[0] for d in diffs)
                base.update(BreakType=f"Field mismatch ({kinds})",
                            Detail="; ".join(diffs))
                breaks.append(base)

    cols = ["TradeID", "Team", "Segment", "Broker", "Symbol", "BreakType",
            "Detail", "TradeDate", "AgeDays", "AgingBucket", "Severity", "Notional"]
    if not breaks:
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame(breaks)
    df["TradeDate"] = pd.to_datetime(df["TradeDate"])
    df["AgeDays"] = _age_days(df["TradeDate"], as_of)
    df["AgingBucket"] = df["AgeDays"].apply(aging_bucket)
    df["Severity"] = df["AgeDays"].apply(severity)
    return df[cols].sort_values(["AgeDays", "Team"], ascending=[False, True]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #
def summary_by_team(internal: pd.DataFrame, breaks: pd.DataFrame) -> pd.DataFrame:
    """Per-team scorecard: trade & break counts, criticals, oldest break."""
    trades = internal.groupby("Team").size().rename("Trades")

    rows = []
    for team in config.TEAMS:
        tb = breaks[breaks["Team"] == team]
        rows.append({
            "Team": team,
            "Segment": config.TEAMS[team]["segment"],
            "Broker": config.TEAMS[team]["broker"],
            "Trades": int(trades.get(team, 0)),
            "Breaks": int(len(tb)),
            "Critical (3+ days)": int((tb["Severity"] == "Critical").sum()) if len(tb) else 0,
            "Oldest (days)": int(tb["AgeDays"].max()) if len(tb) else 0,
            "Break Notional": float(tb["Notional"].sum()) if len(tb) else 0.0,
        })
    out = pd.DataFrame(rows)
    out["Break %"] = (out["Breaks"] / out["Trades"].replace(0, pd.NA) * 100).round(1)
    return out.sort_values(["Critical (3+ days)", "Breaks"], ascending=False).reset_index(drop=True)


def summary_by_type(breaks: pd.DataFrame) -> pd.DataFrame:
    """Break counts grouped by break type."""
    if breaks.empty:
        return pd.DataFrame(columns=["BreakType", "Count"])
    return (breaks.groupby("BreakType").size().rename("Count")
            .reset_index().sort_values("Count", ascending=False).reset_index(drop=True))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def run_report(internal_path: str = config.INTERNAL_TRADES_PATH,
               broker_path: str = config.BROKER_TRADES_PATH) -> None:
    internal, broker = load_trades(internal_path, broker_path)
    breaks = reconcile(internal, broker)

    print("=" * 70)
    print(" TRADE RECONCILIATION — internal book vs clearing broker")
    print(f" As of {AS_OF.date()}   |   {len(internal)} internal, {len(broker)} broker trades")
    print("=" * 70)

    total = len(breaks)
    critical = int((breaks["Severity"] == "Critical").sum()) if total else 0
    print(f"\n  {total} break(s) | {critical} CRITICAL (unresolved >= "
          f"{config.BREAK_AGING_CRITICAL_DAYS} days)\n")

    if critical:
        print(f"  !! COO ALERT: {critical} break(s) aging past "
              f"{config.BREAK_AGING_CRITICAL_DAYS} days !!\n")

    print("-- Per team " + "-" * 56)
    print(summary_by_team(internal, breaks).to_string(index=False))

    print("\n-- By break type " + "-" * 51)
    print(summary_by_type(breaks).to_string(index=False))

    if critical:
        print("\n-- Critical breaks (oldest first) " + "-" * 34)
        crit = breaks[breaks["Severity"] == "Critical"][
            ["TradeID", "Team", "Symbol", "BreakType", "AgeDays", "Detail"]]
        print(crit.to_string(index=False))
    print("\n" + "=" * 70)


def main() -> None:
    p = argparse.ArgumentParser(description="Trade reconciliation (CLI).")
    p.add_argument("--internal", default=config.INTERNAL_TRADES_PATH)
    p.add_argument("--broker", default=config.BROKER_TRADES_PATH)
    args = p.parse_args()
    run_report(args.internal, args.broker)


if __name__ == "__main__":
    main()
