"""
SwingAnalysis — reusable Fibonacci + Market-Stage analysis module
=================================================================

Import this module in any flow to analyse shortlisted stocks.

PUBLIC API
----------
  analyze_symbol(kite, symbol, token, shortlist_date, today, config=None)
      -> dict of all computed fields for one symbol

  run_batch(kite, rows, instrument_map, config=None)
      -> list[dict]  (one dict per row; rows is an iterable of dicts with
                       keys "Symbol" and "Date")

  build_instrument_map(kite, exchange="NSE")
      -> {tradingsymbol: instrument_token}

  compute_market_stage(hist, shortlist_date, config=None)
      -> dict with MA30W, PriceVsMA30WPct, MA30WSlopePct,
                  WeinsteinStage, WyckoffPhase

  save_to_excel(results, output_file)
      -> writes Analysis + Legend sheets to output_file

  authenticate(api_key, api_secret)
      -> authenticated KiteConnect instance  (for standalone / CLI use;
         skip this when reusing an already-authenticated kite object)

CONFIGURATION
-------------
Pass a partial dict as `config` to override any DEFAULT_CONFIG key for
a single call without touching the module-level defaults:

  result = analyze_symbol(kite, "RELIANCE", token, date, today,
                          config={"lookahead_days": 90})

DEFAULT_CONFIG keys
-------------------
  lookahead_days          : 60    calendar days to scan after shortlist date
  fifty_two_week_days     : 364   calendar days for 52-week window
  rate_limit_sleep        : 0.35  seconds between API calls (~3 req/s)
  max_retries             : 5     retries per API call before giving up
  ma_weeks                : 30    rolling MA period (weeks)
  slope_lookback_weeks    : 4     weeks back to measure MA slope
  flat_slope_threshold_pct: 1.0   +/- % slope that counts as "flat"
"""

import os
import time
import datetime as dt

import pandas as pd

# ------------------------------------------------------------------ #
# DEFAULT CONFIGURATION
# ------------------------------------------------------------------ #
DEFAULT_CONFIG = {
    "lookahead_days": 60,
    "fifty_two_week_days": 364,
    "rate_limit_sleep": 0.35,
    "max_retries": 5,
    "ma_weeks": 30,
    "slope_lookback_weeks": 4,
    "flat_slope_threshold_pct": 1.0,
}

# Retracement levels (downside), shallow → deep
RETRACEMENT_LEVELS = [0, 23.6, 38.2, 50, 61.8, 78.6, 100,
                      -23.6, -38.2, -50, -61.8, -78.6, -100]

# Gain / extension levels (upside)
GAIN_LEVELS = [0, 23.6, 38.2, 50, 61.8, 78.6, 100, 123.6, 138.2,
               150, 161.8, 178.6, 200, 223.6, 238.2, 250, 261.8]

STANDARD_LEVELS = (0, 23.6, 38.2, 50, 61.8, 78.6, 100)

STAGE_TABLE = {
    ("above", "up"):   ("Stage 2 - Advancing (Markup)", "Markup"),
    ("above", "flat"): ("Stage 3 - Topping (Distribution)", "Distribution"),
    ("above", "down"): ("Stage 3/4 Transition (Topping, MA turning down)", "Late Distribution / Early Markdown"),
    ("below", "down"): ("Stage 4 - Declining (Markdown)", "Markdown"),
    ("below", "flat"): ("Stage 1 - Basing (Accumulation)", "Accumulation"),
    ("below", "up"):   ("Stage 1/2 Transition (Basing, MA turning up)", "Late Accumulation / Early Markup"),
}


def _cfg(config, key):
    """Resolve a config key: caller override → module default."""
    if config and key in config:
        return config[key]
    return DEFAULT_CONFIG[key]


