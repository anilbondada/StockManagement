"""
Stock Management API
====================
FastAPI endpoints for stock analysis using Zerodha Kite API

Endpoints:
- GET / - Health check
- POST /early-bloom - Fibonacci Retracement Analysis
- POST /morning-stars - Morning Star Pattern Analysis
"""

import json
import time
from datetime import date
from typing import Optional, List
from pydantic import BaseModel
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from kiteconnect import KiteConnect
import pandas as pd

from get_access_token import get_login_url, get_access_token as fetch_access_token, API_KEY

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
EXCHANGE = "NSE"
MARKET_OPEN = "09:15"
FIB_LEVELS = [0.382, 0.500, 0.618]
API_DELAY = 0.35  # seconds between API calls
TOKEN_FILE = "token.json"
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
    global _access_token
    token = load_token()
    if token and validate_token(token):
        _access_token = token
        print("Loaded valid access token from file.")
    else:
        _access_token = None
        print(f"No valid token found. Login here:\n{get_login_url()}")
    yield


app = FastAPI(title="Stock Management API", lifespan=lifespan)

# Token cache
_token_cache: dict = {}
_access_token: Optional[str] = None


# ── Token persistence ─────────────────────────────────────────────────────────

def save_token(token: str):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": token, "date": str(date.today())}, f)


def load_token() -> Optional[str]:
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        if data.get("date") == str(date.today()):
            return data.get("access_token")
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None


def validate_token(token: str) -> bool:
    try:
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(token)
        kite.profile()
        return True
    except Exception:
        return False


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login")
def login():
    """Redirect to Kite login. Set your app's redirect URL to /callback."""
    return RedirectResponse(url=get_login_url())


@app.get("/callback")
def callback(request_token: str):
    """Zerodha redirects here with request_token after login."""
    global _access_token
    try:
        _access_token = fetch_access_token(request_token)
        save_token(_access_token)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}")
    return {"message": "Login successful", "access_token": _access_token}


# ── Kite connection ───────────────────────────────────────────────────────────

def get_kite() -> KiteConnect:
    if not _access_token:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated. Visit /login first.",
        )
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(_access_token)
    return kite


# ── Instrument token lookup ───────────────────────────────────────────────────

def get_token(kite: KiteConnect, symbol: str, exchange: str = EXCHANGE) -> int:
    key = f"{exchange}:{symbol}"
    if key in _token_cache:
        return _token_cache[key]
    instruments = kite.instruments(exchange)
    for inst in instruments:
        if inst["tradingsymbol"] == symbol and inst["instrument_type"] == "EQ":
            _token_cache[key] = inst["instrument_token"]
            return inst["instrument_token"]
    raise ValueError(f"Instrument not found: {key}")


# ── Historical data ───────────────────────────────────────────────────────────

def fetch_candles(kite, token: int, date_str: str, interval: str) -> pd.DataFrame:
    from_dt = f"{date_str} 09:15:00"
    to_dt = f"{date_str} 15:30:00"
    try:
        data = kite.historical_data(token, from_dt, to_dt, interval)
    except Exception as e:
        return pd.DataFrame()
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


# ── Fibonacci helpers ─────────────────────────────────────────────────────────

def fib_price(high: float, low: float, level: float) -> float:
    """Retracement level from high downward (long setup)."""
    return round(high - level * (high - low), 2)


def first_touch(df: pd.DataFrame, price: float) -> str:
    """HH:MM of first candle whose range contains price, else 'Not touched'."""
    for _, row in df.iterrows():
        if row["low"] <= price <= row["high"]:
            return row["date"].strftime("%H:%M")
    return "Not touched"


# ── Models ───────────────────────────────────────────────────────────────────

class EarlyBloomRequest(BaseModel):
    symbol: str
    date: str  # Format: YYYY-MM-DD


class MorningStarsRequest(BaseModel):
    symbols: List[str]
    date: str  # Format: YYYY-MM-DD


