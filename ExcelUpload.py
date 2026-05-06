"""
Excel Upload — Router
=====================
Provides endpoints to upload an Excel file, read its data,
and store it in a dynamically created SQLite table.
"""

import io
import re
import sqlite3
from datetime import datetime, timezone

import pandas as pd
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

DB_FILE = "alerts.db"

router = APIRouter()


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

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for _, row in df.iterrows():
        vals = [now] + [
            None if pd.isna(v) else (int(v) if isinstance(v, float) and v.is_integer() else v)
            for v in row
        ]
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
        <span class="hist-badge">${d.columns.split(',').length} cols</span>
      </div>`;
    });
    document.getElementById('history').innerHTML = h;
  }

  loadHistory();
</script>
</body>
</html>
"""
