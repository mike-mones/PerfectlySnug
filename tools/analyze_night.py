#!/usr/bin/env python3
"""
PerfectlySnug Overnight Analysis Tool

Queries Postgres for a night's controller data and prints a comprehensive report.

Usage:
    python3 tools/analyze_night.py                    # last night
    python3 tools/analyze_night.py --date 2026-04-07  # specific night
    python3 tools/analyze_night.py --compare 7        # compare last 7 nights
"""

import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras

# ── DB connection ────────────────────────────────────────────
PG_HOST = os.environ.get("PG_HOST", "192.168.0.3")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB = os.environ.get("PG_DB", "sleepdata")
PG_USER = os.environ.get("PG_USER", "sleepsync")
PG_PASS = os.environ.get("PG_PASS", "sleepsync_local")

DEFAULT_ZONE = os.environ.get("ZONE", "left")


def get_conn():
    """Get Postgres connection with retry."""
    for attempt in range(3):
        try:
            return psycopg2.connect(
                host=PG_HOST, port=PG_PORT, dbname=PG_DB,
                user=PG_USER, password=PG_PASS,
                connect_timeout=10,
            )
        except psycopg2.OperationalError as e:
            if attempt == 2:
                raise
            print(f"  Connection attempt {attempt + 1} failed: {e}", file=sys.stderr)
            import time; time.sleep(2)


def last_night_date() -> date:
    """Return the date of 'last night' based on current time.
    Before 6 PM → last night = yesterday.  After 6 PM → last night = today."""
    now = datetime.now()
    if now.hour < 18:
        return (now - timedelta(days=1)).date()
    return now.date()


# ── Queries ──────────────────────────────────────────────────

def fetch_summary(conn, night: date, zone: str) -> Optional[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM v_overnight_summary
            WHERE night_date = %s AND zone = %s
        """, (night, zone))
        return cur.fetchone()


def fetch_timeline(conn, night: date, zone: str) -> list:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM v_setting_timeline
            WHERE night_date = %s
              AND zone = %s
            ORDER BY ts
        """, (night, zone))
        return cur.fetchall()


def fetch_hourly_stability(conn, night: date, zone: str) -> list:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM v_body_temp_stability
            WHERE night_date = %s AND zone = %s
            ORDER BY hour_local
        """, (night, zone))
        return cur.fetchall()


def fetch_room_vs_setting(conn, night: date, zone: str) -> list:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM v_room_temp_vs_setting
            WHERE night_date = %s AND zone = %s
            ORDER BY room_temp_band
        """, (night, zone))
        return cur.fetchall()


def fetch_raw_readings(conn, night: date, zone: str) -> list:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            WITH tagged AS (
                SELECT
                    CASE
                        WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'America/New_York') >= 18
                        THEN (ts AT TIME ZONE 'America/New_York')::date
                        ELSE (ts AT TIME ZONE 'America/New_York')::date - 1
                    END AS night_date,
                    ts, zone, phase, action, setting, effective, body_avg_f,
                    room_temp_f, ambient_f, setpoint_f, override_delta,
                    baseline, learned_adj, notes, controller_version
                FROM controller_readings
            )
            SELECT ts, phase, action, setting, effective, body_avg_f, room_temp_f,
                   ambient_f, setpoint_f, override_delta, baseline, learned_adj, notes,
                   controller_version
            FROM tagged
            WHERE zone = %s
              AND night_date = %s
            ORDER BY ts
        """, (zone, night))
        return cur.fetchall()


def fetch_comparison(conn, num_nights: int, zone: str) -> list:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM v_overnight_summary
            WHERE zone = %s
            ORDER BY night_date DESC
            LIMIT %s
        """, (zone, num_nights))
        return cur.fetchall()


