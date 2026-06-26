"""Real-time P&L by team: realized vs unrealized, daily + month-to-date.

The JD's "real-time P&L across equities, futures, options" — the view a COO
running 4-5 desks lives in. Mirrors the style of reconciliation.py and
risk_monitor.py: pure functions over the parquet data + a small CLI.

Accounting model
----------------
We treat the internal book as the source of truth and value it against the
daily marks (data/marks.parquet):

  * Realized P&L   — locked in when a trade *reduces* a position, using
                     average-cost: closing_qty * (exit - avg_cost).
  * Unrealized P&L — the open position marked to the latest price:
                     position * (mark_now - avg_cost).
  * Daily P&L      — standard close-to-close decomposition per symbol:
                     start_position * (mark_d - mark_{d-1})            (price move)
                     + Σ trades_d  side * qty * (mark_d - fill)        (new fills)
                     Each day splits into Realized (the average-cost piece) and
                     Unrealized (the remainder = mark-to-market change).

Invariant (checked in the CLI): Σ daily Total == Realized total + Unrealized now.
"""

from __future__ import annotations

import argparse
from collections import defaultdict

import pandas as pd

import config

AS_OF = pd.Timestamp(config.AS_OF_DATE)

_DAY_COLS = ["Day Realized", "Day Unrealized", "Day Total"]
_MTD_COLS = ["MTD Realized", "MTD Unrealized", "MTD Total"]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_inputs(internal_path: str = config.INTERNAL_TRADES_PATH,
                marks_path: str = config.MARKS_PATH) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load the internal book and the daily marks."""
    internal = pd.read_parquet(internal_path)
    marks = pd.read_parquet(marks_path)
    return internal, marks


def _month_start(as_of: pd.Timestamp) -> pd.Timestamp:
    return as_of.normalize().replace(day=1)


# --------------------------------------------------------------------------- #
# Per-symbol P&L walk (average-cost realized + mark-to-market)
# --------------------------------------------------------------------------- #
def _symbol_series(trades_sym: pd.DataFrame, marks_sym: pd.Series,
                   as_of: pd.Timestamp) -> dict:
    """Walk one (team, symbol)'s trades against its mark path.

    Returns daily realized/total dicts plus the closing position snapshot.
    """
    marks_sym = marks_sym[marks_sym.index <= as_of].sort_index()
    realized_by_date: dict = defaultdict(float)
    daily_total: dict = {}

    trades_by_date = {d: g for d, g in trades_sym.groupby(trades_sym["TradeDate"].dt.normalize())}

    pos = 0.0      # signed position (long > 0, short < 0)
    avg = 0.0      # average cost of the open position
    prev_mark = None

    for d in marks_sym.index:
        mark = float(marks_sym.loc[d])
        pnl = 0.0
        if prev_mark is not None:
            pnl += pos * (mark - prev_mark)          # price move on opening position

        for t in trades_by_date.get(d, pd.DataFrame()).itertuples():
            qty, price = float(t.Quantity), float(t.Price)
            signed = qty if t.Side == "Buy" else -qty

            if pos != 0 and (pos > 0) != (signed > 0):     # reducing / closing
                closing = min(abs(signed), abs(pos))
                realized_by_date[d] += closing * ((price - avg) if pos > 0 else (avg - price))
                new_pos = pos + signed
                if new_pos != 0 and (new_pos > 0) != (pos > 0):
                    avg = price                            # flipped: remainder opens new lot
                pos = new_pos
                if pos == 0:
                    avg = 0.0
            else:                                          # opening / adding
                new_abs = abs(pos) + abs(signed)
                avg = (avg * abs(pos) + price * abs(signed)) / new_abs
                pos += signed

            pnl += signed * (mark - price)                 # fill-to-close on new trade

        daily_total[d] = pnl
        prev_mark = mark

    mark_now = float(marks_sym.iloc[-1]) if len(marks_sym) else 0.0
    return {
        "realized_by_date": realized_by_date,
        "daily_total": daily_total,
        "position": pos,
        "avg_cost": avg,
        "mark": mark_now,
        "open_unrealized": pos * (mark_now - avg),
    }


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def daily_pnl(internal: pd.DataFrame, marks: pd.DataFrame,
              as_of: pd.Timestamp = AS_OF) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (daily, positions).

    daily:     Date, Team, Symbol, Realized, Unrealized, Total  (one row/day/symbol)
    positions: Team, Segment, Symbol, Position, AvgCost, Mark, OpenUnrealized
    """
    internal = internal.copy()
    internal["TradeDate"] = pd.to_datetime(internal["TradeDate"])
    marks = marks.copy()
    marks["Date"] = pd.to_datetime(marks["Date"]).dt.normalize()
    mark_by_symbol = {s: g.set_index("Date")["Price"] for s, g in marks.groupby("Symbol")}

    records, positions = [], []
    for (team, sym), tg in internal.groupby(["Team", "Symbol"]):
        ms = mark_by_symbol.get(sym)
        if ms is None or ms.empty:
            continue
        res = _symbol_series(tg, ms, as_of)
        for d, total in res["daily_total"].items():
            realized = res["realized_by_date"].get(d, 0.0)
            records.append({"Date": d, "Team": team, "Symbol": sym,
                            "Realized": realized, "Unrealized": total - realized, "Total": total})
        positions.append({
            "Team": team, "Segment": tg["Segment"].iloc[0], "Symbol": sym,
            "Position": res["position"], "AvgCost": res["avg_cost"],
            "Mark": res["mark"], "OpenUnrealized": res["open_unrealized"],
        })

    daily = pd.DataFrame(records)
    pos_df = pd.DataFrame(positions)
    return daily, pos_df


