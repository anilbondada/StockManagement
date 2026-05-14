"""
Control Panel — Router
======================
Login, Pause, Resume, Stop controls for the trading system.
"""

import os
import sqlite3
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

DB_FILE    = "alerts.db"
TOKEN_FILE = "token.json"

router = APIRouter()


def _db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── State (shared with Main.py via import) ────────────────────────────────────

import Main as _main


def _get_kite():
    from kiteconnect import KiteConnect
    from get_access_token import API_KEY
    token = _main._access_token
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated. Login first.")
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(token)
    return kite


def _cancel_pending_webhook_orders() -> dict:
    """Cancel all open/trigger-pending orders placed via ChartInk webhook."""
    try:
        kite = _get_kite()
    except HTTPException as e:
        return {"cancelled": 0, "errors": [e.detail]}

    with _db() as conn:
        rows = conn.execute("""
            SELECT order_id, tradingsymbol, trigger_price, quantity FROM order_updates
            WHERE is_webhook_order = 1
              AND is_complete  = 0
              AND is_rejected  = 0
              AND is_cancelled = 0
        """).fetchall()

    cancelled = []
    errors    = []
    for order_id, symbol, trigger, qty in rows:
        try:
            kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=str(order_id))
            cancelled.append({"order_id": order_id, "symbol": symbol, "trigger": trigger, "qty": qty})
            print(f"[control] Cancelled {symbol} order {order_id}")
        except Exception as e:
            errors.append({"order_id": order_id, "symbol": symbol, "error": str(e)})

    return {"cancelled": len(cancelled), "orders": cancelled, "errors": errors}


# ── API endpoints ─────────────────────────────────────────────────────────────

@router.get("/api/control/status")
def control_status():
    return {
        "paused":        _main._paused,
        "authenticated": bool(_main._access_token),
    }


def _stop_ticker():
    if _main._ticker:
        try:
            _main._ticker.close()
            print("[control] KiteTicker disconnected")
        except Exception as e:
            print(f"[control] KiteTicker close error: {e}")


def _start_ticker():
    token = _main._access_token
    if token:
        _main.start_ticker(token)
        print("[control] KiteTicker reconnected")
    else:
        print("[control] No token — KiteTicker not started")


@router.post("/api/control/pause")
def control_pause():
    _main._paused = True
    result = _cancel_pending_webhook_orders()
    _stop_ticker()
    print(f"[control] PAUSED — {result['cancelled']} orders cancelled, ticker disconnected")
    return {"paused": True, "ticker": "disconnected", **result}


@router.post("/api/control/resume")
def control_resume():
    _main._paused = False
    _start_ticker()
    print("[control] RESUMED — ticker reconnected")
    return {"paused": False}


@router.post("/api/control/stop")
def control_stop():
    _main._paused = True
    result = _cancel_pending_webhook_orders()
    _stop_ticker()
    try:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
        _main._access_token = None
        print("[control] STOPPED — token.json deleted, ticker disconnected")
    except Exception as e:
        result["token_error"] = str(e)
    return {"paused": True, "ticker": "disconnected", "token_deleted": True, **result}


# ── UI ────────────────────────────────────────────────────────────────────────

