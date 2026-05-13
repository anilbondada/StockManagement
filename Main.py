"""
Stock Management API
====================
FastAPI endpoints for stock analysis using Zerodha Kite API

Endpoints:
- GET / - Health check
- POST /early-bloom - Fibonacci Retracement Analysis
- POST /morning-stars - Morning Star Pattern Analysis
"""

import asyncio
import json
import sqlite3
import time
from datetime import date, datetime, timezone
from typing import Optional, List
from pydantic import BaseModel
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from kiteconnect import KiteConnect, KiteTicker
import pandas as pd

from get_access_token import get_login_url, get_access_token as fetch_access_token, API_KEY
from ExcelUpload import router as excel_router

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
EXCHANGE = "NSE"
MARKET_OPEN = "09:15"
FIB_LEVELS = [0.382, 0.500, 0.618]
API_DELAY = 0.35  # seconds between API calls
TOKEN_FILE = "token.json"
DB_FILE = "alerts.db"
# ─────────────────────────────────────────────────────────────────────────────


# ── WebSocket manager ─────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, message: str):
        for ws in list(self.active):
            try:
                await ws.send_text(message)
            except Exception:
                self.active.remove(ws)

manager        = ConnectionManager()
order_manager  = ConnectionManager()
_main_loop     = None
_ticker        = None


# ── KiteTicker (real-time order updates) ──────────────────────────────────────

_ticker_shutdown    = False
_ticker_reconnecting = False


def _load_token_from_json() -> Optional[str]:
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f).get("access_token")
    except Exception:
        return None


def start_ticker(access_token: str):
    global _ticker
    if _ticker:
        try:
            _ticker.close()
        except Exception:
            pass

    ticker = KiteTicker(API_KEY, access_token)

    def on_order_update(ws, data):
        try:
            upsert_order_update(data)
            if (data.get("status") or "").upper() == "COMPLETE":
                _fetch_complete_candle(data)
        except Exception as e:
            print(f"[order-update] DB error: {e}")
        if _main_loop:
            asyncio.run_coroutine_threadsafe(
                order_manager.broadcast(json.dumps(data, default=str)),
                _main_loop,
            )

    def on_connect(ws, response):
        print("[ticker] Connected to Zerodha order stream.")

    def on_close(ws, code, reason):
        global _ticker_reconnecting
        print(f"[ticker] Disconnected: {reason}")
        if not _ticker_shutdown and not _ticker_reconnecting:
            _ticker_reconnecting = True
            import threading
            threading.Thread(target=_reconnect_ticker, args=(access_token,), daemon=True).start()

    def on_error(ws, code, reason):
        print(f"[ticker] Error {code}: {reason}")

    ticker.on_order_update  = on_order_update
    ticker.on_connect       = on_connect
    ticker.on_close         = on_close
    ticker.on_error         = on_error
    ticker.connect(threaded=True)
    _ticker = ticker
    return ticker


def _reconnect_ticker(prev_token: str, delay: int = 30):
    """Reload token from file and restart ticker. Backs off if token unchanged."""
    global _ticker_reconnecting
    try:
        time.sleep(delay)
        if _ticker_shutdown:
            return
        new_token = _load_token_from_json()
        if not new_token:
            print("[ticker] Reconnect skipped — no token in token.json")
            return
        if new_token == prev_token:
            print(f"[ticker] Token unchanged, retrying in 60s...")
            time.sleep(60)
            _reconnect_ticker(prev_token, delay=0)
            return
        print(f"[ticker] Reconnecting with refreshed token...")
        start_ticker(new_token)
    finally:
        _ticker_reconnecting = False


# ── Database ─────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chartink_alerts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                stocks         TEXT,
                trigger_prices TEXT,
                triggered_at   TEXT,
                scan_name      TEXT,
                scan_url       TEXT,
                alert_name     TEXT,
                raw            TEXT,
                received_at    TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stocks_fetched_info (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id     INTEGER,
                symbol       TEXT,
                candle_date  TEXT,
                candle_time  TEXT,
                open         REAL,
                high         REAL,
                low          REAL,
                close        REAL,
                volume       INTEGER,
                prev_day_low REAL,
                fetched_at   TEXT,
                FOREIGN KEY (alert_id) REFERENCES chartink_alerts(id)
            )
        """)
        # Add prev_day_low to existing table if missing
        existing = {r[1] for r in conn.execute("PRAGMA table_info(stocks_fetched_info)").fetchall()}
        # Migrate prev_day_low → prev_day_close if needed
        if "prev_day_low" in existing and "prev_day_close" not in existing:
            conn.execute("ALTER TABLE stocks_fetched_info RENAME COLUMN prev_day_low TO prev_day_close")
        elif "prev_day_close" not in existing:
            conn.execute("ALTER TABLE stocks_fetched_info ADD COLUMN prev_day_close REAL")
        if "pct_change" not in existing:
            conn.execute("ALTER TABLE stocks_fetched_info ADD COLUMN pct_change REAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS order_updates (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id           TEXT UNIQUE,
                exchange_order_id  TEXT,
                tradingsymbol      TEXT,
                transaction_type   TEXT,
                product            TEXT,
                quantity           INTEGER,
                price              REAL,
                trigger_price      REAL,
                average_price      REAL,
                exchange           TEXT,
                order_type         TEXT,
                is_open            INTEGER DEFAULT 0,
                is_trigger_pending INTEGER DEFAULT 0,
                is_complete        INTEGER DEFAULT 0,
                is_rejected        INTEGER DEFAULT 0,
                is_cancelled       INTEGER DEFAULT 0,
                is_webhook_order   INTEGER DEFAULT 0,
                status_message     TEXT,
                candle_high        REAL,
                candle_low         REAL,
                order_timestamp    TEXT,
                last_updated       TEXT
            )
        """)
        # Add is_webhook_order to existing table if missing
        ou_cols = {r[1] for r in conn.execute("PRAGMA table_info(order_updates)").fetchall()}
        if "is_webhook_order" not in ou_cols:
            conn.execute("ALTER TABLE order_updates ADD COLUMN is_webhook_order INTEGER DEFAULT 0")


