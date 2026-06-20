"""
Stock Configuration — Router
==============================
Editable auto-order settings stored in SQLite.
Two independent profiles: EarlyBloom and StockInPlay.
"""

import sqlite3
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

DB_FILE = "alerts.db"

router = APIRouter()

# ── EarlyBloom defaults ───────────────────────────────────────────────────────

DEFAULTS = {
    "skip_pct_change":  "8",
    "skip_ltp":         "800",
    "min_book_qty":     "100000",
    "qty_1_500":        "100",
    "qty_500_800":      "100",
    "qty_800_1000":     "50",
    "qty_1000_plus":    "25",
    "eb_deadline_time": "15:00",  # stop monitoring liquidity after this IST time (HH:MM)
}

# ── StockInPlay defaults ──────────────────────────────────────────────────────

SIP_DEFAULTS = {
    "skip_pct_change":       "8",
    "skip_ltp":              "800",
    "min_book_qty":          "100000",
    "qty_1_500":             "100",
    "qty_500_800":           "100",
    "qty_800_1000":          "50",
    "qty_1000_plus":         "25",
    "min_upper_circuit_pct": "20",   # skip if upper circuit % <= this
    "max_gapup_gain_pct":   "10",   # skip if (day_open - prev_close) / prev_close * 100 > this
    "deadline_time":         "15:00", # cancel unfilled orders after this IST time (HH:MM)
    "webhook_cutoff_hour":   "10",   # ignore new SIP webhooks at or after this hour (IST, 24h)
}


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_config_table():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        for k, v in DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO stock_config (key, value) VALUES (?,?)", (k, v)
            )


def init_stockinplay_config_table():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stockinplay_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        for k, v in SIP_DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO stockinplay_config (key, value) VALUES (?,?)", (k, v)
            )


# ── EarlyBloom helpers ────────────────────────────────────────────────────────

def get_config() -> dict:
    with _db() as conn:
        rows = conn.execute("SELECT key, value FROM stock_config").fetchall()
    return {**DEFAULTS, **{k: v for k, v in rows}}


def qty_for_ltp(ltp: float, cfg: dict = None) -> int:
    if cfg is None:
        cfg = get_config()
    if ltp <= 500:
        return int(cfg.get("qty_1_500", 100))
    elif ltp <= 800:
        return int(cfg.get("qty_500_800", 100))
    elif ltp <= 1000:
        return int(cfg.get("qty_800_1000", 50))
    else:
        return int(cfg.get("qty_1000_plus", 25))


# ── StockInPlay helpers ───────────────────────────────────────────────────────

def get_stockinplay_config() -> dict:
    with _db() as conn:
        rows = conn.execute("SELECT key, value FROM stockinplay_config").fetchall()
    return {**SIP_DEFAULTS, **{k: v for k, v in rows}}


def qty_for_ltp_sip(ltp: float, cfg: dict = None) -> int:
    if cfg is None:
        cfg = get_stockinplay_config()
    if ltp <= 500:
        return int(cfg.get("qty_1_500", 100))
    elif ltp <= 800:
        return int(cfg.get("qty_500_800", 100))
    elif ltp <= 1000:
        return int(cfg.get("qty_800_1000", 50))
    else:
        return int(cfg.get("qty_1000_plus", 25))


# ── API endpoints ─────────────────────────────────────────────────────────────

@router.get("/api/stock-config")
def get_stock_config():
    return get_config()


@router.post("/api/stock-config")
def update_stock_config(payload: dict):
    allowed = set(DEFAULTS.keys())
    with _db() as conn:
        for k, v in payload.items():
            if k in allowed:
                conn.execute(
                    "INSERT OR REPLACE INTO stock_config (key, value) VALUES (?,?)",
                    (k, str(v))
                )
    return get_config()


@router.get("/api/stockinplay-config")
def get_stockinplay_config_api():
    return get_stockinplay_config()


@router.post("/api/stockinplay-config")
def update_stockinplay_config(payload: dict):
    allowed = set(SIP_DEFAULTS.keys())
    with _db() as conn:
        for k, v in payload.items():
            if k in allowed:
                conn.execute(
                    "INSERT OR REPLACE INTO stockinplay_config (key, value) VALUES (?,?)",
                    (k, str(v))
                )
    return get_stockinplay_config()