# ------------------------------------------------------------------ #
# AUTHENTICATION  (for standalone / CLI use)
# ------------------------------------------------------------------ #
def authenticate(api_key, api_secret):
    """Interactive login — prints URL, reads request_token from stdin.

    Skip this when your flow already has an authenticated kite object.
    """
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key)
    print("\nLogin URL:\n  ", kite.login_url(), "\n")
    request_token = input("Paste request_token: ").strip()
    session = kite.generate_session(request_token, api_secret=api_secret)
    kite.set_access_token(session["access_token"])
    print("Authenticated as", session.get("user_name", session.get("user_id", "")))
    return kite


# ------------------------------------------------------------------ #
# INSTRUMENT LOOKUP
# ------------------------------------------------------------------ #
def build_instrument_map(kite, exchange="NSE"):
    """Fetch all EQ instruments for exchange → {tradingsymbol: token}.

    Makes a single API call; cache the result for the whole run.
    """
    print(f"Fetching {exchange} instrument list ...")
    instruments = _call_with_retry(kite.instruments, exchange)
    df = pd.DataFrame(instruments)

    if "segment" in df.columns and "instrument_type" in df.columns:
        eq_df = df[(df["segment"] == exchange) & (df["instrument_type"] == "EQ")]
    else:
        eq_df = df

    if eq_df.empty:
        raise RuntimeError(
            f"Instrument filter matched 0 of {len(df)} rows for {exchange}. "
            "Inspect df['segment'].unique() / df['instrument_type'].unique()."
        )

    print(f"  -> {len(eq_df)} {exchange} equities loaded")
    return dict(zip(eq_df["tradingsymbol"], eq_df["instrument_token"]))


# ------------------------------------------------------------------ #
# RATE-LIMITED API WRAPPER
# ------------------------------------------------------------------ #
def _call_with_retry(fn, *args, symbol="", config=None, **kwargs):
    max_retries = _cfg(config, "max_retries")
    rate_sleep = _cfg(config, "rate_limit_sleep")
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            result = fn(*args, **kwargs)
            time.sleep(rate_sleep)
            return result
        except Exception as e:
            last_err = e
            wait = rate_sleep * (2 ** attempt)
            print(f"  [{symbol}] API error attempt {attempt}/{max_retries}: {e} -> retry in {wait:.1f}s")
            time.sleep(wait)
    raise RuntimeError(f"[{symbol}] API call failed after {max_retries} retries: {last_err}")


def _fetch_daily_history(kite, token, from_date, to_date, symbol="", config=None):
    data = _call_with_retry(
        kite.historical_data,
        token, from_date, to_date, "day",
        symbol=symbol, config=config,
    )
    return pd.DataFrame(data)


# ------------------------------------------------------------------ #
# FIBONACCI HELPERS
# ------------------------------------------------------------------ #
def retracement_price(level, day_high, day_low):
    """Price at a retracement level. level=0 → day_high; level=100 → day_low."""
    swing = day_high - day_low
    if level >= 0:
        return day_high - (level / 100.0) * swing
    return day_low - (abs(level) / 100.0) * swing


def gain_price(level, day_high, day_low):
    """Price at a gain/extension level. level=0 → day_high; level=100 → day_high+swing."""
    swing = day_high - day_low
    return day_high + (level / 100.0) * swing


def nearest_standard_level(pct, levels=STANDARD_LEVELS):
    return min(levels, key=lambda l: abs(l - pct))


def first_date_at_or_below(window_df, price):
    """Earliest date in window_df whose Low <= price, or None."""
    hits = window_df[window_df["low"] <= price]
    return hits["date"].iloc[0] if not hits.empty else None


def first_date_at_or_above(window_df, price):
    """Earliest date in window_df whose High >= price, or None."""
    hits = window_df[window_df["high"] >= price]
    return hits["date"].iloc[0] if not hits.empty else None


# ------------------------------------------------------------------ #
# WEINSTEIN STAGE / WYCKOFF PHASE
# ------------------------------------------------------------------ #
def _classify_stage(price_vs_ma_pct, slope_pct, flat_thresh):
    position = "above" if price_vs_ma_pct > 0 else "below"
    if slope_pct > flat_thresh:
        slope = "up"
    elif slope_pct < -flat_thresh:
        slope = "down"
    else:
        slope = "flat"
    return STAGE_TABLE.get((position, slope), ("Undetermined", "Undetermined"))


