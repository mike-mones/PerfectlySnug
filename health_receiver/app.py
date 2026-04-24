"""
Health Auto Export → PostgreSQL Receiver
=========================================
Accepts webhook POSTs from the iOS "Health Auto Export" app and stores
all metrics in PostgreSQL (co-located on the same host).

Endpoints:
  POST /api/health  — receives the full Health Auto Export JSON payload
  GET  /api/health/status — health check
  GET  /api/health/recent — last 24h of ingested metrics

Run: uvicorn app:app --host 0.0.0.0 --port 8080
"""

import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone

import json

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("health_receiver")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "sleepdata")
DB_USER = os.environ.get("DB_USER", "sleepsync")
DB_PASS = os.environ.get("DB_PASS", "sleepsync_local")

# Optional: Home Assistant API for entity updates (fallback if automation misses)
HA_URL = os.environ.get("HA_URL", "http://192.168.0.106:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

app = FastAPI(title="Health Receiver", version="1.0")

# ── Safety Limits ────────────────────────────────────────────────────
MAX_BODY_BYTES = 50 * 1024 * 1024       # 50 MB max request body
MAX_RECORDS_PER_METRIC = 5000           # Cap per metric (daily data = ~365/yr)
MAX_SLEEP_RECORDS = 10000               # Granular sleep segments cap
WARN_RECORDS = 1000                     # Log a warning above this


def get_db():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS,
    )


