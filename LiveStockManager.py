"""
Live Stock Manager
==================
Handles real-time 5-minute candle streaming via KiteTicker.

Flow:
  BUY order COMPLETE
    → subscribe_to_stock(symbol, sl_price)
    → KiteTicker streams ticks via on_ticks()
    → ticks aggregated into 5-min candles in memory
    → on candle close: save to live_candles DB, check SL breach, broadcast via WebSocket
    → at 3:30 PM IST: flush remaining candles, unsubscribe all
"""

import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta, date
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from fastapi import WebSocket, WebSocketDisconnect

DB_FILE = "alerts.db"

router = APIRouter()

# ── Globals ───────────────────────────────────────────────────────────────────

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


live_candle_manager = ConnectionManager()
_tick_candles: dict  = {}   # (token, candle_start_iso) → candle dict
_active_subs: dict   = {}   # instrument_token → {symbol, sl_price}
_tick_buffer: list   = []   # raw tick rows buffered for 1-min batch insert
_tick_buffer_lock    = threading.Lock()


# ── DB ────────────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_live_table():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_candles (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol           TEXT,
                instrument_token INTEGER,
                candle_date      TEXT,
                candle_time      TEXT,
                open             REAL,
                high             REAL,
                low              REAL,
                close            REAL,
                volume           INTEGER,
                sl_price         REAL,
                sl_breached      INTEGER DEFAULT 0,
                created_at       TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS raw_ticks (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol           TEXT,
                instrument_token INTEGER,
                tick_time        TEXT,
                last_price       REAL,
                last_quantity    INTEGER,
                last_trade_time  TEXT,
                average_price    REAL,
                volume_traded    INTEGER,
                buy_quantity     INTEGER,
                sell_quantity    INTEGER,
                day_open         REAL,
                day_high         REAL,
                day_low          REAL,
                prev_close       REAL,
                change_pct       REAL,
                oi               INTEGER,
                oi_day_high      INTEGER,
                oi_day_low       INTEGER,
                depth_json       TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quote_snapshots (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol              TEXT,
                candle_time         TEXT,
                last_price          REAL,
                buy_quantity        INTEGER,
                sell_quantity       INTEGER,
                volume              INTEGER,
                average_price       REAL,
                upper_circuit_limit REAL,
                lower_circuit_limit REAL,
                day_open            REAL,
                day_high            REAL,
                day_low             REAL,
                prev_close          REAL,
                snapshot_at         TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_qs_symbol_date ON quote_snapshots(symbol, snapshot_at)"
        )
    _start_tick_flush_thread()
    _start_quote_poll_thread()


def _flush_tick_buffer():
    while True:
        time.sleep(60)
        with _tick_buffer_lock:
            if not _tick_buffer:
                continue
            rows = list(_tick_buffer)
            _tick_buffer.clear()
        try:
            with _db() as conn:
                conn.executemany("""
                    INSERT INTO raw_ticks
                    (symbol, instrument_token, tick_time,
                     last_price, last_quantity, last_trade_time,
                     average_price, volume_traded, buy_quantity, sell_quantity,
                     day_open, day_high, day_low, prev_close, change_pct,
                     oi, oi_day_high, oi_day_low, depth_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, rows)
            print(f"[raw_ticks] Flushed {len(rows)} ticks to DB")
            import Main as _main
            if _main._main_loop:
                asyncio.run_coroutine_threadsafe(
                    live_candle_manager.broadcast(json.dumps({"type": "tick_flush"})),
                    _main._main_loop
                )
        except Exception as e:
            print(f"[raw_ticks] Flush error: {e}")


def _start_tick_flush_thread():
    t = threading.Thread(target=_flush_tick_buffer, daemon=True, name="tick-flush")
    t.start()


def _quote_poll_loop():
    """At each 5-min candle close, batch-fetch kite.quote() for all active symbols and store."""
    import Main as _main
    ist = timezone(timedelta(hours=5, minutes=30))
    while True:
        now = datetime.now(ist).replace(tzinfo=None)
        mins_to_next = 5 - (now.minute % 5)
        next_boundary = now.replace(second=0, microsecond=0) + timedelta(minutes=mins_to_next)
        wait = (next_boundary - now).total_seconds() + 5  # 5s buffer after candle close
        time.sleep(max(wait, 1))

        if not _active_subs:
            continue
        symbols = [sub["symbol"] for sub in _active_subs.values() if sub.get("symbol")]
        if not symbols:
            continue
        try:
            kite = _main.get_kite()
            instruments = [f"NSE:{s}" for s in symbols]
            quotes = kite.quote(instruments)
            candle_time = datetime.now(ist).strftime("%H:%M")
            snapshot_at = datetime.now(ist).isoformat()
            rows = []
            for sym in symbols:
                q = quotes.get(f"NSE:{sym}", {})
                ohlc = q.get("ohlc", {})
                rows.append((
                    sym, candle_time,
                    q.get("last_price"),
                    q.get("buy_quantity", 0),
                    q.get("sell_quantity", 0),
                    q.get("volume_traded", 0),
                    q.get("average_price"),
                    q.get("upper_circuit_limit"),
                    q.get("lower_circuit_limit"),
                    ohlc.get("open"),
                    ohlc.get("high"),
                    ohlc.get("low"),
                    ohlc.get("close"),
                    snapshot_at,
                ))
            with _db() as conn:
                conn.executemany("""
                    INSERT INTO quote_snapshots
                    (symbol, candle_time, last_price, buy_quantity, sell_quantity,
                     volume, average_price, upper_circuit_limit, lower_circuit_limit,
                     day_open, day_high, day_low, prev_close, snapshot_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, rows)
            print(f"[quote-poll] {candle_time} — saved {len(rows)} snapshots: {', '.join(symbols)}")
        except Exception as e:
            print(f"[quote-poll] Error: {e}")


def _start_quote_poll_thread():
    t = threading.Thread(target=_quote_poll_loop, daemon=True, name="quote-poll")
    t.start()


def restore_subscriptions():
    """On startup, re-populate _active_subs from today's webhook BUY orders.

    _active_subs is in-memory only. After a service restart during market hours
    the subscriptions are gone. This rebuilds them from order_updates so that
    resubscribe_all() can re-subscribe everything when on_connect fires.
    """
    import Main as _main
    from datetime import date as _date
    today = str(_date.today())
    try:
        with _db() as conn:
            # For each today webhook BUY: join to find the SELL SL trigger price (sl_price).
            # SELL trigger_price is None if BUY hasn't completed / SELL not placed yet.
            rows = conn.execute("""
                SELECT b.tradingsymbol, s.trigger_price
                FROM order_updates b
                LEFT JOIN order_updates s
                       ON s.tradingsymbol   = b.tradingsymbol
                      AND s.transaction_type = 'SELL'
                      AND s.is_webhook_order = 1
                      AND DATE(s.last_updated) = ?
                WHERE b.is_webhook_order  = 1
                  AND b.transaction_type  = 'BUY'
                  AND DATE(b.last_updated) = ?
                  AND b.is_cancelled = 0
                  AND b.is_rejected  = 0
            """, (today, today)).fetchall()

        if not rows:
            return

        kite = _main.get_kite()
        for symbol, sl_price in rows:
            if not symbol:
                continue
            try:
                token = _main.get_token(kite, symbol)
                _active_subs[token] = {"symbol": symbol, "sl_price": sl_price}
                print(f"[live] Restored subscription {symbol} sl={sl_price}")
            except Exception as e:
                print(f"[live] Restore subscription error {symbol}: {e}")
        print(f"[live] Restored {len(rows)} subscriptions from DB")
    except Exception as e:
        print(f"[live] restore_subscriptions error: {e}")


# ── Candle helpers ────────────────────────────────────────────────────────────

def _candle_start(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)


def _save_live_candle(token: int, candle: dict, sl_price: float, symbol: str):
    import Main as _main
    sl_breached = 1 if (sl_price and candle["low"] <= sl_price) else 0
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "symbol": symbol, "instrument_token": token,
        "candle_date": candle["start"].strftime("%Y-%m-%d"),
        "candle_time": candle["start"].strftime("%H:%M"),
        "open":  candle["open"],  "high": candle["high"],
        "low":   candle["low"],   "close": candle["close"],
        "volume": candle["volume"],
        "sl_price": sl_price, "sl_breached": sl_breached,
        "created_at": now,
    }
    with _db() as conn:
        conn.execute("""
            INSERT INTO live_candles
            (symbol, instrument_token, candle_date, candle_time,
             open, high, low, close, volume, sl_price, sl_breached, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (row["symbol"], row["instrument_token"], row["candle_date"],
              row["candle_time"], row["open"], row["high"], row["low"],
              row["close"], row["volume"], row["sl_price"],
              row["sl_breached"], row["created_at"]))

    broadcast_data = json.dumps({**row, "start": row["candle_time"]})
    if _main._main_loop:
        asyncio.run_coroutine_threadsafe(
            live_candle_manager.broadcast(broadcast_data),
            _main._main_loop
        )
    if sl_breached:
        print(f"[live] ⚠ SL BREACHED {symbol}: low={candle['low']} <= sl={sl_price}")
    else:
        print(f"[live] Candle saved {symbol} {row['candle_time']} O={candle['open']} H={candle['high']} L={candle['low']} C={candle['close']}")


def _flush_completed_candles(token: int, current_start: datetime):
    to_flush = [(k, v) for k, v in list(_tick_candles.items())
                if k[0] == token and k[1] != current_start.isoformat()]
    sub = _active_subs.get(token, {})
    for key, candle in to_flush:
        _save_live_candle(token, candle, sub.get("sl_price"), sub.get("symbol", ""))
        del _tick_candles[key]


# ── Tick handler (assigned to KiteTicker.on_ticks) ───────────────────────────

def on_ticks(ws, ticks):
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    now    = datetime.now(ist_tz)
    tick_time = now.isoformat()

    for tick in ticks:
        token = tick["instrument_token"]
        if token not in _active_subs:
            continue

        price  = tick.get("last_price", 0)
        volume = tick.get("volume_traded", 0)
        start  = _candle_start(now)
        key    = (token, start.isoformat())

        # ── 5-min candle aggregation ──────────────────────────────────────
        _flush_completed_candles(token, start)

        if key not in _tick_candles:
            _tick_candles[key] = {"open": price, "high": price,
                                   "low":  price, "close": price,
                                   "volume": volume, "start": start}
        else:
            c = _tick_candles[key]
            c["high"]   = max(c["high"], price)
            c["low"]    = min(c["low"],  price)
            c["close"]  = price
            c["volume"] = volume

        # ── Buffer raw tick for 1-min batch insert ────────────────────────
        ohlc  = tick.get("ohlc", {})
        depth = tick.get("depth", {})
        ltt   = tick.get("last_trade_time")
        ltt_str = ltt.isoformat() if hasattr(ltt, "isoformat") else str(ltt) if ltt else None
        row = (
            _active_subs[token].get("symbol", ""),
            token,
            tick_time,
            price,
            tick.get("last_traded_quantity"),
            ltt_str,
            tick.get("average_traded_price"),
            volume,
            tick.get("total_buy_quantity"),
            tick.get("total_sell_quantity"),
            ohlc.get("open"),
            ohlc.get("high"),
            ohlc.get("low"),
            ohlc.get("close"),
            tick.get("change"),
            tick.get("oi"),
            tick.get("oi_day_high"),
            tick.get("oi_day_low"),
            json.dumps(depth) if depth else None,
        )
        with _tick_buffer_lock:
            _tick_buffer.append(row)


# ── Subscribe / Unsubscribe ───────────────────────────────────────────────────

def subscribe_to_stock(symbol: str, sl_price, kite=None):
    import Main as _main
    try:
        if kite is None:
            kite = _main.get_kite()
        token = _main.get_token(kite, symbol)
        _active_subs[token] = {"symbol": symbol, "sl_price": sl_price}
        if _main._ticker:
            _main._ticker.subscribe([token])
            _main._ticker.set_mode(_main._ticker.MODE_FULL, [token])
        print(f"[live] Subscribed {symbol} token={token} sl={sl_price}")
    except Exception as e:
        print(f"[live] Subscribe error {symbol}: {e}")


def resubscribe_all(ws):
    """Called from on_connect after ticker reconnects."""
    if _active_subs:
        tokens = list(_active_subs.keys())
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)
        print(f"[live] Re-subscribed {len(tokens)} tokens after reconnect")


# ── EOD cleanup at 3:30 PM IST ───────────────────────────────────────────────

async def eod_cleanup():
    import Main as _main
    ist = timezone(timedelta(hours=5, minutes=30))
    while True:
        now    = datetime.now(ist)
        target = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        print("[live] EOD 3:30 PM — flushing candles and unsubscribing all tokens")
        for token, sub in list(_active_subs.items()):
            for key, candle in list(_tick_candles.items()):
                if key[0] == token:
                    _save_live_candle(token, candle, sub.get("sl_price"), sub.get("symbol", ""))
            if _main._ticker:
                try:
                    _main._ticker.unsubscribe([token])
                except Exception:
                    pass
        _active_subs.clear()
        _tick_candles.clear()
        print("[live] EOD cleanup complete")


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/api/live-candles")
def api_live_candles():
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM live_candles ORDER BY id DESC LIMIT 500"
        ).fetchall()
    return [dict(r) for r in rows]


@router.websocket("/ws/live-candles")
async def live_candles_ws(ws: WebSocket):
    await live_candle_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        live_candle_manager.disconnect(ws)


@router.get("/api/live-candles/by-symbol")
def api_live_candles_by_symbol():
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT symbol, candle_date, candle_time, volume, open, high, low, close, sl_breached
            FROM live_candles
            WHERE candle_date = DATE('now')
            ORDER BY symbol, candle_date, candle_time
        """).fetchall()
    grouped = {}
    for r in rows:
        sym = r["symbol"]
        if sym not in grouped:
            grouped[sym] = []
        grouped[sym].append(dict(r))
    return grouped


@router.get("/api/live-candles/order-book-by-symbol")
def api_order_book_by_symbol():
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT symbol, tick_time, last_price, last_quantity, depth_json
            FROM raw_ticks
            WHERE DATE(tick_time) = DATE('now')
            ORDER BY symbol, tick_time
        """).fetchall()

    from collections import defaultdict
    buckets: dict = defaultdict(lambda: defaultdict(lambda: {
        "agg_buy_vol": 0, "agg_sell_vol": 0, "last_depth": None
    }))
    for r in rows:
        sym = r["symbol"]
        t   = (r["tick_time"] or "")[:16]
        sep = "T" if "T" in t else " "
        if sep not in t:
            continue
        time_part = t.split(sep)[1]
        h, m = int(time_part[:2]), int(time_part[3:5])
        candle_time = f"{h:02d}:{(m // 5) * 5:02d}"

        lp = r["last_price"]
        lq = r["last_quantity"] or 0
        depth = None
        if r["depth_json"]:
            try:
                depth = json.loads(r["depth_json"])
            except Exception:
                pass

        bucket = buckets[sym][candle_time]
        if depth and lp and lq:
            buy_lvls  = depth.get("buy", [])
            sell_lvls = depth.get("sell", [])
            best_bid  = buy_lvls[0]["price"]  if buy_lvls  else None
            best_ask  = sell_lvls[0]["price"] if sell_lvls else None
            # Aggressive buy: trade at or above best ask (buyer hit the offer)
            if best_ask is not None and lp >= best_ask:
                bucket["agg_buy_vol"] += lq
            # Aggressive sell: trade at or below best bid (seller hit the bid)
            elif best_bid is not None and lp <= best_bid:
                bucket["agg_sell_vol"] += lq
        if depth:
            bucket["last_depth"] = depth

    result = {}
    for sym, candles in buckets.items():
        result[sym] = [
            {
                "candle_time":  ct,
                "agg_buy_vol":  v["agg_buy_vol"],
                "agg_sell_vol": v["agg_sell_vol"],
                "depth":        v["last_depth"],
            }
            for ct, v in sorted(candles.items())
        ]
    return result


@router.get("/api/live-candles/quote-by-symbol")
def api_quote_by_symbol():
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT symbol, candle_time, last_price, buy_quantity, sell_quantity,
                   volume, average_price, upper_circuit_limit, lower_circuit_limit,
                   day_open, day_high, day_low, prev_close, snapshot_at
            FROM quote_snapshots
            WHERE DATE(snapshot_at) = DATE('now')
            ORDER BY symbol, candle_time
        """).fetchall()
    grouped = {}
    for r in rows:
        sym = r["symbol"]
        if sym not in grouped:
            grouped[sym] = []
        grouped[sym].append(dict(r))
    return grouped