class EarlyBloomResponse(BaseModel):
    date: str
    symbol: str
    error: Optional[str] = None
    # 15-min candle data
    min15_open: Optional[float] = None
    min15_high: Optional[float] = None
    min15_low: Optional[float] = None
    min15_close: Optional[float] = None
    entry_trigger: Optional[float] = None
    # Fibonacci levels
    Fib_38_2_price: Optional[float] = None
    Fib_38_2_hit_candle: Optional[str] = None
    Fib_50_0_price: Optional[float] = None
    Fib_50_0_hit_candle: Optional[str] = None
    Fib_61_8_price: Optional[float] = None
    Fib_61_8_hit_candle: Optional[str] = None
    # Entry/SL data
    entry_candle: Optional[str] = None
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    sl_hit_candle: Optional[str] = None
    max_gain: Optional[float] = None
    max_gain_pct: Optional[float] = None


class MorningStarsResponse(BaseModel):
    date: str
    symbol: str
    instrument_token: Optional[int] = None
    daily_open: Optional[float] = None
    daily_high: Optional[float] = None
    daily_low: Optional[float] = None
    daily_close: Optional[float] = None
    daily_volume: Optional[int] = None
    candle1_open: Optional[float] = None
    candle1_high: Optional[float] = None
    candle1_low: Optional[float] = None
    candle1_close: Optional[float] = None
    candle2_open: Optional[float] = None
    candle2_high: Optional[float] = None
    candle2_low: Optional[float] = None
    candle2_close: Optional[float] = None
    candle3_open: Optional[float] = None
    candle3_high: Optional[float] = None
    candle3_low: Optional[float] = None
    candle3_close: Optional[float] = None
    error: Optional[str] = None


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
def read_root():
    return {"message": "Hello, Anil!", "status": "running"}


@app.post("/early-bloom", response_model=EarlyBloomResponse)
def early_bloom_analysis(request: EarlyBloomRequest):
    """
    Fibonacci Retracement Analysis
    ==============================
    Input: symbol and date
    Output: Fibonacci levels, entry trigger, stop loss, max gain analysis
    
    Logic:
    - 15-min range candle: first candle at 09:15
    - Fibonacci levels: 38.2%, 50%, 61.8% retracement from 15-min high downward
    - Long entry trigger: 15-min high + 1
    - Stop loss: low of the 5-min entry candle - 1
    - Max gain: highest price reached after entry BEFORE SL is hit
    """
    kite = get_kite()
    base = {"date": request.date, "symbol": request.symbol}

    try:
        # 1. Instrument token
        token = get_token(kite, request.symbol)
    except ValueError as e:
        return EarlyBloomResponse(**base, error=str(e))

    # 2. First 15-min candle at 09:15
    df15 = fetch_candles(kite, token, request.date, "15minute")
    time.sleep(API_DELAY)
    if df15.empty:
        return EarlyBloomResponse(**base, error="No 15-min data")

    c15 = df15[df15["date"].dt.strftime("%H:%M") == MARKET_OPEN]
    if c15.empty:
        return EarlyBloomResponse(**base, error="09:15 candle not found")
    c15 = c15.iloc[0]

    high_15 = c15["high"]
    low_15 = c15["low"]
    entry_trigger = round(high_15 + 1, 2)

    base.update({
        "min15_open": c15["open"],
        "min15_high": high_15,
        "min15_low": low_15,
        "min15_close": c15["close"],
        "entry_trigger": entry_trigger,
    })

    # 3. Subsequent 5-min candles (strictly after 09:15)
    df5 = fetch_candles(kite, token, request.date, "5minute")
    time.sleep(API_DELAY)
    if df5.empty:
        return EarlyBloomResponse(**base, error="No 5-min data")

    after_open = df5[df5["date"].dt.strftime("%H:%M") > MARKET_OPEN].copy()
    if after_open.empty:
        return EarlyBloomResponse(**base, error="No 5-min candles after 09:15")

    # 4. Fibonacci retracement checks
    for lvl in FIB_LEVELS:
        price = fib_price(high_15, low_15, lvl)
        label_key = f"Fib_{int(lvl * 1000) / 10}_price".replace(".", "_")
        label_hit = f"Fib_{int(lvl * 1000) / 10}_hit_candle".replace(".", "_")
        base[label_key] = price
        base[label_hit] = first_touch(after_open, price)

    # 5. Long entry: first 5-min candle where high >= entry_trigger
    entry_row = None
    for _, row in after_open.iterrows():
        if row["high"] >= entry_trigger:
            entry_row = row
            break

    if entry_row is None:
        base.update({
            "entry_candle": "Entry not possible",
            "entry_price": None,
            "stop_loss": None,
            "sl_hit_candle": "N/A",
            "max_gain": None,
            "max_gain_pct": None,
        })
        return EarlyBloomResponse(**base)

    entry_time = entry_row["date"].strftime("%H:%M")
    stop_loss = round(entry_row["low"] - 1, 2)

    base.update({
        "entry_candle": entry_time,
        "entry_price": entry_trigger,
        "stop_loss": stop_loss,
    })

    # 6. Walk candles after entry: track max high before SL is hit
    post_entry = after_open[after_open["date"] > entry_row["date"]]
    sl_time = None
    peak_price = entry_trigger

    for _, row in post_entry.iterrows():
        if row["low"] <= stop_loss:
            sl_time = row["date"].strftime("%H:%M")
            if row["high"] > peak_price:
                peak_price = row["high"]
            break
        if row["high"] > peak_price:
            peak_price = row["high"]

    if sl_time:
        base["sl_hit_candle"] = sl_time
    else:
        last = after_open.iloc[-1]
        base["sl_hit_candle"] = f"Not hit (EOD @ {last['date'].strftime('%H:%M')})"
        if last["close"] > peak_price:
            peak_price = last["close"]

    max_gain = round(peak_price - entry_trigger, 2)
    max_gain_pct = round((peak_price - entry_trigger) / entry_trigger * 100, 2)

    base["max_gain"] = max_gain
    base["max_gain_pct"] = max_gain_pct

    return EarlyBloomResponse(**base)


