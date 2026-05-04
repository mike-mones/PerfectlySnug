#!/usr/bin/env python3
"""
PerfectlySnug — End-of-night metric computation.

Per docs/proposals/2026-05-04_evaluation.md, computes the canonical metric
stack per (night, zone) over the bed window and upserts into
v6_nightly_summary.

Schema dependency: sql/v6_eval_metrics.sql (additive ALTER applied).

Usage:
    python tools/eval_nightly.py                          # last night, both zones
    python tools/eval_nightly.py --night 2026-05-02
    python tools/eval_nightly.py --night 2026-05-02 --zone left --rebuild
    python tools/eval_nightly.py --backfill --from 2026-04-05 --to 2026-05-03
    python tools/eval_nightly.py --backfill --rebuild

The controller hot path is NOT touched. This is strictly read+write batch.

Notes vs the design doc (verified against live PG 2026-05-04):
  - nightly_summary key column is `night_date` (not `night`).
  - controller_readings.action vocabulary in the live corpus:
      'set'         — controller actually wrote a new setting (controller write)
      'override'    — user manual adjustment
      'hot_safety'  — right-rail forced -10
      'passive'/'hold'/'freeze_hold'/'rate_hold'/'manual_hold'/'telemetry_only'
                    — non-actuating heartbeats (excluded from stability metrics)
    The eval doc's ('controller','init','rail') values do NOT exist; we use
    {'set','hot_safety'} as the controller-write set, with 'hot_safety'
    additionally tracked separately so we don't confuse safety actuation with
    policy oscillation.
  - Cadence is variable (~5 min nominal but drifts). Comfort-minute counts
    use actual inter-row time deltas, not a fixed 5x multiplier.
  - Right-zone controller is shadow-only as of 2026-05-04 (zero 'set' rows
    in 30 days). Stability metrics for right zone are computed but expected
    to be zero / null until R-zone cutover (rollout P7).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras

# ── Constants ─────────────────────────────────────────────────────────
TARGET_F = 80.0                 # BODY_FB_TARGET_F
BAND_HALF_F = 1.5               # in_target_band: target ± 1.5 °F
COLD_BELOW_F = TARGET_F - 2.0   # 78.0
WARM_ABOVE_F = TARGET_F + 2.0   # 82.0

# Controller-write set per the live corpus (doc was wrong about action names).
# We exclude 'hot_safety' from oscillation/overcorrection metrics — safety
# rail engagement is not a policy decision and shouldn't pollute stability.
CONTROLLER_WRITE_ACTIONS = {"set"}
SAFETY_ACTIONS = {"hot_safety"}
OVERRIDE_ACTION = "override"

# Discomfort-signal thresholds per evaluation.md §1.3.
BODY_TREND_WINDOW_MIN = 15
BODY_TREND_THRESHOLD_F = 0.5    # °F per 15 min, away from target
RESPONSE_HORIZON_MIN = 30       # max wait for a corrective write
OVERCORRECTION_WINDOW_MIN = 10  # window in which an opposite-sign write counts

METRICS_SCHEMA_VERSION = "1.0"

# ── DB connection ─────────────────────────────────────────────────────
PG_HOST = os.environ.get("PG_HOST", "192.168.0.3")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB = os.environ.get("PG_DB", "sleepdata")
PG_USER = os.environ.get("PG_USER", "sleepsync")
PG_PASS = os.environ.get("PG_PASS", "sleepsync_local")


def get_conn():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASS, connect_timeout=10,
    )


def get_git_sha() -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return None


# ── Bed window detection ──────────────────────────────────────────────
def fetch_bed_window(conn, night: date) -> Optional[tuple[datetime, datetime, bool]]:
    """Return (bedtime_ts, wake_ts, manual_mode) or None if missing.

    Uses nightly_summary as the authoritative source. The `night_date` column
    is the calendar date BEFORE the bedtime (i.e. "the evening of" date).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT bedtime_ts, wake_ts, manual_mode FROM nightly_summary "
            "WHERE night_date = %s ORDER BY id DESC LIMIT 1",
            (night,),
        )
        row = cur.fetchone()
    if not row or not row[0] or not row[1]:
        return None
    return (row[0], row[1], bool(row[2]))