def fetch_nightly_summary(conn, night: date) -> Optional[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM nightly_summary WHERE night_date = %s
        """, (night,))
        return cur.fetchone()


def fetch_learning_overrides(
    conn,
    controller_version: str = "v5_rc_off",
    lookback_nights: int = 14,
) -> list:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                elapsed_min,
                setting AS override_setting,
                effective AS controller_setting,
                CASE
                    WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'America/New_York') >= 18
                    THEN (ts AT TIME ZONE 'America/New_York')::date
                    ELSE (ts AT TIME ZONE 'America/New_York')::date - 1
                END AS night_date
            FROM controller_readings
            WHERE zone = 'left'
              AND action = 'override'
              AND setting IS NOT NULL
              AND effective IS NOT NULL
              AND setting IS DISTINCT FROM effective
              AND controller_version = %s
              AND ts > now() - %s * interval '1 day'
            ORDER BY ts
        """, (controller_version, lookback_nights))
        return cur.fetchall()


# ── Deep Dive Helpers ────────────────────────────────────────

L1_TO_BLOWER_PCT = {
    -10: 100,
    -9: 87,
    -8: 75,
    -7: 65,
    -6: 50,
    -5: 41,
    -4: 33,
    -3: 26,
    -2: 20,
    -1: 10,
    0: 0,
}
LEARNING_MAX_BLOWER_ADJ = 30
LEARNING_DECAY = 0.7
CYCLE_DURATION_MIN = 90


def parse_notes(notes: Optional[str]) -> dict:
    parsed = {}
    if not notes:
        return parsed
    for token in notes.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key in {
            "cycle",
            "room_comp",
            "base_blower",
            "base_proxy_blower",
            "target_blower",
            "proxy_blower",
            "blower",
            "actual_blower",
            "controller",
            "controller_blower",
            "controller_proxy_blower",
            "override_blower",
            "override_proxy_blower",
            "floor",
        }:
            try:
                parsed[key] = int(value)
                continue
            except ValueError:
                pass
        parsed[key] = value
    return parsed


def cycle_num_for_elapsed(elapsed_min: Optional[float]) -> Optional[int]:
    if elapsed_min is None:
        return None
    return min(max(1, int(float(elapsed_min) / CYCLE_DURATION_MIN) + 1), 6)


def compute_learning_adjustments(override_rows: list) -> dict:
    last_per_cycle_night = {}
    for row in override_rows:
        elapsed_min = row.get("elapsed_min")
        override_val = row.get("override_setting")
        ctrl_val = row.get("controller_setting")
        night_date = row.get("night_date")
        if elapsed_min is None or override_val is None or ctrl_val is None or night_date is None:
            continue
        cycle = cycle_num_for_elapsed(elapsed_min)
        delta = (
            L1_TO_BLOWER_PCT[int(override_val)] -
            L1_TO_BLOWER_PCT[int(ctrl_val)]
        )
        last_per_cycle_night[(night_date, cycle)] = delta

    cycle_deltas = defaultdict(list)
    for (_, cycle), delta in sorted(last_per_cycle_night.items()):
        cycle_deltas[cycle].append(delta)

    adjustments = {}
    for cycle, deltas in cycle_deltas.items():
        weighted_sum = 0
        weight_total = 0
        for i, delta in enumerate(reversed(deltas)):
            weight = LEARNING_DECAY ** i
            weighted_sum += delta * weight
            weight_total += weight
        avg_delta = weighted_sum / weight_total if weight_total else 0
        adj = max(
            -LEARNING_MAX_BLOWER_ADJ,
            min(LEARNING_MAX_BLOWER_ADJ, round(avg_delta)),
        )
        if adj != 0:
            adjustments[str(cycle)] = adj
    return adjustments


