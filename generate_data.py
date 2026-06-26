from __future__ import annotations

import os

import numpy as np
import pandas as pd

import config

SEED = 7
AS_OF = pd.Timestamp(config.AS_OF_DATE)


TRADING_WINDOW = 12

TRADES_PER_DESK_PER_DAY = 8


SYMBOLS = {
    "US Equities":    ["AAPL", "MSFT", "NVDA", "AMZN", "JPM"],
    "Index Futures":  ["ESU6", "NQU6", "RTYU6", "YMU6"],
    "Equity Options": ["AAPL_C190", "MSFT_P400", "NVDA_C130", "SPY_P540"],
    "FX Spot/Fwd":    ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
    "Govt Bonds":     ["UST2Y", "UST5Y", "UST10Y", "UST30Y"],
}


PRICE_RANGE = {
    "US Equities":    (50, 950),
    "Index Futures":  (1800, 6000),
    "Equity Options": (1.0, 25.0),
    "FX Spot/Fwd":    (0.6, 160.0),
    "Govt Bonds":     (95.0, 105.0),
}

# Daily mark-path drift & vol per segment. The drift signs give the demo a
# P&L narrative: some desks clearly green, some clearly red. (Futures is the
# loser that's *also* messy on reconciliation — a coherent "problem desk".)
MARK_DRIFT = {
    "US Equities":    0.0035,   # up
    "Index Futures":  -0.0045,  # down
    "Equity Options": 0.0060,   # up (levered)
    "FX Spot/Fwd":    -0.0008,  # ~flat, slightly down
    "Govt Bonds":     0.0009,   # up modestly
}
MARK_VOL = {
    "US Equities":    0.012,
    "Index Futures":  0.011,
    "Equity Options": 0.030,
    "FX Spot/Fwd":    0.004,
    "Govt Bonds":     0.0015,
}


def _round_price(p: float) -> float:
    """Sane decimals: finer for low-priced instruments (FX, options)."""
    return round(float(p), 4 if p < 5 else 2)


def _trade_dates() -> pd.DatetimeIndex:
    return pd.bdate_range(end=AS_OF, periods=TRADING_WINDOW)