@router.get("/control", response_class=HTMLResponse)
def control_ui():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <meta name="mobile-web-app-capable" content="yes"/>
  <meta name="apple-mobile-web-app-capable" content="yes"/>
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
  <meta name="theme-color" content="#0f0f1a"/>
  <link rel="manifest" href="/manifest.json"/>
  <title>HH Control</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#0f0f1a;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:32px 16px}
    h1{color:#fff;font-size:1.4rem;margin-bottom:6px;text-align:center}
    .sub{color:#6b7280;font-size:.85rem;margin-bottom:32px;text-align:center}

    .status-bar{background:#1e1e2e;border-radius:12px;padding:16px 28px;margin-bottom:32px;display:flex;gap:28px;align-items:center;flex-wrap:wrap}
    .status-item{font-size:.85rem;color:#9ca3af}
    .status-item strong{color:#fff}
    .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle}
    .dot-green{background:#22c55e;animation:pulse 1.2s infinite}
    .dot-red{background:#ef4444}
    .dot-yellow{background:#f59e0b;animation:pulse 1.2s infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

    .buttons{display:grid;grid-template-columns:1fr 1fr;gap:16px;width:100%;max-width:480px}
    button{padding:20px;border:none;border-radius:12px;font-size:1rem;font-weight:700;cursor:pointer;transition:all .2s;display:flex;flex-direction:column;align-items:center;gap:6px}
    button:hover{transform:translateY(-2px);filter:brightness(1.1)}
    button:disabled{opacity:.4;cursor:not-allowed;transform:none}
    button .icon{font-size:1.8rem}
    button .label{font-size:.95rem}
    button .desc{font-size:.72rem;font-weight:400;opacity:.8;text-align:center}

    .btn-login  {background:#4f46e5;color:#fff}
    .btn-pause  {background:#f59e0b;color:#1a1a1a}
    .btn-resume {background:#22c55e;color:#1a1a1a}
    .btn-stop   {background:#ef4444;color:#fff}

    .result{margin-top:20px;width:100%;max-width:480px;background:#1e1e2e;border-radius:10px;padding:14px 18px;font-size:.85rem;color:#cdd6f4;display:none}
    .result.ok{border-left:4px solid #22c55e}
    .result.warn{border-left:4px solid #f59e0b}
    .result.err{border-left:4px solid #ef4444}
  </style>
</head>
<body>
  <h1>Trading Control Panel</h1>
  <p class="sub">Manage the live trading system</p>

  <div class="status-bar">
    <div class="status-item"><span class="dot" id="auth-dot"></span>Auth: <strong id="auth-status">—</strong></div>
    <div class="status-item"><span class="dot" id="sys-dot"></span>System: <strong id="sys-status">—</strong></div>
  </div>

  <div class="buttons">
    <button class="btn-login" onclick="doLogin()">
      <span class="icon">🔑</span>
      <span class="label">Login</span>
      <span class="desc">Authenticate with Kite / refresh token</span>
    </button>

    <button class="btn-pause" id="pauseBtn" onclick="doAction('pause')">
      <span class="icon">⏸</span>
      <span class="label">Pause</span>
      <span class="desc">Cancel pending orders & stop webhook processing</span>
    </button>

    <button class="btn-resume" id="resumeBtn" onclick="doAction('resume')">
      <span class="icon">▶</span>
      <span class="label">Resume</span>
      <span class="desc">Re-enable webhook & order processing</span>
    </button>

    <button class="btn-stop" onclick="doAction('stop')">
      <span class="icon">⏹</span>
      <span class="label">Stop</span>
      <span class="desc">Pause + delete token (full shutdown)</span>
    </button>
  </div>

  <div style="width:100%;max-width:480px;margin-top:12px">
    <a href="/stock-config" style="display:flex;align-items:center;justify-content:center;gap:10px;padding:14px;background:#1e1e2e;color:#cdd6f4;border-radius:10px;text-decoration:none;font-size:.9rem;font-weight:600;transition:background .2s" onmouseover="this.style.background='#2a2a3e'" onmouseout="this.style.background='#1e1e2e'">
      <span style="font-size:1.2rem">⚙️</span> Stock Config Management
    </a>
  </div>

  <div class="result" id="result"></div>

  <script>
    async function refreshStatus() {
      try {
        const s = await (await fetch('/api/control/status')).json();

        const authDot = document.getElementById('auth-dot');
        const sysDot  = document.getElementById('sys-dot');

        document.getElementById('auth-status').textContent = s.authenticated ? 'Connected' : 'Not authenticated';
        authDot.className = 'dot ' + (s.authenticated ? 'dot-green' : 'dot-red');

        document.getElementById('sys-status').textContent = s.paused ? 'PAUSED' : 'Running';
        sysDot.className  = 'dot ' + (s.paused ? 'dot-yellow' : 'dot-green');

        document.getElementById('pauseBtn').disabled  = s.paused;
        document.getElementById('resumeBtn').disabled = !s.paused;
      } catch(e) {}
    }

    function doLogin() {
      window.open('/login', '_blank');
      setTimeout(refreshStatus, 3000);
    }

    async function doAction(action) {
      const res = document.getElementById('result');
      res.style.display = 'none';
      try {
        const r    = await fetch('/api/control/' + action, { method: 'POST' });
        const data = await r.json();
        const hasErrors = data.errors?.length > 0;

        let html = '';

        if (action === 'pause' || action === 'stop') {
          html += `<div style="font-weight:700;margin-bottom:10px;font-size:.95rem">
            ${action === 'stop' ? '⏹ System Stopped' : '⏸ System Paused'}
          </div>`;
          html += `<div style="margin-bottom:6px">🔌 KiteTicker: <strong>Disconnected</strong></div>`;
          html += `<div style="margin-bottom:6px">🚫 ChartInk webhook: <strong>Disabled</strong></div>`;
          if (action === 'stop') {
            html += `<div style="margin-bottom:10px">🗑 Token: <strong>Deleted</strong></div>`;
          }

          if (data.cancelled > 0) {
            html += `<div style="margin-bottom:6px;font-weight:600">Cancelled ${data.cancelled} pending order(s):</div>`;
            html += '<table style="width:100%;border-collapse:collapse;font-size:.82rem;margin-top:4px">';
            html += '<tr style="background:rgba(255,255,255,.05)"><th style="padding:5px 8px;text-align:left">Symbol</th><th style="padding:5px 8px;text-align:right">Trigger</th><th style="padding:5px 8px;text-align:right">Qty</th><th style="padding:5px 8px;text-align:left">Order ID</th></tr>';
            data.orders.forEach(o => {
              html += `<tr style="border-top:1px solid rgba(255,255,255,.08)">
                <td style="padding:5px 8px"><strong>${o.symbol||'—'}</strong></td>
                <td style="padding:5px 8px;text-align:right">₹${o.trigger||'—'}</td>
                <td style="padding:5px 8px;text-align:right">${o.qty||'—'}</td>
                <td style="padding:5px 8px;color:#9ca3af;font-size:.75rem">${o.order_id}</td>
              </tr>`;
            });
            html += '</table>';
          } else {
            html += '<div style="color:#9ca3af">No pending webhook orders to cancel.</div>';
          }

          if (hasErrors) {
            html += `<div style="margin-top:10px;color:#fca5a5">Errors: ${data.errors.map(e=>e.symbol+': '+e.error).join(', ')}</div>`;
          }

        } else if (action === 'resume') {
          html += `<div style="font-weight:700;font-size:.95rem">▶ System Resumed</div>`;
          html += `<div style="margin-top:6px">🔌 KiteTicker: <strong>Reconnected</strong></div>`;
          html += `<div style="margin-top:4px">✅ ChartInk webhook: <strong>Active</strong></div>`;
        }

        res.className   = 'result ' + (hasErrors ? 'warn' : 'ok');
        res.innerHTML   = html;
        res.style.display = 'block';
        refreshStatus();
      } catch(e) {
        res.className   = 'result err';
        res.textContent = 'Error: ' + e.message;
        res.style.display = 'block';
      }
    }

    refreshStatus();
    setInterval(refreshStatus, 10000);
  </script>
</body>
</html>
"""