def _insufficient_stage():
    return {
        "MA30W": None, "PriceVsMA30WPct": None, "MA30WSlopePct": None,
        "WeinsteinStage": "Insufficient data", "WyckoffPhase": "Insufficient data",
    }


def compute_market_stage(hist, shortlist_date, config=None):
    """Weinstein Stage / Wyckoff Phase using weekly 30-MA up to shortlist_date.

    hist : DataFrame with columns date, close (daily bars)
    """
    ma_weeks = _cfg(config, "ma_weeks")
    slope_lookback = _cfg(config, "slope_lookback_weeks")
    flat_thresh = _cfg(config, "flat_slope_threshold_pct")

    df = hist[hist["date"] <= shortlist_date].copy()
    if df.empty:
        return _insufficient_stage()

    df["date"] = pd.to_datetime(df["date"])
    weekly = df.set_index("date")["close"].resample("W-FRI").last().dropna()

    if len(weekly) < ma_weeks + slope_lookback:
        return _insufficient_stage()

    ma = weekly.rolling(ma_weeks).mean()
    ma_valid = ma.dropna()
    if len(ma_valid) <= slope_lookback:
        return _insufficient_stage()

    ma_now = ma_valid.iloc[-1]
    ma_prior = ma_valid.iloc[-1 - slope_lookback]
    price_now = weekly.iloc[-1]

    if ma_prior == 0 or pd.isna(ma_prior) or pd.isna(ma_now):
        return _insufficient_stage()

    slope_pct = (ma_now - ma_prior) / ma_prior * 100
    price_vs_ma_pct = (price_now - ma_now) / ma_now * 100
    weinstein_stage, wyckoff_phase = _classify_stage(price_vs_ma_pct, slope_pct, flat_thresh)

    return {
        "MA30W": round(float(ma_now), 2),
        "PriceVsMA30WPct": round(float(price_vs_ma_pct), 2),
        "MA30WSlopePct": round(float(slope_pct), 2),
        "WeinsteinStage": weinstein_stage,
        "WyckoffPhase": wyckoff_phase,
    }


