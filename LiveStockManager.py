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
_tick_candles: dict = {}   # (token, candle_start_iso) → candle dict
_active_subs: dict  = {}   # instrument_token → {symbol, sl_price}


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

    for tick in ticks:
        token = tick["instrument_token"]
        if token not in _active_subs:
            continue
        price  = tick.get("last_price", 0)
        volume = tick.get("volume_traded", 0)
        start  = _candle_start(now)
        key    = (token, start.isoformat())

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
            _main._ticker.set_mode(_main._ticker.MODE_QUOTE, [token])
        print(f"[live] Subscribed {symbol} token={token} sl={sl_price}")
    except Exception as e:
        print(f"[live] Subscribe error {symbol}: {e}")


def resubscribe_all(ws):
    """Called from on_connect after ticker reconnects."""
    if _active_subs:
        tokens = list(_active_subs.keys())
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_QUOTE, tokens)
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
    .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px}
    h1{font-size:1.3rem;color:#fff}
    .meta{font-size:.82rem;color:#6b7280;margin-top:2px}
    .dot{width:9px;height:9px;border-radius:50%;background:#4b5563;display:inline-block;margin-right:5px}
    .dot.live{background:#22c55e;animation:pulse 1.2s infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
    .controls{display:flex;gap:8px;align-items:center}
    input[type=text]{padding:7px 12px;border:1px solid #374151;border-radius:8px;background:#1e1e2e;color:#cdd6f4;font-size:.85rem;outline:none;width:180px}
    button{padding:7px 16px;border:none;border-radius:8px;font-size:.82rem;font-weight:700;cursor:pointer;background:#374151;color:#9ca3af}
    button:hover{background:#4b5563}
    .wrap{overflow-x:auto;background:#1e1e2e;border-radius:12px}
    table{width:100%;border-collapse:collapse;font-size:.85rem}
    thead th{background:#0f0f1a;color:#6b7280;padding:10px 14px;text-align:right;white-space:nowrap;font-size:.73rem;text-transform:uppercase;letter-spacing:.05em;position:sticky;top:0}
    thead th:first-child,thead th:nth-child(2){text-align:left}
    tbody td{padding:9px 14px;border-bottom:1px solid #1a1a2e;text-align:right;white-space:nowrap}
    tbody td:first-child,tbody td:nth-child(2){text-align:left}
    tbody tr:last-child td{border-bottom:none}
    tbody tr:hover{background:#262638}
    .bull{color:#22c55e;font-weight:700}
    .bear{color:#ef4444;font-weight:700}
    .sl-hit{background:#450a0a!important}
    .badge-breach{background:#450a0a;color:#fca5a5;padding:2px 8px;border-radius:999px;font-size:.72rem;font-weight:700}
    .badge-ok{background:#14532d;color:#86efac;padding:2px 8px;border-radius:999px;font-size:.72rem;font-weight:700}
    .empty{text-align:center;padding:60px;color:#4b5563}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>Live 5-Min Candles</h1>
      <div class="meta"><span class="dot" id="dot"></span><span id="st">Connecting...</span></div>
    </div>
    <div class="controls">
      <input type="text" id="filter" placeholder="Filter symbol..." oninput="render()"/>
      <button onclick="loadHistory()">Refresh</button>
      <button onclick="rows=[];render()">Clear</button>
    </div>
  </div>
  <div class="wrap">
    <table>
      <thead><tr>
        <th>Time</th><th>Symbol</th><th>Open</th><th>High</th><th>Low</th>
        <th>Close</th><th>Volume</th><th>SL Price</th><th>SL Status</th>
      </tr></thead>
      <tbody id="tbody"><tr><td class="empty" colspan="9">Waiting for candles...</td></tr></tbody>
    </table>
  </div>
  <script>
    let rows = [];
    const fmt    = v => v != null ? Number(v).toFixed(2) : '—';
    const fmtVol = v => v >= 1e6 ? (v/1e6).toFixed(1)+'M' : v >= 1e3 ? (v/1e3).toFixed(0)+'K' : (v||'—');

    function render() {
      const q = document.getElementById('filter').value.trim().toUpperCase();
      const data = q ? rows.filter(r => r.symbol.includes(q)) : rows;
      if (!data.length) { document.getElementById('tbody').innerHTML='<tr><td class="empty" colspan="9">No candles yet.</td></tr>'; return; }
      document.getElementById('tbody').innerHTML = data.map(r => {
        const cls   = r.close > r.open ? 'bull' : r.close < r.open ? 'bear' : '';
        const badge = r.sl_breached ? '<span class="badge-breach">⚠ BREACHED</span>' : (r.sl_price ? '<span class="badge-ok">Safe</span>' : '—');
        return `<tr class="${r.sl_breached?'sl-hit':''}">
          <td>${r.candle_date} ${r.candle_time}</td><td><strong>${r.symbol}</strong></td>
          <td>${fmt(r.open)}</td><td>${fmt(r.high)}</td><td>${fmt(r.low)}</td>
          <td class="${cls}">${fmt(r.close)}</td><td>${fmtVol(r.volume)}</td>
          <td>${r.sl_price?fmt(r.sl_price):'—'}</td><td>${badge}</td>
        </tr>`;
      }).join('');
    }

    async function loadHistory() {
      try {
        const d = await (await fetch('/api/live-candles')).json();
        rows = d;
        render();
      } catch(e) { console.error('fetch failed', e); }
    }

    // Use wss:// on HTTPS pages to avoid mixed-content browser block
    const wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${wsProto}://${location.host}/ws/live-candles`);
    ws.onopen    = () => { document.getElementById('dot').classList.add('live'); document.getElementById('st').textContent = 'Live — 5-min candles streaming'; };
    ws.onclose   = () => {
      document.getElementById('dot').classList.remove('live');
      document.getElementById('st').textContent = 'Disconnected — polling every 30s';
      // Fall back to polling when WebSocket is unavailable
      setInterval(loadHistory, 30000);
    };
    ws.onmessage = e => { rows.unshift(JSON.parse(e.data)); if(rows.length>500) rows.pop(); render(); };

    loadHistory();
  </script>
</body>
</html>
"""