def find_blower_divergence_segments(readings: list, threshold: int = 15, min_rows: int = 2) -> list:
    segments = []
    current = []

    def flush():
        nonlocal current
        if len(current) < min_rows:
            current = []
            return
        diffs = [r["_blower_diff"] for r in current]
        segments.append({
            "start": current[0]["ts"],
            "end": current[-1]["ts"],
            "minutes": (current[-1]["ts"] - current[0]["ts"]).total_seconds() / 60,
            "rows": len(current),
            "setting": current[0].get("setting"),
            "proxy_blower": current[0]["_proxy_blower"],
            "actual_blower_min": min(r["_blower_actual"] for r in current),
            "actual_blower_max": max(r["_blower_actual"] for r in current),
            "avg_diff": sum(diffs) / len(diffs),
            "max_abs_diff": max(abs(d) for d in diffs),
            "actions": Counter(r.get("action") for r in current),
        })
        current = []

    for row in readings:
        parsed = parse_notes(row.get("notes"))
        proxy = parsed.get("proxy_blower", parsed.get("target_blower"))
        actual = parsed.get("actual_blower", parsed.get("blower"))
        if proxy is None or actual is None:
            flush()
            continue
        enriched = dict(row)
        enriched["_proxy_blower"] = proxy
        enriched["_blower_actual"] = actual
        enriched["_blower_diff"] = actual - proxy
        if abs(enriched["_blower_diff"]) >= threshold:
            current.append(enriched)
        else:
            flush()
    flush()
    return segments


def format_adjustments(adjustments: dict) -> str:
    if not adjustments:
        return "{}"
    items = ", ".join(f"cycle {cycle}: {adj:+d}%" for cycle, adj in sorted(adjustments.items()))
    return "{ " + items + " }"