@app.post("/morning-stars", response_model=List[MorningStarsResponse])
def morning_stars_analysis(request: MorningStarsRequest):
    """
    Morning Star Pattern Analysis
    =============================
    Input: list of symbols and date
    Output: Daily OHLCV + first 3 five-minute candles
    
    Fetches:
    - Daily OHLCV data
    - First 3 five-minute candles (9:15-9:20, 9:20-9:25, 9:25-9:30)
    """
    kite = get_kite()
    results = []

    try:
        instruments = kite.instruments("NSE")
        instruments_df = pd.DataFrame(instruments)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch instruments: {str(e)}")

    for symbol in request.symbols:
        base = {"date": request.date, "symbol": symbol}

        try:
            # 1. Get NSE equity instrument
            instrument = instruments_df[
                (instruments_df['tradingsymbol'] == symbol) &
                (instruments_df['exchange'] == 'NSE') &
                (instruments_df['instrument_type'] == 'EQ')
            ]

            if instrument.empty:
                results.append(MorningStarsResponse(**base, error=f"Instrument {symbol} not found"))
                continue

            instrument_token = instrument.iloc[0]['instrument_token']

            # 2. Get daily OHLCV
            from_date = request.date
            to_date = request.date

            daily_data = kite.historical_data(
                instrument_token=instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval="day"
            )
            time.sleep(API_DELAY)

            if not daily_data:
                daily_ohlcv = {
                    'daily_open': None, 'daily_high': None,
                    'daily_low': None, 'daily_close': None, 'daily_volume': None
                }
            else:
                daily_candle = daily_data[0]
                daily_ohlcv = {
                    'daily_open': daily_candle['open'],
                    'daily_high': daily_candle['high'],
                    'daily_low': daily_candle['low'],
                    'daily_close': daily_candle['close'],
                    'daily_volume': daily_candle['volume']
                }

            # 3. Get first three 5-minute candles
            five_min_data = kite.historical_data(
                instrument_token=instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval="5minute"
            )
            time.sleep(API_DELAY)

            candle_data = {}
            if five_min_data and len(five_min_data) >= 3:
                for i, candle in enumerate(five_min_data[:3], 1):
                    candle_data[f'candle{i}_open'] = candle['open']
                    candle_data[f'candle{i}_high'] = candle['high']
                    candle_data[f'candle{i}_low'] = candle['low']
                    candle_data[f'candle{i}_close'] = candle['close']

            result = {
                **base,
                'instrument_token': instrument_token,
                **daily_ohlcv,
                **candle_data
            }
            results.append(MorningStarsResponse(**result))

        except Exception as e:
            results.append(MorningStarsResponse(**base, error=str(e)))

    return results