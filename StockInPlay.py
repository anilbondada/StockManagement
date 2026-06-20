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
       if day high/low unchanged → keep order, wait for next candle
       if day high/low changed   → modify order price to new Fib 61.8 (no cancel/replace)
  9. Repeat until filled, cancelled, or deadline_time reached
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
    def __init__(self, symbol: str, alert_id: int, alert_time: datetime, simulate: bool = False):
        self.symbol      = symbol
        self.alert_id    = alert_id
        self.alert_time  = alert_time
        self.simulate    = simulate   # bypasses deadline check when True
        self.cancel_evt  = threading.Event()
        self.db_id: Optional[int] = None
        self.status      = "waiting"


def _tag_webhook_order(order_id, symbol: str, txn_type: str):
    """Tag a SIP order in order_updates so it's treated as a webhook order."""
    with _db() as conn:
        conn.execute(
            """INSERT INTO order_updates (order_id, tradingsymbol, transaction_type, is_webhook_order, last_updated)
               VALUES (?,?,?,1,?)
               ON CONFLICT(order_id) DO UPDATE SET is_webhook_order=1""",
            (str(order_id), symbol, txn_type, datetime.now(timezone.utc).isoformat())
        )


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


def _modify_order_price(kite, symbol: str, order_id, price: float):
    try:
        kite.modify_order(variety=kite.VARIETY_REGULAR, order_id=str(order_id), price=price)
        print(f"[sip] {symbol}: modified order {order_id} → price={price}")
    except Exception as e:
        print(f"[sip] {symbol}: modify {order_id} error: {e}")


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
        if _main._paused or _sip_paused:
            print(f"[sip] {symbol}: pausing — main={_main._paused} sip={_sip_paused}")
            flow.status = "paused"
            _save_flow(flow)
            _sip_flows.pop(symbol, None)
            return

        # Per-stock cancel check — can be triggered from UI independently of cancel_evt
        if symbol in _sip_disabled_stocks:
            print(f"[sip] {symbol}: cancelled mid-flow (stock disabled)")
            flow.status = "cancelled"
            _save_flow(flow)
            break

        now      = _now_ist()
        _dl_cfg  = get_stockinplay_config().get("deadline_time", "15:00")
        _dl_h, _dl_m = (int(x) for x in _dl_cfg.split(":"))
        deadline = now.replace(hour=_dl_h, minute=_dl_m, second=0, microsecond=0)

        if not flow.simulate and now >= deadline:
            print(f"[sip] {symbol}: reached {_dl_cfg} deadline")
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
            max_gapup_gain_pct = float(cfg.get("max_gapup_gain_pct", 10))
            skip_ltp           = float(cfg.get("skip_ltp", 1000))

            upper_ckt_pct  = ((upper_circuit_limit - prev_day_close) / prev_day_close * 100
                              if prev_day_close else 0)
            gapup_gain_pct = ((day_open - prev_day_close) / prev_day_close * 100
                              if prev_day_close else 0)

            # Static conditions — fixed for the day, no point retrying
            if ltp > skip_ltp:
                note = f"ltp {ltp} > skip_ltp {skip_ltp}"
                print(f"[sip] {symbol}: skip (permanent) — {note}")
                flow.status = "skipped"
                _save_flow(flow, note=note)
                break
            if upper_ckt_pct < min_upper_ckt_pct:
                note = f"upper_circuit {upper_ckt_pct:.1f}% < {min_upper_ckt_pct}%"
                print(f"[sip] {symbol}: skip (permanent) — {note}")
                flow.status = "skipped"
                _save_flow(flow, note=note)
                break
            if gapup_gain_pct >= max_gapup_gain_pct:
                note = (f"gapup {gapup_gain_pct:.1f}% >= max {max_gapup_gain_pct}% "
                        f"(open={day_open} prev_close={prev_day_close})")
                print(f"[sip] {symbol}: skip (permanent) — {note}")
                flow.status = "skipped"
                _save_flow(flow, note=note)
                break

            # Dynamic conditions — can change each candle, retry on next close
            skip_reason = None
            if c_close <= day_open:
                skip_reason = f"c_close {c_close} <= day_open {day_open}"
            elif c_close <= prev_day_close:
                skip_reason = f"c_close {c_close} <= prev_close {prev_day_close}"
            elif buy_qty < min_book_qty or sell_qty < min_book_qty:
                skip_reason = f"liquidity buy={buy_qty} sell={sell_qty} need>={min_book_qty}"

            if skip_reason:
                print(f"[sip] {symbol}: condition not met — {skip_reason}, retrying next candle")
                flow.status = "waiting"
                _save_flow(flow, note=skip_reason)
                next_close = _next_candle_close(_now_ist())
                wait       = _secs_until(next_close)
                print(f"[sip] {symbol}: waiting {wait:.0f}s for next candle at {next_close.strftime('%H:%M')}")
                if flow.cancel_evt.wait(timeout=wait):
                    flow.status = "cancelled"
                    _save_flow(flow)
                    _sip_flows.pop(symbol, None)
                    return
                continue

            # ── Fibonacci 61.8% ───────────────────────────────────────────
            fib_raw   = day_low + 0.618 * (day_high - day_low)
            fib_level = round(round(fib_raw / 0.05) * 0.05, 2)

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
            _tag_webhook_order(limit_order_id, symbol, "BUY")

            # ── Wait candle-by-candle; modify LIMIT price if day range changes ──
            _order_high = day_high   # range used for the current order's fib level
            _order_low  = day_low

            while True:
                next_close = _next_candle_close(_now_ist())
                wait       = _secs_until(next_close)
                print(f"[sip] {symbol}: waiting {wait:.0f}s — fill check at {next_close.strftime('%H:%M')}")
                if flow.cancel_evt.wait(timeout=wait):
                    _cancel_order(kite, symbol, limit_order_id)
                    flow.status = "cancelled"
                    _save_flow(flow)
                    _sip_flows.pop(symbol, None)
                    return

                # Check fill
                orders       = {str(o["order_id"]): o for o in kite.orders()}
                limit_status = orders.get(str(limit_order_id), {}).get("status", "")

                if limit_status == "COMPLETE":
                    break

                # Not filled — check deadline before holding/modifying further
                if not flow.simulate and _now_ist() >= deadline:
                    print(f"[sip] {symbol}: deadline reached, cancelling LIMIT BUY")
                    _cancel_order(kite, symbol, limit_order_id)
                    flow.status = "deadline"
                    _save_flow(flow)
                    _sip_flows.pop(symbol, None)
                    return

                # Not filled — check if day range changed
                new_quote    = kite.quote(f"NSE:{symbol}")
                new_qdata    = new_quote[f"NSE:{symbol}"]
                new_day_high = new_qdata["ohlc"]["high"]
                new_day_low  = new_qdata["ohlc"]["low"]

                if new_day_high == _order_high and new_day_low == _order_low:
                    print(f"[sip] {symbol}: LIMIT BUY unfilled, range unchanged "
                          f"(high={_order_high} low={_order_low}) — holding order")
                    continue  # keep order, wait for next candle

                # Range changed — modify the existing order to the new fib level
                new_fib_raw   = new_day_low + 0.618 * (new_day_high - new_day_low)
                new_fib_level = round(round(new_fib_raw / 0.05) * 0.05, 2)
                print(f"[sip] {symbol}: range changed "
                      f"(high {_order_high}→{new_day_high} low {_order_low}→{new_day_low}), "
                      f"modifying LIMIT BUY {fib_level}→{new_fib_level}")
                _modify_order_price(kite, symbol, limit_order_id, new_fib_level)
                _order_high, _order_low = new_day_high, new_day_low
                day_high, day_low, fib_level = new_day_high, new_day_low, new_fib_level
                _save_flow(flow, fib_level=fib_level, day_high=day_high, day_low=day_low)
                continue  # keep waiting on the (modified) order

            # ── Steps 6 & 7: SL-BUY / SL-SELL placement moved to on_order_update ──
            # Main.py's _fetch_complete_candle places SL-BUY (day_high+1) and
            # SL-SELL (candle_low−1) after the fill candle closes via KiteTicker.
            # Step 8 (SL-SELL monitor) also removed — cancel_evt on disable/pause
            # cancels webhook-tagged SL orders via _cancel_pending_webhook_orders.
            print(f"[sip] {symbol}: LIMIT BUY filled — SL orders will be placed via order-update listener")
            flow.status = "filled"
            _save_flow(flow)
            break  # exit outer while loop

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

    ist_now   = _now_ist()
    is_sim    = bool(payload.get("_simulate"))   # bypass time checks when simulating

    from StockConfig import get_stockinplay_config
    cutoff_hour = int(get_stockinplay_config().get("webhook_cutoff_hour", 10))
    if not is_sim and ist_now.hour >= cutoff_hour:
        print(f"[sip] webhook ignored — after {cutoff_hour}:00")
        return {"status": "ignored", "reason": f"after_cutoff ({cutoff_hour}:00 IST)"}

    if _main._paused:
        print("[sip] webhook ignored — system paused")
        return {"status": "ignored", "reason": "system_paused"}

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

        flow = SIPFlow(symbol=symbol, alert_id=alert_id, alert_time=ist_now, simulate=is_sim)
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
    # Pull today's latest flow record per symbol from DB
    today = _now_ist().date().isoformat()
    with _db() as conn:
        rows = conn.execute("""
            SELECT symbol, status, note, alert_time, limit_order_id, sl_buy_order_id, sl_sell_order_id
            FROM sip_flows
            WHERE DATE(created_at) = ?
            ORDER BY symbol, id DESC
        """, (today,)).fetchall()

    db_by_symbol = {}
    for symbol, status, note, alert_time, limit_oid, sl_buy_oid, sl_sell_oid in rows:
        if symbol not in db_by_symbol:   # first row = latest (ORDER BY id DESC)
            db_by_symbol[symbol] = {
                "status":          status,
                "note":            note or "",
                "alert_time":      alert_time or "",
                "limit_order_id":  limit_oid,
                "sl_buy_order_id": sl_buy_oid,
                "sl_sell_order_id": sl_sell_oid,
            }

    all_symbols = (set(db_by_symbol.keys())
                   | set(_sip_flows.keys())
                   | _sip_disabled_stocks)
    stocks = []
    for sym in sorted(all_symbols):
        db  = db_by_symbol.get(sym, {})
        if sym in _sip_flows:
            run_status = _sip_flows[sym].status
        elif sym in _sip_disabled_stocks:
            run_status = "cancelled"
        else:
            run_status = db.get("status", "idle")

        stocks.append({
            "symbol":           sym,
            "status":           run_status,
            "note":             db.get("note", ""),
            "alert_time":       db.get("alert_time", ""),
            "limit_order_id":   db.get("limit_order_id"),
            "sl_buy_order_id":  db.get("sl_buy_order_id"),
            "sl_sell_order_id": db.get("sl_sell_order_id"),
            "disabled":         sym in _sip_disabled_stocks,
        })
    return {
        "paused":          _sip_paused,
        "stocks":          stocks,
        "active_flows":    [{"symbol": s, "status": f.status} for s, f in _sip_flows.items()],
        "last_webhook":    _sip_last_webhook_stocks,
        "disabled_stocks": sorted(_sip_disabled_stocks),
    }