def generate_marks(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Daily end-of-day market price per symbol (a gentle random walk).

    These are the marks used to value positions for P&L: realized P&L is the
    fill-vs-cost on closed lots, unrealized is the open position marked at the
    latest price here.
    """
    rng = np.random.default_rng(SEED + 1)
    rows = []
    for seg, syms in SYMBOLS.items():
        lo, hi = PRICE_RANGE[seg]
        drift, vol = MARK_DRIFT[seg], MARK_VOL[seg]
        for s in syms:
            price = float(rng.uniform(lo, hi))
            for i, d in enumerate(dates):
                if i > 0:
                    price = max(price * (1 + drift + vol * float(rng.normal())), 0.01)
                rows.append({
                    "Date": d.normalize(), "Symbol": s, "Segment": seg,
                    "Price": _round_price(price),
                })
    return pd.DataFrame(rows)


def generate_trades(dates: pd.DatetimeIndex,
                    marks_lookup: dict[tuple, float]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build the internal 'truth' then derive a broker view with breaks.

    Fills are drawn near that day's mark for the symbol (small spread) so the
    resulting P&L is realistic rather than noise.
    """
    rng = np.random.default_rng(SEED)

    rows = []
    tid = 0
    for team, meta in config.TEAMS.items():
        seg = meta["segment"]
        broker = meta["broker"]
        symbols = SYMBOLS[seg]
        for d in dates:
            n = rng.poisson(TRADES_PER_DESK_PER_DAY)
            for _ in range(n):
                tid += 1
                sym = str(rng.choice(symbols))
                mark = marks_lookup[(d.normalize(), sym)]
                price = _round_price(mark * (1 + float(rng.normal(0, 0.0008))))
                rows.append({
                    "TradeID": f"T{tid:06d}",
                    "TradeDate": d.normalize(),
                    "Team": team,
                    "Segment": seg,
                    "Broker": broker,
                    "Symbol": sym,
                    "Side": rng.choice(["Buy", "Sell"]),
                    "Quantity": int(rng.integers(1, 50) * 100),
                    "Price": price,
                })
    internal = pd.DataFrame(rows)
    internal["Notional"] = (internal["Quantity"] * internal["Price"]).round(2)

    # ---- 2. Broker view: start as a perfect copy, then inject breaks ----- #
    broker = internal.copy(deep=True)

    # Helper: pick row indices whose TradeDate age (calendar days) falls in
    # [min_age, max_age], so we can place fresh vs. aged breaks deterministically.
    # Realistic recon: MOST breaks are fresh (max_age small); only a deliberate
    # handful are aged past the critical threshold.
    def pick(n: int, min_age: int = 0, max_age: int | None = None,
             exclude: set[int] | None = None) -> list[int]:
        exclude = exclude or set()
        age = (AS_OF - internal["TradeDate"]).dt.days
        mask = age >= min_age
        if max_age is not None:
            mask &= age <= max_age
        candidates = internal.index[mask].difference(list(exclude))
        if len(candidates) == 0:
            return []
        chosen = rng.choice(candidates, size=min(n, len(candidates)), replace=False)
        return list(map(int, chosen))

    used: set[int] = set()

    # (a) Quantity mismatches — mostly fresh, with 2 deliberately aged.
    qty_idx = pick(6, max_age=2, exclude=used) + pick(2, min_age=4, exclude=used)
    for i in set(qty_idx):
        broker.loc[i, "Quantity"] = int(broker.loc[i, "Quantity"]) + int(rng.choice([100, 200, 900, -100]))
        used.add(i)

    # (b) Price mismatches (beyond tolerance) — mostly fresh, 1 aged.
    px_idx = pick(5, max_age=2, exclude=used) + pick(1, min_age=5, exclude=used)
    for i in set(px_idx):
        bump = rng.choice([0.25, 0.5, 1.5, -0.75]) + config.PRICE_BREAK_TOLERANCE * 2
        broker.loc[i, "Price"] = round(float(broker.loc[i, "Price"]) + float(bump), 2)
        used.add(i)

    # (c) Side mismatches (Buy vs Sell) — rare but serious; one fresh, one aged.
    side_idx = pick(1, max_age=1, exclude=used) + pick(1, min_age=3, exclude=used)
    for i in side_idx:
        broker.loc[i, "Side"] = "Sell" if broker.loc[i, "Side"] == "Buy" else "Buy"
        used.add(i)

    # Recompute broker notionals after qty/price edits.
    broker["Notional"] = (broker["Quantity"] * broker["Price"]).round(2)

    # (d) Missing at broker — we booked it, broker has no record. Mostly fresh,
    #     with 2 OLD ones (the scary unconfirmed trades aging past 3 days).
    miss_broker = pick(4, max_age=2, exclude=used) + pick(2, min_age=4, exclude=used)
    miss_broker = set(miss_broker)
    used |= miss_broker
    broker = broker[~broker.index.isin(miss_broker)].copy()

    # (e) Missing internally — broker alleges trades we never booked. Create
    #     fresh broker-only rows (new TradeIDs absent from internal).
    alleged = []
    for k in range(4):
        team = rng.choice(list(config.TEAMS.keys()))
        meta = config.TEAMS[team]
        seg = meta["segment"]
        lo, hi = PRICE_RANGE[seg]
        # Mix of fresh and a few-days-old alleged trades.
        d = (AS_OF - pd.Timedelta(days=int(rng.choice([0, 1, 2, 5])))).normalize()
        price = round(float(rng.uniform(lo, hi)), 2)
        qty = int(rng.integers(1, 50) * 100)
        alleged.append({
            "TradeID": f"B{90000 + k:06d}",
            "TradeDate": d,
            "Team": team,
            "Segment": seg,
            "Broker": meta["broker"],
            "Symbol": rng.choice(SYMBOLS[seg]),
            "Side": rng.choice(["Buy", "Sell"]),
            "Quantity": qty,
            "Price": price,
            "Notional": round(qty * price, 2),
        })
    broker = pd.concat([broker, pd.DataFrame(alleged)], ignore_index=True)

    internal = internal.sort_values("TradeID").reset_index(drop=True)
    broker = broker.sort_values("TradeID").reset_index(drop=True)
    return internal, broker

def generate_portfolio() -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    n_days = 180
    tickers = ["AAPL", "MSFT", "EURUSD", "GOVT", "GLD"]
    dates = pd.bdate_range(end=AS_OF, periods=n_days)

    vol = {"AAPL": 0.018, "MSFT": 0.017, "EURUSD": 0.005, "GOVT": 0.002, "GLD": 0.010}
    mu = {"AAPL": 0.0006, "MSFT": 0.0006, "EURUSD": 0.0, "GOVT": 0.0001, "GLD": 0.0002}
    returns = {t: rng.normal(mu[t], vol[t], n_days) for t in tickers}

    returns["MSFT"] = 0.9 * returns["AAPL"] + 0.1 * returns["MSFT"]  # crowded pair
    returns["EURUSD"][-20:] *= 6.0                                   # vol regime shift

    rows = []
    for t in tickers:
        position = rng.integers(50, 500) * 1000
        for d, r in zip(dates, returns[t]):
            rows.append({"Date": d.normalize(), "Ticker": t,
                         "PositionSize": position, "Return": round(float(r), 6)})
    df = pd.DataFrame(rows)

    # Inject data-quality issues for the risk panel to flag.
    df.loc[(df["Ticker"] == "GLD") & (df["Date"] == dates[100].normalize()), "Return"] = 0.45
    df = pd.concat([df, df.iloc[[10]].copy()], ignore_index=True)  # duplicate
    gap = dates[60].normalize()
    df = df[~((df["Ticker"] == "AAPL") & (df["Date"] == gap))]      # missing day
    return df.sort_values(["Date", "Ticker"]).reset_index(drop=True)


def main() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)

    dates = _trade_dates()
    marks = generate_marks(dates)
    marks_lookup = {(r.Date, r.Symbol): r.Price for r in marks.itertuples()}

    internal, broker = generate_trades(dates, marks_lookup)
    internal.to_parquet(config.INTERNAL_TRADES_PATH, index=False)
    broker.to_parquet(config.BROKER_TRADES_PATH, index=False)
    marks.to_parquet(config.MARKS_PATH, index=False)

    portfolio = generate_portfolio()
    portfolio.to_parquet(config.PORTFOLIO_PATH, index=False)

    print(f"internal_trades : {len(internal):>5} rows -> {config.INTERNAL_TRADES_PATH}")
    print(f"broker_trades   : {len(broker):>5} rows -> {config.BROKER_TRADES_PATH}")
    print(f"marks           : {len(marks):>5} rows -> {config.MARKS_PATH}")
    print(f"portfolio       : {len(portfolio):>5} rows -> {config.PORTFOLIO_PATH}")


if __name__ == "__main__":
    main()
