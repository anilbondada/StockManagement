"""
StockInPlay — Router
=====================
Fibonacci 61.8% retracement strategy triggered by ChartInk webhook.

Flow per symbol:
  1. Webhook received → wait for current 5-min candle to close
  2. Verify: candle_close > day_open  AND  candle_close > prev_day_close
  3. Verify: liquidity, upper circuit %, entry gain %
  4. Fib 61.8 = day_high − 0.618 × (day_high − day_low)
  5. Place LIMIT BUY at Fib 61.8
  6. Wait for LIMIT BUY to fill; then place SL-BUY at day_high + 1
  7. After LIMIT BUY fills: wait for active candle to close → SL-SELL at candle_low − 1
  8. If LIMIT BUY unfilled by next candle close:
       cancel LIMIT BUY, recalibrate Fib, place new LIMIT BUY (repeat steps 5–7)
  9. Repeat until manual cancel or 12:30 PM IST
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

DB_FILE           = "alerts.db"
IST               = timezone(timedelta(hours=5, minutes=30))
DEADLINE_HOUR     = 12
DEADLINE_MIN      = 30
WEBHOOK_CUTOFF    = 10   # ignore webhooks at or after 10:00 AM

router = APIRouter()

# ── In-memory state ───────────────────────────────────────────────────────────

_sip_paused           = False
_sip_disabled_stocks: set  = set()
_sip_flows: dict           = {}   # symbol → SIPFlow
_sip_last_webhook_stocks: list = []


# ── DB ────────────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_sip_table():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sip_flows (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol           TEXT,
                alert_id         INTEGER,
                alert_time       TEXT,
                day_high         REAL,
                day_low          REAL,
                prev_day_close   REAL,
                fib_level        REAL,
                limit_order_id   TEXT,
                sl_buy_order_id  TEXT,
                sl_sell_order_id TEXT,
                status           TEXT,
                note             TEXT,
                created_at       TEXT,
                updated_at       TEXT
            )
        """)


# ── Timing helpers ────────────────────────────────────────────────────────────

def _now_ist() -> datetime:
    return datetime.now(IST)


def _candle_start(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)


def _next_candle_close(dt: datetime) -> datetime:
    """Start time of the next 5-min candle = close time of the current one."""
    return _candle_start(dt) + timedelta(minutes=5)


def _secs_until(target: datetime) -> float:
    return max(0.0, (target - _now_ist()).total_seconds())


# ── Flow state ────────────────────────────────────────────────────────────────

class SIPFlow:
    def __init__(self, symbol: str, alert_id: int, alert_time: datetime):
        self.symbol      = symbol
        self.alert_id    = alert_id
        self.alert_time  = alert_time
        self.cancel_evt  = threading.Event()
        self.db_id: Optional[int] = None
        self.status      = "waiting"


def _save_flow(flow: SIPFlow, **cols):
    now = _now_ist().isoformat()
    if flow.db_id is None:
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO sip_flows (symbol,alert_id,alert_time,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (flow.symbol, flow.alert_id, flow.alert_time.isoformat(), flow.status, now, now)
            )
            flow.db_id = cur.lastrowid
    else:
        update = {**cols, "status": flow.status, "updated_at": now}
        clause = ", ".join(f"{k}=?" for k in update)
        with _db() as conn:
            conn.execute(f"UPDATE sip_flows SET {clause} WHERE id=?",
                         (*update.values(), flow.db_id))


# ── Cancel helper ─────────────────────────────────────────────────────────────

def _cancel_order(kite, symbol: str, order_id):
    if not order_id:
        return
    try:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=str(order_id))
        print(f"[sip] {symbol}: cancelled order {order_id}")
    except Exception as e:
        print(f"[sip] {symbol}: cancel {order_id} error: {e}")


# ── Historical candle fetch ───────────────────────────────────────────────────