# ------------------------------------------------------------------ #
# CORE PER-SYMBOL ANALYSIS
# ------------------------------------------------------------------ #
def analyze_symbol(kite, symbol, token, shortlist_date, today, config=None):
    """Fetch history and compute all Fibonacci + stage metrics for one symbol.

    Parameters
    ----------
    kite          : authenticated KiteConnect instance
    symbol        : e.g. "RELIANCE"
    token         : instrument_token (from build_instrument_map)
    shortlist_date: datetime.date — the scanner-hit date
    today         : datetime.date — upper bound for look-ahead window
    config        : optional dict to override DEFAULT_CONFIG keys

    Returns
    -------
    dict — all computed fields; includes "Error" key (and only that) on failure
    """
    lookahead = _cfg(config, "lookahead_days")
    w52_days = _cfg(config, "fifty_two_week_days")

    from_date = shortlist_date - dt.timedelta(days=w52_days + 30)
    to_date = min(shortlist_date + dt.timedelta(days=lookahead), today)

    hist = _fetch_daily_history(kite, token, from_date, to_date, symbol=symbol, config=config)
    if hist.empty:
        return {"Symbol": symbol, "ShortlistDate": shortlist_date, "Error": "No historical data returned"}

    hist["date"] = pd.to_datetime(hist["date"]).dt.date
    hist = hist.sort_values("date").reset_index(drop=True)

    sl_matches = hist.index[hist["date"] == shortlist_date].tolist()
    if not sl_matches:
        return {"Symbol": symbol, "ShortlistDate": shortlist_date,
                "Error": "Shortlist date has no trading data (holiday/not listed?)"}
    sl_idx = sl_matches[0]
    if sl_idx == 0:
        return {"Symbol": symbol, "ShortlistDate": shortlist_date,
                "Error": "No prior trading day in fetched window"}

    shortlist_row = hist.loc[sl_idx]
    prev_row = hist.loc[sl_idx - 1]
    prev_close = float(prev_row["close"])

    day_high = float(shortlist_row["high"])
    day_low = float(shortlist_row["low"])
    open_shortlist = float(shortlist_row["open"])

    result = {
        "Symbol": symbol,
        "ShortlistDate": shortlist_date,
        "PrevTradingDate": prev_row["date"],
        "PrevDayClose": prev_close,
        "ShortlistDayOpen": open_shortlist,
        "ShortlistDayHigh": day_high,
        "ShortlistDayLow": day_low,
        "GapUp": "Yes" if open_shortlist > prev_close else "No",
    }

    # RETRACEMENT window: every day strictly after shortlist date
    ret_window = hist.loc[sl_idx + 1:].sort_values("date").reset_index(drop=True)

    # ENTRY window: skip the very next trading day (shortlist+2 onward)
    entry_window = hist.loc[sl_idx + 2:].sort_values("date").reset_index(drop=True)

    # --- (1) Fibonacci Retracement ----------------------------------------
    if ret_window.empty:
        for lvl in RETRACEMENT_LEVELS:
            result[f"Fib_Retracement_{lvl}%"] = "No data yet"
            result[f"Fib_Retracement_{lvl}%_Date"] = None
        result["MAX_FBR"] = "No data yet"
    else:
        max_fbr = None
        for lvl in RETRACEMENT_LEVELS:
            level_price = retracement_price(lvl, day_high, day_low)
            hit_date = first_date_at_or_below(ret_window, level_price)
            hit = hit_date is not None
            result[f"Fib_Retracement_{lvl}%"] = "Yes" if hit else "No"
            result[f"Fib_Retracement_{lvl}%_Date"] = hit_date
            if hit:
                max_fbr = lvl
        result["MAX_FBR"] = max_fbr

    # --- (2) Absolute High/Low + Fibonacci Gain ---------------------------
    entry_price = day_high + 1
    result["EntryPrice"] = entry_price
    if entry_window.empty:
        result["AbsoluteHighAfter"] = None
        result["AbsoluteHighAfterDate"] = None
        result["AbsoluteLowAfter"] = None
        result["AbsoluteLowAfterDate"] = None
        result["EntryTriggered"] = "No data yet"
        result["EntryTriggerDate"] = None
        for lvl in GAIN_LEVELS:
            result[f"Fib_Gain_{lvl}%"] = "No data yet"
            result[f"Fib_Gain_{lvl}%_Date"] = None
        result["MAX_FBG"] = "No data yet"
    else:
        max_high_idx = entry_window["high"].idxmax()
        min_low_idx = entry_window["low"].idxmin()
        abs_high = float(entry_window.loc[max_high_idx, "high"])
        abs_low = float(entry_window.loc[min_low_idx, "low"])
        result["AbsoluteHighAfter"] = abs_high
        result["AbsoluteHighAfterDate"] = entry_window.loc[max_high_idx, "date"]
        result["AbsoluteLowAfter"] = abs_low
        result["AbsoluteLowAfterDate"] = entry_window.loc[min_low_idx, "date"]

        entry_hit_date = first_date_at_or_above(entry_window, entry_price)
        result["EntryTriggered"] = "Yes" if entry_hit_date is not None else "No"
        result["EntryTriggerDate"] = entry_hit_date

        for lvl in GAIN_LEVELS:
            level_price = gain_price(lvl, day_high, day_low)
            hit_date = first_date_at_or_above(entry_window, level_price)
            result[f"Fib_Gain_{lvl}%"] = "Yes" if hit_date is not None else "No"
            result[f"Fib_Gain_{lvl}%_Date"] = hit_date

        swing_range = day_high - day_low
        if swing_range == 0:
            result["MAX_FBG"] = None
        else:
            result["MAX_FBG"] = round((abs_high - day_high) / swing_range * 100, 2)

    # --- (3) 52-week High/Low + Open Fib level ----------------------------
    prev_date = prev_row["date"]
    w52_start = prev_date - dt.timedelta(days=w52_days)
    w52 = hist[(hist["date"] >= w52_start) & (hist["date"] <= prev_date)]
    if len(w52) < 2:
        result["High52W"] = None
        result["Low52W"] = None
        result["OpenFibPct"] = None
        result["OpenNearestFibLevel"] = None
    else:
        high52 = float(w52["high"].max())
        low52 = float(w52["low"].min())
        rng52 = high52 - low52
        result["High52W"] = high52
        result["Low52W"] = low52
        if rng52 == 0:
            result["OpenFibPct"] = None
            result["OpenNearestFibLevel"] = None
        else:
            open_fib_pct = (high52 - open_shortlist) / rng52 * 100
            result["OpenFibPct"] = round(open_fib_pct, 2)
            result["OpenNearestFibLevel"] = nearest_standard_level(open_fib_pct)

    # --- (4) Weinstein Stage / Wyckoff Phase ------------------------------
    result.update(compute_market_stage(hist, shortlist_date, config=config))

    return result


