"""
Excel Upload — Router
=====================
Provides endpoints to upload an Excel file, read its data,
and store it in a dynamically created SQLite table.
"""

import io
import json
import os
import re
import sqlite3
import time
import boto3
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, File, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from kiteconnect import KiteConnect
from get_access_token import API_KEY

DB_FILE    = "alerts.db"
TOKEN_FILE = "token.json"

router = APIRouter()

# ── Kite helpers ──────────────────────────────────────────────────────────────

_inst_cache: dict = {}
_instruments_list: list = []


def _load_token_from_file() -> Optional[str]:
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f).get("access_token")
    except Exception:
        return None


def _get_kite(fallback_token: Optional[str] = None) -> KiteConnect:
    token = _load_token_from_file() or fallback_token
    if not token:
        raise HTTPException(status_code=401, detail="No access token. Provide one or login via /login.")
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(token)
    return kite


def _instrument_token(kite: KiteConnect, symbol: str) -> int:
    global _instruments_list
    key = f"NSE:{symbol}"
    if key in _inst_cache:
        return _inst_cache[key]
    if not _instruments_list:
        _instruments_list = kite.instruments("NSE")
    for inst in _instruments_list:
        if inst["tradingsymbol"] == symbol and inst["instrument_type"] == "EQ":
            _inst_cache[key] = inst["instrument_token"]
            return inst["instrument_token"]
    raise ValueError(f"{symbol} not found on NSE")