def _fetch_candle(kite, token: int, candle_close_time: datetime) -> Optional[dict]:
    """Fetch the 5-min candle that closed at candle_close_time."""
    start = candle_close_time - timedelta(minutes=5)
    # Kite historical_data expects naive IST datetimes
    candles = kite.historical_data(
        token,
        from_date=start.replace(tzinfo=None),
        to_date=candle_close_time.replace(tzinfo=None),
        interval="5minute"
    )
    return candles[-1] if candles else None


# ── Core flow (one background thread per symbol) ──────────────────────────────

def _run_sip_flow(flow: SIPFlow):
    import Main as _main
    from StockConfig import get_stockinplay_config, qty_for_ltp_sip

    symbol = flow.symbol

    # Wait until the current 5-min candle closes
    first_close = _next_candle_close(flow.alert_time)
    wait = _secs_until(first_close)
    print(f"[sip] {symbol}: alert at {flow.alert_time.strftime('%H:%M:%S')}, "
          f"waiting {wait:.0f}s for candle close at {first_close.strftime('%H:%M')}")

    if flow.cancel_evt.wait(timeout=wait):
        flow.status = "cancelled"
        _save_flow(flow)
        _sip_flows.pop(symbol, None)
        return

    while not flow.cancel_evt.is_set():
        now     = _now_ist()
        deadline = now.replace(hour=DEADLINE_HOUR, minute=DEADLINE_MIN, second=0, microsecond=0)

        if now >= deadline:
            print(f"[sip] {symbol}: reached {DEADLINE_HOUR}:{DEADLINE_MIN:02d} deadline")
            flow.status = "deadline"
            _save_flow(flow)
            break

        try:
            kite = _main.get_kite()
            cfg  = get_stockinplay_config()

            # ── Market quote ─────────────────────────────────────────────
            quote  = kite.quote(f"NSE:{symbol}")
            qdata  = quote[f"NSE:{symbol}"]
            ltp                  = qdata["last_price"]
            day_open             = qdata["ohlc"]["open"]
            day_high             = qdata["ohlc"]["high"]
            day_low              = qdata["ohlc"]["low"]
            prev_day_close       = qdata["ohlc"]["close"]
            buy_qty              = qdata.get("buy_quantity", 0)
            sell_qty             = qdata.get("sell_quantity", 0)
            upper_circuit_limit  = qdata.get("upper_circuit_limit", 0)

            # ── Just-closed 5-min candle ──────────────────────────────────
            token        = _main.get_token(kite, symbol)
            candle_close = _candle_start(now)   # start of current candle = close of previous
            candle       = _fetch_candle(kite, token, candle_close)
            if not candle:
                print(f"[sip] {symbol}: no candle data at {candle_close.strftime('%H:%M')}, stopping")
                flow.status = "error"
                _save_flow(flow, note="no candle data")
                break

            c_close = candle["close"]

            # ── Condition checks ──────────────────────────────────────────
            min_book_qty       = int(cfg.get("min_book_qty", 100000))
            min_upper_ckt_pct  = float(cfg.get("min_upper_circuit_pct", 20))
            max_entry_gain_pct = float(cfg.get("max_entry_gain_pct", 10))

            upper_ckt_pct = ((upper_circuit_limit - prev_day_close) / prev_day_close * 100
                             if prev_day_close else 0)

            if c_close <= day_open:
                print(f"[sip] {symbol}: skip — c_close {c_close} <= day_open {day_open}")
                flow.status = "skipped"
                _save_flow(flow, note=f"c_close {c_close} <= day_open {day_open}")
                break

            if c_close <= prev_day_close:
                print(f"[sip] {symbol}: skip — c_close {c_close} <= prev_close {prev_day_close}")
                flow.status = "skipped"
                _save_flow(flow, note=f"c_close {c_close} <= prev_close {prev_day_close}")
                break

            if buy_qty < min_book_qty or sell_qty < min_book_qty:
                print(f"[sip] {symbol}: skip — liquidity buy={buy_qty} sell={sell_qty} need>={min_book_qty}")
                flow.status = "skipped"
                _save_flow(flow, note=f"liquidity buy={buy_qty} sell={sell_qty}")
                break

            if upper_ckt_pct <= min_upper_ckt_pct:
                print(f"[sip] {symbol}: skip — upper_circuit {upper_ckt_pct:.1f}% <= {min_upper_ckt_pct}%")
                flow.status = "skipped"
                _save_flow(flow, note=f"upper_circuit {upper_ckt_pct:.1f}%")
                break

            # ── Fibonacci 61.8% ───────────────────────────────────────────
            fib_level       = round(day_high - 0.618 * (day_high - day_low), 2)
            max_entry_price = round(prev_day_close * (1 + max_entry_gain_pct / 100), 2)

            if fib_level >= max_entry_price:
                print(f"[sip] {symbol}: skip — fib {fib_level} >= max_entry {max_entry_price} "
                      f"({max_entry_gain_pct}% above prev_close {prev_day_close})")
                flow.status = "skipped"
                _save_flow(flow, note=f"fib {fib_level} >= max_entry {max_entry_price}")
                break

            qty = qty_for_ltp_sip(ltp, cfg)

            # ── Step 5: LIMIT BUY at Fib 61.8 ────────────────────────────
            limit_order_id = kite.place_order(
                variety          = kite.VARIETY_REGULAR,
                exchange         = "NSE",
                tradingsymbol    = symbol,
                transaction_type = "BUY",
                quantity         = qty,
                product          = "MIS",
                order_type       = "LIMIT",
                validity         = "DAY",
                price            = fib_level,
            )
            print(f"[sip] {symbol}: LIMIT BUY order_id={limit_order_id} "
                  f"fib={fib_level} high={day_high} low={day_low} qty={qty}")
            flow.status = "limit_placed"
            _save_flow(flow, limit_order_id=str(limit_order_id), fib_level=fib_level,
                       day_high=day_high, day_low=day_low, prev_day_close=prev_day_close)

            # ── Wait for current candle to close ──────────────────────────
            next_close = _next_candle_close(_now_ist())
            wait       = _secs_until(next_close)
            print(f"[sip] {symbol}: waiting {wait:.0f}s — fill check at {next_close.strftime('%H:%M')}")
            if flow.cancel_evt.wait(timeout=wait):
                _cancel_order(kite, symbol, limit_order_id)
                _cancel_order(kite, symbol, sl_buy_order_id)
                flow.status = "cancelled"
                _save_flow(flow)
                _sip_flows.pop(symbol, None)
                return

            # ── Check if LIMIT BUY filled ─────────────────────────────────
            orders       = {str(o["order_id"]): o for o in kite.orders()}
            limit_status = orders.get(str(limit_order_id), {}).get("status", "")

            if limit_status == "COMPLETE":
                # ── Step 6: SL-BUY at day_high+1 (placed after fill) ─────
                sl_trigger = round(day_high + 1, 2)
                sl_buy_order_id = kite.place_order(
                    variety          = kite.VARIETY_REGULAR,
                    exchange         = "NSE",
                    tradingsymbol    = symbol,
                    transaction_type = "BUY",
                    quantity         = qty,
                    product          = "MIS",
                    order_type       = "SL",
                    validity         = "DAY",
                    price            = sl_trigger,
                    trigger_price    = sl_trigger,
                )
                print(f"[sip] {symbol}: SL-BUY order_id={sl_buy_order_id} trigger={sl_trigger}")
                _save_flow(flow, sl_buy_order_id=str(sl_buy_order_id))

                # ── Step 7: SL-SELL at closed candle low − 1 ─────────────
                fill_candle = _fetch_candle(kite, token, next_close)
                if fill_candle:
                    sl_sell_price = round(fill_candle["low"] - 1, 2)
                    sl_sell_order_id = kite.place_order(
                        variety          = kite.VARIETY_REGULAR,
                        exchange         = "NSE",
                        tradingsymbol    = symbol,
                        transaction_type = "SELL",
                        quantity         = qty,
                        product          = "MIS",
                        order_type       = "SL",
                        validity         = "DAY",
                        price            = sl_sell_price,
                        trigger_price    = sl_sell_price,
                    )
                    print(f"[sip] {symbol}: SL-SELL order_id={sl_sell_order_id} trigger={sl_sell_price} "
                          f"(candle_low={fill_candle['low']})")
                    flow.status = "sl_placed"
                    _save_flow(flow, sl_sell_order_id=str(sl_sell_order_id))
                else:
                    print(f"[sip] {symbol}: LIMIT filled but no candle data for SL-SELL")
                    flow.status = "filled_no_sl"
                    _save_flow(flow)
                break  # flow complete for this symbol

            else:
                # ── Step 8: unfilled — cancel and recalibrate ─────────────
                print(f"[sip] {symbol}: LIMIT BUY unfilled (status={limit_status}), recalibrating…")
                _cancel_order(kite, symbol, limit_order_id)
                flow.status = "recalibrating"
                _save_flow(flow)

                # Check deadline before next attempt
                if _now_ist() >= deadline:
                    print(f"[sip] {symbol}: deadline reached after cancel")
                    flow.status = "deadline"
                    _save_flow(flow)
                    break

                # Wait for next candle close before recalibrating
                next_close = _next_candle_close(_now_ist())
                wait       = _secs_until(next_close)
                print(f"[sip] {symbol}: recalibrate check at {next_close.strftime('%H:%M')} ({wait:.0f}s)")
                if flow.cancel_evt.wait(timeout=wait):
                    flow.status = "cancelled"
                    _save_flow(flow)
                    _sip_flows.pop(symbol, None)
                    return
                # loop → re-fetch quote, recalculate Fib, place new LIMIT BUY

        except Exception as e:
            print(f"[sip] {symbol}: ERROR {type(e).__name__}({getattr(e,'code','')}) {e}")
            flow.status = "error"
            _save_flow(flow, note=f"{type(e).__name__}: {e}")
            break

    _sip_flows.pop(symbol, None)


