AS_OF_DATE = "2026-06-24"

# --- Data paths
DATA_DIR = "data"
PORTFOLIO_PATH = "data/portfolio.parquet"          # risk panel (returns/vol/corr)
INTERNAL_TRADES_PATH = "data/internal_trades.parquet"  # our books
BROKER_TRADES_PATH = "data/broker_trades.parquet"      # clearing broker's report

# --- Teams & market segments ----------------------------------------------- #

TEAMS = {
    "Equities Desk": {"segment": "US Equities",    "broker": "Goldman Sachs"},
    "Futures Desk":  {"segment": "Index Futures",  "broker": "Morgan Stanley"},
    "Options Desk":  {"segment": "Equity Options", "broker": "JP Morgan"},
    "FX Desk":       {"segment": "FX Spot/Fwd",    "broker": "Citi"},
    "Rates Desk":    {"segment": "Govt Bonds",     "broker": "Barclays"},
}

# --- Reconciliation
# unreconciled break sitting unresolved for 3 days.
BREAK_AGING_CRITICAL_DAYS = 3   # Critical (red)
BREAK_AGING_WARNING_DAYS = 1    # Warning (amber)
PRICE_BREAK_TOLERANCE = 0.005   # |price diff| 

# --- Risk panel thresholds

CORR_THRESHOLD = 0.8
VOL_WINDOW = 20        # rolling window in trading days
VOL_THRESHOLD = 0.025  # daily rolling stdev that counts as a regime shift

OUTLIER_Z = 6.0


DATA_PATH = PORTFOLIO_PATH