# ------------------------------------------------------------------ #
# SINGLE-SYMBOL CONVENIENCE
# ------------------------------------------------------------------ #
_instrument_cache = {}  # exchange -> {tradingsymbol: token}


def analyze_stock(kite, symbol, shortlist_date, today=None, exchange="NSE", config=None):
    """Analyse a single symbol — handles instrument lookup and date normalisation.

    Parameters
    ----------
    kite          : authenticated KiteConnect instance
    symbol        : e.g. "RELIANCE"
    shortlist_date: date the stock was shortlisted — datetime.date, datetime, or "YYYY-MM-DD"
    today         : upper bound for look-ahead (defaults to today)
    exchange      : "NSE" (default) or "BSE"
    config        : optional dict to override DEFAULT_CONFIG keys

    Returns
    -------
    dict — all computed fields, or a dict with an "Error" key on failure

    Example
    -------
    result = sa.analyze_stock(kite, "DENTA", "2024-09-10")
    print(result["MAX_FBG"], result["WeinsteinStage"])
    """
    if exchange not in _instrument_cache:
        _instrument_cache[exchange] = build_instrument_map(kite, exchange)

    token = _instrument_cache[exchange].get(symbol)
    if token is None:
        return {"Symbol": symbol, "ShortlistDate": shortlist_date,
                "Error": f"Symbol not found in {exchange} instruments"}

    if hasattr(shortlist_date, "date"):
        shortlist_date = shortlist_date.date()
    elif isinstance(shortlist_date, str):
        shortlist_date = dt.date.fromisoformat(shortlist_date)

    if today is None:
        today = dt.date.today()

    return analyze_symbol(kite, symbol, token, shortlist_date, today, config=config)


# ------------------------------------------------------------------ #
# STAGE-ONLY LOOKUP  (lighter than analyze_stock — no lookahead data)
# ------------------------------------------------------------------ #
def get_stage(kite, symbol, as_of_date=None, exchange="NSE", config=None):
    """Return Weinstein stage / Wyckoff phase for a symbol as of as_of_date.

    Fetches ~300 days of daily history (enough for 30-week MA + slope).
    Much lighter than analyze_stock() — use this when you only need stage.

    Returns a dict with keys: symbol, WeinsteinStage, WyckoffPhase,
    MA30W, PriceVsMA30WPct, MA30WSlopePct.
    """
    import datetime as _dt
    if as_of_date is None:
        as_of_date = _dt.date.today()
    if hasattr(as_of_date, "date"):
        as_of_date = as_of_date.date()
    elif isinstance(as_of_date, str):
        as_of_date = _dt.date.fromisoformat(as_of_date)

    if exchange not in _instrument_cache:
        _instrument_cache[exchange] = build_instrument_map(kite, exchange)

    token = _instrument_cache[exchange].get(symbol)
    if token is None:
        return {"symbol": symbol, "WeinsteinStage": "Symbol not found",
                "WyckoffPhase": "—", "MA30W": None,
                "PriceVsMA30WPct": None, "MA30WSlopePct": None}

    from_date = as_of_date - _dt.timedelta(days=300)
    try:
        hist = _fetch_daily_history(kite, token, from_date, as_of_date,
                                    symbol=symbol, config=config)
        if hist.empty:
            return {"symbol": symbol, "WeinsteinStage": "No data",
                    "WyckoffPhase": "—", "MA30W": None,
                    "PriceVsMA30WPct": None, "MA30WSlopePct": None}
        hist["date"] = pd.to_datetime(hist["date"]).dt.date
        hist = hist.sort_values("date").reset_index(drop=True)
        stage = compute_market_stage(hist, as_of_date, config=config)
        return {"symbol": symbol, **stage}
    except Exception as e:
        return {"symbol": symbol, "WeinsteinStage": "Error",
                "WyckoffPhase": str(e), "MA30W": None,
                "PriceVsMA30WPct": None, "MA30WSlopePct": None}