def summarize_deep_dive(conn, night: date, zone: str) -> None:
    if zone != "left":
        print("\n  Deep dive is currently optimized for the left-side v5 controller.")
        return

    left = fetch_raw_readings(conn, night, "left")
    right = fetch_raw_readings(conn, night, "right")
    if not left:
        print("\n  No left-side raw readings found for deep dive.")
        return

    left_actions = Counter(r.get("action") for r in left)
    right_actions = Counter(r.get("action") for r in right)
    versions = Counter(r.get("controller_version") for r in left if r.get("controller_version"))
    parsed_left = [parse_notes(r.get("notes")) for r in left]

    room_comps = [p["room_comp"] for p in parsed_left if isinstance(p.get("room_comp"), int)]
    override_rows = [r for r in left if r.get("action") == "override"]
    blower_divergence = find_blower_divergence_segments(left)

    learning_rows = fetch_learning_overrides(conn)
    learned_before = compute_learning_adjustments(
        [r for r in learning_rows if r.get("night_date") and r["night_date"] < night]
    )
    learned_after = compute_learning_adjustments(
        [r for r in learning_rows if r.get("night_date") and r["night_date"] <= night]
    )

    header("🧠 Deep Dive — v5 Controller Behavior")
    print(f"  Left rows:     {len(left)} ({dict(left_actions)})")
    if right:
        print(f"  Right rows:    {len(right)} ({dict(right_actions)})")
    print(f"  Versions:      {dict(versions)}")
    if room_comps:
        print(
            f"  Room comp:     avg {sum(room_comps) / len(room_comps):.1f}% "
            f"(range {min(room_comps):+d} to {max(room_comps):+d})"
        )
    print(f"  Overrides:     {len(override_rows)} left-side override(s)")

    if override_rows:
        header("🎛️  Override Signals")
        for row in override_rows:
            parsed = parse_notes(row.get("notes"))
            ctrl = parsed.get("controller")
            ctrl_proxy = parsed.get("controller_proxy_blower", parsed.get("controller_blower"))
            override_proxy = parsed.get("override_proxy_blower", parsed.get("override_blower"))
            actual_blower = parsed.get("actual_blower", parsed.get("blower"))
            ts_str = row["ts"].strftime("%H:%M")
            delta = None
            if ctrl_proxy is not None and override_proxy is not None:
                delta = override_proxy - ctrl_proxy
            delta_str = f"{delta:+d}%" if delta is not None else "—"
            ctrl_str = f"{ctrl:+d}/{ctrl_proxy}%" if ctrl is not None and ctrl_proxy is not None else "—"
            override_str = f"{row['setting']:+d}/{override_proxy}%" if override_proxy is not None else "—"
            actual_str = f"{actual_blower}%" if actual_blower is not None else "—"
            print(
                f"  {ts_str}: controller proxy {ctrl_str} -> "
                f"user {override_str}  "
                f"(actual blower {actual_str}, body {row['body_avg_f']:.1f}°F, "
                f"room {row['room_temp_f']:.1f}°F, proxy delta {delta_str})"
            )

    header("🤖 Learning Impact")
    print(f"  Learned before this night: {format_adjustments(learned_before)}")
    print(f"  Learned after this night:  {format_adjustments(learned_after)}")
    if learned_before == learned_after:
        print("  Update effect:            no change from this night's override data")
    else:
        changed = []
        all_cycles = sorted(set(learned_before) | set(learned_after), key=int)
        for cycle in all_cycles:
            before = learned_before.get(cycle, 0)
            after = learned_after.get(cycle, 0)
            if before != after:
                changed.append(f"cycle {cycle}: {before:+d}% -> {after:+d}%")
        print(f"  Update effect:            {', '.join(changed)}")
        print("  Note: v5 reloads learned adjustments at sleep start, so the new value applies on the next night.")

    header("🌀 Blower Proxy vs Actual")
    if blower_divergence:
        print("  Significant proxy-vs-actual blower divergence segments:")
        for seg in blower_divergence[:6]:
            actions = ", ".join(f"{k}:{v}" for k, v in seg["actions"].items())
            print(
                f"  {seg['start'].strftime('%H:%M')}–{seg['end'].strftime('%H:%M')} "
                f"({seg['minutes']:.0f}m, {seg['rows']} rows): "
                f"setting {seg['setting']:+d}, proxy {seg['proxy_blower']}%, "
                f"actual {seg['actual_blower_min']}-{seg['actual_blower_max']}%, "
                f"avg diff {seg['avg_diff']:+.1f}% [{actions}]"
            )
    else:
        print("  ✅ No long blower proxy divergence segments detected.")

    header("🛠️  Tuning Guidance")
    suggestions = []
    if override_rows:
        first_override = override_rows[-1]
        parsed = parse_notes(first_override.get("notes"))
        ctrl_proxy = parsed.get("controller_proxy_blower", parsed.get("controller_blower"))
        override_proxy = parsed.get("override_proxy_blower", parsed.get("override_blower"))
        if ctrl_proxy is not None and override_proxy is not None:
            delta = override_proxy - ctrl_proxy
            if delta <= -20:
                suggestions.append(
                    "Late-night cooling was likely too aggressive; let the latest learned adjustment take effect before changing the whole curve."
                )
    if room_comps and (sum(room_comps) / len(room_comps)) >= 10:
        suggestions.append(
            "Room compensation was strongly positive all night, so the Aqara room sensor is pushing the controller colder than the base cycle plan."
        )
    if blower_divergence:
        suggestions.append(
            "Treat the RC-off blower proxy as a coarse control model, not a literal expected fan command; the actual overnight blower telemetry diverged for long stretches."
        )
    if right_actions.get("override"):
        suggestions.append(
            "Right-side passive logs captured a manual change too, which is useful context when comparing comfort across both sides."
        )
    if not suggestions:
        suggestions.append("No immediate tuning change stands out from this night alone.")

    for idx, suggestion in enumerate(suggestions, start=1):
        print(f"  {idx}. {suggestion}")


# ── Problem Detection ────────────────────────────────────────

