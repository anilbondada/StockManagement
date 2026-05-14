"""
Stock Configuration — Router
==============================
Editable auto-order settings stored in SQLite.
"""

import sqlite3
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

DB_FILE = "alerts.db"

router = APIRouter()

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULTS = {
    "skip_pct_change":  "8",     # skip if pct_change >= this
    "skip_ltp":         "800",   # skip if LTP > this
    "qty_1_500":        "100",   # qty for LTP 1–500
    "qty_500_800":      "100",   # qty for LTP 500–800
    "qty_800_1000":     "50",    # qty for LTP 800–1000
    "qty_1000_plus":    "25",    # qty for LTP > 1000
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
        # Seed defaults if table is empty
        for k, v in DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO stock_config (key, value) VALUES (?,?)", (k, v)
            )


def get_config() -> dict:
    with _db() as conn:
        rows = conn.execute("SELECT key, value FROM stock_config").fetchall()
    cfg = {**DEFAULTS, **{k: v for k, v in rows}}
    return cfg


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
    .sub{color:#6b7280;font-size:.85rem;margin-bottom:28px}

    .card{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1);padding:24px 28px;width:100%;max-width:560px;margin-bottom:20px}
    .card-title{font-size:.8rem;font-weight:800;text-transform:uppercase;letter-spacing:.08em;color:#4f46e5;margin-bottom:18px;padding-bottom:10px;border-bottom:2px solid #e0e7ff}

    .field{margin-bottom:16px}
    .field label{display:block;font-size:.8rem;font-weight:700;color:#374151;margin-bottom:5px}
    .field .hint{font-size:.72rem;color:#9ca3af;margin-top:3px}
    .input-row{display:flex;align-items:center;gap:10px}
    .input-row span{font-size:.82rem;color:#6b7280;white-space:nowrap}
    input[type=number]{width:100%;padding:9px 12px;border:1px solid #ddd;border-radius:8px;font-size:.95rem;outline:none;transition:border .2s}
    input[type=number]:focus{border-color:#4f46e5}

    .qty-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}

    .range-badge{display:inline-block;background:#e0e7ff;color:#3730a3;padding:3px 10px;border-radius:999px;font-size:.75rem;font-weight:700;margin-bottom:6px}

    button{padding:12px 28px;border:none;border-radius:8px;font-size:.95rem;font-weight:700;cursor:pointer;transition:background .2s}
    .btn-save{background:#4f46e5;color:#fff;width:100%}
    .btn-save:hover{background:#4338ca}
    .btn-save:disabled{background:#a5b4fc;cursor:not-allowed}

    .toast{margin-top:14px;padding:10px 16px;border-radius:8px;font-size:.88rem;font-weight:600;display:none;text-align:center}
    .toast.ok{background:#d1fae5;color:#065f46;display:block}
    .toast.err{background:#fee2e2;color:#991b1b;display:block}
    .spinner{display:inline-block;width:14px;height:14px;border:2px solid #e5e7eb;border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:5px}
    @keyframes spin{to{transform:rotate(360deg)}}
  </style>
</head>
<body>
  <h1>Stock Configuration</h1>
  <p class="sub">Auto-order rules and quantity settings</p>

  <!-- Skip Conditions -->
  <div class="card">
    <div class="card-title">Skip Conditions</div>

    <div class="field">
      <label>Skip if % Change ≥</label>
      <div class="input-row">
        <input type="number" id="skip_pct_change" step="0.1" min="0" placeholder="8"/>
        <span>%</span>
      </div>
      <div class="hint">Stocks with pct_change above this are skipped (e.g. 8 = skip if ≥ 8%)</div>
    </div>

    <div class="field">
      <label>Skip if LTP &gt;</label>
      <div class="input-row">
        <input type="number" id="skip_ltp" step="1" min="0" placeholder="800"/>
        <span>₹</span>
      </div>
      <div class="hint">Stocks with LTP above this are skipped</div>
    </div>
  </div>

  <!-- Buy Quantity -->
  <div class="card">
    <div class="card-title">Buy Order Quantity by LTP Range</div>

    <div class="qty-grid">
      <div class="field">
        <span class="range-badge">₹1 – ₹500</span>
        <div class="input-row">
          <input type="number" id="qty_1_500" step="1" min="1" placeholder="100"/>
          <span>qty</span>
        </div>
      </div>

      <div class="field">
        <span class="range-badge">₹500 – ₹800</span>
        <div class="input-row">
          <input type="number" id="qty_500_800" step="1" min="1" placeholder="100"/>
          <span>qty</span>
        </div>
      </div>

      <div class="field">
        <span class="range-badge">₹800 – ₹1000</span>
        <div class="input-row">
          <input type="number" id="qty_800_1000" step="1" min="1" placeholder="50"/>
          <span>qty</span>
        </div>
      </div>

      <div class="field">
        <span class="range-badge">₹1000+</span>
        <div class="input-row">
          <input type="number" id="qty_1000_plus" step="1" min="1" placeholder="25"/>
          <span>qty</span>
        </div>
      </div>
    </div>
  </div>

  <div style="width:100%;max-width:560px">
    <button class="btn-save" id="saveBtn" onclick="save()">Save Configuration</button>
    <div class="toast" id="toast"></div>
  </div>

  <script>
    const FIELDS = ['skip_pct_change','skip_ltp','qty_1_500','qty_500_800','qty_800_1000','qty_1000_plus'];

    async function load() {
      const cfg = await (await fetch('/api/stock-config')).json();
      FIELDS.forEach(f => {
        const el = document.getElementById(f);
        if (el) el.value = cfg[f] ?? '';
      });
    }

    async function save() {
      const btn   = document.getElementById('saveBtn');
      const toast = document.getElementById('toast');
      toast.className = 'toast';

      const payload = {};
      for (const f of FIELDS) {
        const v = document.getElementById(f)?.value;
        if (v === '' || v == null) {
          toast.className = 'toast err';
          toast.textContent = `${f.replace(/_/g,' ')} cannot be empty.`;
          return;
        }
        payload[f] = v;
      }

      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>Saving...';
      try {
        const res = await fetch('/api/stock-config', {
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
        btn.textContent = 'Save Configuration';
      }
    }

    load();
  </script>
</body>
</html>
"""