def _parse_date(date_str: str) -> str:
    """Convert MM/DD/YY [HH:MM:SS] → YYYY-MM-DD."""
    part = str(date_str).strip().split()[0]
    for fmt in ("%m/%d/%y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(part, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return part


def _ensure_candle_cols(table_name: str):
    with _db() as conn:
        existing = {r[1] for r in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()}
        for col in ["open", "high", "low", "close", "volume", "prev_close", "pct_change", "day_high", "day_low"]:
            if col not in existing:
                conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" REAL')


def _prev_trading_day(date_str: str) -> str:
    from datetime import timedelta
    dt = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)
    while dt.weekday() >= 5:
        dt -= timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _sanitize(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_").lower()
    return name or "excel_data"


def _pandas_type_to_sql(dtype) -> str:
    if pd.api.types.is_integer_dtype(dtype):
        return "INTEGER"
    if pd.api.types.is_float_dtype(dtype):
        return "REAL"
    return "TEXT"


def _ensure_sessions_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS excel_upload_sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name   TEXT,
            table_name  TEXT,
            row_count   INTEGER,
            columns     TEXT,
            uploaded_at TEXT
        )
    """)


def create_and_insert(df: pd.DataFrame, table_name: str, file_name: str) -> dict:
    cols = [_sanitize(c) for c in df.columns]
    df.columns = cols

    col_defs = ", ".join(
        f'"{c}" {_pandas_type_to_sql(df[c].dtype)}' for c in cols
    )
    create_sql = f"""
        CREATE TABLE IF NOT EXISTS "{table_name}" (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            uploaded_at TEXT,
            {col_defs}
        )
    """

    placeholders = ", ".join(["?"] * (len(cols) + 1))
    quoted_cols  = ", ".join(f'"{c}"' for c in cols)
    insert_sql   = f'INSERT INTO "{table_name}" (uploaded_at, {quoted_cols}) VALUES ({placeholders})'

    def _safe(v):
        if pd.isna(v) if not isinstance(v, (list, dict)) else False:
            return None
        if isinstance(v, pd.Timestamp):
            return v.strftime("%m/%d/%y %H:%M:%S") if v.time().hour or v.time().minute else v.strftime("%m/%d/%y")
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return v

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for _, row in df.iterrows():
        vals = [now] + [_safe(v) for v in row]
        rows.append(vals)

    with _db() as conn:
        _ensure_sessions_table(conn)
        conn.execute(create_sql)
        conn.executemany(insert_sql, rows)
        conn.execute(
            """INSERT INTO excel_upload_sessions (file_name, table_name, row_count, columns, uploaded_at)
               VALUES (?, ?, ?, ?, ?)""",
            (file_name, table_name, len(df), ", ".join(cols), now),
        )

    return {"table": table_name, "rows": len(df), "columns": cols}


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/api/upload-excel")
async def upload_excel(file: UploadFile = File(...)):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only .xlsx or .xls files are supported.")
    try:
        content    = await file.read()
        df         = pd.read_excel(io.BytesIO(content))
        # Re-parse string columns that look like MM/DD/YY dates
        for col in df.columns:
            if df[col].dtype == object:
                try:
                    df[col] = pd.to_datetime(df[col], dayfirst=False, errors="ignore")
                except Exception:
                    pass
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read Excel: {e}")

    if df.empty:
        raise HTTPException(status_code=400, detail="Excel file is empty.")

    table_name = _sanitize(file.filename.rsplit(".", 1)[0])
    result     = create_and_insert(df, table_name, file.filename)

    # Return preview (first 5 rows)
    preview = df.head(5).fillna("").astype(str).to_dict(orient="records")
    return {**result, "preview": preview}


@router.get("/api/excel-tables")
def list_excel_tables():
    try:
        with _db() as conn:
            _ensure_sessions_table(conn)
            rows = conn.execute(
                "SELECT id, file_name, table_name, row_count, columns, uploaded_at FROM excel_upload_sessions ORDER BY id DESC"
            ).fetchall()
        return [
            {"id": r[0], "file_name": r[1], "table": r[2], "rows": r[3], "columns": r[4], "uploaded_at": r[5]}
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/excel-data/{table_name}")
def get_excel_data(table_name: str):
    safe = _sanitize(table_name)
    try:
        with _db() as conn:
            rows = conn.execute(f'SELECT * FROM "{safe}" ORDER BY id DESC').fetchall()
            cols = [d[0] for d in conn.execute(f'SELECT * FROM "{safe}" LIMIT 0').description]
        return {"table": safe, "columns": cols, "rows": [dict(zip(cols, r)) for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Table '{safe}' not found: {e}")


def _run_fetch(safe: str, kite, rows: list):
    for row_id, date_str, symbol in rows:
        try:
            trade_date = _parse_date(str(date_str))
            prev_date  = _prev_trading_day(trade_date)
            token      = _instrument_token(kite, str(symbol).strip().upper())

            candles       = kite.historical_data(token, f"{trade_date} 09:15:00", f"{trade_date} 09:30:00", "15minute")
            time.sleep(0.35)
            prev_day_data = kite.historical_data(token, f"{prev_date} 00:00:00", f"{prev_date} 23:59:59", "day")
            time.sleep(0.35)
            curr_day_data = kite.historical_data(token, f"{trade_date} 00:00:00", f"{trade_date} 23:59:59", "day")
            time.sleep(0.35)

            if candles:
                c          = candles[0]
                prev_close = prev_day_data[0]["close"] if prev_day_data else None
                pct_change = round((c["close"] - prev_close) / prev_close * 100, 2) if prev_close else None
                day_high   = curr_day_data[0]["high"] if curr_day_data else None
                day_low    = curr_day_data[0]["low"]  if curr_day_data else None
                with _db() as conn:
                    conn.execute(
                        f'UPDATE "{safe}" SET open=?, high=?, low=?, close=?, volume=?, prev_close=?, pct_change=?, day_high=?, day_low=? WHERE id=?',
                        (c["open"], c["high"], c["low"], c["close"], c["volume"], prev_close, pct_change, day_high, day_low, row_id)
                    )
        except Exception as e:
            print(f"[fetch] {symbol} ({date_str}): {e}")


@router.post("/api/fetch-candles/{table_name}")
def fetch_candles_for_table(table_name: str, background_tasks: BackgroundTasks, x_kite_token: Optional[str] = Header(default=None)):
    safe = _sanitize(table_name)
    kite = _get_kite(x_kite_token)
    _ensure_candle_cols(safe)

    with _db() as conn:
        rows = conn.execute(f'SELECT id, date, symbol FROM "{safe}" WHERE open IS NULL OR prev_close IS NULL OR day_high IS NULL').fetchall()

    if not rows:
        return {"started": False, "total": 0, "message": "All rows already have candle data."}

    background_tasks.add_task(_run_fetch, safe, kite, rows)
    est_seconds = len(rows) * 3
    return {"started": True, "total": len(rows), "est_seconds": est_seconds, "message": f"Fetching {len(rows)} rows in background..."}


@router.post("/api/export-s3/{table_name}")
def export_to_s3(table_name: str):
    safe       = _sanitize(table_name)
    bucket     = "backtest-earlybloom"
    s3_key     = f"{safe}.csv"

    try:
        with _db() as conn:
            rows = conn.execute(f'SELECT * FROM "{safe}" ORDER BY id').fetchall()
            cols = [d[0] for d in conn.execute(f'SELECT * FROM "{safe}" LIMIT 0').description]

        if not rows:
            raise HTTPException(status_code=404, detail="Table is empty.")

        df  = pd.DataFrame(rows, columns=cols).drop(columns=["id", "uploaded_at"], errors="ignore")
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        csv_bytes = buf.getvalue().encode("utf-8")

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.put_object(Bucket=bucket, Key=s3_key, Body=csv_bytes, ContentType="text/csv")

        return {"success": True, "bucket": bucket, "key": s3_key, "rows": len(df)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/view-excel/{table_name}", response_class=HTMLResponse)
def view_excel_table(table_name: str):
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{table_name}</title>

  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Segoe UI',sans-serif;background:#f0f2f5;padding:28px 16px}}
    .header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:12px}}
    h1{{color:#1a1a2e;font-size:1.3rem}}
    .meta{{font-size:.82rem;color:#6b7280;margin-top:2px}}
    .toolbar{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:16px;background:#fff;padding:12px 16px;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
    input[type=text],input[type=password]{{padding:8px 12px;border:1px solid #ddd;border-radius:8px;font-size:.85rem;outline:none}}
    input[type=text]{{width:200px}} input[type=password]{{width:260px}}
    input:focus{{border-color:#4f46e5}}
    button{{padding:8px 18px;border:none;border-radius:8px;font-size:.85rem;font-weight:700;cursor:pointer}}
    .btn-fetch{{background:#16a34a;color:#fff}} .btn-fetch:hover{{background:#15803d}} .btn-fetch:disabled{{background:#86efac;cursor:not-allowed}}
    .btn-s3{{background:#f97316;color:#fff}} .btn-s3:hover{{background:#ea6c0a}} .btn-s3:disabled{{background:#fdba74;cursor:not-allowed}}
    .btn-back{{background:#e0e7ff;color:#3730a3;text-decoration:none;padding:8px 16px;border-radius:8px;font-size:.85rem;font-weight:700}}
    .btn-back:hover{{background:#c7d2fe}}
    .status{{font-size:.82rem;padding:6px 12px;border-radius:6px;display:none}}
    .status.ok{{background:#d1fae5;color:#065f46;display:block}}
    .status.err{{background:#fee2e2;color:#991b1b;display:block}}
    .status.info{{background:#dbeafe;color:#1e40af;display:block}}
    .wrap{{overflow-x:auto;background:#fff;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.08)}}
    table{{width:100%;border-collapse:collapse;font-size:.85rem}}
    thead th{{background:#1e1e2e;color:#cdd6f4;padding:10px 14px;text-align:left;white-space:nowrap;font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;position:sticky;top:0}}
    tbody td{{padding:9px 14px;border-bottom:1px solid #f1f5f9;white-space:nowrap}}
    tbody tr:last-child td{{border-bottom:none}}
    tbody tr:hover{{background:#fafafa}}
    .empty{{text-align:center;padding:60px;color:#9ca3af}}
    .badge{{background:#e0e7ff;color:#3730a3;padding:2px 10px;border-radius:999px;font-size:.75rem;font-weight:700;margin-left:8px}}
    .spinner{{display:inline-block;width:14px;height:14px;border:2px solid #e5e7eb;border-top-color:#16a34a;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:5px}}
    @keyframes spin{{to{{transform:rotate(360deg)}}}}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>{table_name} <span class="badge" id="count"></span></h1>
      <div class="meta" id="meta">Loading...</div>
    </div>
    <a class="btn-back" href="/upload-excel">← Upload</a>
  </div>

  <div class="toolbar">
    <input type="password" id="token" placeholder="Access token (uses token.json if blank)"/>
    <button class="btn-fetch" id="fetchBtn" onclick="fetchCandles()">Fetch 9:15–9:30 Candles</button>
    <button class="btn-s3" id="s3Btn" onclick="exportS3()">Export to S3</button>
    <input type="text" id="search" placeholder="Search..." oninput="filter()"/>
    <span class="status" id="status"></span>
  </div>

  <div class="wrap"><table id="tbl"><thead id="thead"></thead><tbody id="tbody"><tr><td class="empty">Loading...</td></tr></tbody></table></div>

  <script>
    let allRows = [], allCols = [];

    const COL_ORDER = ['date','symbol','prev_close','open','high','low','close','volume','pct_change','day_high','day_low'];

    async function load() {{
      const res  = await fetch('/api/excel-data/{table_name}');
      const data = await res.json();
      allRows = data.rows;
      const available = new Set(data.columns.filter(c => c !== 'id' && c !== 'uploaded_at'));
      // Show in preferred order, then any remaining columns
      const ordered = COL_ORDER.filter(c => available.has(c));
      const rest     = data.columns.filter(c => !COL_ORDER.includes(c) && c !== 'id' && c !== 'uploaded_at');
      allCols = [...ordered, ...rest];
      document.getElementById('thead').innerHTML = '<tr>' + allCols.map(c => `<th>${{c}}</th>`).join('') + '</tr>';
      document.getElementById('count').textContent = allRows.length + ' rows';
      document.getElementById('meta').textContent  = 'Table: {table_name} · ' + allCols.length + ' columns';
      render(allRows);
    }}

    function render(rows) {{
      if (!rows.length) {{
        document.getElementById('tbody').innerHTML = '<tr><td class="empty" colspan="' + allCols.length + '">No data found.</td></tr>';
        return;
      }}
      document.getElementById('tbody').innerHTML = rows.map(r => {{
        const cells = allCols.map(c => {{
          if (c === 'pct_change' && r[c] != null) {{
            const v   = parseFloat(r[c]);
            const cls = v > 0 ? 'color:#16a34a;font-weight:700' : v < 0 ? 'color:#dc2626;font-weight:700' : '';
            const arrow = v > 0 ? '▲' : v < 0 ? '▼' : '';
            return `<td style="${{cls}}">${{arrow}} ${{v.toFixed(2)}}%</td>`;
          }}
          return `<td>${{r[c] ?? ''}}</td>`;
        }});
        return '<tr>' + cells.join('') + '</tr>';
      }}).join('');
    }}

    async function fetchCandles() {{
      const btn    = document.getElementById('fetchBtn');
      const st     = document.getElementById('status');
      const token  = document.getElementById('token').value.trim();
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>Fetching...';
      st.className = 'status info'; st.textContent = 'Fetching candles from Kite — this may take a minute...'; st.style.display='block';

      const headers = {{}};
      if (token) headers['X-Kite-Token'] = token;

      try {{
        const res  = await fetch('/api/fetch-candles/{table_name}', {{ method:'POST', headers }});
        const data = await res.json();
        if (!res.ok) {{
          st.className = 'status err'; st.textContent = 'Error: ' + data.detail;
        }} else {{
          st.className = 'status ok';
          st.textContent = `✓ Updated ${{data.updated}} rows, skipped ${{data.skipped}}${{data.errors?.length ? ' | Errors: ' + data.errors.slice(0,3).join('; ') : ''}}`;
          await load();
        }}
      }} catch(e) {{
        st.className = 'status err'; st.textContent = 'Request failed: ' + e.message;
      }} finally {{
        btn.disabled = false; btn.textContent = 'Fetch 9:15–9:30 Candles';
      }}
    }}

    async function exportS3() {{
      const btn = document.getElementById('s3Btn');
      const st  = document.getElementById('status');
      btn.disabled = true; btn.textContent = 'Exporting...';
      st.className = 'status info'; st.textContent = 'Uploading CSV to S3...'; st.style.display='block';
      try {{
        const res  = await fetch('/api/export-s3/{table_name}', {{ method:'POST' }});
        const data = await res.json();
        if (!res.ok) {{
          st.className = 'status err'; st.textContent = 'Error: ' + data.detail;
        }} else {{
          st.className = 'status ok';
          st.textContent = `✓ Uploaded ${{data.rows}} rows → s3://${{data.bucket}}/${{data.key}}`;
        }}
      }} catch(e) {{
        st.className = 'status err'; st.textContent = 'Failed: ' + e.message;
      }} finally {{
        btn.disabled = false; btn.textContent = 'Export to S3';
      }}
    }}

    function filter() {{
      const q = document.getElementById('search').value.toLowerCase();
      if (!q) {{ render(allRows); return; }}
      render(allRows.filter(r => allCols.some(c => String(r[c] ?? '').toLowerCase().includes(q))));
    }}

    load();
  </script>
</body>
</html>
"""