# ------------------------------------------------------------------ #
# BATCH RUNNER
# ------------------------------------------------------------------ #
def run_batch(kite, rows, instrument_map, config=None, exchange="NSE"):
    """Analyse a list of shortlist rows.

    Parameters
    ----------
    kite           : authenticated KiteConnect instance
    rows           : iterable of dicts with keys "Symbol" and "Date"
                     (Date may be a str, datetime, or date object)
    instrument_map : {tradingsymbol: token} from build_instrument_map()
    config         : optional dict to override DEFAULT_CONFIG keys
    exchange       : used only in the error message when a symbol is missing

    Returns
    -------
    list[dict] — one dict per input row
    """
    today = dt.date.today()
    results = []

    rows = list(rows)
    for i, row in enumerate(rows):
        symbol = str(row["Symbol"]).strip()
        shortlist_date = row["Date"]
        if hasattr(shortlist_date, "date"):
            shortlist_date = shortlist_date.date()
        elif isinstance(shortlist_date, str):
            shortlist_date = dt.date.fromisoformat(shortlist_date)

        print(f"[{i + 1}/{len(rows)}] {symbol}  ({shortlist_date}) ...")

        token = instrument_map.get(symbol)
        if token is None:
            print(f"  -> not found in {exchange} instrument list, skipping")
            results.append({"Symbol": symbol, "ShortlistDate": shortlist_date,
                             "Error": f"Symbol not found in {exchange} instruments"})
            continue

        try:
            results.append(analyze_symbol(kite, symbol, token, shortlist_date, today, config=config))
        except RuntimeError as e:
            print(f"  -> {e}")
            results.append({"Symbol": symbol, "ShortlistDate": shortlist_date, "Error": str(e)})

    return results


# ------------------------------------------------------------------ #
# EXCEL OUTPUT  (optional utility)
# ------------------------------------------------------------------ #
def save_to_excel(results, output_file):
    """Write results list to an xlsx with Analysis + Legend sheets."""
    out_df = pd.DataFrame(results)

    retracement_cols = []
    for lvl in RETRACEMENT_LEVELS:
        retracement_cols += [f"Fib_Retracement_{lvl}%", f"Fib_Retracement_{lvl}%_Date"]

    gain_cols = []
    for lvl in GAIN_LEVELS:
        gain_cols += [f"Fib_Gain_{lvl}%", f"Fib_Gain_{lvl}%_Date"]

    preferred_order = [
        "Symbol", "ShortlistDate", "Error", "PrevTradingDate", "PrevDayClose",
        "ShortlistDayOpen", "ShortlistDayHigh", "ShortlistDayLow", "GapUp",
    ] + retracement_cols + [
        "MAX_FBR",
        "EntryPrice", "EntryTriggered", "EntryTriggerDate",
        "AbsoluteHighAfter", "AbsoluteHighAfterDate", "AbsoluteLowAfter", "AbsoluteLowAfterDate",
    ] + gain_cols + [
        "MAX_FBG",
        "High52W", "Low52W", "OpenFibPct", "OpenNearestFibLevel",
        "MA30W", "PriceVsMA30WPct", "MA30WSlopePct", "WeinsteinStage", "WyckoffPhase",
    ]
    cols = [c for c in preferred_order if c in out_df.columns] + \
           [c for c in out_df.columns if c not in preferred_order]
    out_df = out_df[cols]

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        out_df.to_excel(writer, sheet_name="Analysis", index=False)
        _build_legend_df().to_excel(writer, sheet_name="Legend", index=False)

    print(f"Wrote {len(out_df)} rows to {output_file}")
    return out_df