# ── Webhook ───────────────────────────────────────────────────────────────────

@router.post("/webhook/stockinplay")
async def webhook_stockinplay(payload: dict):
    import Main as _main
    global _sip_last_webhook_stocks

    ist_now = _now_ist()

    if ist_now.hour >= WEBHOOK_CUTOFF:
        print(f"[sip] webhook ignored — after {WEBHOOK_CUTOFF}:00 AM")
        return {"status": "ignored", "reason": "after_cutoff"}

    if _sip_paused:
        print("[sip] webhook ignored — strategy paused")
        return {"status": "ignored", "reason": "paused"}

    if not _main._access_token:
        return {"status": "error", "reason": "not_authenticated"}

    stocks = [s.strip().upper() for s in (payload.get("stocks") or "").split(",") if s.strip()]
    _sip_last_webhook_stocks = stocks

    with _db() as conn:
        cur = conn.execute("""
            INSERT INTO chartink_alerts
                (stocks, trigger_prices, triggered_at, scan_name, scan_url, alert_name, raw, received_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            payload.get("stocks"), payload.get("trigger_prices"),
            payload.get("triggered_at"), payload.get("scan_name"),
            payload.get("scan_url"), payload.get("alert_name"),
            json.dumps(payload), ist_now.isoformat()
        ))
        alert_id = cur.lastrowid

    started = []
    skipped = []
    for symbol in stocks:
        if symbol in _sip_disabled_stocks:
            skipped.append({"symbol": symbol, "reason": "disabled"})
            continue
        if symbol in _sip_flows:
            skipped.append({"symbol": symbol, "reason": "already_active"})
            continue

        flow = SIPFlow(symbol=symbol, alert_id=alert_id, alert_time=ist_now)
        _save_flow(flow)
        _sip_flows[symbol] = flow
        threading.Thread(target=_run_sip_flow, args=(flow,),
                         daemon=True, name=f"sip-{symbol}").start()
        started.append(symbol)
        print(f"[sip] {symbol}: flow started alert_id={alert_id}")

    return {"status": "ok", "alert_id": alert_id, "started": started, "skipped": skipped}


# ── Control API ───────────────────────────────────────────────────────────────

@router.get("/api/sip/status")
def sip_status():
    return {
        "paused":          _sip_paused,
        "disabled_stocks": sorted(_sip_disabled_stocks),
        "active_flows":    [{"symbol": s, "status": f.status} for s, f in _sip_flows.items()],
        "last_webhook":    _sip_last_webhook_stocks,
    }


@router.post("/api/sip/pause")
def sip_pause():
    global _sip_paused
    _sip_paused = True
    for flow in list(_sip_flows.values()):
        flow.cancel_evt.set()
    print("[sip] strategy paused — all flows cancelled")
    return {"paused": True}


@router.post("/api/sip/resume")
def sip_resume():
    global _sip_paused
    _sip_paused = False
    print("[sip] strategy resumed")
    return {"paused": False}


@router.post("/api/sip/disable-stock")
def sip_disable_stock(payload: dict):
    symbol = (payload.get("symbol") or "").strip().upper()
    if not symbol:
        return {"error": "symbol required"}
    _sip_disabled_stocks.add(symbol)
    if symbol in _sip_flows:
        _sip_flows[symbol].cancel_evt.set()
        print(f"[sip] {symbol}: disabled and flow cancelled")
    return {"disabled": symbol, "disabled_stocks": sorted(_sip_disabled_stocks)}


@router.post("/api/sip/enable-stock")
def sip_enable_stock(payload: dict):
    symbol = (payload.get("symbol") or "").strip().upper()
    _sip_disabled_stocks.discard(symbol)
    return {"enabled": symbol, "disabled_stocks": sorted(_sip_disabled_stocks)}


@router.post("/api/sip/cancel-flow")
def sip_cancel_flow(payload: dict):
    symbol = (payload.get("symbol") or "").strip().upper()
    if symbol in _sip_flows:
        _sip_flows[symbol].cancel_evt.set()
        return {"cancelled": symbol}
    return {"error": f"no active flow for {symbol}"}


# ── UI ────────────────────────────────────────────────────────────────────────

@router.get("/sip-control", response_class=HTMLResponse)
def sip_control_ui():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>StockInPlay Control</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#0f0f1a;color:#cdd6f4;padding:28px 16px;min-height:100vh}
    .page{max-width:700px;margin:0 auto}
    h1{font-size:1.3rem;color:#fff;margin-bottom:4px}
    .sub{font-size:.82rem;color:#6b7280;margin-bottom:24px}

    /* Status bar */
    .status-bar{background:#1e1e2e;border-radius:12px;padding:16px 20px;margin-bottom:20px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}
    .stat-item{font-size:.85rem;color:#9ca3af}
    .stat-item strong{color:#fff}
    .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:middle}
    .dot-green{background:#22c55e;animation:pulse 1.2s infinite}
    .dot-red{background:#ef4444}
    .dot-yellow{background:#f59e0b;animation:pulse 1.2s infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

    /* Cards */
    .card{background:#1e1e2e;border-radius:12px;padding:20px;margin-bottom:16px;border:1px solid #2a2a3e}
    .card-title{font-size:.75rem;font-weight:800;text-transform:uppercase;letter-spacing:.08em;color:#0891b2;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid #2a2a3e}

    /* Buttons */
    .btn{padding:9px 20px;border:none;border-radius:8px;font-size:.85rem;font-weight:700;cursor:pointer;transition:all .15s}
    .btn-pause{background:#f59e0b;color:#1a1a1a}
    .btn-pause:hover{background:#d97706}
    .btn-resume{background:#22c55e;color:#1a1a1a}
    .btn-resume:hover{background:#16a34a}
    .btn-danger{background:#ef4444;color:#fff;padding:5px 12px;font-size:.78rem}
    .btn-danger:hover{background:#dc2626}
    .btn-sm{background:#2a2a3e;color:#9ca3af;padding:5px 12px;font-size:.78rem;border-radius:6px;border:none;cursor:pointer}
    .btn-sm:hover{background:#374151}
    .btn-cyan{background:#0891b2;color:#fff}
    .btn-cyan:hover{background:#0e7490}

    /* Flows table */
    table{width:100%;border-collapse:collapse;font-size:.83rem}
    thead th{color:#6b7280;padding:6px 10px;text-align:left;font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #2a2a3e}
    tbody td{padding:8px 10px;border-bottom:1px solid #1a1a2e;vertical-align:middle}
    tbody tr:last-child td{border-bottom:none}
    .status-badge{padding:2px 9px;border-radius:999px;font-size:.72rem;font-weight:700;display:inline-block}
    .s-waiting{background:#1e3a5f;color:#93c5fd}
    .s-limit_placed{background:#14532d;color:#86efac}
    .s-recalibrating{background:#44350a;color:#fde68a}
    .s-sl_placed{background:#14532d;color:#86efac}
    .s-skipped{background:#1f2937;color:#6b7280}
    .s-cancelled{background:#1f2937;color:#6b7280}
    .s-deadline{background:#1f2937;color:#6b7280}
    .s-error{background:#450a0a;color:#fca5a5}
    .s-filled_no_sl{background:#44350a;color:#fde68a}

    /* Disabled stocks pills */
    .pills{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
    .pill{background:#2a2a3e;color:#cdd6f4;padding:5px 12px;border-radius:999px;font-size:.82rem;display:flex;align-items:center;gap:8px}
    .pill button{background:none;border:none;color:#ef4444;cursor:pointer;font-size:.9rem;line-height:1;padding:0}

    /* Stock disable input */
    .input-row{display:flex;gap:8px;margin-top:10px}
    input[type=text]{flex:1;padding:8px 12px;border:1px solid #374151;border-radius:8px;background:#161625;color:#cdd6f4;font-size:.88rem;outline:none}
    input[type=text]:focus{border-color:#0891b2}

    /* Webhook stocks list */
    .stock-grid{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
    .stock-chip{padding:5px 12px;border-radius:999px;font-size:.82rem;cursor:pointer;border:1px solid #374151;background:#1a1a2e;color:#cdd6f4;transition:all .15s;user-select:none}
    .stock-chip:hover{background:#2a2a3e}
    .stock-chip.disabled{background:#450a0a;color:#fca5a5;border-color:#7f1d1d}

    .empty{color:#4b5563;font-size:.85rem;padding:12px 0}
    .result{margin-top:10px;padding:8px 14px;border-radius:8px;font-size:.83rem;display:none}
    .result.ok{background:#14532d;color:#86efac;display:block}
    .result.err{background:#450a0a;color:#fca5a5;display:block}
  </style>
</head>
<body>
<div class="page">
  <h1>StockInPlay Control</h1>
  <p class="sub">Fibonacci 61.8% retracement strategy — manage and monitor active flows</p>

  <!-- Status bar -->
  <div class="status-bar">
    <div class="stat-item"><span class="dot" id="sys-dot"></span>Strategy: <strong id="sys-status">—</strong></div>
    <div class="stat-item">Active flows: <strong id="flow-count">—</strong></div>
    <div class="stat-item">Disabled stocks: <strong id="disabled-count">—</strong></div>
    <div style="flex:1"></div>
    <button class="btn btn-pause"   id="pauseBtn"  onclick="pauseResume(true)">Pause Strategy</button>
    <button class="btn btn-resume"  id="resumeBtn" onclick="pauseResume(false)" style="display:none">Resume Strategy</button>
  </div>

  <!-- Active flows -->
  <div class="card">
    <div class="card-title">Active Flows</div>
    <div id="flows-wrap">
      <div class="empty">No active flows.</div>
    </div>
  </div>

  <!-- Disable specific stocks from last webhook -->
  <div class="card">
    <div class="card-title">Last Webhook — Quick Disable</div>
    <p style="font-size:.8rem;color:#6b7280">Click a stock to toggle disabled for today.</p>
    <div class="stock-grid" id="webhook-stocks">
      <div class="empty">No webhook received yet.</div>
    </div>
  </div>

  <!-- Disabled stocks -->
  <div class="card">
    <div class="card-title">Disabled Stocks Today</div>
    <div class="input-row">
      <input type="text" id="disable-input" placeholder="Symbol e.g. RELIANCE" onkeydown="if(event.key==='Enter')disableStock()"/>
      <button class="btn btn-cyan" onclick="disableStock()">Disable</button>
    </div>
    <div class="pills" id="disabled-pills"></div>
    <div class="result" id="disable-result"></div>
  </div>
</div>

<script>
  async function refresh() {
    try {
      const s = await (await fetch('/api/sip/status')).json();

      // Status bar
      const paused = s.paused;
      document.getElementById('sys-dot').className    = 'dot ' + (paused ? 'dot-yellow' : 'dot-green');
      document.getElementById('sys-status').textContent = paused ? 'PAUSED' : 'Running';
      document.getElementById('flow-count').textContent = s.active_flows.length;
      document.getElementById('disabled-count').textContent = s.disabled_stocks.length;
      document.getElementById('pauseBtn').style.display  = paused ? 'none' : '';
      document.getElementById('resumeBtn').style.display = paused ? '' : 'none';

      // Active flows table
      const wrap = document.getElementById('flows-wrap');
      if (!s.active_flows.length) {
        wrap.innerHTML = '<div class="empty">No active flows.</div>';
      } else {
        wrap.innerHTML = '<table><thead><tr><th>Symbol</th><th>Status</th><th>Action</th></tr></thead><tbody>' +
          s.active_flows.map(f =>
            '<tr><td><strong>' + f.symbol + '</strong></td>' +
            '<td><span class="status-badge s-' + f.status + '">' + f.status.replace(/_/g,' ') + '</span></td>' +
            '<td><button class="btn-danger" onclick="cancelFlow(\'' + f.symbol + '\')">Cancel</button></td></tr>'
          ).join('') + '</tbody></table>';
      }

      // Last webhook stocks
      const wrapWH = document.getElementById('webhook-stocks');
      if (!s.last_webhook.length) {
        wrapWH.innerHTML = '<div class="empty">No webhook received yet.</div>';
      } else {
        wrapWH.innerHTML = s.last_webhook.map(sym => {
          const dis = s.disabled_stocks.includes(sym);
          return '<span class="stock-chip' + (dis ? ' disabled' : '') + '" onclick="toggleStock(\'' + sym + '\',' + !dis + ')">' +
            sym + (dis ? ' ✕' : '') + '</span>';
        }).join('');
      }

      // Disabled pills
      const pills = document.getElementById('disabled-pills');
      pills.innerHTML = s.disabled_stocks.length
        ? s.disabled_stocks.map(sym =>
            '<div class="pill">' + sym +
            '<button onclick="enableStock(\'' + sym + '\')" title="Re-enable">✕</button></div>'
          ).join('')
        : '<div class="empty" style="margin-top:8px">None disabled.</div>';

    } catch(e) {}
  }

  async function pauseResume(pause) {
    await fetch('/api/sip/' + (pause ? 'pause' : 'resume'), {method:'POST'});
    refresh();
  }

  async function cancelFlow(symbol) {
    await fetch('/api/sip/cancel-flow', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({symbol})});
    refresh();
  }

  async function disableStock() {
    const inp = document.getElementById('disable-input');
    const res = document.getElementById('disable-result');
    const sym = inp.value.trim().toUpperCase();
    if (!sym) return;
    const r = await fetch('/api/sip/disable-stock', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({symbol: sym})});
    const d = await r.json();
    res.className = d.error ? 'result err' : 'result ok';
    res.textContent = d.error || (sym + ' disabled for today');
    inp.value = '';
    refresh();
  }

  async function enableStock(sym) {
    await fetch('/api/sip/enable-stock', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({symbol: sym})});
    refresh();
  }

  async function toggleStock(sym, disable) {
    const endpoint = disable ? 'disable-stock' : 'enable-stock';
    await fetch('/api/sip/' + endpoint, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({symbol: sym})});
    refresh();
  }

  refresh();
  setInterval(refresh, 5000);
</script>
</body>
</html>
"""