def summary_by_team(daily: pd.DataFrame, pos_df: pd.DataFrame,
                    as_of: pd.Timestamp = AS_OF) -> pd.DataFrame:
    """Per-team P&L scorecard: day & MTD realized/unrealized/total + open MTM."""
    mstart = _month_start(as_of)
    day = daily[daily["Date"] == as_of]
    mtd = daily[(daily["Date"] >= mstart) & (daily["Date"] <= as_of)]

    def agg(df: pd.DataFrame) -> pd.DataFrame:
        return df.groupby("Team")[["Realized", "Unrealized", "Total"]].sum()

    d = agg(day).rename(columns=lambda c: f"Day {c}")
    m = agg(mtd).rename(columns=lambda c: f"MTD {c}")
    open_u = pos_df.groupby("Team")["OpenUnrealized"].sum().rename("Open Unrealized")

    out = pd.concat([d, m, open_u], axis=1)
    out = out.reindex(list(config.TEAMS.keys())).fillna(0.0)
    out.insert(0, "Segment", [config.TEAMS[t]["segment"] for t in out.index])
    return out.reset_index().rename(columns={"index": "Team"})


def firm_totals(team_summary: pd.DataFrame) -> dict:
    """Aggregate the per-team scorecard into firm-level numbers."""
    num = team_summary.select_dtypes("number").sum()
    return {k: float(v) for k, v in num.items()}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def run_report(internal_path: str = config.INTERNAL_TRADES_PATH,
               marks_path: str = config.MARKS_PATH) -> None:
    internal, marks = load_inputs(internal_path, marks_path)
    daily, pos_df = daily_pnl(internal, marks)
    team = summary_by_team(daily, pos_df)
    firm = firm_totals(team)

    print("=" * 78)
    print(" REAL-TIME P&L BY TEAM — realized vs unrealized, daily + MTD")
    print(f" As of {AS_OF.date()}   |   {len(internal)} trades, "
          f"{pos_df['Symbol'].nunique()} symbols, {len(config.TEAMS)} desks")
    print("=" * 78)

    def money(x: float) -> str:
        return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"

    print(f"\n  FIRM  Day {money(firm['Day Total'])}  "
          f"(R {money(firm['Day Realized'])} / U {money(firm['Day Unrealized'])})"
          f"   |   MTD {money(firm['MTD Total'])}  "
          f"(R {money(firm['MTD Realized'])} / U {money(firm['MTD Unrealized'])})\n")

    show = team.copy()
    for c in _DAY_COLS + _MTD_COLS + ["Open Unrealized"]:
        show[c] = show[c].map(money)
    print(show.to_string(index=False))

    # P&L invariant: cumulative daily == realized-to-date + unrealized now.
    cum_daily = float(daily["Total"].sum())
    realized_total = float(daily["Realized"].sum())
    unreal_now = float(pos_df["OpenUnrealized"].sum())
    drift = cum_daily - (realized_total + unreal_now)
    print(f"\n  check  sum(daily) {money(cum_daily)} == realized {money(realized_total)} "
          f"+ open unrealized {money(unreal_now)}  (residual {money(drift)})")
    print("=" * 78)


def main() -> None:
    p = argparse.ArgumentParser(description="Real-time P&L by team (CLI).")
    p.add_argument("--internal", default=config.INTERNAL_TRADES_PATH)
    p.add_argument("--marks", default=config.MARKS_PATH)
    args = p.parse_args()
    run_report(args.internal, args.marks)


if __name__ == "__main__":
    main()