def _build_legend_df():
    cfg = DEFAULT_CONFIG
    rows = [
        ("ShortlistDayHigh / ShortlistDayLow",
         "High and Low of the shortlist date. Anchors for both retracement and gain Fibonacci tables."),
        ("GapUp",
         "Yes if ShortlistDayOpen > PrevDayClose."),
        ("Fib_Retracement_<level>% / _Date",
         f"Retracement window = every trading day after shortlist date (within {cfg['lookahead_days']} cal days). "
         "Yes if any day traded at/below the level price; _Date = earliest such date."),
        ("MAX_FBR",
         "Deepest retracement level reached (capped at -100%). None = not even 0% was reached."),
        ("ENTRY window",
         f"Starts shortlist_date+2 trading days (next day skipped). Bounded by {cfg['lookahead_days']} cal days."),
        ("AbsoluteHighAfter / AbsoluteLowAfter",
         "Highest High / Lowest Low in the ENTRY window, plus the date each occurred."),
        ("EntryPrice / EntryTriggered / EntryTriggerDate",
         "EntryPrice = ShortlistDayHigh + 1. EntryTriggered = Yes if reached in ENTRY window."),
        ("Fib_Gain_<level>% / _Date",
         "ENTRY window. Yes if any day traded at/above the level price (same High/Low anchors)."),
        ("MAX_FBG",
         "Actual gain % = (AbsoluteHighAfter - day_high) / swing * 100. Unbounded — can exceed 261.8%."),
        ("High52W / Low52W",
         f"Highest High / Lowest Low over {cfg['fifty_two_week_days']} cal days up to PrevTradingDate."),
        ("OpenFibPct / OpenNearestFibLevel",
         "(High52W - ShortlistDayOpen) / (High52W - Low52W) * 100. 0% = open at 52W high."),
        ("MA30W / PriceVsMA30WPct / MA30WSlopePct",
         f"30-week MA of weekly closes as of shortlist date, price vs MA (%), MA slope over "
         f"prior {cfg['slope_lookback_weeks']} weeks (%)."),
        ("WeinsteinStage / WyckoffPhase",
         f"Rule-based classification from MA position + slope. 'Insufficient data' = fewer than "
         f"{cfg['ma_weeks'] + cfg['slope_lookback_weeks']} weeks of history available."),
    ]
    return pd.DataFrame(rows, columns=["Field", "Definition"])


# ------------------------------------------------------------------ #
# CLI ENTRY POINT  (python SwingAnalysis.py)
# ------------------------------------------------------------------ #
if __name__ == "__main__":
    API_KEY = os.environ.get("KITE_API_KEY", "")
    API_SECRET = os.environ.get("KITE_API_SECRET", "")
    INPUT_FILE = os.environ.get("SWING_INPUT", "Backtest_Aug_subset.xlsx")
    OUTPUT_FILE = os.environ.get("SWING_OUTPUT", "Fib_Retracement_Analysis.xlsx")

    if not API_KEY or not API_SECRET:
        raise SystemExit("Set KITE_API_KEY and KITE_API_SECRET environment variables.")

    kite = authenticate(API_KEY, API_SECRET)
    instrument_map = build_instrument_map(kite)

    input_df = pd.read_excel(INPUT_FILE)
    rows = input_df.to_dict("records")

    results = run_batch(kite, rows, instrument_map)
    save_to_excel(results, OUTPUT_FILE)