@router.get("/api/sip/table", response_class=HTMLResponse)
def sip_table():
    status = sip_status()
    return _render_stocks_table(status["stocks"])


@router.post("/api/sip/pause")
def sip_pause():
    global _sip_paused
    _sip_paused = True

    # Cancel in-memory flows (threads still running)
    for flow in list(_sip_flows.values()):
        flow.cancel_evt.set()

    # Cancel orphaned LIMIT BUY orders for flows lost in a service restart
    # (they exist in DB with active status but no thread in _sip_flows)
    import Main as _main
    today = _now_ist().date().isoformat()
    with _db() as conn:
        orphans = conn.execute("""
            SELECT symbol, limit_order_id FROM sip_flows
            WHERE status IN ('limit_placed', 'waiting')
            AND DATE(created_at) = ?
            AND id IN (SELECT MAX(id) FROM sip_flows GROUP BY symbol)
        """, (today,)).fetchall()

    for symbol, limit_order_id in orphans:
        if symbol in _sip_flows:
            continue  # already handled by cancel_evt above
        if limit_order_id:
            try:
                kite = _main.get_kite()
                kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=str(limit_order_id))
                print(f"[sip] {symbol}: cancelled orphaned LIMIT BUY {limit_order_id} on pause")
            except Exception as e:
                print(f"[sip] {symbol}: cancel orphan error — {e}")
        with _db() as conn:
            conn.execute("""
                UPDATE sip_flows SET status='paused'
                WHERE symbol=? AND status IN ('limit_placed', 'waiting')
                AND DATE(created_at)=?
                AND id=(SELECT MAX(id) FROM sip_flows WHERE symbol=?)
            """, (symbol, today, symbol))
        print(f"[sip] {symbol}: orphaned flow marked paused")

    print("[sip] strategy paused — all flows cancelled")
    return {"paused": True}