# ── Per-zone metric computation ───────────────────────────────────────
@dataclass
class NightMetrics:
    night: date
    zone: str
    user_id: str
    controller_version: Optional[str]
    bed_onset_ts: datetime
    wake_ts: datetime
    in_bed_minutes: int

    # §1.1 discomfort
    adj_count_per_night: int = 0
    adj_magnitude_sum: int = 0
    adj_weighted_score: float = 0.0

    # §1.2 stability
    oscillation_count: int = 0
    overcorrection_rate: Optional[float] = None
    setting_total_variation: int = 0

    # §1.3 responsiveness
    discomfort_event_count: int = 0
    time_to_correct_median_min: Optional[float] = None
    unaddressed_discomfort_min: int = 0

    # §1.4 comfort outcomes
    body_in_target_band_pct: Optional[float] = None
    cold_minutes: int = 0
    warm_minutes: int = 0

    notes: dict = field(default_factory=dict)


def _zone_user(zone: str) -> str:
    # Fixed mapping per evaluation.md §1; column exists for future portability.
    return {"left": "mike", "right": "partner"}[zone]


def _fetch_zone_rows(conn, zone: str, start: datetime, end: datetime) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT ts, action, setting, override_delta,
                   body_avg_f, bed_occupied_left, bed_occupied_right,
                   controller_version
            FROM controller_readings
            WHERE zone = %s AND ts >= %s AND ts < %s
            ORDER BY ts
            """,
            (zone, start, end),
        )
        return list(cur.fetchall())


def _row_minutes(rows: list[dict], end: datetime) -> list[float]:
    """Time slice (in minutes) each row represents = delta to next row or end."""
    out = []
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["ts"] if i + 1 < len(rows) else end
        out.append(max(0.0, (nxt - r["ts"]).total_seconds() / 60.0))
    return out


def _zone_occupied(row: dict, zone: str) -> bool:
    key = "bed_occupied_left" if zone == "left" else "bed_occupied_right"
    return bool(row.get(key))


def _compute_discomfort(rows: list[dict]) -> tuple[int, int, float, list[dict]]:
    overrides = [r for r in rows if r["action"] == OVERRIDE_ACTION
                 and r["override_delta"] is not None]
    count = len(overrides)
    mag = sum(abs(r["override_delta"]) for r in overrides)
    weighted = count + 0.5 * mag
    return count, mag, weighted, overrides


def _compute_stability(rows: list[dict]) -> tuple[int, Optional[float], int, list[dict]]:
    """Sign flips, overcorrection rate, total variation on controller writes only."""
    writes: list[dict] = []
    prev_setting = None
    for r in rows:
        if r["action"] not in CONTROLLER_WRITE_ACTIONS:
            continue
        if r["setting"] is None:
            continue
        if prev_setting is None:
            prev_setting = r["setting"]
            continue
        d = r["setting"] - prev_setting
        if d != 0:
            writes.append({"ts": r["ts"], "setting": r["setting"], "delta": d})
            prev_setting = r["setting"]
    if not writes:
        return 0, None, 0, []
    tv = sum(abs(w["delta"]) for w in writes)
    flips = 0
    last_sign = 0
    for w in writes:
        s = 1 if w["delta"] > 0 else -1 if w["delta"] < 0 else 0
        if s != 0 and last_sign != 0 and s != last_sign:
            flips += 1
        if s != 0:
            last_sign = s
    overc_num = 0
    overc_den = max(0, len(writes) - 1)  # last write can't be followed
    for i, w in enumerate(writes[:-1]):
        s = 1 if w["delta"] > 0 else -1 if w["delta"] < 0 else 0
        if s == 0:
            continue
        horizon = w["ts"] + timedelta(minutes=OVERCORRECTION_WINDOW_MIN)
        for w2 in writes[i + 1:]:
            if w2["ts"] > horizon:
                break
            s2 = 1 if w2["delta"] > 0 else -1 if w2["delta"] < 0 else 0
            if s2 == -s:
                overc_num += 1
                break
    overc_rate = (overc_num / overc_den) if overc_den > 0 else None
    return flips, overc_rate, tv, writes


def _compute_responsiveness(
    rows: list[dict], overrides: list[dict], writes: list[dict],
    zone: str, end: datetime,
) -> tuple[int, Optional[float], int]:
    """Discomfort events: overrides ∪ body-trend excursions while occupied.

    Time-to-correct: minutes from event to first sign-correct controller write
    within RESPONSE_HORIZON_MIN. Censored events contribute the horizon value.

    Unaddressed discomfort: in-bed minutes where body is outside target band
    AND no corrective write occurred in the trailing 15 min.
    """
    # Build a per-row body trend over BODY_TREND_WINDOW_MIN.
    occupied_rows = [r for r in rows if _zone_occupied(r, zone) and r["body_avg_f"] is not None]
    events: list[tuple[datetime, str]] = []  # (ts, direction) direction ∈ {'too_warm','too_cold'}

    for r in overrides:
        # override delta>0 = "too cold, warm me"; <0 = "too warm, cool me"
        if r["override_delta"] is None or r["override_delta"] == 0:
            continue
        direction = "too_cold" if r["override_delta"] > 0 else "too_warm"
        events.append((r["ts"], direction))

    # Body trend events: scan with two-pointer over occupied_rows.
    j = 0
    for i, r in enumerate(occupied_rows):
        while j < i and (r["ts"] - occupied_rows[j]["ts"]).total_seconds() / 60.0 > BODY_TREND_WINDOW_MIN:
            j += 1
        if j == i:
            continue
        delta_min = (r["ts"] - occupied_rows[j]["ts"]).total_seconds() / 60.0
        if delta_min < BODY_TREND_WINDOW_MIN * 0.66:
            continue  # not enough span to call it a 15-min trend
        body_now = r["body_avg_f"]
        body_then = occupied_rows[j]["body_avg_f"]
        trend = body_now - body_then  # °F over ~15 min
        if trend > BODY_TREND_THRESHOLD_F and body_now > TARGET_F:
            events.append((r["ts"], "too_warm"))
        elif trend < -BODY_TREND_THRESHOLD_F and body_now < TARGET_F:
            events.append((r["ts"], "too_cold"))

    # Dedupe events within 5 min (a single excursion shouldn't count 3x).
    events.sort()
    deduped: list[tuple[datetime, str]] = []
    for ts, d in events:
        if deduped and (ts - deduped[-1][0]).total_seconds() < 300 and deduped[-1][1] == d:
            continue
        deduped.append((ts, d))
    events = deduped

    # For each event find first sign-correct write within horizon.
    times_to_correct: list[float] = []
    for ts, direction in events:
        horizon = ts + timedelta(minutes=RESPONSE_HORIZON_MIN)
        chosen = None
        for w in writes:
            if w["ts"] <= ts:
                continue
            if w["ts"] > horizon:
                break
            sign = 1 if w["delta"] > 0 else -1 if w["delta"] < 0 else 0
            if direction == "too_warm" and sign < 0:
                chosen = w; break
            if direction == "too_cold" and sign > 0:
                chosen = w; break
        if chosen is not None:
            times_to_correct.append((chosen["ts"] - ts).total_seconds() / 60.0)
        else:
            times_to_correct.append(float(RESPONSE_HORIZON_MIN))
    median_ttc = statistics.median(times_to_correct) if times_to_correct else None

    # Unaddressed discomfort minutes.
    minute_slices = _row_minutes(rows, end)
    unaddr = 0.0
    for r, dt_min in zip(rows, minute_slices):
        if not _zone_occupied(r, zone):
            continue
        body = r["body_avg_f"]
        if body is None:
            continue
        out_of_band = body < (TARGET_F - BAND_HALF_F) or body > (TARGET_F + BAND_HALF_F)
        if not out_of_band:
            continue
        # Look back 15 min for any controller write that pushed toward correction.
        too_warm = body > (TARGET_F + BAND_HALF_F)
        corrective = False
        for w in writes:
            age = (r["ts"] - w["ts"]).total_seconds() / 60.0
            if age < 0:
                break
            if age > 15:
                continue
            if too_warm and w["delta"] < 0:
                corrective = True; break
            if (not too_warm) and w["delta"] > 0:
                corrective = True; break
        if not corrective:
            unaddr += dt_min
    return len(events), median_ttc, int(round(unaddr))


def _compute_comfort(rows: list[dict], zone: str, end: datetime) -> tuple[Optional[float], int, int, int]:
    """Body-in-band %, cold minutes, warm minutes, total in-bed minutes."""
    minute_slices = _row_minutes(rows, end)
    in_band = 0.0
    cold = 0.0
    warm = 0.0
    in_bed = 0.0
    for r, dt_min in zip(rows, minute_slices):
        if not _zone_occupied(r, zone):
            continue
        in_bed += dt_min
        body = r["body_avg_f"]
        if body is None:
            continue
        if (TARGET_F - BAND_HALF_F) <= body <= (TARGET_F + BAND_HALF_F):
            in_band += dt_min
        if body < COLD_BELOW_F:
            cold += dt_min
        if body > WARM_ABOVE_F:
            warm += dt_min
    pct = (in_band / in_bed * 100.0) if in_bed > 0 else None
    return pct, int(round(cold)), int(round(warm)), int(round(in_bed))


def _majority_controller_version(rows: list[dict]) -> Optional[str]:
    versions = [r["controller_version"] for r in rows if r["controller_version"]]
    if not versions:
        return None
    return Counter(versions).most_common(1)[0][0]


def compute_night_zone(
    conn, night: date, zone: str, bed_onset: datetime, wake: datetime,
) -> Optional[NightMetrics]:
    rows = _fetch_zone_rows(conn, zone, bed_onset, wake)
    if not rows:
        return None
    in_bed_min = max(0, int(round((wake - bed_onset).total_seconds() / 60.0)))
    cv = _majority_controller_version(rows)

    adj_count, adj_mag, adj_weighted, overrides = _compute_discomfort(rows)
    flips, overc, tv, writes = _compute_stability(rows)
    n_events, median_ttc, unaddr = _compute_responsiveness(rows, overrides, writes, zone, wake)
    pct, cold, warm, derived_in_bed_min = _compute_comfort(rows, zone, wake)

    notes = {
        "metrics_schema_version": METRICS_SCHEMA_VERSION,
        "controller_writes": len(writes),
        "occupied_minutes": derived_in_bed_min,
        "row_count": len(rows),
        "safety_writes": sum(1 for r in rows if r["action"] in SAFETY_ACTIONS),
    }

    return NightMetrics(
        night=night, zone=zone, user_id=_zone_user(zone),
        controller_version=cv,
        bed_onset_ts=bed_onset, wake_ts=wake, in_bed_minutes=in_bed_min,
        adj_count_per_night=adj_count, adj_magnitude_sum=adj_mag,
        adj_weighted_score=adj_weighted,
        oscillation_count=flips, overcorrection_rate=overc,
        setting_total_variation=tv,
        discomfort_event_count=n_events,
        time_to_correct_median_min=median_ttc,
        unaddressed_discomfort_min=unaddr,
        body_in_target_band_pct=pct, cold_minutes=cold, warm_minutes=warm,
        notes=notes,
    )


# ── Upsert ────────────────────────────────────────────────────────────
UPSERT_SQL = """
INSERT INTO v6_nightly_summary
    (night, zone, controller_version, user_id,
     bed_onset_ts, wake_ts, in_bed_minutes,
     adj_count_per_night, adj_magnitude_sum, adj_weighted_score,
     oscillation_count, overcorrection_rate, setting_total_variation,
     discomfort_event_count, time_to_correct_median_min, unaddressed_discomfort_min,
     body_in_target_band_pct, cold_minutes, warm_minutes,
     metrics_target_f, metrics_computed_at, metrics_source_commit,
     metrics_schema_version, notes)