def detect_problems(readings: list) -> list:
    """Analyze readings for common problems."""
    problems = []
    if not readings:
        return ["No readings found for this night"]

    # 1. Gaps in readings (>10 min between consecutive readings)
    for i in range(1, len(readings)):
        gap = (readings[i]["ts"] - readings[i - 1]["ts"]).total_seconds() / 60
        if gap > 10:
            problems.append(
                f"⚠️  Gap: {gap:.0f}min between "
                f"{readings[i-1]['ts'].strftime('%H:%M')} and "
                f"{readings[i]['ts'].strftime('%H:%M')}"
            )

    # 2. Body temp anomalies
    for r in readings:
        bt = r.get("body_avg_f") or 0
        if bt > 100:
            problems.append(
                f"🔥 Body temp spike: {bt:.1f}°F at {r['ts'].strftime('%H:%M')}"
            )
        elif 0 < bt < 60:
            problems.append(
                f"🥶 Body temp drop: {bt:.1f}°F at {r['ts'].strftime('%H:%M')}"
            )

    # 3. Setting swings (setting changes > 4 levels in short period)
    settings_window = []
    for r in readings:
        if r.get("setting") is not None:
            settings_window.append((r["ts"], r["setting"]))
            # Check 30-min window
            cutoff = r["ts"] - timedelta(minutes=30)
            settings_window = [(t, s) for t, s in settings_window if t >= cutoff]
            if len(settings_window) >= 2:
                swing = max(s for _, s in settings_window) - min(s for _, s in settings_window)
                if swing > 4:
                    problems.append(
                        f"🔄 Setting swing: {swing} levels in 30min around "
                        f"{r['ts'].strftime('%H:%M')}"
                    )

    # 4. Override fights (multiple overrides close together)
    overrides = [r for r in readings if r.get("action") == "override"]
    for i in range(1, len(overrides)):
        gap = (overrides[i]["ts"] - overrides[i - 1]["ts"]).total_seconds() / 60
        if gap < 30:
            problems.append(
                f"⚡ Override fight: overrides {gap:.0f}min apart at "
                f"{overrides[i-1]['ts'].strftime('%H:%M')} and "
                f"{overrides[i]['ts'].strftime('%H:%M')}"
            )

    # 5. Room temp swings
    room_temps = [(r["ts"], r["room_temp_f"]) for r in readings
                  if r.get("room_temp_f") and r["room_temp_f"] > 0]
    if room_temps:
        rt_max = max(t for _, t in room_temps)
        rt_min = min(t for _, t in room_temps)
        if rt_max - rt_min > 5:
            problems.append(
                f"🌡️  Room temp swing: {rt_min:.1f}-{rt_max:.1f}°F "
                f"({rt_max - rt_min:.1f}°F range)"
            )

    return problems


# ── Display ──────────────────────────────────────────────────

W = 72  # output width


def header(text: str):
    print(f"\n{'─' * W}")
    print(f"  {text}")
    print(f"{'─' * W}")