@router.get("/upload-excel", response_class=HTMLResponse)
def upload_excel_ui():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Excel Upload</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#f0f2f5;min-height:100vh;padding:32px 16px;display:flex;flex-direction:column;align-items:center}
    h1{color:#1a1a2e;font-size:1.4rem;margin-bottom:24px}

    .card{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1);padding:28px;width:100%;max-width:640px;margin-bottom:24px}
    .card h2{font-size:1rem;color:#374151;margin-bottom:16px}

    .drop-zone{border:2px dashed #c7d2fe;border-radius:10px;padding:36px;text-align:center;cursor:pointer;transition:all .2s;background:#f8f9ff}
    .drop-zone:hover,.drop-zone.over{border-color:#4f46e5;background:#eef2ff}
    .drop-zone p{color:#6b7280;font-size:.9rem;margin-top:8px}
    .drop-zone .icon{font-size:2.2rem}

    input[type=file]{display:none}
    .file-name{margin-top:10px;font-size:.85rem;color:#4f46e5;font-weight:600;min-height:20px}

    button{padding:11px 24px;border:none;border-radius:8px;font-size:.95rem;font-weight:700;cursor:pointer;transition:background .2s}
    .btn-upload{background:#4f46e5;color:#fff;width:100%;margin-top:16px}
    .btn-upload:hover{background:#4338ca}
    .btn-upload:disabled{background:#a5b4fc;cursor:not-allowed}

    .result{margin-top:16px;padding:14px 16px;border-radius:8px;font-size:.88rem;display:none}
    .result.ok{background:#d1fae5;color:#065f46}
    .result.err{background:#fee2e2;color:#991b1b}

    .preview-wrap{width:100%;max-width:900px;overflow-x:auto}
    table{width:100%;border-collapse:collapse;font-size:.82rem;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,.08)}
    thead th{background:#1e1e2e;color:#cdd6f4;padding:9px 13px;text-align:left;white-space:nowrap}
    tbody td{padding:8px 13px;border-bottom:1px solid #f1f5f9;white-space:nowrap}
    tbody tr:last-child td{border-bottom:none}
    tbody tr:hover{background:#fafafa}

    .history{width:100%;max-width:640px}
    .history h2{font-size:1rem;color:#374151;margin-bottom:10px}
    .hist-row{background:#fff;border-radius:8px;padding:12px 16px;margin-bottom:8px;box-shadow:0 1px 4px rgba(0,0,0,.07);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
    .hist-name{font-weight:700;color:#1a1a2e;font-size:.9rem}
    .hist-meta{font-size:.78rem;color:#6b7280}
    .hist-badge{background:#e0e7ff;color:#3730a3;padding:2px 10px;border-radius:999px;font-size:.75rem;font-weight:700}
    .spinner{display:inline-block;width:16px;height:16px;border:2px solid #e5e7eb;border-top-color:#4f46e5;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:6px}
    @keyframes spin{to{transform:rotate(360deg)}}
  </style>
</head>
<body>
<h1>Excel Upload</h1>

<div class="card">
  <h2>Upload Excel File</h2>
  <div class="drop-zone" id="drop" onclick="document.getElementById('fileInput').click()"
       ondragover="ev(event,'over')" ondragleave="ev(event,'')" ondrop="drop(event)">
    <div class="icon">📊</div>
    <strong>Click to browse or drag & drop</strong>
    <p>.xlsx or .xls files only</p>
  </div>
  <input type="file" id="fileInput" accept=".xlsx,.xls" onchange="fileChosen(this)"/>
  <div class="file-name" id="fname"></div>
  <button class="btn-upload" id="uploadBtn" onclick="upload()" disabled>Upload & Store</button>
  <div class="result" id="result"></div>
</div>

<div class="preview-wrap" id="previewWrap" style="display:none">
  <div class="card" style="max-width:100%;overflow-x:auto">
    <h2 id="previewTitle">Preview (first 5 rows)</h2>
    <div id="previewTable"></div>
  </div>
</div>

<div class="history" id="history"></div>

<script>
  let selectedFile = null;

  function ev(e, cls) { e.preventDefault(); document.getElementById('drop').className = 'drop-zone ' + cls; }
  function drop(e) { e.preventDefault(); document.getElementById('drop').className = 'drop-zone'; setFile(e.dataTransfer.files[0]); }
  function fileChosen(inp) { if (inp.files[0]) setFile(inp.files[0]); }
  function setFile(f) {
    selectedFile = f;
    document.getElementById('fname').textContent = f.name;
    document.getElementById('uploadBtn').disabled = false;
    document.getElementById('result').style.display = 'none';
  }

  async function upload() {
    if (!selectedFile) return;
    const btn = document.getElementById('uploadBtn');
    const res = document.getElementById('result');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>Uploading...';
    res.style.display = 'none';
    document.getElementById('previewWrap').style.display = 'none';

    const form = new FormData();
    form.append('file', selectedFile);
    try {
      const r    = await fetch('/api/upload-excel', { method: 'POST', body: form });
      const data = await r.json();
      if (!r.ok) { res.className='result err'; res.textContent='Error: '+data.detail; res.style.display='block'; return; }

      res.className = 'result ok';
      res.textContent = `✓ Stored ${data.rows} rows into table "${data.table}" (${data.columns.length} columns)`;
      res.style.display = 'block';

      renderPreview(data.preview, data.columns, data.table);
      loadHistory();
    } catch(e) {
      res.className = 'result err';
      res.textContent = 'Request failed: ' + e.message;
      res.style.display = 'block';
    } finally {
      btn.disabled = false;
      btn.textContent = 'Upload & Store';
    }
  }

  function renderPreview(rows, cols, table) {
    if (!rows || !rows.length) return;
    document.getElementById('previewTitle').textContent = `Preview — table: ${table} (first 5 rows)`;
    let h = '<table><thead><tr>' + cols.map(c=>`<th>${c}</th>`).join('') + '</tr></thead><tbody>';
    h += rows.map(r => '<tr>' + cols.map(c=>`<td>${r[c] ?? ''}</td>`).join('') + '</tr>').join('');
    h += '</tbody></table>';
    document.getElementById('previewTable').innerHTML = h;
    document.getElementById('previewWrap').style.display = 'block';
  }

  async function loadHistory() {
    const r    = await fetch('/api/excel-tables');
    const data = await r.json();
    if (!data.length) { document.getElementById('history').innerHTML=''; return; }
    let h = '<h2>Uploaded Files</h2>';
    data.forEach(d => {
      h += `<div class="hist-row">
        <div><div class="hist-name">${d.file_name}</div>
        <div class="hist-meta">Table: ${d.table} &nbsp;·&nbsp; ${d.rows} rows &nbsp;·&nbsp; ${new Date(d.uploaded_at).toLocaleString()}</div></div>
        <div style="display:flex;gap:8px;align-items:center">
          <span class="hist-badge">${d.columns.split(',').length} cols</span>
          <a href="/view-excel/${d.table}" style="padding:4px 12px;background:#4f46e5;color:#fff;border-radius:6px;font-size:.75rem;font-weight:700;text-decoration:none">View</a>
        </div>
      </div>`;
    });
    document.getElementById('history').innerHTML = h;
  }

  loadHistory();
</script>
</body>
</html>
"""