VALUES (%(night)s, %(zone)s, %(controller_version)s, %(user_id)s,
        %(bed_onset_ts)s, %(wake_ts)s, %(in_bed_minutes)s,
        %(adj_count_per_night)s, %(adj_magnitude_sum)s, %(adj_weighted_score)s,
        %(oscillation_count)s, %(overcorrection_rate)s, %(setting_total_variation)s,
        %(discomfort_event_count)s, %(time_to_correct_median_min)s, %(unaddressed_discomfort_min)s,
        %(body_in_target_band_pct)s, %(cold_minutes)s, %(warm_minutes)s,
        %(metrics_target_f)s, %(metrics_computed_at)s, %(metrics_source_commit)s,
        %(metrics_schema_version)s, %(notes)s::jsonb)
ON CONFLICT (night, zone, controller_version) DO UPDATE
SET user_id                    = EXCLUDED.user_id,
    bed_onset_ts               = EXCLUDED.bed_onset_ts,
    wake_ts                    = EXCLUDED.wake_ts,
    in_bed_minutes             = EXCLUDED.in_bed_minutes,
    adj_count_per_night        = EXCLUDED.adj_count_per_night,
    adj_magnitude_sum          = EXCLUDED.adj_magnitude_sum,
    adj_weighted_score         = EXCLUDED.adj_weighted_score,
    oscillation_count          = EXCLUDED.oscillation_count,
    overcorrection_rate        = EXCLUDED.overcorrection_rate,
    setting_total_variation    = EXCLUDED.setting_total_variation,
    discomfort_event_count     = EXCLUDED.discomfort_event_count,
    time_to_correct_median_min = EXCLUDED.time_to_correct_median_min,
    unaddressed_discomfort_min = EXCLUDED.unaddressed_discomfort_min,
    body_in_target_band_pct    = EXCLUDED.body_in_target_band_pct,
    cold_minutes               = EXCLUDED.cold_minutes,
    warm_minutes               = EXCLUDED.warm_minutes,
    metrics_target_f           = EXCLUDED.metrics_target_f,
    metrics_computed_at        = EXCLUDED.metrics_computed_at,
    metrics_source_commit      = EXCLUDED.metrics_source_commit,
    metrics_schema_version     = EXCLUDED.metrics_schema_version,
    notes                      = COALESCE(v6_nightly_summary.notes, '{}'::jsonb) || EXCLUDED.notes