def print_single_night(conn, night: date, zone: str, deep_dive: bool = False):
    summary = fetch_summary(conn, night, zone)
    if not summary:
        print(f"\n  No data found for {night}")
        return

    nightly = fetch_nightly_summary(conn, night)
    timeline = fetch_timeline(conn, night, zone)
    hourly = fetch_hourly_stability(conn, night, zone)
    room_setting = fetch_room_vs_setting(conn, night, zone)
    readings = fetch_raw_readings(conn, night, zone)

    # ── Header
    print(f"\n{'═' * W}")
    print(f"  PerfectlySnug Overnight Report — {night}")
    ver = summary.get("controller_version", "v3")
    print(f"  Controller: {ver}  |  Zone: {zone}")
    print(f"{'═' * W}")

    # ── Overview
    header("📊 Overview")
    dur = summary.get("duration_hours")
    dur_str = f"{float(dur):.1f}h" if dur else "N/A"
    print(f"  Duration:     {dur_str} ({summary['reading_count']} readings)")
    print(f"  Settings:     avg {summary['avg_setting']}, "
          f"range [{summary['min_setting']}, {summary['max_setting']}]")
    print(f"  Overrides:    {summary['override_count']}")
    print(f"  Phases seen:  {', '.join(summary['phases_seen'] or [])}")
    print(f"  Actions seen: {', '.join(summary['actions_seen'] or [])}")

    # ── Sleep quality from nightly_summary
    if nightly and nightly.get("total_sleep_min"):
        header("😴 Sleep Quality (Apple Health)")
        total = nightly["total_sleep_min"]
        print(f"  Total sleep:  {total:.0f} min ({total/60:.1f}h)")
        for stage, key in [("Deep", "deep_sleep_min"), ("REM", "rem_sleep_min"),
                           ("Core", "core_sleep_min"), ("Awake", "awake_min")]:
            val = nightly.get(key)
            if val:
                pct = val / total * 100 if total > 0 else 0
                bar = "█" * int(pct / 2)
                print(f"  {stage:10s}  {val:5.0f}min  ({pct:4.1f}%) {bar}")

    # ── Body temp
    header("🌡️  Body Temperature")
    print(f"  Average:  {summary['body_avg']}°F")
    print(f"  Range:    {summary['body_min']}–{summary['body_max']}°F")
    print(f"  Stdev:    {summary['body_stdev']}°F "
          f"({'stable' if float(summary['body_stdev'] or 99) < 2 else 'variable'})")

    # ── Room temp
    header("🏠 Room Temperature")
    print(f"  Average:  {summary['room_avg']}°F")
    print(f"  Range:    {summary['room_min']}–{summary['room_max']}°F")
    print(f"  Stdev:    {summary['room_stdev']}°F")

    # ── Hourly body temp stability
    if hourly:
        header("📈 Hourly Body Temp Stability")
        print(f"  {'Hour':>8s}  {'Avg':>6s}  {'Stdev':>6s}  {'Rating':>12s}  {'Setting':>7s}  {'Room':>6s}")
        for h in hourly:
            avg = f"{h['avg_body_f']:.1f}" if h["avg_body_f"] else "  —"
            sd = f"{h['stdev_body_f']:.2f}" if h["stdev_body_f"] else "  —"
            print(f"  {h['hour_label']:>8s}  {avg:>6s}  {sd:>6s}  "
                  f"{h['stability_rating']:>12s}  {str(h['avg_setting']):>7s}  "
                  f"{str(h['avg_room_f'] or '—'):>6s}")

    # ── Setting timeline
    if timeline:
        header("🔧 Setting Changes")
        print(f"  {'Time':>8s}  {'Setting':>7s}  {'Δ':>3s}  {'Source':>16s}  "
              f"{'Phase':>8s}  {'Body°F':>6s}  {'Room°F':>6s}")
        for t in timeline:
            ts_str = t["ts"].strftime("%H:%M")
            delta = t.get("setting_delta")
            delta_str = f"{delta:+d}" if delta else " —"
            body = f"{t['body_avg_f']:.1f}" if t.get("body_avg_f") else "  —"
            room = f"{t['room_temp_f']:.1f}" if t.get("room_temp_f") else "  —"
            print(f"  {ts_str:>8s}  {str(t.get('setting', '—')):>7s}  "
                  f"{delta_str:>3s}  {t['change_source']:>16s}  "
                  f"{str(t.get('phase', '')):>8s}  {body:>6s}  {room:>6s}")

    # ── Room temp vs setting (ambient compensation)
    if room_setting:
        header("🌡️  Room Temp vs Setting (Ambient Compensation)")
        print(f"  {'Room Range':>12s}  {'Readings':>8s}  {'Avg Set':>7s}  "
              f"{'Avg Eff':>7s}  {'Body°F':>6s}  {'Quality':>16s}")
        for rs in room_setting:
            print(f"  {rs['room_temp_range']:>12s}  {rs['readings']:>8d}  "
                  f"{str(rs['avg_setting']):>7s}  {str(rs['avg_effective']):>7s}  "
                  f"{str(rs['avg_body_f'] or '—'):>6s}  {rs['compensation_quality']:>16s}")

    # ── Problems
    problems = detect_problems(readings)
    header("🔍 Problem Detection")
    if problems:
        for p in problems:
            print(f"  {p}")
    else:
        print("  ✅ No problems detected")

    if deep_dive:
        summarize_deep_dive(conn, night, zone)

    print(f"\n{'═' * W}\n")