@router.get("/volume-chart", response_class=HTMLResponse)
def volume_chart_ui():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Volume Chart — Live Candles</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#0f0f1a;color:#cdd6f4;padding:24px 16px}
    .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;flex-wrap:wrap;gap:12px}
    h1{font-size:1.25rem;color:#fff}
    .meta{font-size:.8rem;color:#6b7280;margin-top:2px}
    .dot{width:8px;height:8px;border-radius:50%;background:#4b5563;display:inline-block;margin-right:5px;vertical-align:middle}
    .dot.live{background:#22c55e;animation:pulse 1.2s infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
    .btn{padding:7px 14px;border:none;border-radius:8px;font-size:.82rem;font-weight:700;cursor:pointer;background:#2a2a3e;color:#9ca3af}
    .btn:hover{background:#374151}
    .card{background:#1e1e2e;border-radius:12px;border:1px solid #2a2a3e;padding:20px;margin-bottom:20px}
    .card-title{font-size:.9rem;font-weight:700;color:#9ca3af;margin-bottom:14px;text-transform:uppercase;letter-spacing:.05em}
    .chart-wrap{position:relative;height:320px;cursor:pointer}
    .empty{text-align:center;padding:80px;color:#4b5563}
    /* Depth detail panel */
    #depth-panel{display:none;background:#1e1e2e;border:1px solid #374151;border-radius:12px;padding:18px;margin-bottom:20px}
    #depth-panel.visible{display:block}
    .dp-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
    .dp-title{font-size:.95rem;font-weight:700;color:#fff}
    .dp-meta{font-size:.8rem;color:#6b7280}
    .dp-close{background:none;border:none;color:#6b7280;font-size:1.1rem;cursor:pointer;padding:2px 6px}
    .dp-close:hover{color:#fff}
    .dp-body{display:grid;grid-template-columns:1fr 1fr;gap:16px}
    .dp-section-title{font-size:.75rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;margin-bottom:8px}
    .dp-section-title.bid{color:#34d399}
    .dp-section-title.ask{color:#f87171}
    .dp-table{width:100%;border-collapse:collapse;font-size:.78rem}
    .dp-table th{color:#6b7280;font-weight:600;padding:4px 8px;text-align:right;border-bottom:1px solid #2a2a3e}
    .dp-table th:first-child{text-align:left}
    .dp-table td{padding:4px 8px;text-align:right;border-bottom:1px solid #1a1a2e}
    .dp-table td:first-child{text-align:left}
    .dp-table tr:last-child td{border-bottom:none}
    .dp-table .bid-price{color:#34d399}
    .dp-table .ask-price{color:#f87171}
    .dp-summary{margin-top:14px;display:flex;gap:24px;flex-wrap:wrap;padding-top:12px;border-top:1px solid #2a2a3e}
    .dp-stat{display:flex;flex-direction:column;gap:2px}
    .dp-stat-label{font-size:.7rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em}
    .dp-stat-value{font-size:.95rem;font-weight:700}
    .dp-stat-value.green{color:#34d399}
    .dp-stat-value.red{color:#f87171}
    .dp-stat-value.blue{color:#818cf8}
  </style>
</head>
<body>
<div class="header">
  <div>
    <h1>Live 5-Min Charts</h1>
    <div class="meta"><span class="dot" id="dot"></span><span id="st">Loading...</span></div>
  </div>
  <div style="display:flex;gap:8px">
    <button class="btn" onclick="loadAll()">Refresh</button>
    <a href="/live-candles" class="btn" style="text-decoration:none;display:inline-flex;align-items:center">Candle View</a>
  </div>
</div>

<!-- Depth detail panel (shown on click) -->
<div id="depth-panel">
  <div class="dp-header">
    <div>
      <div class="dp-title" id="dp-title">—</div>
      <div class="dp-meta" id="dp-meta">Click any chart point to see order book snapshot</div>
    </div>
    <button class="dp-close" onclick="closeDepth()">✕</button>
  </div>
  <div id="dp-bid" style="display:none"></div>
  <div id="dp-ask" style="display:none"></div>
  <div class="dp-summary">
    <div class="dp-stat"><span class="dp-stat-label">Buy Quantity</span><span class="dp-stat-value green" id="dp-agg-buy">—</span></div>
    <div class="dp-stat"><span class="dp-stat-label">Sell Quantity</span><span class="dp-stat-value red" id="dp-agg-sell">—</span></div>
    <div class="dp-stat"><span class="dp-stat-label">Traded Volume</span><span class="dp-stat-value blue" id="dp-vol">—</span></div>
  </div>
</div>

<div class="card">
  <div class="card-title">Buy Quantity (Quote API)</div>
  <div class="chart-wrap"><canvas id="cv-buy"></canvas></div>
</div>
<div class="card">
  <div class="card-title">Sell Quantity (Quote API)</div>
  <div class="chart-wrap"><canvas id="cv-sell"></canvas></div>
</div>
<div class="card">
  <div class="card-title">Traded Volume</div>
  <div class="chart-wrap"><canvas id="cv-vol"></canvas></div>
</div>

<script>
const COLORS = ['#818cf8','#34d399','#f472b6','#fb923c','#38bdf8','#a78bfa','#4ade80','#facc15'];
const charts = {};
let _quoteData = {};  // latest quote snapshot data, keyed by symbol

function fmtVol(v){
  if(v==null||v===0) return '0';
  if(v>=1e6) return (v/1e6).toFixed(2)+'M';
  if(v>=1e3) return (v/1e3).toFixed(0)+'K';
  return String(Math.round(v));
}

function makeLabels(data, candleKey) {
  const timeSet = new Set();
  Object.values(data).forEach(arr => arr.forEach(c => timeSet.add(c[candleKey])));
  return Array.from(timeSet).sort();
}

function makeDatasets(data, labels, valueKey) {
  return Object.keys(data).sort().map((sym, i) => {
    const byTime = {};
    data[sym].forEach(c => byTime[c.candle_time] = c[valueKey] ?? null);
    return {
      label: sym,
      data: labels.map(t => byTime[t] ?? null),
      borderColor: COLORS[i % COLORS.length],
      backgroundColor: 'transparent',
      borderWidth: 2,
      pointRadius: 3,
      pointHoverRadius: 6,
      tension: 0.3,
      spanGaps: false,
    };
  });
}

function chartOptions(fmtFn) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { intersect: false, mode: 'index' },
    plugins: {
      legend: { display: true, position: 'top',
        labels: { color: '#9ca3af', font: { size: 12 }, boxWidth: 14, padding: 16 } },
      tooltip: { callbacks: { label: ctx => ' ' + ctx.dataset.label + ': ' + fmtFn(ctx.parsed.y) } }
    },
    scales: {
      x: { ticks: { color: '#6b7280', font: { size: 10 }, maxRotation: 45 }, grid: { color: '#1a1a2e' } },
      y: { ticks: { color: '#6b7280', font: { size: 10 }, callback: v => fmtFn(v) }, grid: { color: '#1a1a2e' } }
    },
    onClick(evt, active) {
      if (!active.length) return;
      const idx     = active[0].index;
      const dsIdx   = active[0].datasetIndex;
      const chart   = active[0].chart;
      const time    = chart.data.labels[idx];
      const sym     = chart.data.datasets[dsIdx].label;
      showDepth(sym, time);
    }
  };
}

function renderChart(id, data, valueKey, fmtFn) {
  const labels   = makeLabels(data, 'candle_time');
  const datasets = makeDatasets(data, labels, valueKey);
  if (!charts[id]) {
    charts[id] = new Chart(document.getElementById(id).getContext('2d'), {
      type: 'line',
      data: { labels, datasets },
      options: chartOptions(fmtFn),
    });
  } else {
    charts[id].data.labels   = labels;
    charts[id].data.datasets = datasets;
    charts[id].update('none');
  }
}

function showDepth(sym, time) {
  const quoteCandles = _quoteData[sym] || [];
  const quoteCandle  = quoteCandles.find(c => c.candle_time === time);
  const vol = quoteCandle ? quoteCandle.volume : null;

  document.getElementById('dp-title').textContent = sym + '  —  ' + time;
  document.getElementById('dp-meta').textContent  = 'Quote snapshot at candle close · click another point to update';

  const bidLevels = [];
  const askLevels = [];
  const bidTotal  = 0;
  const askTotal  = 0;

  function buildRows(levels, cls) {
    return '<tr><td colspan="3" style="color:#4b5563;text-align:center">No data</td></tr>';
  }

  document.getElementById('dp-bid').innerHTML     = buildRows(bidLevels, 'bid-price');
  document.getElementById('dp-ask').innerHTML     = buildRows(askLevels, 'ask-price');
  document.getElementById('dp-bid-total').textContent = '—';
  document.getElementById('dp-ask-total').textContent = '—';
  document.getElementById('dp-agg-buy').textContent  = quoteCandle ? fmtVol(quoteCandle.buy_quantity)  : '—';
  document.getElementById('dp-agg-sell').textContent = quoteCandle ? fmtVol(quoteCandle.sell_quantity) : '—';
  document.getElementById('dp-vol').textContent       = vol != null ? fmtVol(vol) : '—';

  const panel = document.getElementById('depth-panel');
  panel.classList.add('visible');
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function closeDepth() {
  document.getElementById('depth-panel').classList.remove('visible');
}

async function loadQuote() {
  try {
    const qData = await fetch('/api/live-candles/quote-by-symbol').then(r => r.json());
    _quoteData = qData;
    if (Object.keys(qData).length) {
      renderChart('cv-buy',  qData, 'buy_quantity',  fmtVol);
      renderChart('cv-sell', qData, 'sell_quantity', fmtVol);
      renderChart('cv-vol',  qData, 'volume',        fmtVol);
    }
    document.getElementById('st').textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('st').textContent = 'Load error: ' + e.message;
  }
}

async function loadAll() {
  try {
    const qData = await fetch('/api/live-candles/quote-by-symbol').then(r => r.json());
    _quoteData = qData;
    if (Object.keys(qData).length) {
      renderChart('cv-buy',  qData, 'buy_quantity',  fmtVol);
      renderChart('cv-sell', qData, 'sell_quantity', fmtVol);
      renderChart('cv-vol',  qData, 'volume',        fmtVol);
    }
    document.getElementById('st').textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('st').textContent = 'Load error: ' + e.message;
  }
}

const wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
const ws = new WebSocket(`${wsProto}://${location.host}/ws/live-candles`);
ws.onopen  = () => { document.getElementById('dot').classList.add('live'); document.getElementById('st').textContent = 'Live'; };
ws.onclose = () => { document.getElementById('dot').classList.remove('live'); setInterval(loadAll, 30000); };
ws.onmessage = e => {
  try {
    const msg = JSON.parse(e.data);
    if (msg.type === 'tick_flush') { loadQuote(); }
    else { loadAll(); }
  } catch { loadAll(); }
};

loadAll();
</script>
</body>
</html>"""


@router.get("/live-candles", response_class=HTMLResponse)
def live_candles_ui():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Live 5-Min Candles</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#0f0f1a;color:#cdd6f4;padding:28px 16px}
    .page-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px}
    h1{font-size:1.3rem;color:#fff}
    .meta{font-size:.82rem;color:#6b7280;margin-top:2px}
    .dot{width:9px;height:9px;border-radius:50%;background:#4b5563;display:inline-block;margin-right:5px;vertical-align:middle}
    .dot.live{background:#22c55e;animation:pulse 1.2s infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
    .controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    input[type=text]{padding:7px 12px;border:1px solid #374151;border-radius:8px;background:#1e1e2e;color:#cdd6f4;font-size:.85rem;outline:none;width:180px}
    .btn{padding:7px 14px;border:none;border-radius:8px;font-size:.82rem;font-weight:700;cursor:pointer;background:#2a2a3e;color:#9ca3af;transition:background .15s}
    .btn:hover{background:#374151}

    .stock-group{background:#1e1e2e;border-radius:12px;margin-bottom:12px;overflow:hidden;border:1px solid #2a2a3e}
    .stock-group.sl-breach{border-color:#7f1d1d}
    .group-header{display:flex;align-items:center;padding:13px 16px;cursor:pointer;user-select:none;gap:12px;transition:background .15s;flex-wrap:wrap}
    .group-header:hover{background:#252535}
    .group-header.sl-breach{background:#1f0d0d}
    .chevron{font-size:.75rem;color:#6b7280;transition:transform .2s;flex-shrink:0;width:12px}
    .chevron.open{transform:rotate(90deg)}
    .sym-name{font-size:1rem;font-weight:800;color:#fff;min-width:110px}
    .sl-hit-tag{font-size:.68rem;font-weight:700;padding:2px 7px;border-radius:999px;margin-left:8px;background:#450a0a;color:#fca5a5;vertical-align:middle}
    .latest-close{font-size:1rem;font-weight:700;min-width:80px}
    .bull{color:#22c55e}
    .bear{color:#ef4444}
    .header-stats{display:flex;gap:14px;align-items:center;flex:1;flex-wrap:wrap}
    .stat{font-size:.78rem;color:#6b7280}
    .stat span{color:#d1d5db}
    .sl-badge-breach{background:#450a0a;color:#fca5a5;padding:2px 10px;border-radius:999px;font-size:.74rem;font-weight:700;flex-shrink:0}
    .sl-badge-ok{background:#14532d;color:#86efac;padding:2px 10px;border-radius:999px;font-size:.74rem;font-weight:700;flex-shrink:0}
    .sl-badge-none{color:#4b5563;font-size:.74rem;flex-shrink:0}
    .candle-count{font-size:.73rem;color:#6b7280;background:#0f0f1a;padding:3px 10px;border-radius:999px;flex-shrink:0}

    .group-body{display:none;border-top:1px solid #2a2a3e}
    .group-body.open{display:block}
    .wrap{overflow-x:auto}
    table{width:100%;border-collapse:collapse;font-size:.83rem}
    thead th{background:#161625;color:#5b6374;padding:8px 14px;text-align:right;white-space:nowrap;font-size:.7rem;text-transform:uppercase;letter-spacing:.05em}
    thead th:first-child{text-align:left}
    tbody td{padding:8px 14px;border-bottom:1px solid #1a1a2e;text-align:right;white-space:nowrap}
    tbody td:first-child{text-align:left;color:#9ca3af}
    tbody tr:last-child td{border-bottom:none}
    tbody tr:hover{background:#262638}
    .row-sl{background:#1a0808!important}
    .badge-breach{background:#450a0a;color:#fca5a5;padding:2px 8px;border-radius:999px;font-size:.7rem;font-weight:700}
    .badge-ok{background:#14532d;color:#86efac;padding:2px 8px;border-radius:999px;font-size:.7rem;font-weight:700}
    .empty{text-align:center;padding:60px;color:#4b5563;font-size:.9rem}
  </style>
</head>
<body>
  <div class="page-header">
    <div>
      <h1>Live 5-Min Candles</h1>
      <div class="meta"><span class="dot" id="dot"></span><span id="st">Connecting...</span></div>
    </div>
    <div class="controls">
      <input type="text" id="filter" placeholder="Filter symbol..." oninput="render()"/>
      <button class="btn" onclick="toggleAll(true)">Expand All</button>
      <button class="btn" onclick="toggleAll(false)">Collapse All</button>
      <button class="btn" onclick="loadHistory()">Refresh</button>
      <button class="btn" onclick="clearData()">Clear</button>
    </div>
  </div>
  <div id="container"><div class="empty">Waiting for candles...</div></div>
  <script>
    const groups = {}; // symbol → {candles:[], expanded:bool, hasBreached:bool}
    const fmt    = v => v != null ? Number(v).toFixed(2) : '—';
    const fmtVol = v => !v ? '—' : v>=1e6?(v/1e6).toFixed(1)+'M':v>=1e3?(v/1e3).toFixed(0)+'K':String(v);

    function candleKey(c) { return (c.candle_date||'')+'|'+(c.candle_time||c.start||''); }

    function addCandle(c) {
      const sym = c.symbol;
      if (!groups[sym]) groups[sym] = {candles:[], expanded:true, hasBreached:false};
      const g = groups[sym];
      const key = candleKey(c);
      if (!g.candles.some(x => candleKey(x) === key)) {
        g.candles.push(c);
        g.candles.sort((a,b) => candleKey(b).localeCompare(candleKey(a))); // newest first
      }
      if (c.sl_breached) g.hasBreached = true;
    }

    function orderedSymbols() {
      return Object.keys(groups).sort((a,b) => {
        const ca = groups[a].candles[0], cb = groups[b].candles[0];
        return (cb ? candleKey(cb) : '').localeCompare(ca ? candleKey(ca) : '');
      });
    }

    function render() {
      const q = document.getElementById('filter').value.trim().toUpperCase();
      const syms = orderedSymbols().filter(s => !q || s.toUpperCase().includes(q));
      const container = document.getElementById('container');
      if (!syms.length) { container.innerHTML='<div class="empty">No candles yet.</div>'; return; }

      container.innerHTML = syms.map(sym => {
        const g = groups[sym];
        const latest = g.candles[0];
        const priceCls = latest.close > latest.open ? 'bull' : latest.close < latest.open ? 'bear' : '';
        const slBadge = g.hasBreached
          ? '<span class="sl-badge-breach">⚠ SL Breached</span>'
          : latest.sl_price
            ? '<span class="sl-badge-ok">SL ₹'+fmt(latest.sl_price)+'</span>'
            : '<span class="sl-badge-none">No SL</span>';
        const hitTag = g.hasBreached ? '<span class="sl-hit-tag">⚠ SL HIT</span>' : '';
        const latestTime = (latest.candle_date ? latest.candle_date+' ' : '')+(latest.candle_time||latest.start||'');
        const rows = g.candles.map(r => {
          const cls = r.close>r.open?'bull':r.close<r.open?'bear':'';
          const badge = r.sl_breached
            ? '<span class="badge-breach">⚠ BREACHED</span>'
            : r.sl_price ? '<span class="badge-ok">Safe</span>' : '—';
          const t = (r.candle_date ? r.candle_date+' ' : '')+(r.candle_time||r.start||'');
          return '<tr class="'+(r.sl_breached?'row-sl':'')+'"><td>'+t+'</td><td>'+fmt(r.open)+'</td><td>'+fmt(r.high)+'</td><td>'+fmt(r.low)+'</td><td class="'+cls+'">'+fmt(r.close)+'</td><td>'+fmtVol(r.volume)+'</td><td>'+badge+'</td></tr>';
        }).join('');
        const encSym = sym.replace(/&/g,'&amp;');
        return '<div class="stock-group'+(g.hasBreached?' sl-breach':'')+'" id="grp-'+encSym+'">'+
          '<div class="group-header'+(g.hasBreached?' sl-breach':'')+'" data-sym="'+encSym+'" onclick="toggle(this.dataset.sym)">'+
            '<span class="chevron'+(g.expanded?' open':'')+'" id="chv-'+encSym+'">▶</span>'+
            '<span class="sym-name">'+encSym+hitTag+'</span>'+
            '<span class="latest-close '+priceCls+'">₹'+fmt(latest.close)+'</span>'+
            '<div class="header-stats">'+
              '<span class="stat">O <span>'+fmt(latest.open)+'</span></span>'+
              '<span class="stat">H <span>'+fmt(latest.high)+'</span></span>'+
              '<span class="stat">L <span>'+fmt(latest.low)+'</span></span>'+
              '<span class="stat">Vol <span>'+fmtVol(latest.volume)+'</span></span>'+
              '<span class="stat">Updated <span>'+latestTime+'</span></span>'+
            '</div>'+
            slBadge+
            '<span class="candle-count">'+g.candles.length+' candle'+(g.candles.length!==1?'s':'')+'</span>'+
          '</div>'+
          '<div class="group-body'+(g.expanded?' open':'')+'" id="body-'+encSym+'">'+
            '<div class="wrap"><table>'+
              '<thead><tr><th>Time</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th><th>SL</th></tr></thead>'+
              '<tbody>'+rows+'</tbody>'+
            '</table></div>'+
          '</div>'+
        '</div>';
      }).join('');
    }

    function toggle(sym) {
      if (!groups[sym]) return;
      groups[sym].expanded = !groups[sym].expanded;
      const body = document.getElementById('body-'+sym);
      const chv  = document.getElementById('chv-'+sym);
      if (body) body.classList.toggle('open', groups[sym].expanded);
      if (chv)  chv.classList.toggle('open', groups[sym].expanded);
    }

    function toggleAll(open) {
      for (const sym in groups) groups[sym].expanded = open;
      render();
    }

    function clearData() {
      for (const sym in groups) delete groups[sym];
      render();
    }

    async function loadHistory() {
      try {
        const data = await (await fetch('/api/live-candles')).json();
        for (const c of data) addCandle(c);
        render();
      } catch(e) { console.error('fetch failed', e); }
    }

    const wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${wsProto}://${location.host}/ws/live-candles`);
    ws.onopen  = () => { document.getElementById('dot').classList.add('live'); document.getElementById('st').textContent='Live — 5-min candles streaming'; };
    ws.onclose = () => {
      document.getElementById('dot').classList.remove('live');
      document.getElementById('st').textContent='Disconnected — polling every 30s';
      setInterval(loadHistory, 30000);
    };
    ws.onmessage = e => { addCandle(JSON.parse(e.data)); render(); };
    loadHistory();
  </script>
</body>
</html>
"""