def parse_date(date_str: str) -> datetime:
    """Parse Health Auto Export date format: '2026-03-04 00:24:45 -0500'."""
    for fmt in (
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {date_str}")


def parse_date_only(date_str: str):
    """Extract just the date portion."""
    return parse_date(date_str).date()


# ── Sleep Analysis ───────────────────────────────────────────────────

# Map Health Auto Export stage values to simple stage names
STAGE_MAP = {
    # Granular format values (HKCategoryValueSleepAnalysis...)
    "asleepdeep": "deep",
    "asleeprem": "rem",
    "asleepcore": "core",
    "awake": "awake",
    "inbed": "inBed",
    "asleep": "asleep",
    "asleepunspecified": "asleep",
    # Some exports use shorter names
    "deep": "deep",
    "rem": "rem",
    "core": "core",
    "inBed": "inBed",
}

SLEEP_STAGE_FIELDS = ["deep", "rem", "core", "awake", "inBed", "asleep"]


def _normalize_stage(raw_value: str) -> str:
    """Normalize sleep stage value from Health Auto Export."""
    # Strip common prefixes
    v = raw_value.strip()
    for prefix in ("HKCategoryValueSleepAnalysis", "SleepAnalysis"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    return STAGE_MAP.get(v.lower(), v.lower())


def _is_granular(record: dict) -> bool:
    """Detect if this is a granular per-interval record vs a nightly summary."""
    return "value" in record and ("start" in record or "startDate" in record)


def _store_sleep_granular(conn, records: list):
    """Store granular per-interval sleep records.

    Each record is one sleep interval:
      {"value": "AsleepDeep", "start": "...", "end": "...", "qty": 0.5, "date": "..."}
    """
    # Group by night_date to build nightly summaries
    from collections import defaultdict
    nights = defaultdict(lambda: {
        "segments": [],
        "stage_mins": defaultdict(float),
        "earliest_start": None,
        "latest_end": None,
        "source": None,
    })

    for rec in records:
        start_str = rec.get("start") or rec.get("startDate")
        end_str = rec.get("end") or rec.get("endDate")
        if not start_str or not end_str:
            continue

        try:
            start_ts = parse_date(start_str)
            end_ts = parse_date(end_str)
        except ValueError:
            continue

        stage = _normalize_stage(rec.get("value", ""))
        duration_min = (end_ts - start_ts).total_seconds() / 60.0
        source = rec.get("source", "apple_health")

        # Assign to night: use date field, or if start is before 6pm use previous day
        try:
            night_date = parse_date_only(rec["date"])
        except (KeyError, ValueError):
            # Fallback: if sleep started before noon, it belongs to previous day's night
            if start_ts.hour < 12:
                night_date = (start_ts - timedelta(days=1)).date()
            else:
                night_date = start_ts.date()

        night = nights[night_date]
        night["source"] = source
        if night["earliest_start"] is None or start_ts < night["earliest_start"]:
            night["earliest_start"] = start_ts
        if night["latest_end"] is None or end_ts > night["latest_end"]:
            night["latest_end"] = end_ts
        night["stage_mins"][stage] += duration_min
        night["segments"].append((night_date, start_ts, end_ts, stage, duration_min, source))

    # Insert segments
    with conn.cursor() as cur:
        for night_date, night in nights.items():
            for seg in night["segments"]:
                cur.execute("""
                    INSERT INTO sleep_segments
                        (night_date, start_ts, end_ts, stage, duration_min, source)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (night_date, stage, start_ts, COALESCE(source, ''))
                    DO UPDATE SET
                        end_ts = EXCLUDED.end_ts,
                        duration_min = EXCLUDED.duration_min
                """, seg)

            # Build nightly summary from aggregated segments
            sm = night["stage_mins"]
            deep = sm.get("deep", 0)
            rem = sm.get("rem", 0)
            core = sm.get("core", 0)
            awake = sm.get("awake", 0)
            asleep = sm.get("asleep", 0)
            total_sleep = deep + rem + core + asleep
            duration_hrs = ((night["latest_end"] - night["earliest_start"]).total_seconds() / 3600
                            if night["earliest_start"] and night["latest_end"] else None)

            cur.execute("""
                INSERT INTO nightly_summary
                    (night_date, bedtime_ts, wake_ts, duration_hours,
                     total_sleep_min, deep_sleep_min, rem_sleep_min,
                     core_sleep_min, awake_min)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (night_date) DO UPDATE SET
                    bedtime_ts = EXCLUDED.bedtime_ts,
                    wake_ts = EXCLUDED.wake_ts,
                    duration_hours = EXCLUDED.duration_hours,
                    total_sleep_min = EXCLUDED.total_sleep_min,
                    deep_sleep_min = EXCLUDED.deep_sleep_min,
                    rem_sleep_min = EXCLUDED.rem_sleep_min,
                    core_sleep_min = EXCLUDED.core_sleep_min,
                    awake_min = EXCLUDED.awake_min
            """, (
                night_date, night["earliest_start"], night["latest_end"],
                duration_hrs, total_sleep, deep, rem, core, awake,
            ))

    conn.commit()
    log.info("Stored %d granular sleep segments across %d nights",
             sum(len(n["segments"]) for n in nights.values()), len(nights))


def _store_sleep_summary(conn, record: dict):
    """Store a summary-format sleep_analysis record (from manual file export)."""
    night_date = parse_date_only(record["date"])
    sleep_start = record.get("sleepStart") or record.get("inBedStart")
    sleep_end = record.get("sleepEnd") or record.get("inBedEnd")
    source = record.get("source", "apple_health")

    start_ts = parse_date(sleep_start) if sleep_start else None
    end_ts = parse_date(sleep_end) if sleep_end else None

    total_sleep_hrs = record.get("totalSleep", 0)
    deep_hrs = record.get("deep", 0)
    rem_hrs = record.get("rem", 0)
    core_hrs = record.get("core", 0)
    awake_hrs = record.get("awake", 0)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO nightly_summary
                (night_date, bedtime_ts, wake_ts, duration_hours,
                 total_sleep_min, deep_sleep_min, rem_sleep_min,
                 core_sleep_min, awake_min)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (night_date) DO UPDATE SET
                bedtime_ts = COALESCE(EXCLUDED.bedtime_ts, nightly_summary.bedtime_ts),
                wake_ts = COALESCE(EXCLUDED.wake_ts, nightly_summary.wake_ts),
                duration_hours = COALESCE(EXCLUDED.duration_hours, nightly_summary.duration_hours),
                total_sleep_min = COALESCE(EXCLUDED.total_sleep_min, nightly_summary.total_sleep_min),
                deep_sleep_min = COALESCE(EXCLUDED.deep_sleep_min, nightly_summary.deep_sleep_min),
                rem_sleep_min = COALESCE(EXCLUDED.rem_sleep_min, nightly_summary.rem_sleep_min),
                core_sleep_min = COALESCE(EXCLUDED.core_sleep_min, nightly_summary.core_sleep_min),
                awake_min = COALESCE(EXCLUDED.awake_min, nightly_summary.awake_min)
        """, (
            night_date, start_ts, end_ts,
            total_sleep_hrs + awake_hrs if total_sleep_hrs else None,
            total_sleep_hrs * 60 if total_sleep_hrs else None,
            deep_hrs * 60, rem_hrs * 60, core_hrs * 60, awake_hrs * 60,
        ))

        for stage in SLEEP_STAGE_FIELDS:
            hrs = record.get(stage, 0)
            if hrs and hrs > 0 and start_ts:
                cur.execute("""
                    INSERT INTO sleep_segments
                        (night_date, start_ts, end_ts, stage, duration_min, source)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (night_date, stage, start_ts, COALESCE(source, ''))
                    DO UPDATE SET
                        end_ts = EXCLUDED.end_ts,
                        duration_min = EXCLUDED.duration_min
                """, (
                    night_date, start_ts, end_ts or start_ts,
                    stage, hrs * 60, source,
                ))

    conn.commit()
    log.info("Stored sleep summary for %s (%.1fh total)", night_date, total_sleep_hrs)


# ── Real-time Sleep Stage (from SleepSync) ───────────────────────────

def _night_date_for(ts: datetime):
    """Assign a timestamp to its sleep night (before noon → previous day)."""
    local = ts.astimezone()
    if local.hour < 12:
        return (local - timedelta(days=1)).date()
    return local.date()


def _store_sleep_stage_realtime(conn, records: list, source: str = "sleepsync"):
    """Store a real-time sleep stage sample from SleepSync.

    Maintains proper segments: extends the current segment if the stage
    hasn't changed, otherwise closes it and opens a new one.
    """
    if not records:
        return

    stage = records[0].get("stage", "unknown")
    if stage == "unknown":
        return

    now = datetime.now(timezone.utc)
    night_date = _night_date_for(now)

    with conn.cursor() as cur:
        # Find the most recent segment for tonight from this source
        cur.execute("""
            SELECT id, stage, start_ts FROM sleep_segments
            WHERE night_date = %s AND source = %s
            ORDER BY start_ts DESC LIMIT 1
        """, (night_date, source))
        prev = cur.fetchone()

        if prev:
            prev_id, prev_stage, prev_start = prev
            if prev_stage == stage:
                # Same stage — extend the segment
                cur.execute("""
                    UPDATE sleep_segments
                    SET end_ts = %s,
                        duration_min = EXTRACT(EPOCH FROM (%s - start_ts)) / 60.0
                    WHERE id = %s
                """, (now, now, prev_id))
            else:
                # Stage changed — close previous, open new
                cur.execute("""
                    UPDATE sleep_segments
                    SET end_ts = %s,
                        duration_min = EXTRACT(EPOCH FROM (%s - start_ts)) / 60.0
                    WHERE id = %s
                """, (now, now, prev_id))
                cur.execute("""
                    INSERT INTO sleep_segments
                        (night_date, start_ts, end_ts, stage, duration_min, source)
                    VALUES (%s, %s, %s, %s, 0, %s)
                """, (night_date, now, now, stage, source))
        else:
            # First segment of the night
            cur.execute("""
                INSERT INTO sleep_segments
                    (night_date, start_ts, end_ts, stage, duration_min, source)
                VALUES (%s, %s, %s, %s, 0, %s)
            """, (night_date, now, now, stage, source))

    conn.commit()
    log.info("Sleep stage: %s (night %s)", stage, night_date)

    # Best-effort HA entity update (in case the webhook automation didn't fire)
    _update_ha_sleep_stage(stage)


def _update_ha_sleep_stage(stage: str):
    """Update input_text.apple_health_sleep_stage via HA REST API (best-effort)."""
    if not HA_TOKEN:
        return
    try:
        url = f"{HA_URL}/api/services/input_text/set_value"
        data = json.dumps({
            "entity_id": "input_text.apple_health_sleep_stage",
            "value": stage,
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {HA_TOKEN}")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log.warning("HA entity update failed (non-critical): %s", e)


# ── General Metrics ──────────────────────────────────────────────────

def _store_metric(conn, metric_name: str, units: str, records: list):
    """Store a general health metric (HR, HRV, steps, etc.).

    Handles both daily aggregates and per-minute granular records.
    Uses batch inserts for performance on large datasets.
    """
    rows = []
    for rec in records:
        date_field = rec.get("date") or rec.get("start") or rec.get("startDate")
        if date_field:
            try:
                ts = parse_date(date_field)
            except (ValueError, TypeError):
                continue
        else:
            # SleepSync real-time format: no date field → use current time
            ts = datetime.now(timezone.utc)

        value = rec.get("qty") or rec.get("Avg")
        value_min = rec.get("Min")
        value_max = rec.get("Max")
        source = rec.get("source") or "sleepsync"

        if value is None and value_min is None:
            continue

        rows.append((ts, metric_name, value, value_min, value_max, units, source))

    if not rows:
        return

    BATCH = 1000
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH):
            batch = rows[i:i + BATCH]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO health_metrics
                       (ts, metric_name, value, value_min, value_max, units, source)
                   VALUES %s
                   ON CONFLICT (ts, metric_name, COALESCE(source, ''))
                   DO UPDATE SET
                       value = EXCLUDED.value,
                       value_min = EXCLUDED.value_min,
                       value_max = EXCLUDED.value_max,
                       units = EXCLUDED.units""",
                batch,
                template="(%s, %s, %s, %s, %s, %s, %s)",
            )
            conn.commit()
            if i + BATCH < len(rows):
                log.info("  %s: batch %d/%d", metric_name, i + BATCH, len(rows))
    log.info("Stored %d points for %s", len(rows), metric_name)


# ── API Endpoints ────────────────────────────────────────────────────

@app.post("/api/health")
async def receive_health(request: Request):
    """Receive Health Auto Export webhook payload."""
    # ── Size check ──
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        return JSONResponse(
            {"error": f"Payload too large ({int(content_length)} bytes, max {MAX_BODY_BYTES})"},
            status_code=413)

    try:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse(
                {"error": f"Payload too large ({len(body)} bytes, max {MAX_BODY_BYTES})"},
                status_code=413)
        payload = json.loads(body)
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    data = payload.get("data", payload)
    metrics = data.get("metrics", [])

    # ── Pre-flight: check record counts before touching the DB ──
    rejected = []
    for m in metrics:
        name = m.get("name", "?")
        records = m.get("data", [])
        limit = MAX_SLEEP_RECORDS if name == "sleep_analysis" else MAX_RECORDS_PER_METRIC
        if len(records) > limit:
            rejected.append(f"{name}: {len(records)} records (max {limit})")
        elif len(records) > WARN_RECORDS:
            log.warning("Large metric: %s has %d records", name, len(records))

    if rejected:
        log.error("REJECTED oversized metrics: %s", rejected)
        return JSONResponse({
            "error": "Too many records — use daily aggregation instead of per-minute",
            "rejected": rejected,
            "hint": "In Health Auto Export, set time grouping to 'daily' for non-sleep metrics",
        }, status_code=413)

    if not metrics:
        return JSONResponse({"error": "no metrics found"}, status_code=400)

    # Log summary
    for m in metrics:
        name = m.get("name", "?")
        count = len(m.get("data", []))
        if count > 0:
            log.info("Ingesting %s: %d records", name, count)

    conn = get_db()
    try:
        stored = 0
        sleep_stored = 0
        for metric in metrics:
            name = metric.get("name", "")
            units = metric.get("units", "")
            records = metric.get("data", [])

            if name == "sleep_analysis":
                if records and _is_granular(records[0]):
                    _store_sleep_granular(conn, records)
                    sleep_stored += len(records)
                else:
                    for rec in records:
                        _store_sleep_summary(conn, rec)
                        sleep_stored += 1
            elif name == "sleep_stage":
                _store_sleep_stage_realtime(conn, records)
                sleep_stored += 1
            else:
                _store_metric(conn, name, units, records)
                stored += len(records)

        return {
            "status": "ok",
            "metrics_stored": stored,
            "sleep_records_stored": sleep_stored,
        }
    except Exception as e:
        log.exception("Failed to store health data")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()


@app.get("/api/health/status")
async def health_status():
    """Health check endpoint."""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM health_metrics")
            metric_count = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM nightly_summary")
            sleep_count = cur.fetchone()[0]
        conn.close()
        return {
            "status": "ok",
            "health_metrics_rows": metric_count,
            "nightly_summary_rows": sleep_count,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/health/recent")
async def recent_metrics():
    """Return metrics from the last 7 days."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT ts, metric_name, value, value_min, value_max, units
                FROM health_metrics
                WHERE ts >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY ts DESC, metric_name
                LIMIT 1000
            """)
            metrics = cur.fetchall()

            cur.execute("""
                SELECT night_date, duration_hours,
                       total_sleep_min, deep_sleep_min, rem_sleep_min,
                       core_sleep_min, awake_min
                FROM nightly_summary
                WHERE night_date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY night_date DESC
            """)
            sleep = cur.fetchall()

        return {"metrics": metrics, "sleep": sleep}
    finally:
        conn.close()


# ── File Upload UI ───────────────────────────────────────────────────

UPLOAD_PAGE = """<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Health Import</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 480px; margin: 40px auto; padding: 0 20px; }
  h1 { font-size: 1.4em; }
  .drop { border: 2px dashed #ccc; border-radius: 12px; padding: 40px 20px;
          text-align: center; margin: 20px 0; transition: .2s; }
  .drop.over { border-color: #007aff; background: #f0f7ff; }
  input[type=file] { margin: 10px 0; }
  button { background: #007aff; color: white; border: none; padding: 12px 24px;
           border-radius: 8px; font-size: 1em; cursor: pointer; width: 100%; }
  button:disabled { background: #ccc; }
  .result { margin: 20px 0; padding: 12px; border-radius: 8px; }
  .ok { background: #e8f5e9; color: #2e7d32; }
  .err { background: #ffebee; color: #c62828; }
  .stats { color: #666; font-size: 0.9em; margin-top: 20px; }
</style></head>
<body>
<h1>Health Data Import</h1>
<p>Upload a JSON export from Health Auto Export.</p>
<div class="drop" id="drop">
  <p>Drag &amp; drop JSON file here, or:</p>
  <input type="file" id="file" accept=".json,application/json">
</div>
<button id="btn" disabled onclick="upload()">Upload</button>
<div id="result"></div>
<div class="stats" id="stats"></div>
<script>
const drop=document.getElementById('drop'), fi=document.getElementById('file'),
      btn=document.getElementById('btn'), res=document.getElementById('result'),
      stats=document.getElementById('stats');
let file=null;
fi.onchange=e=>{file=e.target.files[0];btn.disabled=!file;};
drop.ondragover=e=>{e.preventDefault();drop.classList.add('over');};
drop.ondragleave=()=>drop.classList.remove('over');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('over');
  file=e.dataTransfer.files[0];fi.files=e.dataTransfer.files;btn.disabled=!file;};
async function upload(){
  btn.disabled=true; btn.textContent='Uploading...'; res.innerHTML='';
  try{
    const r=await fetch('/api/health',{method:'POST',
      headers:{'Content-Type':'application/json'},body:await file.text()});
    const d=await r.json();
    if(r.ok){res.className='result ok';
      res.textContent='Stored '+d.metrics_stored+' metrics, '+d.sleep_records_stored+' sleep records.';
    }else{res.className='result err';res.textContent='Error: '+(d.error||r.statusText);}
  }catch(e){res.className='result err';res.textContent='Failed: '+e.message;}
  btn.disabled=false;btn.textContent='Upload';loadStats();
}
async function loadStats(){
  try{const r=await fetch('/api/health/status');const d=await r.json();
    stats.textContent='DB: '+d.health_metrics_rows+' metrics, '+d.nightly_summary_rows+' sleep nights';
  }catch(e){}
}
loadStats();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def upload_page():
    return UPLOAD_PAGE


@app.post("/api/health/upload")
async def upload_file(file: UploadFile = File(...)):
    """Accept a JSON file upload and process it."""
    try:
        content = await file.read()
        payload = json.loads(content)
    except (json.JSONDecodeError, Exception) as e:
        return JSONResponse({"error": f"Invalid JSON: {e}"}, status_code=400)

    data = payload.get("data", payload)
    metrics = data.get("metrics", [])

    if not metrics:
        return JSONResponse({"error": "no metrics found"}, status_code=400)

    conn = get_db()
    try:
        stored = 0
        sleep_stored = 0
        for metric in metrics:
            name = metric.get("name", "")
            units = metric.get("units", "")
            records = metric.get("data", [])

            if name == "sleep_analysis":
                if records and _is_granular(records[0]):
                    _store_sleep_granular(conn, records)
                    sleep_stored += len(records)
                else:
                    for rec in records:
                        _store_sleep_summary(conn, rec)
                        sleep_stored += 1
            elif name == "sleep_stage":
                _store_sleep_stage_realtime(conn, records)
                sleep_stored += 1
            else:
                _store_metric(conn, name, units, records)
                stored += len(records)

        return {
            "status": "ok",
            "metrics_stored": stored,
            "sleep_records_stored": sleep_stored,
        }
    except Exception as e:
        log.exception("Failed to store uploaded health data")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()