@router.post("/api/sip/resume")
def sip_resume():
    global _sip_paused
    import Main as _main
    _sip_paused = False
    print("[sip] strategy resumed")

    if not _main._access_token:
        return {"paused": False, "restarted": []}

    today = _now_ist().date().isoformat()
    with _db() as conn:
        rows = conn.execute("""
            SELECT symbol, limit_order_id FROM sip_flows
            WHERE status IN ('paused', 'limit_placed', 'waiting')
            AND DATE(created_at) = ?
            AND id IN (SELECT MAX(id) FROM sip_flows GROUP BY symbol)
        """, (today,)).fetchall()

    restarted = []
    for symbol, limit_order_id in rows:
        if symbol in _sip_flows or symbol in _sip_disabled_stocks:
            continue
        flow = SIPFlow(symbol=symbol, alert_id=None, alert_time=_now_ist(), simulate=False)
        _save_flow(flow)
        _sip_flows[symbol] = flow
        threading.Thread(target=_run_sip_flow, args=(flow,),
                         daemon=True, name=f"sip-{symbol}").start()
        restarted.append(symbol)
        print(f"[sip] {symbol}: flow restarted on strategy resume")

    return {"paused": False, "restarted": restarted}


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
    import Main as _main

    symbol = (payload.get("symbol") or "").strip().upper()
    _sip_disabled_stocks.discard(symbol)

    started = False
    if symbol and symbol not in _sip_flows and _main._access_token:
        flow = SIPFlow(symbol=symbol, alert_id=None, alert_time=_now_ist(), simulate=False)
        _save_flow(flow)
        _sip_flows[symbol] = flow
        threading.Thread(target=_run_sip_flow, args=(flow,),
                         daemon=True, name=f"sip-{symbol}").start()
        started = True
        print(f"[sip] {symbol}: flow restarted via enable-stock")

    return {"enabled": symbol, "started": started, "disabled_stocks": sorted(_sip_disabled_stocks)}