def upsert_order_update(data: dict):
    order_id = data.get("order_id")
    status   = (data.get("status") or "").upper().replace(" ", "_")
    now      = datetime.now(timezone.utc).isoformat()
    flag     = {"OPEN": "is_open", "TRIGGER_PENDING": "is_trigger_pending",
                "COMPLETE": "is_complete", "REJECTED": "is_rejected", "CANCELLED": "is_cancelled"}
    col      = flag.get(status)

    with _db() as conn:
        existing = conn.execute("SELECT id FROM order_updates WHERE order_id=?", (order_id,)).fetchone()
        if existing:
            sets = ["average_price=?", "status_message=?", "last_updated=?"]
            vals = [data.get("average_price"), data.get("status_message"), now]
            if col:
                sets.append(f"{col}=1")
            conn.execute(f"UPDATE order_updates SET {', '.join(sets)} WHERE order_id=?",
                         vals + [order_id])
        else:
            conn.execute("""
                INSERT INTO order_updates
                (order_id, exchange_order_id, tradingsymbol, transaction_type, product,
                 quantity, price, trigger_price, average_price, exchange, order_type,
                 is_open, is_trigger_pending, is_complete, is_rejected, is_cancelled,
                 status_message, order_timestamp, last_updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                order_id, data.get("exchange_order_id"), data.get("tradingsymbol"),
                data.get("transaction_type"), data.get("product"), data.get("quantity"),
                data.get("price"), data.get("trigger_price"), data.get("average_price"),
                data.get("exchange"), data.get("order_type"),
                1 if status == "OPEN" else 0,
                1 if status == "TRIGGER_PENDING" else 0,
                1 if status == "COMPLETE" else 0,
                1 if status == "REJECTED" else 0,
                1 if status == "CANCELLED" else 0,
                data.get("status_message"), data.get("order_timestamp"), now,
            ))


def _fetch_complete_candle(data: dict):
    import threading
    from datetime import timedelta as _td, timezone as _tz

    def _run():
        try:
            kite             = get_kite()
            symbol           = data.get("tradingsymbol")
            transaction_type = (data.get("transaction_type") or "").upper()
            quantity         = data.get("quantity") or 0
            ts               = data.get("exchange_timestamp") or data.get("order_timestamp") or ""

            # Parse execution time (Kite timestamps are in IST)
            exec_dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")

            # Round down to current 5-min candle start and end
            candle_min   = (exec_dt.minute // 5) * 5
            candle_start = exec_dt.replace(minute=candle_min, second=0, microsecond=0)
            candle_end   = candle_start + _td(minutes=5)
            trade_date   = exec_dt.strftime("%Y-%m-%d")

            # Calculate how many seconds until the 5-min candle completes
            # Current IST time for accurate wait calculation
            now_ist      = datetime.now(_tz(_td(hours=5, minutes=30))).replace(tzinfo=None)
            wait_secs    = max(0, (candle_end - now_ist).total_seconds()) + 10  # +10s buffer
            print(f"[order-update] {symbol} BUY COMPLETE at {exec_dt.strftime('%H:%M:%S')} IST — waiting {wait_secs:.0f}s for 5-min candle {candle_start.strftime('%H:%M')}–{candle_end.strftime('%H:%M')} to close")
            time.sleep(wait_secs)

            # Fetch the completed 5-min candle
            token   = get_token(kite, symbol)
            candles = kite.historical_data(
                token,
                candle_start.strftime("%Y-%m-%d %H:%M:%S"),
                candle_end.strftime("%Y-%m-%d %H:%M:%S"),
                "5minute"
            )

            if candles:
                c = candles[0]
                with _db() as conn:
                    conn.execute("UPDATE order_updates SET candle_high=?, candle_low=? WHERE order_id=?",
                                 (c["high"], c["low"], data.get("order_id")))
                print(f"[order-update] {symbol}: 5-min candle high={c['high']} low={c['low']}")

                # Place SELL SL only for BUY orders placed via ChartInk webhook
                with _db() as conn:
                    row = conn.execute(
                        "SELECT is_webhook_order FROM order_updates WHERE order_id=?",
                        (data.get("order_id"),)
                    ).fetchone()
                is_webhook = row and row[0] == 1

                if not is_webhook:
                    print(f"[sell-order] {symbol}: skipped — not a webhook order")
                elif transaction_type == "BUY" and quantity > 0:
                    trigger_price = round(c["low"] - 1,   2)
                    limit_price   = round(c["low"] - 1.5, 2)
                    sell_order_id = kite.place_order(
                        variety          = kite.VARIETY_REGULAR,
                        exchange         = data.get("exchange", "NSE"),
                        tradingsymbol    = symbol,
                        transaction_type = "SELL",
                        quantity         = quantity,
                        product          = data.get("product", "MIS"),
                        order_type       = "SL",
                        validity         = "DAY",
                        price            = limit_price,
                        trigger_price    = trigger_price,
                    )
                    print(f"[sell-order] {symbol}: order_id={sell_order_id} trigger={trigger_price} limit={limit_price} qty={quantity}")
            else:
                print(f"[order-update] {symbol}: no 5-min candle data returned")

        except Exception as e:
            print(f"[order-update] candle fetch / sell order error: {e}")
    threading.Thread(target=_run, daemon=True).start()


def save_alert(data: dict) -> int:
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO chartink_alerts
               (stocks, trigger_prices, triggered_at, scan_name, scan_url, alert_name, raw, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("stocks"),
                data.get("trigger_prices"),
                data.get("triggered_at"),
                data.get("scan_name"),
                data.get("scan_url"),
                data.get("alert_name"),
                json.dumps(data),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


# ── Stock candle fetching ─────────────────────────────────────────────────────

def get_previous_trading_day() -> str:
    from datetime import timedelta
    day = date.today() - timedelta(days=1)
    while day.weekday() >= 5:  # skip Saturday(5) and Sunday(6)
        day -= timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def save_stock_candles(alert_id: int, symbol: str, date_str: str, candles: list, prev_day_close: float = None):
    rows = [
        (
            alert_id,
            symbol,
            date_str,
            str(c["date"]),
            c["open"],
            c["high"],
            c["low"],
            c["close"],
            c["volume"],
            prev_day_close,
            round((c["high"] - prev_day_close) / prev_day_close * 100, 2) if prev_day_close and c["high"] else None,
            datetime.now(timezone.utc).isoformat(),
        )
        for c in candles
    ]
    with _db() as conn:
        conn.executemany(
            """INSERT INTO stocks_fetched_info
               (alert_id, symbol, candle_date, candle_time, open, high, low, close, volume, prev_day_close, pct_change, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def fetch_and_store_candles(alert_id: int, symbols: list[str], date_str: str):
    global _access_token
    # Try _access_token first, fall back to token.json
    kite = None
    for token in [_access_token, _load_token_from_json()]:
        if not token:
            continue
        try:
            kite = KiteConnect(api_key=API_KEY)
            kite.set_access_token(token)
            kite.profile()   # validate
            if token != _access_token:
                _access_token = token
                print(f"[candle fetch] alert_id={alert_id} reloaded token from token.json")
            break
        except Exception:
            kite = None
    if not kite:
        print(f"[candle fetch] alert_id={alert_id} no valid token available, skipping")
        return

    from_dt = f"{date_str} 09:15:00"
    to_dt   = f"{date_str} 09:30:00"
    print(f"[candle fetch] alert_id={alert_id} fetching {symbols} for {date_str}")

    from datetime import timedelta
    prev_date = (date.today() - timedelta(days=1))
    while prev_date.weekday() >= 5:
        prev_date -= timedelta(days=1)
    prev_date_str = prev_date.strftime("%Y-%m-%d")

    for symbol in symbols:
        try:
            token   = get_token(kite, symbol)

            # Fetch both current day candle and prev day in one call
            both = kite.historical_data(token, f"{prev_date_str} 00:00:00", f"{date_str} 23:59:59", "day")
            time.sleep(API_DELAY)
            prev_day_close = both[0]["close"] if both and len(both) >= 1 else None

            candles = kite.historical_data(token, from_dt, to_dt, "15minute")
            time.sleep(API_DELAY)

            if candles:
                save_stock_candles(alert_id, symbol, date_str, candles, prev_day_close)
                print(f"[candle fetch] alert_id={alert_id} {symbol}: {len(candles)} candles saved, prev_day_close={prev_day_close}")
            else:
                print(f"[candle fetch] alert_id={alert_id} {symbol}: no data returned")
        except Exception as e:
            print(f"[candle fetch] alert_id={alert_id} {symbol}: ERROR {e}")

    # After all candles saved, place orders only before 10:00 AM IST
    try:
        from datetime import timezone as tz
        ist_now = datetime.now(tz(timedelta(hours=5, minutes=30)))
        if ist_now.hour < 10:
            with _db() as conn:
                order_rows = conn.execute("""
                    SELECT symbol, high, pct_change FROM stocks_fetched_info
                    WHERE alert_id = ? AND pct_change IS NOT NULL AND high IS NOT NULL
                """, (alert_id,)).fetchall()
            if order_rows:
                result = _run_auto_orders(kite, order_rows)
                print(f"[auto-order] alert_id={alert_id}: placed={len(result['placed'])} skipped={len(result['skipped'])} errors={len(result['errors'])}")
        else:
            print(f"[auto-order] alert_id={alert_id}: skipped — current IST time {ist_now.strftime('%H:%M')} >= 10:00")
    except Exception as e:
        print(f"[auto-order] alert_id={alert_id}: ERROR {e}")




@asynccontextmanager
async def lifespan(_: FastAPI):
    global _access_token, _main_loop
    _main_loop = asyncio.get_running_loop()
    token = load_token()
    if token and validate_token(token):
        _access_token = token
        print("Loaded valid access token from file.")
        start_ticker(_access_token)
    else:
        _access_token = None
        print(f"No valid token found. Login here:\n{get_login_url()}")
    init_db()
    yield
    global _ticker_shutdown
    _ticker_shutdown = True
    if _ticker:
        _ticker.close()


app = FastAPI(title="Stock Management API", lifespan=lifespan)
app.include_router(excel_router)

# Token cache
_token_cache: dict = {}        # NSE:SYMBOL -> instrument_token
_instruments_cache: dict = {}  # exchange   -> full instruments list
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
        start_ticker(_access_token)
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
    # Fetch instruments list once per exchange and reuse across all symbols
    if exchange not in _instruments_cache:
        _instruments_cache[exchange] = kite.instruments(exchange)
    for inst in _instruments_cache[exchange]:
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


@app.get("/api/order-updates-table")
def api_order_updates_table():
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT order_id, exchange_order_id, tradingsymbol, transaction_type,
                   product, quantity, price, trigger_price, average_price,
                   exchange, order_type, is_open, is_trigger_pending, is_complete,
                   is_rejected, is_cancelled, status_message,
                   candle_high, candle_low, order_timestamp, last_updated
            FROM order_updates ORDER BY last_updated DESC
        """).fetchall()
    return [dict(r) for r in rows]


@app.get("/order-updates-table", response_class=HTMLResponse)
def order_updates_table_ui():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Order Updates</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#f0f2f5;padding:28px 16px}
    .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px}
    h1{color:#1a1a2e;font-size:1.4rem}
    .meta{font-size:.82rem;color:#6b7280;margin-top:2px}
    .controls{display:flex;gap:10px;align-items:center}
    input[type=text]{padding:8px 12px;border:1px solid #ddd;border-radius:8px;font-size:.85rem;outline:none;width:200px}
    input[type=text]:focus{border-color:#4f46e5}
    button{padding:8px 18px;border:none;border-radius:8px;font-size:.85rem;font-weight:700;cursor:pointer;background:#4f46e5;color:#fff}
    button:hover{background:#4338ca}
    .wrap{overflow-x:auto;background:#fff;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.08)}
    table{width:100%;border-collapse:collapse;font-size:.85rem}
    thead th{background:#1e1e2e;color:#cdd6f4;padding:10px 14px;text-align:left;white-space:nowrap;font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;position:sticky;top:0}
    tbody td{padding:9px 14px;border-bottom:1px solid #f1f5f9;white-space:nowrap}
    tbody tr:last-child td{border-bottom:none}
    tbody tr:hover{background:#fafafa}
    .buy{color:#16a34a;font-weight:700} .sell{color:#dc2626;font-weight:700}
    .badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:.73rem;font-weight:700;margin:1px}
    .b-open{background:#dbeafe;color:#1e40af}
    .b-tp{background:#fef9c3;color:#854d0e}
    .b-complete{background:#d1fae5;color:#065f46}
    .b-rejected{background:#fee2e2;color:#991b1b}
    .b-cancelled{background:#f3f4f6;color:#374151}
    .empty{text-align:center;padding:60px;color:#9ca3af}
    .spinner{display:inline-block;width:16px;height:16px;border:2px solid #e5e7eb;border-top-color:#4f46e5;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:6px}
    @keyframes spin{to{transform:rotate(360deg)}}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>Order Updates</h1>
      <div class="meta" id="meta">Loading...</div>
    </div>
    <div class="controls">
      <input type="text" id="search" placeholder="Search symbol..." oninput="filter()"/>
      <button onclick="load()"><span id="spin"></span>Refresh</button>
    </div>
  </div>
  <div class="wrap" id="wrap"><div class="empty"><span class="spinner"></span> Loading...</div></div>

  <script>
    let all = [];

    function fmt(v){ return v != null ? Number(v).toFixed(2) : '—'; }
    function fmtTs(ts){ if(!ts) return '—'; try{ return new Date(ts).toLocaleString('en-IN'); }catch{ return ts; } }
    function statusBadges(r){
      let b = '';
      if(r.is_open)            b += '<span class="badge b-open">OPEN</span>';
      if(r.is_trigger_pending) b += '<span class="badge b-tp">TRIGGER PENDING</span>';
      if(r.is_complete)        b += '<span class="badge b-complete">COMPLETE</span>';
      if(r.is_rejected)        b += '<span class="badge b-rejected">REJECTED</span>';
      if(r.is_cancelled)       b += '<span class="badge b-cancelled">CANCELLED</span>';
      return b || '—';
    }

    function render(rows){
      if(!rows.length){
        document.getElementById('wrap').innerHTML='<div class="empty">No order updates found.</div>';
        return;
      }
      let h = `<table><thead><tr>
        <th>Symbol</th><th>Side</th><th>Qty</th><th>Product</th><th>Order Type</th>
        <th>Price</th><th>Trigger</th><th>Avg Price</th>
        <th>Status</th><th>Candle High</th><th>Candle Low</th>
        <th>Order ID</th><th>Last Updated</th>
      </tr></thead><tbody>`;
      h += rows.map(r => `<tr>
        <td><strong>${r.tradingsymbol||'—'}</strong></td>
        <td class="${(r.transaction_type||'').toLowerCase()}">${r.transaction_type||'—'}</td>
        <td>${r.quantity||'—'}</td>
        <td>${r.product||'—'}</td>
        <td>${r.order_type||'—'}</td>
        <td>${fmt(r.price)}</td>
        <td>${fmt(r.trigger_price)}</td>
        <td>${r.average_price ? fmt(r.average_price) : '—'}</td>
        <td>${statusBadges(r)}</td>
        <td>${r.candle_high ? fmt(r.candle_high) : '—'}</td>
        <td>${r.candle_low  ? fmt(r.candle_low)  : '—'}</td>
        <td style="font-size:.75rem;color:#6b7280">${r.order_id||'—'}</td>
        <td style="font-size:.78rem">${fmtTs(r.last_updated)}</td>
      </tr>`).join('');
      h += '</tbody></table>';
      document.getElementById('wrap').innerHTML = h;
    }

    function filter(){
      const q = document.getElementById('search').value.toLowerCase();
      render(q ? all.filter(r => (r.tradingsymbol||'').toLowerCase().includes(q)) : all);
    }

    async function load(){
      document.getElementById('spin').innerHTML='<span class="spinner"></span>';
      try{
        const res = await fetch('/api/order-updates-table');
        all = await res.json();
        const complete = all.filter(r=>r.is_complete).length;
        document.getElementById('meta').textContent =
          `${all.length} orders · ${complete} complete · Last refresh: ${new Date().toLocaleTimeString()}`;
        filter();
      }catch(e){
        document.getElementById('wrap').innerHTML=`<div class="empty">Error: ${e.message}</div>`;
      }finally{
        document.getElementById('spin').innerHTML='';
      }
    }

    load();
    setInterval(load, 15000);
  </script>
</body>
</html>
"""


@app.get("/api/chartink-alerts")
def api_chartink_alerts():
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, stocks, trigger_prices, triggered_at, scan_name, alert_name, received_at FROM chartink_alerts ORDER BY id DESC"
        ).fetchall()
    cols = ["id", "stocks", "trigger_prices", "triggered_at", "scan_name", "alert_name", "received_at"]
    return [dict(zip(cols, r)) for r in rows]


@app.get("/chartink-alerts", response_class=HTMLResponse)
def chartink_alerts_ui():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>ChartInk Alerts</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#f0f2f5;padding:28px 16px}
    .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px}
    h1{color:#1a1a2e;font-size:1.4rem}
    .meta{font-size:.82rem;color:#6b7280;margin-top:2px}
    .controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
    input[type=text]{padding:8px 12px;border:1px solid #ddd;border-radius:8px;font-size:.85rem;outline:none;width:220px}
    input[type=text]:focus{border-color:#4f46e5}
    button{padding:8px 18px;border:none;border-radius:8px;font-size:.85rem;font-weight:700;cursor:pointer;background:#4f46e5;color:#fff}
    button:hover{background:#4338ca}
    .grid{display:flex;flex-direction:column;gap:12px}
    .card{background:#fff;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.08);padding:16px 20px;border-left:4px solid #4f46e5}
    .card-top{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;margin-bottom:12px}
    .scan-name{font-size:1rem;font-weight:700;color:#1a1a2e}
    .alert-name{font-size:.8rem;color:#6b7280;margin-top:2px}
    .time-badge{font-size:.78rem;background:#f1f5f9;color:#374151;padding:3px 10px;border-radius:999px;white-space:nowrap}
    .stocks-wrap{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
    .stock-pill{background:#e0e7ff;color:#3730a3;padding:3px 12px;border-radius:999px;font-size:.82rem;font-weight:700}
    .meta-row{display:flex;gap:20px;flex-wrap:wrap;font-size:.8rem;color:#6b7280}
    .meta-row span strong{color:#374151}
    .empty{text-align:center;padding:60px;color:#9ca3af;background:#fff;border-radius:12px}
    .spinner{display:inline-block;width:16px;height:16px;border:2px solid #e5e7eb;border-top-color:#4f46e5;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:6px}
    @keyframes spin{to{transform:rotate(360deg)}}
    .dot{width:9px;height:9px;border-radius:50%;background:#22c55e;display:inline-block;animation:pulse 1.2s infinite;margin-right:5px}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>ChartInk Alerts</h1>
      <div class="meta"><span class="dot"></span><span id="meta">Loading...</span></div>
    </div>
    <div class="controls">
      <input type="text" id="search" placeholder="Search stock or scan..." oninput="filter()"/>
      <button onclick="load()"><span id="spin"></span>Refresh</button>
    </div>
  </div>

  <div class="grid" id="grid"><div class="empty"><span class="spinner"></span> Loading alerts...</div></div>

  <script>
    let allAlerts = [];

    function fmtTime(ts) {
      if (!ts) return '—';
      try { return new Date(ts).toLocaleString('en-IN'); } catch { return ts; }
    }

    function render(alerts) {
      const grid = document.getElementById('grid');
      if (!alerts.length) {
        grid.innerHTML = '<div class="empty">No alerts found.</div>';
        return;
      }
      grid.innerHTML = alerts.map(a => {
        const stocks = (a.stocks || '').split(',').map(s => s.trim()).filter(Boolean);
        const prices = (a.trigger_prices || '').split(',').map(s => s.trim());
        const pills  = stocks.map((s, i) =>
          `<span class="stock-pill">${s}${prices[i] ? ' <span style="opacity:.7">₹'+prices[i]+'</span>' : ''}</span>`
        ).join('');
        return `
          <div class="card">
            <div class="card-top">
              <div>
                <div class="scan-name">${a.scan_name || '—'}</div>
                <div class="alert-name">${a.alert_name || ''}</div>
              </div>
              <span class="time-badge">Alert #${a.id} &nbsp;·&nbsp; ${a.triggered_at || '—'}</span>
            </div>
            <div class="stocks-wrap">${pills || '<span style="color:#9ca3af;font-size:.85rem">No stocks</span>'}</div>
            <div class="meta-row">
              <span><strong>Received:</strong> ${fmtTime(a.received_at)}</span>
              <span><strong>Stocks:</strong> ${stocks.length}</span>
            </div>
          </div>`;
      }).join('');
    }

    function filter() {
      const q = document.getElementById('search').value.toLowerCase();
      render(q ? allAlerts.filter(a =>
        (a.stocks || '').toLowerCase().includes(q) ||
        (a.scan_name || '').toLowerCase().includes(q) ||
        (a.alert_name || '').toLowerCase().includes(q)
      ) : allAlerts);
    }

    async function load() {
      document.getElementById('spin').innerHTML = '<span class="spinner"></span>';
      try {
        const res    = await fetch('/api/chartink-alerts');
        allAlerts    = await res.json();
        const total  = allAlerts.length;
        const stocks = [...new Set(allAlerts.flatMap(a => (a.stocks||'').split(',').map(s=>s.trim()).filter(Boolean)))];
        document.getElementById('meta').textContent = `${total} alerts · ${stocks.length} unique stocks · refreshes every 30s`;
        filter();
      } catch(e) {
        document.getElementById('grid').innerHTML = `<div class="empty">Error: ${e.message}</div>`;
      } finally {
        document.getElementById('spin').innerHTML = '';
      }
    }

    load();
    setInterval(load, 30000);
  </script>
</body>
</html>
"""


# ── Order update stream ───────────────────────────────────────────────────────

@app.websocket("/ws/order-updates")
async def order_updates_ws(ws: WebSocket):
    await order_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        order_manager.disconnect(ws)


@app.get("/order-updates", response_class=HTMLResponse)
def order_updates_ui():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Live Order Updates</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#0f0f1a;color:#cdd6f4;min-height:100vh;padding:28px 16px}
    .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;flex-wrap:wrap;gap:12px}
    h1{font-size:1.3rem;color:#fff}
    .status{display:flex;align-items:center;gap:8px;font-size:.85rem}
    .dot{width:10px;height:10px;border-radius:50%;background:#4b5563;flex-shrink:0}
    .dot.live{background:#22c55e;animation:pulse 1.2s infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
    .controls{display:flex;gap:8px}
    button{padding:7px 16px;border:none;border-radius:8px;font-size:.82rem;font-weight:700;cursor:pointer}
    .btn-clear{background:#374151;color:#9ca3af}
    .btn-clear:hover{background:#4b5563}
    .feed{display:flex;flex-direction:column;gap:10px}
    .card{background:#1e1e2e;border-radius:10px;padding:16px 20px;border-left:4px solid #4b5563;animation:slidein .3s ease}
    @keyframes slidein{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}
    .card.COMPLETE{border-left-color:#22c55e}
    .card.OPEN{border-left-color:#3b82f6}
    .card.REJECTED{border-left-color:#ef4444}
    .card.CANCELLED{border-left-color:#eab308}
    .card-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;flex-wrap:wrap;gap:8px}
    .symbol{font-size:1.1rem;font-weight:800;color:#fff}
    .side-buy{color:#22c55e;font-weight:700}
    .side-sell{color:#ef4444;font-weight:700}
    .badge{padding:3px 12px;border-radius:999px;font-size:.75rem;font-weight:700}
    .badge.COMPLETE{background:#14532d;color:#86efac}
    .badge.OPEN{background:#1e3a5f;color:#93c5fd}
    .badge.REJECTED{background:#450a0a;color:#fca5a5}
    .badge.CANCELLED{background:#422006;color:#fde68a}
    .badge.default{background:#374151;color:#9ca3af}
    .card-body{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:6px 16px}
    .field label{font-size:.7rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em}
    .field span{font-size:.88rem;color:#e2e8f0}
    .time{font-size:.75rem;color:#6b7280;margin-top:8px}
    .empty{text-align:center;padding:80px;color:#4b5563;font-size:.95rem}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>Live Order Updates</h1>
      <div class="status"><span class="dot" id="dot"></span><span id="st">Connecting...</span></div>
    </div>
    <div class="controls">
      <button class="btn-clear" onclick="clearFeed()">Clear</button>
    </div>
  </div>

  <div class="feed" id="feed">
    <div class="empty" id="empty">Waiting for order updates...</div>
  </div>

  <script>
    const ws = new WebSocket(`ws://${location.host}/ws/order-updates`);

    ws.onopen  = () => {
      document.getElementById('dot').classList.add('live');
      document.getElementById('st').textContent = 'Connected — listening for order updates';
    };
    ws.onclose = () => {
      document.getElementById('dot').classList.remove('live');
      document.getElementById('st').textContent = 'Disconnected';
    };

    ws.onmessage = e => {
      const d = JSON.parse(e.data);
      document.getElementById('empty')?.remove();

      const status = (d.status || '').toUpperCase();
      const side   = (d.transaction_type || '').toUpperCase();
      const sideClass = side === 'BUY' ? 'side-buy' : 'side-sell';
      const badgeClass = ['COMPLETE','OPEN','REJECTED','CANCELLED'].includes(status) ? status : 'default';

      const ts = d.order_timestamp
        ? new Date(d.order_timestamp).toLocaleTimeString('en-IN', {hour:'2-digit',minute:'2-digit',second:'2-digit'})
        : new Date().toLocaleTimeString();

      const card = document.createElement('div');
      card.className = `card ${status}`;
      card.innerHTML = `
        <div class="card-top">
          <span class="symbol">${d.tradingsymbol || '—'}
            <span class="${sideClass}" style="font-size:.85rem;margin-left:8px">${side}</span>
          </span>
          <span class="badge ${badgeClass}">${status || '—'}</span>
        </div>
        <div class="card-body">
          <div class="field"><label>Order ID</label><span>${d.order_id || '—'}</span></div>
          <div class="field"><label>Type</label><span>${d.order_type || '—'}</span></div>
          <div class="field"><label>Product</label><span>${d.product || '—'}</span></div>
          <div class="field"><label>Qty</label><span>${d.quantity || '—'}</span></div>
          <div class="field"><label>Price</label><span>${d.price || 'MKT'}</span></div>
          <div class="field"><label>Avg Price</label><span>${d.average_price || '—'}</span></div>
          <div class="field"><label>Filled</label><span>${d.filled_quantity ?? '—'}</span></div>
          <div class="field"><label>Exchange</label><span>${d.exchange || '—'}</span></div>
        </div>
        <div class="time">Received at ${ts}</div>`;

      document.getElementById('feed').prepend(card);
    };

    function clearFeed() {
      document.getElementById('feed').innerHTML =
        '<div class="empty" id="empty">Waiting for order updates...</div>';
    }
  </script>
</body>
</html>
"""


# ── Place Order ───────────────────────────────────────────────────────────────

def get_kite_with_token(token: Optional[str]) -> KiteConnect:
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(token or _access_token)
    return kite


@app.get("/api/quote/{symbol}")
def get_quote(symbol: str, x_kite_token: Optional[str] = Header(default=None)):
    kite = get_kite_with_token(x_kite_token)
    symbol = symbol.upper()
    try:
        quote = kite.ltp(f"NSE:{symbol}")
        ltp   = quote[f"NSE:{symbol}"]["last_price"]
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Symbol not found: {e}")
    instruments = kite.instruments("NSE")
    inst = next((i for i in instruments if i["tradingsymbol"] == symbol and i["instrument_type"] == "EQ"), None)
    if not inst:
        raise HTTPException(status_code=404, detail=f"Instrument {symbol} not found on NSE")
    return {
        "symbol":   symbol,
        "name":     inst["name"],
        "exchange": "NSE",
        "lot_size": inst["lot_size"],
        "ltp":      ltp,
    }


class OrderRequest(BaseModel):
    symbol:           str
    transaction_type: str
    quantity:         int
    product:          str
    order_type:       str
    price:            float = 0
    trigger_price:    float = 0
    validity:         str = "DAY"


@app.post("/api/place-order")
def place_order(req: OrderRequest, x_kite_token: Optional[str] = Header(default=None)):
    kite = get_kite_with_token(x_kite_token)
    try:
        order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = "NSE",
            tradingsymbol    = req.symbol.upper(),
            transaction_type = req.transaction_type,
            quantity         = req.quantity,
            product          = req.product,
            order_type       = req.order_type,
            validity         = req.validity,
            price            = req.price,
            trigger_price    = req.trigger_price,
        )
        return {"success": True, "order_id": order_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/orders")
def api_orders(x_kite_token: Optional[str] = Header(default=None)):
    kite = get_kite_with_token(x_kite_token)
    try:
        return kite.orders()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/orders", response_class=HTMLResponse)
def orders_ui():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Order Book</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#f0f2f5;padding:28px 16px}
    .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px}
    h1{color:#1a1a2e;font-size:1.4rem}
    .meta{font-size:.82rem;color:#6b7280}
    .controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
    input[type=password]{padding:8px 12px;border:1px solid #ddd;border-radius:8px;font-size:.88rem;outline:none;width:280px}
    input[type=password]:focus{border-color:#4f46e5}
    button{padding:8px 18px;border:none;border-radius:8px;font-size:.88rem;font-weight:700;cursor:pointer}
    .btn-primary{background:#4f46e5;color:#fff}
    .btn-primary:hover{background:#4338ca}
    .card{background:#fff;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.08);overflow:hidden}
    table{width:100%;border-collapse:collapse;font-size:.87rem}
    thead th{background:#1e1e2e;color:#cdd6f4;padding:10px 14px;text-align:left;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}
    tbody td{padding:9px 14px;border-bottom:1px solid #f1f5f9;white-space:nowrap}
    tbody tr:last-child td{border-bottom:none}
    tbody tr:hover{background:#fafafa}
    .buy{color:#16a34a;font-weight:700}
    .sell{color:#dc2626;font-weight:700}
    .badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:.75rem;font-weight:700}
    .badge-complete{background:#d1fae5;color:#065f46}
    .badge-open{background:#dbeafe;color:#1e40af}
    .badge-cancelled{background:#fef9c3;color:#854d0e}
    .badge-rejected{background:#fee2e2;color:#991b1b}
    .badge-default{background:#f3f4f6;color:#374151}
    .summary{display:flex;gap:20px;padding:14px 20px;background:#f8fafc;border-top:1px solid #e2e8f0;font-size:.85rem;flex-wrap:wrap}
    .sum-item span{font-weight:700}
    .empty{text-align:center;padding:60px;color:#9ca3af}
    .spinner{display:inline-block;width:16px;height:16px;border:2px solid #e5e7eb;border-top-color:#4f46e5;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:6px}
    @keyframes spin{to{transform:rotate(360deg)}}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>Order Book</h1>
      <div class="meta" id="meta">—</div>
    </div>
    <div class="controls">
      <input type="password" id="token" placeholder="Paste access token (optional)"/>
      <button class="btn-primary" onclick="load()">
        <span id="spin"></span>Fetch Orders
      </button>
    </div>
  </div>

  <div id="content"><div class="card"><div class="empty">Click "Fetch Orders" to load today's orders.</div></div></div>

  <script>
    function badge(status) {
      const map = {
        COMPLETE:'badge-complete', OPEN:'badge-open',
        CANCELLED:'badge-cancelled', REJECTED:'badge-rejected'
      };
      const cls = map[status] || 'badge-default';
      return `<span class="badge ${cls}">${status}</span>`;
    }

    function fmt(v) { return (v && v !== 0) ? v : '—'; }

    function fmtTime(ts) {
      if (!ts) return '—';
      try { return new Date(ts).toLocaleTimeString('en-IN', {hour:'2-digit',minute:'2-digit',second:'2-digit'}); }
      catch { return ts; }
    }

    async function load() {
      const token = document.getElementById('token').value.trim();
      const spin  = document.getElementById('spin');
      spin.innerHTML = '<span class="spinner"></span>';
      document.getElementById('meta').textContent = 'Loading...';

      const headers = { 'Content-Type': 'application/json' };
      if (token) headers['X-Kite-Token'] = token;

      try {
        const res    = await fetch('/api/orders', { headers });
        if (!res.ok) { const e = await res.json(); throw new Error(e.detail); }
        const orders = await res.json();

        if (!orders.length) {
          document.getElementById('content').innerHTML = '<div class="card"><div class="empty">No orders found for today.</div></div>';
          document.getElementById('meta').textContent  = '0 orders';
          return;
        }

        const counts = { COMPLETE:0, OPEN:0, CANCELLED:0, REJECTED:0 };
        orders.forEach(o => { if (counts[o.status] !== undefined) counts[o.status]++; });

        let rows = orders.map(o => `
          <tr>
            <td>${fmtTime(o.order_timestamp)}</td>
            <td><strong>${o.tradingsymbol}</strong></td>
            <td class="${o.transaction_type==='BUY'?'buy':'sell'}">${o.transaction_type}</td>
            <td>${o.order_type}</td>
            <td>${o.product}</td>
            <td>${o.quantity}</td>
            <td>${fmt(o.price) === '—' ? 'MKT' : '₹'+o.price}</td>
            <td>${o.average_price ? '₹'+o.average_price : '—'}</td>
            <td>${o.trigger_price ? '₹'+o.trigger_price : '—'}</td>
            <td>${badge(o.status)}</td>
            <td style="color:#6b7280;font-size:.8rem">${o.order_id}</td>
          </tr>`).join('');

        document.getElementById('content').innerHTML = `
          <div class="card">
            <table>
              <thead><tr>
                <th>Time</th><th>Symbol</th><th>Side</th><th>Type</th><th>Product</th>
                <th>Qty</th><th>Price</th><th>Avg</th><th>Trigger</th><th>Status</th><th>Order ID</th>
              </tr></thead>
              <tbody>${rows}</tbody>
            </table>
            <div class="summary">
              <div class="sum-item">Total: <span>${orders.length}</span></div>
              <div class="sum-item" style="color:#065f46">Complete: <span>${counts.COMPLETE}</span></div>
              <div class="sum-item" style="color:#1e40af">Open: <span>${counts.OPEN}</span></div>
              <div class="sum-item" style="color:#991b1b">Rejected: <span>${counts.REJECTED}</span></div>
              <div class="sum-item" style="color:#854d0e">Cancelled: <span>${counts.CANCELLED}</span></div>
            </div>
          </div>`;

        document.getElementById('meta').textContent = `${orders.length} orders · Last updated: ${new Date().toLocaleTimeString()}`;
      } catch(e) {
        document.getElementById('content').innerHTML = `<div class="card"><div class="empty" style="color:#dc2626">Error: ${e.message}</div></div>`;
        document.getElementById('meta').textContent  = 'Failed';
      } finally {
        spin.innerHTML = '';
      }
    }
  </script>
</body>
</html>
"""


def _run_auto_orders(kite, rows: list) -> dict:
    """Place SL BUY MIS orders for rows meeting criteria: pct_change < 8 and LTP <= 800."""
    placed  = []
    skipped = []
    errors  = []

    for symbol, candle_high, pct_change in rows:
        try:
            quote = kite.ltp(f"NSE:{symbol}")
            ltp   = quote[f"NSE:{symbol}"]["last_price"]

            if pct_change >= 8 or ltp > 800:
                reason = f"pct_change={pct_change}% >= 8" if pct_change >= 8 else f"ltp={ltp} > 800"
                skipped.append({"symbol": symbol, "ltp": ltp, "pct_change": pct_change, "reason": reason})
                continue

            trigger_price = round(candle_high + 1, 2)
            limit_price   = round(candle_high + 1, 2)

            order_id = kite.place_order(
                variety          = kite.VARIETY_REGULAR,
                exchange         = "NSE",
                tradingsymbol    = symbol,
                transaction_type = "BUY",
                quantity         = 100,
                product          = "MIS",
                order_type       = "SL",
                validity         = "DAY",
                price            = limit_price,
                trigger_price    = trigger_price,
            )
            placed.append({"symbol": symbol, "order_id": order_id, "trigger": trigger_price,
                            "limit": limit_price, "ltp": ltp, "pct_change": pct_change})
            print(f"[auto-order] {symbol}: order_id={order_id} trigger={trigger_price} limit={limit_price}")
            # Mark as webhook-originated so SELL logic applies
            with _db() as conn:
                conn.execute(
                    """INSERT INTO order_updates (order_id, tradingsymbol, transaction_type, is_webhook_order, last_updated)
                       VALUES (?,?,?,1,?)
                       ON CONFLICT(order_id) DO UPDATE SET is_webhook_order=1""",
                    (str(order_id), symbol, "BUY", datetime.now(timezone.utc).isoformat())
                )

        except Exception as e:
            errors.append({"symbol": symbol, "error": str(e)})
            print(f"[auto-order] {symbol}: ERROR {e}")

    return {"placed": placed, "skipped": skipped, "errors": errors}


@app.post("/api/stocks-info/auto-order")
def stocks_auto_order(x_kite_token: Optional[str] = Header(default=None)):
    kite = get_kite_with_token(x_kite_token)

    with _db() as conn:
        rows = conn.execute("""
            SELECT s.symbol, s.high, s.pct_change
            FROM stocks_fetched_info s
            INNER JOIN (
                SELECT symbol, MAX(alert_id) AS max_alert
                FROM stocks_fetched_info
                GROUP BY symbol
            ) latest ON s.symbol = latest.symbol AND s.alert_id = latest.max_alert
            WHERE s.pct_change IS NOT NULL AND s.high IS NOT NULL
        """).fetchall()

    return _run_auto_orders(kite, rows)


@app.get("/place-order", response_class=HTMLResponse)
def place_order_ui():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Place Order</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#f0f2f5;min-height:100vh;padding:32px 16px;display:flex;flex-direction:column;align-items:center}
    h1{color:#1a1a2e;font-size:1.4rem;margin-bottom:24px}
    .card{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1);padding:24px;width:100%;max-width:520px;margin-bottom:20px}
    label{display:block;font-size:.8rem;font-weight:700;color:#555;margin-bottom:5px;margin-top:14px;text-transform:uppercase;letter-spacing:.04em}
    input,select{width:100%;padding:10px 13px;border:1px solid #ddd;border-radius:8px;font-size:.95rem;outline:none;transition:border .2s}
    input:focus,select:focus{border-color:#4f46e5}
    .search-row{display:flex;gap:8px}
    .search-row input{flex:1}
    button{padding:10px 20px;border:none;border-radius:8px;font-size:.9rem;font-weight:700;cursor:pointer;transition:background .2s}
    .btn-search{background:#e0e7ff;color:#3730a3}
    .btn-search:hover{background:#c7d2fe}
    .btn-buy{background:#16a34a;color:#fff;width:100%;padding:13px;font-size:1rem;margin-top:20px}
    .btn-buy:hover{background:#15803d}
    .btn-sell{background:#dc2626;color:#fff;width:100%;padding:13px;font-size:1rem;margin-top:20px}
    .btn-sell:hover{background:#b91c1c}
    .stock-info{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:14px 18px;margin-top:16px;display:none}
    .stock-info .name{font-size:1rem;font-weight:700;color:#1a1a2e}
    .stock-info .ltp{font-size:1.6rem;font-weight:800;color:#4f46e5;margin-top:4px}
    .stock-info .meta{font-size:.8rem;color:#6b7280;margin-top:2px}
    .row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    .price-fields{display:none;margin-top:0}
    .result{border-radius:10px;padding:16px;font-size:.9rem;font-weight:600;display:none;margin-top:16px}
    .result.ok{background:#d1fae5;color:#065f46}
    .result.err{background:#fee2e2;color:#991b1b}
    .spinner{display:inline-block;width:16px;height:16px;border:2px solid #e5e7eb;border-top-color:#4f46e5;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:6px}
    @keyframes spin{to{transform:rotate(360deg)}}
  </style>
</head>
<body>
<h1>Place Order</h1>
<div class="card">

  <!-- Token -->
  <label>Access Token</label>
  <input id="token" type="password" placeholder="Paste your Kite access token (optional)"/>

  <!-- Search -->
  <label>Stock Symbol</label>
  <div class="search-row">
    <input id="symbol" type="text" placeholder="e.g. RELIANCE" onkeydown="if(event.key==='Enter') search()"/>
    <button class="btn-search" onclick="search()">Search</button>
  </div>

  <!-- Stock info strip -->
  <div class="stock-info" id="info">
    <div class="name" id="info-name"></div>
    <div class="ltp" id="info-ltp"></div>
    <div class="meta" id="info-meta"></div>
  </div>

  <!-- Order fields (shown after search) -->
  <div id="order-form" style="display:none">
    <div class="row2">
      <div>
        <label>Quantity</label>
        <input id="qty" type="number" min="1" value="1"/>
      </div>
      <div>
        <label>Validity</label>
        <select id="validity">
          <option value="DAY">DAY</option>
          <option value="IOC">IOC</option>
        </select>
      </div>
    </div>

    <div class="row2">
      <div>
        <label>Product</label>
        <select id="product">
          <option value="CNC">CNC — Delivery</option>
          <option value="MIS">MIS — Intraday</option>
          <option value="NRML">NRML — Normal</option>
        </select>
      </div>
      <div>
        <label>Order Type</label>
        <select id="order_type" onchange="togglePrice()">
          <option value="MARKET">MARKET</option>
          <option value="LIMIT">LIMIT</option>
          <option value="SL">SL (Stop-Loss Limit)</option>
          <option value="SL-M">SL-M (Stop-Loss Market)</option>
        </select>
      </div>
    </div>

    <div class="price-fields" id="price-fields">
      <div class="row2">
        <div id="limit-wrap">
          <label>Limit Price (₹)</label>
          <input id="price" type="number" step="0.05" min="0" value="0"/>
        </div>
        <div id="trigger-wrap" style="display:none">
          <label>Trigger Price (₹)</label>
          <input id="trigger_price" type="number" step="0.05" min="0" value="0"/>
        </div>
      </div>
    </div>

    <div class="row2" style="margin-top:12px">
      <button class="btn-buy"  onclick="placeOrder('BUY')">▲ BUY</button>
      <button class="btn-sell" onclick="placeOrder('SELL')">▼ SELL</button>
    </div>

    <div class="result" id="result"></div>
  </div>
</div>

<script>
  let currentLtp = 0;

  function getHeaders(extra = {}) {
    const token = document.getElementById('token').value.trim();
    const h = { 'Content-Type': 'application/json', ...extra };
    if (token) h['X-Kite-Token'] = token;
    return h;
  }

  async function search() {
    const sym = document.getElementById('symbol').value.trim().toUpperCase();
    if (!sym) return;
    document.getElementById('info').style.display = 'none';
    document.getElementById('order-form').style.display = 'none';
    document.getElementById('result').style.display = 'none';

    try {
      const res  = await fetch(`/api/quote/${sym}`, { headers: getHeaders() });
      if (!res.ok) { const e = await res.json(); alert(e.detail); return; }
      const data = await res.json();
      currentLtp = data.ltp;

      document.getElementById('info-name').textContent = data.name;
      document.getElementById('info-ltp').textContent  = `₹${data.ltp.toFixed(2)}`;
      document.getElementById('info-meta').textContent = `${data.exchange}  ·  Lot size: ${data.lot_size}`;
      document.getElementById('info').style.display       = 'block';
      document.getElementById('order-form').style.display = 'block';
      document.getElementById('price').value = data.ltp.toFixed(2);
    } catch(e) {
      alert('Error: ' + e.message);
    }
  }

  function togglePrice() {
    const ot = document.getElementById('order_type').value;
    const pf = document.getElementById('price-fields');
    const lw = document.getElementById('limit-wrap');
    const tw = document.getElementById('trigger-wrap');
    pf.style.display = (ot === 'MARKET') ? 'none' : 'block';
    lw.style.display = (ot === 'SL-M')   ? 'none' : 'block';
    tw.style.display = (ot === 'SL' || ot === 'SL-M') ? 'block' : 'none';
  }

  async function placeOrder(side) {
    const sym    = document.getElementById('symbol').value.trim().toUpperCase();
    const qty    = parseInt(document.getElementById('qty').value);
    const ot     = document.getElementById('order_type').value;
    const result = document.getElementById('result');

    if (!qty || qty < 1) { alert('Enter a valid quantity.'); return; }

    const body = {
      symbol:           sym,
      transaction_type: side,
      quantity:         qty,
      product:          document.getElementById('product').value,
      order_type:       ot,
      validity:         document.getElementById('validity').value,
      price:            ot === 'MARKET' || ot === 'SL-M' ? 0 : parseFloat(document.getElementById('price').value) || 0,
      trigger_price:    (ot === 'SL' || ot === 'SL-M')  ? parseFloat(document.getElementById('trigger_price').value) || 0 : 0,
    };

    result.style.display = 'block';
    result.className     = 'result';
    result.innerHTML     = '<span class="spinner"></span> Placing order...';

    try {
      const res  = await fetch('/api/place-order', { method:'POST', headers: getHeaders(), body:JSON.stringify(body) });
      const data = await res.json();
      if (res.ok) {
        result.className   = 'result ok';
        result.innerHTML   = `✓ Order placed!  Order ID: ${data.order_id}`;
      } else {
        result.className   = 'result err';
        result.innerHTML   = `✗ ${data.detail}`;
      }
    } catch(e) {
      result.className = 'result err';
      result.innerHTML = `✗ ${e.message}`;
    }
  }
</script>
</body>
</html>
"""


@app.get("/api/stocks-info")
def api_stocks_info():
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT s.id, s.alert_id, s.symbol, s.candle_date, s.candle_time,
                   s.open, s.high, s.low, s.close, s.volume, s.prev_day_close, s.fetched_at,
                   a.scan_name, a.triggered_at
            FROM stocks_fetched_info s
            JOIN chartink_alerts a ON a.id = s.alert_id
            ORDER BY s.alert_id DESC, s.symbol, s.candle_time
        """).fetchall()
    return [dict(r) for r in rows]


def _do_refresh(kite, force: bool = False):
    from datetime import timedelta
    query = "SELECT id, symbol, candle_date, high FROM stocks_fetched_info" + (
        "" if force else " WHERE prev_day_close IS NULL"
    )
    with _db() as conn:
        rows = conn.execute(query).fetchall()

    if not rows:
        return {"updated": 0, "message": "No missing data."}

    updated = 0
    errors  = []
    for row_id, symbol, candle_date, candle_high in rows:
        try:
            trade_date = candle_date[:10]
            prev_dt    = date.fromisoformat(trade_date) - timedelta(days=1)
            while prev_dt.weekday() >= 5:
                prev_dt -= timedelta(days=1)
            prev_date_str  = prev_dt.strftime("%Y-%m-%d")
            token          = get_token(kite, symbol)
            both           = kite.historical_data(token, f"{prev_date_str} 00:00:00", f"{trade_date} 23:59:59", "day")
            time.sleep(API_DELAY)
            prev_day_close = both[0]["close"] if both else None
            pct_change     = round((candle_high - prev_day_close) / prev_day_close * 100, 2) if prev_day_close and candle_high else None
            with _db() as conn:
                conn.execute(
                    "UPDATE stocks_fetched_info SET prev_day_close=?, pct_change=? WHERE id=?",
                    (prev_day_close, pct_change, row_id)
                )
            updated += 1
        except Exception as e:
            errors.append(f"{symbol}: {e}")

    return {"updated": updated, "errors": errors[:10]}


@app.post("/api/stocks-info/refresh")
def stocks_info_refresh():
    try:
        kite = get_kite()
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    return _do_refresh(kite, force=False)


@app.post("/api/stocks-info/force-refresh")
def stocks_info_force_refresh():
    try:
        kite = get_kite()
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    return _do_refresh(kite, force=True)


@app.get("/stocks-info", response_class=HTMLResponse)
def stocks_info_ui():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Stocks Fetched Info</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', sans-serif; background: #f0f2f5; padding: 28px 16px; }

    .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
    h1 { color: #1a1a2e; font-size: 1.4rem; }
    .meta { font-size: 0.82rem; color: #6b7280; }

    .controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    input[type=text] { padding: 8px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 0.88rem; outline: none; }
    input[type=text]:focus { border-color: #4f46e5; }
    button { padding: 8px 18px; border: none; border-radius: 8px; font-size: 0.88rem; font-weight: 600; cursor: pointer; }
    .btn-primary { background: #4f46e5; color: #fff; }
    .btn-primary:hover { background: #4338ca; }
    .btn-ghost { background: #e5e7eb; color: #374151; }
    .btn-ghost:hover { background: #d1d5db; }

    .alert-block { background: #fff; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.08); margin-bottom: 24px; overflow: hidden; }
    .alert-header { background: #1e1e2e; color: #cdd6f4; padding: 12px 18px; display: flex; gap: 24px; align-items: center; flex-wrap: wrap; font-size: 0.85rem; }
    .alert-header strong { font-size: 0.95rem; color: #fff; }
    .badge { background: #4f46e5; color: #fff; padding: 2px 10px; border-radius: 999px; font-size: 0.75rem; font-weight: 700; }

    .stock-section { border-top: 1px solid #f1f5f9; }
    .stock-label { background: #f8fafc; padding: 8px 18px; font-size: 0.78rem; font-weight: 700; color: #374151; letter-spacing: 0.05em; text-transform: uppercase; border-bottom: 1px solid #e2e8f0; }

    table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
    thead th { background: #f1f5f9; color: #6b7280; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; padding: 8px 14px; text-align: right; }
    thead th:first-child { text-align: left; }
    tbody td { padding: 8px 14px; border-bottom: 1px solid #f1f5f9; text-align: right; color: #111827; }
    tbody td:first-child { text-align: left; color: #6b7280; font-size: 0.82rem; }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:hover { background: #fafafa; }

    .bull { color: #16a34a; font-weight: 700; }
    .bear { color: #dc2626; font-weight: 700; }
    .flat { color: #6b7280; }

    .empty { text-align: center; padding: 60px; color: #9ca3af; font-size: 0.95rem; }
    .spinner { display: inline-block; width: 18px; height: 18px; border: 2px solid #e5e7eb; border-top-color: #4f46e5; border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle; margin-right: 6px; }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>Stocks Fetched Info</h1>
      <div class="meta" id="meta">Loading...</div>
    </div>
    <div class="controls">
      <input type="text" id="filter" placeholder="Filter symbol..." oninput="render()"/>
      <button class="btn-primary" onclick="load(false)"><span id="spin"></span>Refresh</button>
      <button class="btn-primary" style="background:#dc2626" onclick="load(true)">Force Fetch</button>
    </div>
  </div>

  <div id="content"><div class="empty"><span class="spinner"></span> Loading data...</div></div>

  <script>
    let allData = [];

    function fmt(n) { return n != null ? Number(n).toFixed(2) : '—'; }
    function fmtTime(t) {
      if (!t) return '—';
      const m = t.match(/(\\d{2}:\\d{2})/);
      return m ? m[1] : t;
    }
    function fmtVol(v) {
      if (!v) return '—';
      return v >= 1e6 ? (v/1e6).toFixed(2)+'M' : v >= 1e3 ? (v/1e3).toFixed(1)+'K' : v;
    }

    function render() {
      const q = document.getElementById('filter').value.trim().toUpperCase();
      const filtered = q ? allData.filter(r => r.symbol.includes(q)) : allData;

      if (!filtered.length) {
        document.getElementById('content').innerHTML = '<div class="empty">No data found.</div>';
        return;
      }

      // Group by alert_id
      const alerts = {};
      for (const row of filtered) {
        if (!alerts[row.alert_id]) alerts[row.alert_id] = { meta: row, stocks: {} };
        if (!alerts[row.alert_id].stocks[row.symbol]) alerts[row.alert_id].stocks[row.symbol] = [];
        alerts[row.alert_id].stocks[row.symbol].push(row);
      }

      let html = '';
      for (const aid of Object.keys(alerts).sort((a,b) => b-a)) {
        const { meta, stocks } = alerts[aid];
        html += `<div class="alert-block">
          <div class="alert-header">
            <strong>${meta.scan_name || 'Alert'}</strong>
            <span class="badge">Alert #${aid}</span>
            <span>Triggered: ${meta.triggered_at || '—'}</span>
            <span>Date: ${meta.candle_date}</span>
          </div>`;

        for (const sym of Object.keys(stocks).sort()) {
          const candles = stocks[sym];
          html += `<div class="stock-section">
            <div class="stock-label">${sym}</div>
            <table>
              <thead><tr>
                <th>Time</th><th>Prev Day Close</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th><th>Change</th>
              </tr></thead>
              <tbody>`;
          for (const c of candles) {
            const chg = (c.prev_day_close && c.high) ? ((c.high - c.prev_day_close) / c.prev_day_close * 100) : null;
            const cls = chg == null ? 'flat' : chg > 0 ? 'bull' : chg < 0 ? 'bear' : 'flat';
            const arrow = chg == null ? '—' : chg > 0 ? '▲' : '▼';
            html += `<tr>
              <td>${fmtTime(c.candle_time)}</td>
              <td>${fmt(c.prev_day_close)}</td>
              <td>${fmt(c.open)}</td>
              <td>${fmt(c.high)}</td>
              <td>${fmt(c.low)}</td>
              <td class="${cls}">${fmt(c.close)}</td>
              <td>${fmtVol(c.volume)}</td>
              <td class="${cls}">${chg != null ? arrow + ' ' + Math.abs(chg).toFixed(2) + '%' : '—'}</td>
            </tr>`;
          }
          html += '</tbody></table></div>';
        }
        html += '</div>';
      }
      document.getElementById('content').innerHTML = html;
    }

    async function load(force = false) {
      document.getElementById('spin').innerHTML = '<span class="spinner"></span>';
      try {
        const endpoint = force ? '/api/stocks-info/force-refresh' : '/api/stocks-info/refresh';
        await fetch(endpoint, { method: 'POST' });
        const res  = await fetch('/api/stocks-info');
        allData    = await res.json();
        const total = allData.length;
        const syms  = [...new Set(allData.map(r => r.symbol))].length;
        document.getElementById('meta').textContent = `${total} candles · ${syms} symbols · Last refresh: ${new Date().toLocaleTimeString()}`;
        render();
      } catch(e) {
        document.getElementById('content').innerHTML = `<div class="empty">Error: ${e.message}</div>`;
      } finally {
        document.getElementById('spin').innerHTML = '';
      }
    }

    async function autoOrder() {
      if (!confirm('Place SL BUY orders for all stocks with change < 8% and LTP ≤ ₹800?')) return;
      try {
        const res  = await fetch('/api/stocks-info/auto-order', { method: 'POST' });
        const data = await res.json();
        let msg = '';
        if (data.placed?.length)
          msg += `✓ Orders placed (${data.placed.length}): ` + data.placed.map(o => `${o.symbol} #${o.order_id} T:${o.trigger} L:${o.limit}`).join(', ') + '\\n';
        if (data.skipped?.length)
          msg += `⊘ Skipped (${data.skipped.length}): ` + data.skipped.map(o => `${o.symbol} (${o.reason})`).join(', ') + '\\n';
        if (data.errors?.length)
          msg += `✗ Errors (${data.errors.length}): ` + data.errors.map(o => `${o.symbol}: ${o.error}`).join(', ');
        alert(msg || 'No qualifying stocks found.');
      } catch(e) {
        alert('Auto order failed: ' + e.message);
      }
    }

    load(false);
    setInterval(() => load(false), 30000);
  </script>
</body>
</html>
"""


@app.post("/webhook/earlybloom")
async def earlybloom_webhook(payload: dict):
    """ChartInk posts alerts here. Saves to DB, fetches candles, broadcasts to listeners."""
    loop     = asyncio.get_running_loop()
    alert_id = await loop.run_in_executor(None, save_alert, payload)
    await manager.broadcast(json.dumps(payload))
    symbols  = [s.strip() for s in payload.get("stocks", "").split(",") if s.strip()]
    current_day = str(date.today())
    async def _fetch(aid=alert_id, syms=symbols, day=current_day, _loop=loop):
        await _loop.run_in_executor(None, fetch_and_store_candles, aid, syms, day)
    asyncio.create_task(_fetch())
    return {"received": True, "alert_id": alert_id, "stocks": symbols}


@app.post("/simulate/send")
async def simulate_send(payload: dict):
    # broadcast only — save happens in chartink_ws when clients receive and echo back
    await manager.broadcast(json.dumps(payload))
    return {"sent": True}


@app.get("/simulate", response_class=HTMLResponse)
def simulate_ui():
    sample = json.dumps({
        "stocks": "SBIN,RELIANCE,INFY,TATAMOTORS",
        "trigger_prices": "3.75,541.8,2.1,0.2",
        "triggered_at": "2:34 pm",
        "scan_name": "Short term breakouts",
        "scan_url": "short-term-breakouts",
        "alert_name": "Alert for Short term breakouts"
    }, indent=2)
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>ChartInk Simulator</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Segoe UI', sans-serif; background: #f0f2f5; padding: 36px 16px; display: flex; flex-direction: column; align-items: center; }}
    h1 {{ color: #1a1a2e; font-size: 1.4rem; margin-bottom: 6px; }}
    p {{ color: #6b7280; font-size: 0.85rem; margin-bottom: 24px; }}
    .card {{ background: #fff; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.1); padding: 24px; width: 100%; max-width: 560px; }}
    label {{ font-size: 0.8rem; font-weight: 600; color: #555; display: block; margin-bottom: 8px; }}
    textarea {{
      width: 100%; height: 240px; font-family: monospace; font-size: 0.88rem;
      padding: 14px; border: 1px solid #ddd; border-radius: 8px;
      resize: vertical; outline: none; line-height: 1.6;
      background: #1e1e2e; color: #cdd6f4;
    }}
    textarea:focus {{ border-color: #4f46e5; }}
    .actions {{ display: flex; gap: 10px; margin-top: 14px; }}
    button {{
      flex: 1; padding: 11px; border: none; border-radius: 8px;
      font-size: 0.95rem; font-weight: 600; cursor: pointer; transition: background 0.2s;
    }}
    #sendBtn {{ background: #4f46e5; color: #fff; }}
    #sendBtn:hover {{ background: #4338ca; }}
    #sendBtn:disabled {{ background: #a5b4fc; cursor: not-allowed; }}
    #resetBtn {{ background: #f3f4f6; color: #374151; }}
    #resetBtn:hover {{ background: #e5e7eb; }}
    .toast {{
      margin-top: 14px; padding: 10px 16px; border-radius: 8px;
      font-size: 0.88rem; font-weight: 600; display: none;
    }}
    .toast.ok  {{ background: #d1fae5; color: #065f46; display: block; }}
    .toast.err {{ background: #fee2e2; color: #991b1b; display: block; }}
  </style>
</head>
<body>
  <h1>ChartInk Simulator</h1>
  <p>Edit the payload and click Send — all clients on <code>/earlybloom</code> will receive it instantly.</p>
  <div class="card">
    <label>Payload JSON</label>
    <textarea id="payload">{sample}</textarea>
    <div class="actions">
      <button id="sendBtn" onclick="send()">Send to WebSocket</button>
      <button id="resetBtn" onclick="reset()">Reset</button>
    </div>
    <div class="toast" id="toast"></div>
  </div>

  <script>
    const SAMPLE = {json.dumps(sample)};

    async function send() {{
      const btn   = document.getElementById('sendBtn');
      const toast = document.getElementById('toast');
      const raw   = document.getElementById('payload').value;
      toast.className = 'toast';

      let parsed;
      try {{ parsed = JSON.parse(raw); }}
      catch (e) {{
        toast.textContent = 'Invalid JSON: ' + e.message;
        toast.className = 'toast err';
        return;
      }}

      if (!ws || ws.readyState !== WebSocket.OPEN) {{
        toast.textContent = 'WebSocket not connected. Retrying...';
        toast.className = 'toast err';
        connectWs();
        return;
      }}
      btn.disabled = true;
      btn.textContent = 'Sending...';
      try {{
        ws.send(JSON.stringify(parsed));
        toast.textContent = 'Sent via WebSocket at ' + new Date().toLocaleTimeString();
        toast.className = 'toast ok';
      }} catch (e) {{
        toast.textContent = 'Send failed: ' + e.message;
        toast.className = 'toast err';
      }} finally {{
        btn.disabled = false;
        btn.textContent = 'Send to WebSocket';
      }}
    }}

    function reset() {{
      document.getElementById('payload').value = SAMPLE;
      document.getElementById('toast').className = 'toast';
    }}

    let ws;
    function connectWs() {{
      ws = new WebSocket(`ws://${{location.host}}/ws/earlybloom`);
      ws.onopen  = () => {{ document.getElementById('toast').className = 'toast'; }};
      ws.onclose = () => setTimeout(connectWs, 2000);
    }}
    connectWs();
  </script>
</body>
</html>
"""


@app.websocket("/ws/earlybloom")
async def earlybloom_ws(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            text = await ws.receive_text()

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue

            loop     = asyncio.get_running_loop()
            alert_id = await loop.run_in_executor(None, save_alert, data)
            await manager.broadcast(json.dumps(data))

            symbols  = [s.strip() for s in data.get("stocks", "").split(",") if s.strip()]
            current_day = str(date.today())

            async def _fetch_candles(aid=alert_id, syms=symbols, day=current_day, _loop=loop):
                await _loop.run_in_executor(None, fetch_and_store_candles, aid, syms, day)

            asyncio.create_task(_fetch_candles())

    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.get("/earlybloom", response_class=HTMLResponse)
def earlybloom_ui():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>ChartInk - Early Stock In Play</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', sans-serif; background: #f0f2f5; padding: 30px 16px; }
    h1 { color: #1a1a2e; font-size: 1.4rem; margin-bottom: 6px; }
    .meta { font-size: 0.82rem; color: #6b7280; margin-bottom: 20px; display: flex; gap: 16px; align-items: center; }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: #9ca3af; display: inline-block; }
    .dot.live { background: #22c55e; animation: pulse 1.2s infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
    pre {
      background: #1e1e2e; color: #cdd6f4; font-size: 0.82rem;
      padding: 20px; border-radius: 12px; overflow: auto;
      white-space: pre-wrap; word-break: break-all;
      max-height: 80vh; line-height: 1.6;
      box-shadow: 0 2px 12px rgba(0,0,0,0.15);
    }
  </style>
</head>
<body>
  <h1>ChartInk — Early Stock In Play</h1>
  <div class="meta">
    <span><span class="dot" id="dot"></span> <span id="status">Connecting...</span></span>
    <span id="ts"></span>
  </div>
  <pre id="output">Waiting for data...</pre>

  <script>
    const host = location.host;
    const ws   = new WebSocket(`ws://${host}/ws/earlybloom`);
    const out  = document.getElementById('output');
    const dot  = document.getElementById('dot');
    const st   = document.getElementById('status');
    const ts   = document.getElementById('ts');

    ws.onopen = () => {
      dot.classList.add('live');
      st.textContent = 'Connected — updates every 60s';
    };

    ws.onmessage = e => {
      try {
        const data = JSON.parse(e.data);
        out.textContent = JSON.stringify(data, null, 2);
      } catch {
        out.textContent = e.data;
      }
      ts.textContent = 'Last update: ' + new Date().toLocaleTimeString();
    };

    ws.onclose = () => {
      dot.classList.remove('live');
      st.textContent = 'Disconnected';
    };
  </script>
</body>
</html>
"""


@app.get("/getStockDetails", response_class=HTMLResponse)
def get_stock_details():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Early Bloom Analysis</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', sans-serif; background: #f0f2f5; min-height: 100vh; display: flex; flex-direction: column; align-items: center; padding: 40px 16px; }
    h1 { color: #1a1a2e; margin-bottom: 24px; font-size: 1.6rem; }
    .card { background: #fff; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.1); padding: 28px; width: 100%; max-width: 480px; }
    label { display: block; font-size: 0.85rem; font-weight: 600; color: #555; margin-bottom: 4px; margin-top: 16px; }
    input { width: 100%; padding: 10px 14px; border: 1px solid #ddd; border-radius: 8px; font-size: 1rem; outline: none; transition: border 0.2s; }
    input:focus { border-color: #4f46e5; }
    button { margin-top: 22px; width: 100%; padding: 12px; background: #4f46e5; color: #fff; border: none; border-radius: 8px; font-size: 1rem; font-weight: 600; cursor: pointer; transition: background 0.2s; }
    button:hover { background: #4338ca; }
    button:disabled { background: #a5b4fc; cursor: not-allowed; }
    #result { margin-top: 28px; width: 100%; max-width: 480px; }
    .error-box { background: #fef2f2; border: 1px solid #fca5a5; border-radius: 10px; padding: 16px; color: #b91c1c; font-size: 0.95rem; }
    .result-card { background: #fff; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.1); overflow: hidden; }
    .result-header { background: #4f46e5; color: #fff; padding: 14px 20px; font-size: 1rem; font-weight: 700; display: flex; justify-content: space-between; }
    .section { padding: 16px 20px; border-bottom: 1px solid #f1f1f1; }
    .section:last-child { border-bottom: none; }
    .section-title { font-size: 0.72rem; font-weight: 700; text-transform: uppercase; color: #9ca3af; margin-bottom: 10px; letter-spacing: 0.05em; }
    .row { display: flex; justify-content: space-between; padding: 5px 0; font-size: 0.9rem; }
    .row span:first-child { color: #6b7280; }
    .row span:last-child { font-weight: 600; color: #111827; }
    .badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 0.78rem; font-weight: 600; }
    .badge-green { background: #d1fae5; color: #065f46; }
    .badge-red { background: #fee2e2; color: #991b1b; }
    .badge-gray { background: #f3f4f6; color: #374151; }
  </style>
</head>
<body>
  <h1>Early Bloom Analysis</h1>
  <div class="card">
    <label for="symbol">Stock Symbol</label>
    <input id="symbol" type="text" placeholder="e.g. RELIANCE" />
    <label for="date">Date</label>
    <input id="date" type="date" />
    <button id="btn" onclick="analyse()">Analyse</button>
  </div>
  <div id="result"></div>

  <script>
    document.getElementById('date').valueAsDate = new Date();

    async function analyse() {
      const symbol = document.getElementById('symbol').value.trim().toUpperCase();
      const date   = document.getElementById('date').value;
      const btn    = document.getElementById('btn');
      const out    = document.getElementById('result');

      if (!symbol || !date) { alert('Please enter both symbol and date.'); return; }

      btn.disabled = true;
      btn.textContent = 'Analysing...';
      out.innerHTML = '';

      try {
        const res  = await fetch('/early-bloom', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol, date })
        });
        const data = await res.json();

        if (data.error) {
          out.innerHTML = `<div class="error-box">Error: ${data.error}</div>`;
          return;
        }

        const fmt  = v => (v != null ? v : '—');
        const gain = data.max_gain;
        const gainBadge = gain == null ? 'badge-gray'
                        : gain > 0    ? 'badge-green' : 'badge-red';

        out.innerHTML = `
          <div class="result-card">
            <div class="result-header">
              <span>${data.symbol}</span><span>${data.date}</span>
            </div>
            <div class="section">
              <div class="section-title">15-Min Range Candle</div>
              <div class="row"><span>Open</span><span>${fmt(data.min15_open)}</span></div>
              <div class="row"><span>High</span><span>${fmt(data.min15_high)}</span></div>
              <div class="row"><span>Low</span><span>${fmt(data.min15_low)}</span></div>
              <div class="row"><span>Close</span><span>${fmt(data.min15_close)}</span></div>
              <div class="row"><span>Entry Trigger</span><span>${fmt(data.entry_trigger)}</span></div>
            </div>
            <div class="section">
              <div class="section-title">Fibonacci Levels</div>
              <div class="row"><span>38.2% Price</span><span>${fmt(data.Fib_38_2_price)}</span></div>
              <div class="row"><span>38.2% Hit</span><span>${fmt(data.Fib_38_2_hit_candle)}</span></div>
              <div class="row"><span>50.0% Price</span><span>${fmt(data.Fib_50_0_price)}</span></div>
              <div class="row"><span>50.0% Hit</span><span>${fmt(data.Fib_50_0_hit_candle)}</span></div>
              <div class="row"><span>61.8% Price</span><span>${fmt(data.Fib_61_8_price)}</span></div>
              <div class="row"><span>61.8% Hit</span><span>${fmt(data.Fib_61_8_hit_candle)}</span></div>
            </div>
            <div class="section">
              <div class="section-title">Trade</div>
              <div class="row"><span>Entry Candle</span><span>${fmt(data.entry_candle)}</span></div>
              <div class="row"><span>Entry Price</span><span>${fmt(data.entry_price)}</span></div>
              <div class="row"><span>Stop Loss</span><span>${fmt(data.stop_loss)}</span></div>
              <div class="row"><span>SL Hit At</span><span>${fmt(data.sl_hit_candle)}</span></div>
              <div class="row">
                <span>Max Gain</span>
                <span><span class="badge ${gainBadge}">${gain != null ? gain + ' (' + data.max_gain_pct + '%)' : '—'}</span></span>
              </div>
            </div>
          </div>`;
      } catch (e) {
        out.innerHTML = `<div class="error-box">Request failed: ${e.message}</div>`;
      } finally {
        btn.disabled = false;
        btn.textContent = 'Analyse';
      }
    }
  </script>
</body>
</html>
"""


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