"""


def upsert(conn, m: NightMetrics, *, source_commit: Optional[str]) -> None:
    payload = {
        "night": m.night, "zone": m.zone, "controller_version": m.controller_version,
        "user_id": m.user_id, "bed_onset_ts": m.bed_onset_ts, "wake_ts": m.wake_ts,
        "in_bed_minutes": m.in_bed_minutes,
        "adj_count_per_night": m.adj_count_per_night, "adj_magnitude_sum": m.adj_magnitude_sum,
        "adj_weighted_score": m.adj_weighted_score,
        "oscillation_count": m.oscillation_count, "overcorrection_rate": m.overcorrection_rate,
        "setting_total_variation": m.setting_total_variation,
        "discomfort_event_count": m.discomfort_event_count,
        "time_to_correct_median_min": m.time_to_correct_median_min,
        "unaddressed_discomfort_min": m.unaddressed_discomfort_min,
        "body_in_target_band_pct": m.body_in_target_band_pct,
        "cold_minutes": m.cold_minutes, "warm_minutes": m.warm_minutes,
        "metrics_target_f": TARGET_F,
        "metrics_computed_at": datetime.now(timezone.utc),
        "metrics_source_commit": source_commit,
        "metrics_schema_version": METRICS_SCHEMA_VERSION,
        "notes": json.dumps(m.notes),
    }
    with conn.cursor() as cur:
        cur.execute(UPSERT_SQL, payload)


# ── Driver ────────────────────────────────────────────────────────────
def parse_night(s: str) -> date:
    if s in ("yesterday", ""):
        return (datetime.now() - timedelta(days=1)).date()
    return datetime.strptime(s, "%Y-%m-%d").date()


def run_one(conn, night: date, zone: str, *, rebuild: bool,
            source_commit: Optional[str], skip_manual: bool) -> str:
    bw = fetch_bed_window(conn, night)
    if bw is None:
        return f"  {night} {zone}: SKIP (no nightly_summary row)"
    bed_onset, wake, manual = bw
    if manual and skip_manual:
        return f"  {night} {zone}: SKIP (manual_mode)"
    if rebuild:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM v6_nightly_summary "
                "WHERE night=%s AND zone=%s AND metrics_schema_version IS NOT NULL",
                (night, zone),
            )
    m = compute_night_zone(conn, night, zone, bed_onset, wake)
    if m is None:
        return f"  {night} {zone}: SKIP (no controller_readings rows)"
    upsert(conn, m, source_commit=source_commit)
    conn.commit()
    return (
        f"  {night} {zone}: ver={m.controller_version}  "
        f"adj={m.adj_count_per_night}/{m.adj_magnitude_sum} "
        f"osc={m.oscillation_count} tv={m.setting_total_variation} "
        f"in_band={m.body_in_target_band_pct:.0f}% " if m.body_in_target_band_pct is not None
        else f"  {night} {zone}: ver={m.controller_version}  adj={m.adj_count_per_night}/{m.adj_magnitude_sum} "
             f"osc={m.oscillation_count} tv={m.setting_total_variation} in_band=NA "
    ) + f"cold={m.cold_minutes} warm={m.warm_minutes}"


def enumerate_nights(conn, start: date, end: date) -> list[date]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT night_date FROM nightly_summary "
            "WHERE night_date >= %s AND night_date <= %s "
            "ORDER BY night_date",
            (start, end),
        )
        return [r[0] for r in cur.fetchall()]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--night", default="yesterday", help="YYYY-MM-DD or 'yesterday'")
    p.add_argument("--zone", choices=["left", "right", "both"], default="both")
    p.add_argument("--rebuild", action="store_true",
                   help="Delete and recompute (idempotent re-run).")
    p.add_argument("--backfill", action="store_true",
                   help="Loop over a range of nights from --from to --to.")
    p.add_argument("--from", dest="from_", help="YYYY-MM-DD (inclusive)")
    p.add_argument("--to", dest="to_", help="YYYY-MM-DD (inclusive)")
    p.add_argument("--include-manual", action="store_true",
                   help="Don't skip manual_mode nights (default: skip).")
    args = p.parse_args()

    source_commit = get_git_sha()
    conn = get_conn()
    try:
        zones = ["left", "right"] if args.zone == "both" else [args.zone]
        if args.backfill:
            start = parse_night(args.from_) if args.from_ else (datetime.now() - timedelta(days=45)).date()
            end = parse_night(args.to_) if args.to_ else (datetime.now() - timedelta(days=1)).date()
            nights = enumerate_nights(conn, start, end)
            print(f"Backfilling {len(nights)} nights × {len(zones)} zones "
                  f"({start} .. {end}); rebuild={args.rebuild}")
        else:
            nights = [parse_night(args.night)]
            print(f"Processing {nights[0]} × {len(zones)} zones; rebuild={args.rebuild}")

        for n in nights:
            for z in zones:
                msg = run_one(conn, n, z, rebuild=args.rebuild,
                              source_commit=source_commit,
                              skip_manual=not args.include_manual)
                print(msg)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