@router.post("/api/sip/cancel-flow")
def sip_cancel_flow(payload: dict):
    symbol = (payload.get("symbol") or "").strip().upper()
    if symbol in _sip_flows:
        _sip_flows[symbol].cancel_evt.set()
        return {"cancelled": symbol}
    return {"error": f"no active flow for {symbol}"}


# ── UI ────────────────────────────────────────────────────────────────────────

def _render_stocks_table(stocks: list) -> str:
    if not stocks:
        return '<div class="empty">No stocks yet — waiting for webhook.</div>'
    def esc(s):
        return str(s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")
    rows = []
    for st in stocks:
        status_key   = "cancelled" if st["disabled"] else st["status"]
        status_label = status_key.replace("_", " ")
        alert_time   = (st.get("alert_time") or "").replace("T"," ")[11:16] or "—"
        detail = ""
        if st.get("note"):
            detail = f'<span style="color:#9ca3af;font-size:.78rem">{esc(st["note"])}</span>'
        if st.get("limit_order_id"):
            parts = [f'Limit&nbsp;<code style="font-size:.72rem;color:#93c5fd">{esc(st["limit_order_id"])}</code>']
            if st.get("sl_buy_order_id"):
                parts.append(f'SL-BUY&nbsp;<code style="font-size:.72rem;color:#86efac">{esc(st["sl_buy_order_id"])}</code>')
            if st.get("sl_sell_order_id"):
                parts.append(f'SL-SELL&nbsp;<code style="font-size:.72rem;color:#fca5a5">{esc(st["sl_sell_order_id"])}</code>')
            detail = "<br>".join(parts)
        action_btn = (
            f'<button class="action-btn restore-btn" onclick="restoreStock(\'{esc(st["symbol"])}\')">Restore Run</button>'
            if st["disabled"] else
            f'<button class="action-btn cancel-btn" onclick="cancelStockRun(\'{esc(st["symbol"])}\')">Cancel Run</button>'
        )
        detail_cell = detail or '<span style="color:#4b5563">—</span>'
        rows.append(
            f'<tr>'
            f'<td><strong>{esc(st["symbol"])}</strong></td>'
            f'<td style="color:#9ca3af;font-size:.78rem">{alert_time}</td>'
            f'<td><span class="badge s-{status_key}">{status_label}</span></td>'
            f'<td style="max-width:220px">{detail_cell}</td>'
            f'<td>{action_btn}</td>'
            f'</tr>'
        )
    return (
        '<table><thead><tr>'
        '<th>Symbol</th><th>Alert</th><th>Status</th><th>Reason / Orders</th><th>Action</th>'
        '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
    )


@router.get("/sip-control", response_class=HTMLResponse)
def sip_control_ui():
    status = sip_status()
    initial_table = _render_stocks_table(status["stocks"])
    return ("""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Strategy Control</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#0f0f1a;color:#cdd6f4;padding:28px 16px;min-height:100vh}
    .page{max-width:760px;margin:0 auto}
    h1{font-size:1.3rem;color:#fff;margin-bottom:4px}
    .sub{font-size:.82rem;color:#6b7280;margin-bottom:20px}

    /* ── Tabs ── */
    .tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid #2a2a3e;padding-bottom:0}
    .tab{padding:9px 22px;border:none;border-radius:8px 8px 0 0;font-size:.88rem;font-weight:700;cursor:pointer;background:transparent;color:#6b7280;border-bottom:2px solid transparent;transition:all .15s}
    .tab.active-sip{color:#0891b2;border-bottom:2px solid #0891b2}
    .tab.active-eb{color:#f59e0b;border-bottom:2px solid #f59e0b}
    .tab:hover{color:#cdd6f4}

    /* ── Common layout ── */
    .status-bar{background:#1e1e2e;border-radius:12px;padding:14px 20px;margin-bottom:20px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}
    .stat-item{font-size:.85rem;color:#9ca3af}
    .stat-item strong{color:#fff}
    .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:middle}
    .dot-green{background:#22c55e;animation:pulse 1.2s infinite}
    .dot-yellow{background:#f59e0b;animation:pulse 1.2s infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

    .card{background:#1e1e2e;border-radius:12px;padding:20px;margin-bottom:16px;border:1px solid #2a2a3e}
    .card-title{font-size:.75rem;font-weight:800;text-transform:uppercase;letter-spacing:.08em;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid #2a2a3e}
    .ct-sip{color:#0891b2} .ct-eb{color:#f59e0b}

    .btn{padding:9px 20px;border:none;border-radius:8px;font-size:.85rem;font-weight:700;cursor:pointer;transition:all .15s}
    .btn-pause{background:#f59e0b;color:#1a1a1a}.btn-pause:hover{background:#d97706}
    .btn-resume{background:#22c55e;color:#1a1a1a}.btn-resume:hover{background:#16a34a}
    .btn-cyan{background:#0891b2;color:#fff}.btn-cyan:hover{background:#0e7490}

    table{width:100%;border-collapse:collapse;font-size:.83rem}
    thead th{color:#6b7280;padding:7px 10px;text-align:left;font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #2a2a3e}
    tbody td{padding:9px 10px;border-bottom:1px solid #1a1a2e;vertical-align:middle}
    tbody tr:last-child td{border-bottom:none}

    .badge{padding:2px 9px;border-radius:999px;font-size:.72rem;font-weight:700;display:inline-block;margin:1px}
    .s-waiting{background:#1e3a5f;color:#93c5fd}
    .s-limit_placed{background:#14532d;color:#86efac}
    .s-filled{background:#14532d;color:#86efac}
    .s-sl_placed{background:#14532d;color:#86efac}
    .s-filled_no_sl{background:#44350a;color:#fde68a}
    .s-paused{background:#44350a;color:#fde68a}
    .s-skipped,.s-deadline,.s-idle{background:#1f2937;color:#6b7280}
    .s-cancelled{background:#450a0a;color:#fca5a5}
    .s-error{background:#450a0a;color:#fca5a5}
    .s-placed{background:#14532d;color:#86efac}
    .s-pending{background:#1e3a5f;color:#93c5fd}
    .b-open{background:#dbeafe;color:#1e40af}
    .b-tp{background:#fef9c3;color:#854d0e}
    .b-complete{background:#d1fae5;color:#065f46}
    .b-rejected{background:#fee2e2;color:#991b1b}
    .b-cancelled{background:#f3f4f6;color:#374151}
    .buy{color:#22c55e;font-weight:700} .sell{color:#ef4444;font-weight:700}

    .action-btn{border:none;border-radius:6px;padding:4px 12px;font-size:.75rem;font-weight:700;cursor:pointer;transition:all .15s}
    .cancel-btn{background:#ef444420;color:#ef4444;border:1px solid #ef444440}
    .cancel-btn:hover{background:#ef4444;color:#fff}
    .restore-btn{background:#22c55e20;color:#22c55e;border:1px solid #22c55e40}
    .restore-btn:hover{background:#22c55e;color:#1a1a1a}

    .input-row{display:flex;gap:8px;margin-top:12px}
    input[type=text]{flex:1;padding:8px 12px;border:1px solid #374151;border-radius:8px;background:#161625;color:#cdd6f4;font-size:.88rem;outline:none}
    input[type=text]:focus{border-color:#0891b2}

    .empty{color:#4b5563;font-size:.85rem;padding:10px 0}
    .pane{display:none} .pane.active{display:block}
  </style>
</head>
<body>
<div class="page">
  <h1>Strategy Control</h1>
  <p class="sub">StockInPlay &amp; EarlyBloom strategy management</p>

  <!-- Tabs -->
  <div class="tabs">
    <button class="tab active-sip" id="tab-sip" onclick="switchTab('sip')">&#9679; StockInPlay</button>
    <button class="tab" id="tab-eb"  onclick="switchTab('eb')">&#9679; EarlyBloom</button>
  </div>

  <!-- ── StockInPlay Pane ── -->
  <div class="pane active" id="pane-sip">
    <div class="status-bar">
      <div class="stat-item"><span class="dot" id="sip-dot"></span>Strategy: <strong id="sip-status">—</strong></div>
      <div class="stat-item">Active: <strong id="sip-flow-count">—</strong></div>
      <div class="stat-item">Cancelled: <strong id="sip-cancelled-count">—</strong></div>
      <div style="flex:1"></div>
      <button class="btn btn-pause"  id="sip-pauseBtn"  onclick="sipPauseResume(true)">Pause Strategy</button>
      <button class="btn btn-resume" id="sip-resumeBtn" onclick="sipPauseResume(false)" style="display:none">Resume Strategy</button>
    </div>
    <div class="card">
      <div class="card-title ct-sip">Stock Run Status</div>
      <div id="sip-stocks-wrap">__STOCKS_TABLE__</div>
      <div class="input-row">
        <input type="text" id="sip-add-input" placeholder="Symbol e.g. RELIANCE" onkeydown="if(event.key==='Enter')sipCancelStock()"/>
        <button class="btn btn-cyan" onclick="sipCancelStock()">Cancel Run</button>
      </div>
    </div>
  </div>

  <!-- ── EarlyBloom Pane ── -->
  <div class="pane" id="pane-eb">
    <div class="status-bar">
      <div class="stat-item"><span class="dot" id="eb-dot"></span>Strategy: <strong id="eb-status">—</strong></div>
      <div style="flex:1"></div>
      <button class="btn btn-pause"  id="eb-pauseBtn"  onclick="ebPauseResume(true)">Pause Strategy</button>
      <button class="btn btn-resume" id="eb-resumeBtn" onclick="ebPauseResume(false)" style="display:none">Resume Strategy</button>
    </div>
    <div class="card">
      <div class="card-title ct-eb">Today's EarlyBloom Orders</div>
      <div id="eb-orders-wrap"><div class="empty">Loading...</div></div>
    </div>
  </div>
</div>

<script>
  function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

  let activeTab = 'sip';
  function switchTab(t) {
    activeTab = t;
    document.getElementById('pane-sip').classList.toggle('active', t==='sip');
    document.getElementById('pane-eb').classList.toggle('active',  t==='eb');
    document.getElementById('tab-sip').className = 'tab' + (t==='sip' ? ' active-sip' : '');
    document.getElementById('tab-eb').className  = 'tab' + (t==='eb'  ? ' active-eb'  : '');
    if(t==='eb') refreshEB();
  }

  /* ── StockInPlay ── */
  async function refreshSIP() {
    try {
      const [s, html] = await Promise.all([
        fetch('/api/sip/status').then(r => r.json()),
        fetch('/api/sip/table').then(r => r.text()),
      ]);
      const paused = s.paused;
      document.getElementById('sip-dot').className              = 'dot ' + (paused ? 'dot-yellow' : 'dot-green');
      document.getElementById('sip-status').textContent         = paused ? 'PAUSED' : 'Running';
      document.getElementById('sip-flow-count').textContent     = (s.active_flows||[]).length;
      document.getElementById('sip-cancelled-count').textContent= (s.disabled_stocks||[]).length;
      document.getElementById('sip-pauseBtn').style.display     = paused ? 'none' : '';
      document.getElementById('sip-resumeBtn').style.display    = paused ? '' : 'none';
      document.getElementById('sip-stocks-wrap').innerHTML      = html;
    } catch(e) {
      document.getElementById('sip-stocks-wrap').innerHTML =
        '<div style="color:#ef4444;font-size:.82rem;padding:8px">Error: ' + (e.message||e) + '</div>';
    }
  }

  async function sipPauseResume(pause) {
    await fetch('/api/sip/' + (pause ? 'pause' : 'resume'), {method:'POST'});
    refreshSIP();
  }
  async function cancelStockRun(sym) {
    await fetch('/api/sip/disable-stock', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({symbol:sym})});
    refreshSIP();
  }
  async function restoreStock(sym) {
    await fetch('/api/sip/enable-stock', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({symbol:sym})});
    refreshSIP();
  }
  async function sipCancelStock() {
    const inp = document.getElementById('sip-add-input');
    const sym = inp.value.trim().toUpperCase();
    if(!sym) return;
    await cancelStockRun(sym);
    inp.value = '';
  }

  /* ── EarlyBloom ── */
  async function refreshEB() {
    try {
      const [s, html] = await Promise.all([
        fetch('/api/eb/status').then(r => r.json()),
        fetch('/api/eb/table').then(r => r.text()),
      ]);
      const paused = s.paused;
      document.getElementById('eb-dot').className           = 'dot ' + (paused ? 'dot-yellow' : 'dot-green');
      document.getElementById('eb-status').textContent      = paused ? 'PAUSED' : 'Running';
      document.getElementById('eb-pauseBtn').style.display  = paused ? 'none' : '';
      document.getElementById('eb-resumeBtn').style.display = paused ? '' : 'none';
      document.getElementById('eb-orders-wrap').innerHTML   = html;
    } catch(e) {
      document.getElementById('eb-orders-wrap').innerHTML =
        '<div style="color:#ef4444;font-size:.82rem;padding:8px">Error: ' + (e.message||e) + '</div>';
    }
  }

  async function ebPauseResume(pause) {
    await fetch('/api/eb/' + (pause ? 'pause' : 'resume'), {method:'POST'});
    refreshEB();
  }

  async function cancelEBOrder(orderId) {
    const res = await fetch('/api/eb/cancel-order', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({order_id: orderId})
    });
    const data = await res.json();
    if(data.error) alert('Cancel failed: ' + data.error);
    refreshEB();
  }

  async function cancelEBMonitor(symbol) {
    const res = await fetch('/api/eb/cancel-stock', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({symbol})
    });
    const data = await res.json();
    if(data.error) alert('Cancel failed: ' + data.error);
    refreshEB();
  }

  async function cancelEBRun(symbol, orderId) {
    const res = await fetch('/api/eb/cancel-stock-run', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({symbol, order_id: orderId})
    });
    const data = await res.json();
    if(data.error) alert('Cancel failed: ' + data.error);
    refreshEB();
  }

  async function restoreEBRun(symbol) {
    const res = await fetch('/api/eb/restore-stock', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({symbol})
    });
    const data = await res.json();
    if(data.error) alert('Restore failed: ' + data.error);
    refreshEB();
  }

  /* ── Init & polling ── */
  refreshSIP();
  setInterval(() => {
    refreshSIP();
    if(activeTab === 'eb') refreshEB();
  }, 5000);
</script>
</body>
</html>
""").replace('__STOCKS_TABLE__', initial_table)