# ── UI ────────────────────────────────────────────────────────────────────────

@router.get("/stock-config", response_class=HTMLResponse)
def stock_config_ui():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Stock Configuration</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#f0f2f5;min-height:100vh;padding:32px 16px;display:flex;flex-direction:column;align-items:center}
    h1{color:#1a1a2e;font-size:1.4rem;margin-bottom:6px}
    .sub{color:#6b7280;font-size:.85rem;margin-bottom:20px}

    /* Tabs */
    .tabs{display:flex;gap:0;margin-bottom:24px;background:#e5e7eb;border-radius:10px;padding:4px;width:100%;max-width:560px}
    .tab{flex:1;padding:9px 0;border:none;border-radius:7px;font-size:.88rem;font-weight:700;cursor:pointer;background:transparent;color:#6b7280;transition:all .2s}
    .tab.active{background:#fff;color:#4f46e5;box-shadow:0 1px 4px rgba(0,0,0,.12)}

    /* Cards */
    .panel{display:none;width:100%;max-width:560px}
    .panel.active{display:block}
    .card{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1);padding:24px 28px;margin-bottom:20px}
    .card-title{font-size:.8rem;font-weight:800;text-transform:uppercase;letter-spacing:.08em;margin-bottom:18px;padding-bottom:10px;border-bottom:2px solid}
    .eb .card-title{color:#4f46e5;border-color:#e0e7ff}
    .sip .card-title{color:#0891b2;border-color:#cffafe}

    .field{margin-bottom:16px}
    .field label{display:block;font-size:.8rem;font-weight:700;color:#374151;margin-bottom:5px}
    .field .hint{font-size:.72rem;color:#9ca3af;margin-top:3px}
    .input-row{display:flex;align-items:center;gap:10px}
    .input-row span{font-size:.82rem;color:#6b7280;white-space:nowrap}
    input[type=number]{width:100%;padding:9px 12px;border:1px solid #ddd;border-radius:8px;font-size:.95rem;outline:none;transition:border .2s}
    .eb input[type=number]:focus{border-color:#4f46e5}
    .sip input[type=number]:focus{border-color:#0891b2}

    .qty-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    .range-badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:.75rem;font-weight:700;margin-bottom:6px}
    .eb .range-badge{background:#e0e7ff;color:#3730a3}
    .sip .range-badge{background:#cffafe;color:#0e7490}

    button{padding:12px 28px;border:none;border-radius:8px;font-size:.95rem;font-weight:700;cursor:pointer;transition:background .2s}
    .eb .btn-save{background:#4f46e5;color:#fff;width:100%}
    .eb .btn-save:hover{background:#4338ca}
    .eb .btn-save:disabled{background:#a5b4fc;cursor:not-allowed}
    .sip .btn-save{background:#0891b2;color:#fff;width:100%}
    .sip .btn-save:hover{background:#0e7490}
    .sip .btn-save:disabled{background:#67e8f9;cursor:not-allowed}

    .toast{margin-top:14px;padding:10px 16px;border-radius:8px;font-size:.88rem;font-weight:600;display:none;text-align:center}
    .toast.ok{background:#d1fae5;color:#065f46;display:block}
    .toast.err{background:#fee2e2;color:#991b1b;display:block}
    .spinner{display:inline-block;width:14px;height:14px;border:2px solid #e5e7eb;border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:5px}
    @keyframes spin{to{transform:rotate(360deg)}}
  </style>
</head>
<body>
  <h1>Stock Configuration</h1>
  <p class="sub">Auto-order rules and quantity settings per strategy</p>

  <div class="tabs">
    <button class="tab active" onclick="switchTab('eb')">EarlyBloom</button>
    <button class="tab"        onclick="switchTab('sip')">StockInPlay</button>
  </div>

  <!-- EarlyBloom panel -->
  <div class="panel eb active" id="panel-eb">
    <div class="card">
      <div class="card-title">Skip Conditions</div>
      <div class="field">
        <label>Skip if % Change ≥</label>
        <div class="input-row">
          <input type="number" id="eb_skip_pct_change" step="0.1" min="0" placeholder="8"/>
          <span>%</span>
        </div>
        <div class="hint">Stocks with pct_change above this are skipped (e.g. 8 = skip if ≥ 8%)</div>
      </div>
      <div class="field">
        <label>Skip if LTP &gt;</label>
        <div class="input-row">
          <input type="number" id="eb_skip_ltp" step="1" min="0" placeholder="800"/>
          <span>₹</span>
        </div>
        <div class="hint">Stocks with LTP above this are skipped</div>
      </div>
      <div class="field">
        <label>Min Order Book Qty (Buy &amp; Sell)</label>
        <div class="input-row">
          <input type="number" id="eb_min_book_qty" step="1000" min="0" placeholder="100000"/>
          <span>qty</span>
        </div>
        <div class="hint">Skip if total pending buy qty OR sell qty is below this (liquidity check)</div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Buy Order Quantity by LTP Range</div>
      <div class="qty-grid">
        <div class="field">
          <span class="range-badge">₹1 – ₹500</span>
          <div class="input-row">
            <input type="number" id="eb_qty_1_500" step="1" min="1" placeholder="100"/>
            <span>qty</span>
          </div>
        </div>
        <div class="field">
          <span class="range-badge">₹500 – ₹800</span>
          <div class="input-row">
            <input type="number" id="eb_qty_500_800" step="1" min="1" placeholder="100"/>
            <span>qty</span>
          </div>
        </div>
        <div class="field">
          <span class="range-badge">₹800 – ₹1000</span>
          <div class="input-row">
            <input type="number" id="eb_qty_800_1000" step="1" min="1" placeholder="50"/>
            <span>qty</span>
          </div>
        </div>
        <div class="field">
          <span class="range-badge">₹1000+</span>
          <div class="input-row">
            <input type="number" id="eb_qty_1000_plus" step="1" min="1" placeholder="25"/>
            <span>qty</span>
          </div>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="section-title">Flow Control</div>
        <label>Flow Deadline (IST)</label>
        <div class="input-row">
          <input type="text" id="eb_eb_deadline_time" placeholder="15:00" style="max-width:90px"/>
        </div>
        <div class="hint">Stop monitoring liquidity and skip stock after this time (HH:MM, 24h IST)</div>
    </div>
    <button class="btn-save" id="eb_saveBtn" onclick="save('eb')">Save EarlyBloom Configuration</button>
    <div class="toast" id="eb_toast"></div>
  </div>

  <!-- StockInPlay panel -->
  <div class="panel sip" id="panel-sip">
    <div class="card">
      <div class="card-title">Skip Conditions</div>
      <div class="field">
        <label>Skip if % Change ≥</label>
        <div class="input-row">
          <input type="number" id="sip_skip_pct_change" step="0.1" min="0" placeholder="8"/>
          <span>%</span>
        </div>
        <div class="hint">Stocks with pct_change above this are skipped (e.g. 8 = skip if ≥ 8%)</div>
      </div>
      <div class="field">
        <label>Skip if LTP &gt;</label>
        <div class="input-row">
          <input type="number" id="sip_skip_ltp" step="1" min="0" placeholder="800"/>
          <span>₹</span>
        </div>
        <div class="hint">Stocks with LTP above this are skipped</div>
      </div>
      <div class="field">
        <label>Min Order Book Qty (Buy &amp; Sell)</label>
        <div class="input-row">
          <input type="number" id="sip_min_book_qty" step="1000" min="0" placeholder="100000"/>
          <span>qty</span>
        </div>
        <div class="hint">Skip if total pending buy qty OR sell qty is below this (liquidity check)</div>
      </div>
      <div class="field">
        <label>Min Upper Circuit %</label>
        <div class="input-row">
          <input type="number" id="sip_min_upper_circuit_pct" step="1" min="0" placeholder="20"/>
          <span>%</span>
        </div>
        <div class="hint">Skip if the stock's upper circuit limit is ≤ this value (e.g. 20 = only place order if upper circuit &gt; 20%)</div>
      </div>
      <div class="field">
        <label>Max Gap-Up Gain from Prev Close</label>
        <div class="input-row">
          <input type="number" id="sip_max_gapup_gain_pct" step="0.1" min="0" placeholder="10"/>
          <span>%</span>
        </div>
        <div class="hint">Skip if today's open − prev day close &gt; this % of prev close. Avoids stocks that gap-up too much at open.</div>
      </div>
      <div class="field">
        <label>Flow Deadline (IST)</label>
        <div class="input-row">
          <input type="text" id="sip_deadline_time" placeholder="15:00" style="max-width:90px"/>
        </div>
        <div class="hint">Cancel unfilled limit orders and stop recalibrating after this time (HH:MM, 24h IST)</div>
      </div>
      <div class="field">
        <label>Webhook Cutoff Hour (IST)</label>
        <div class="input-row">
          <input type="number" id="sip_webhook_cutoff_hour" step="1" min="0" max="23" placeholder="10" style="max-width:90px"/>
          <span>h</span>
        </div>
        <div class="hint">Ignore new SIP webhooks received at or after this hour (IST, 24h). Default: 10 = ignore from 10:00 AM onwards.</div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Buy Order Quantity by LTP Range</div>
      <div class="qty-grid">
        <div class="field">
          <span class="range-badge">₹1 – ₹500</span>
          <div class="input-row">
            <input type="number" id="sip_qty_1_500" step="1" min="1" placeholder="100"/>
            <span>qty</span>
          </div>
        </div>
        <div class="field">
          <span class="range-badge">₹500 – ₹800</span>
          <div class="input-row">
            <input type="number" id="sip_qty_500_800" step="1" min="1" placeholder="100"/>
            <span>qty</span>
          </div>
        </div>
        <div class="field">
          <span class="range-badge">₹800 – ₹1000</span>
          <div class="input-row">
            <input type="number" id="sip_qty_800_1000" step="1" min="1" placeholder="50"/>
            <span>qty</span>
          </div>
        </div>
        <div class="field">
          <span class="range-badge">₹1000+</span>
          <div class="input-row">
            <input type="number" id="sip_qty_1000_plus" step="1" min="1" placeholder="25"/>
            <span>qty</span>
          </div>
        </div>
      </div>
    </div>
    <button class="btn-save" id="sip_saveBtn" onclick="save('sip')">Save StockInPlay Configuration</button>
    <div class="toast" id="sip_toast"></div>
  </div>

  <script>
    const FIELDS = {
      eb:  ['skip_pct_change','skip_ltp','min_book_qty','qty_1_500','qty_500_800','qty_800_1000','qty_1000_plus','eb_deadline_time'],
      sip: ['skip_pct_change','skip_ltp','min_book_qty','qty_1_500','qty_500_800','qty_800_1000','qty_1000_plus',
            'min_upper_circuit_pct','max_gapup_gain_pct','deadline_time','webhook_cutoff_hour']
    };
    const API = { eb: '/api/stock-config', sip: '/api/stockinplay-config' };

    function switchTab(profile) {
      document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['eb','sip'][i] === profile));
      document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
      document.getElementById('panel-'+profile).classList.add('active');
    }

    async function load(profile) {
      try {
        const cfg = await (await fetch(API[profile])).json();
        FIELDS[profile].forEach(f => {
          const el = document.getElementById(profile+'_'+f);
          if (el) el.value = cfg[f] ?? '';
        });
      } catch(e) { console.error('load failed', e); }
    }

    async function save(profile) {
      const btn   = document.getElementById(profile+'_saveBtn');
      const toast = document.getElementById(profile+'_toast');
      toast.className = 'toast';

      const payload = {};
      for (const f of FIELDS[profile]) {
        const v = document.getElementById(profile+'_'+f)?.value;
        if (v === '' || v == null) {
          toast.className = 'toast err';
          toast.textContent = f.replace(/_/g,' ') + ' cannot be empty.';
          return;
        }
        payload[f] = v;
      }

      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>Saving...';
      try {
        const res  = await fetch(API[profile], {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Save failed');
        toast.className = 'toast ok';
        toast.textContent = '✓ Configuration saved successfully';
      } catch(e) {
        toast.className = 'toast err';
        toast.textContent = 'Error: ' + e.message;
      } finally {
        btn.disabled = false;
        btn.textContent = 'Save ' + (profile === 'eb' ? 'EarlyBloom' : 'StockInPlay') + ' Configuration';
      }
    }

    load('eb');
    load('sip');
  </script>
</body>
</html>
"""