def print_comparison(conn, num_nights: int, zone: str):
    rows = fetch_comparison(conn, num_nights, zone)
    if not rows:
        print("\n  No data found for comparison")
        return

    print(f"\n{'═' * W}")
    print(f"  PerfectlySnug — {len(rows)}-Night Comparison  |  Zone: {zone}")
    print(f"{'═' * W}")

    header("📊 Night-by-Night Summary")
    print(f"  {'Date':>10s}  {'Dur':>5s}  {'Body':>5s}  {'σ':>5s}  "
          f"{'Room':>5s}  {'Set':>4s}  {'OVR':>3s}  {'#':>4s}  {'Phases'}")
    print(f"  {'':>10s}  {'(h)':>5s}  {'°F':>5s}  {'°F':>5s}  "
          f"{'°F':>5s}  {'avg':>4s}  {'':>3s}  {'rdgs':>4s}")
    print(f"  {'─' * 64}")

    for r in rows:
        dur = float(r["duration_hours"]) if r["duration_hours"] else 0
        phases = ", ".join(r["phases_seen"] or [])[:20]
        print(
            f"  {str(r['night_date']):>10s}  {dur:5.1f}  "
            f"{str(r['body_avg']):>5s}  {str(r['body_stdev']):>5s}  "
            f"{str(r['room_avg']):>5s}  {str(r['avg_setting']):>4s}  "
            f"{r['override_count']:>3d}  {r['reading_count']:>4d}  {phases}"
        )

    # Trends
    if len(rows) >= 2:
        header("📈 Trends")
        stdevs = [float(r["body_stdev"]) for r in rows if r.get("body_stdev")]
        if stdevs:
            trend = stdevs[0] - stdevs[-1]
            direction = "improving ↗" if trend < 0 else "degrading ↘" if trend > 0 else "stable →"
            print(f"  Body temp stability: {direction} "
                  f"(σ {stdevs[-1]:.2f} → {stdevs[0]:.2f})")

        overrides = [r["override_count"] for r in rows]
        avg_ovr = sum(overrides) / len(overrides)
        print(f"  Avg overrides/night: {avg_ovr:.1f}")

        rooms = [float(r["room_avg"]) for r in rows if r.get("room_avg")]
        if rooms:
            print(f"  Room temp range: {min(rooms):.1f}–{max(rooms):.1f}°F across nights")

    print(f"\n{'═' * W}\n")


# ── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PerfectlySnug overnight sleep analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # analyze last night
  %(prog)s --date 2026-04-07  # specific night
  %(prog)s --compare 7        # compare last 7 nights
  %(prog)s --zone right       # right side of bed
  %(prog)s --date 2026-04-15 --deep-dive
""",
    )
    parser.add_argument("--date", "-d", type=str, default=None,
                        help="Night date (YYYY-MM-DD). Default: last night")
    parser.add_argument("--compare", "-c", type=int, default=None,
                        help="Compare last N nights")
    parser.add_argument("--zone", "-z", type=str, default=DEFAULT_ZONE,
                        help="Bed zone (left/right). Default: left")
    parser.add_argument("--deep-dive", action="store_true",
                        help="Add v5-specific tuning, blower, and learning analysis")
    args = parser.parse_args()

    zone = args.zone

    try:
        conn = get_conn()
    except Exception as e:
        print(f"❌ Cannot connect to Postgres: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.compare:
            print_comparison(conn, args.compare, zone)
        else:
            night = date.fromisoformat(args.date) if args.date else last_night_date()
            print_single_night(conn, night, zone, deep_dive=args.deep_dive)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
