#!/usr/bin/env python3
"""
Sleep Temperature Correlation Analysis
=======================================
Aligns topper sensor data (from HA) with sleep stage data (from Apple Health export)
to understand the relationship between temperature settings (-10 to +10) and body
temperature across sleep stages.

Outputs a per-stage analysis and recommended settings.
"""

import json
import urllib.request
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ── Config ──────────────────────────────────────────────────────────────────

HA_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI0ZDkxMTM5NjE2Yzk0OTAzOGYwZDllMzc1OGM1ODE1YiIsImlhdCI6MTc3Mjc0MjczMCwiZXhwIjoyMDg4MTAyNzMwfQ.u3MI5cK4KrnYkCZ_EODYS7LHX4BNkFbf74exeZdkQF0"
HA_URL = "http://192.168.0.106:8123"
HEALTH_EXPORT = "/Users/mikemones/Documents/GitHub/HomeAssistant/HealthAutoExport-2026-02-23-2026-03-09.json"
ZONE = "left"
EST = timezone(timedelta(hours=-5))
EDT = timezone(timedelta(hours=-4))

# ── HA API ──────────────────────────────────────────────────────────────────

def ha_history(entity, start_iso, end_iso):
    # URL-encode the + in timezone offsets
    start_enc = start_iso.replace("+", "%2B")
    end_enc = end_iso.replace("+", "%2B")
    url = (f"{HA_URL}/api/history/period/{start_enc}"
           f"?filter_entity_id={entity}&minimal_response&no_attributes"
           f"&end_time={end_enc}")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {HA_TOKEN}"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data[0] if data else []

def ha_state(entity):
    url = f"{HA_URL}/api/states/{entity}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {HA_TOKEN}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

# ── Parse timestamps ────────────────────────────────────────────────────────

def parse_health_ts(ts_str):
    """Parse Health Auto Export timestamp like '2026-03-08 00:03:27 -0500'"""
    # Format: YYYY-MM-DD HH:MM:SS -HHMM
    dt_part = ts_str[:19]
    tz_part = ts_str[20:]
    dt = datetime.strptime(dt_part, "%Y-%m-%d %H:%M:%S")
    tz_hours = int(tz_part[:3])
    tz_mins = int(tz_part[0] + tz_part[3:5])
    tz = timezone(timedelta(hours=tz_hours, minutes=tz_mins))
    return dt.replace(tzinfo=tz)

def parse_ha_ts(ts_str):
    """Parse HA ISO timestamp"""
    # Remove microseconds for simplicity
    if "." in ts_str:
        ts_str = ts_str[:ts_str.index(".")] + ts_str[ts_str.index("+"):]
    return datetime.fromisoformat(ts_str)

# ── Load sleep stages ───────────────────────────────────────────────────────

def load_sleep_stages():
    """Load sleep stage intervals from Health Auto Export JSON."""
    with open(HEALTH_EXPORT) as f:
        data = json.load(f)
    
    stages = []
    for m in data["data"]["metrics"]:
        if m["name"] == "sleep_analysis":
            for r in m["data"]:
                start = parse_health_ts(r["startDate"])
                end = parse_health_ts(r["endDate"])
                value = r["value"]  # Core, Deep, REM, Awake, Asleep
                # Normalize
                stage_map = {
                    "Core": "core", "Deep": "deep", "REM": "rem",
                    "Awake": "awake", "Asleep": "core",  # "Asleep" = generic, treat as core
                }
                stage = stage_map.get(value, "unknown")
                stages.append({"start": start, "end": end, "stage": stage})
    
    stages.sort(key=lambda s: s["start"])
    return stages

# ── Load topper runs from HA ───────────────────────────────────────────────

def load_topper_runs():
    """Find overnight runs from run_progress history."""
    records = ha_history(
        f"sensor.smart_topper_{ZONE}_side_run_progress",
        "2026-03-01T00:00:00-05:00",
        "2026-03-10T00:00:00-05:00"
    )
    
    runs = []
    run_start = None
    prev_p = 0
    for r in records:
        try:
            p = float(r["state"])
        except (ValueError, KeyError):
            continue
        t = parse_ha_ts(r["last_changed"])
        if p > 0 and prev_p == 0:
            run_start = t
        if p == 0 and prev_p > 0 and run_start:
            runs.append({"start": run_start, "end": t})
            run_start = None
        prev_p = p
    if run_start:
        runs.append({"start": run_start, "end": datetime.now(timezone.utc)})
    
    # Filter to real overnights (> 3 hours)
    runs = [r for r in runs if (r["end"] - r["start"]).total_seconds() > 3 * 3600]
    return runs

# ── Load body sensor + setting data for a run ──────────────────────────────

def load_run_data(run):
    """Load body sensor, preset offsets, and ambient temp for a single run."""
    s = run["start"].isoformat()
    e = run["end"].isoformat()
    
    body_l = ha_history(f"sensor.smart_topper_{ZONE}_side_body_sensor_left", s, e)
    body_c = ha_history(f"sensor.smart_topper_{ZONE}_side_body_sensor_center", s, e)
    body_r = ha_history(f"sensor.smart_topper_{ZONE}_side_body_sensor_right", s, e)
    ambient = ha_history(f"sensor.smart_topper_{ZONE}_side_ambient_temperature", s, e)
    bedtime_h = ha_history(f"number.smart_topper_{ZONE}_side_bedtime_temperature", s, e)
    sleep_h = ha_history(f"number.smart_topper_{ZONE}_side_sleep_temperature", s, e)
    wake_h = ha_history(f"number.smart_topper_{ZONE}_side_wake_temperature", s, e)
    
    # Get schedule durations
    start_len = float(ha_state(f"number.smart_topper_{ZONE}_side_start_length_minutes")["state"])
    wake_len = float(ha_state(f"number.smart_topper_{ZONE}_side_wake_length_minutes")["state"])
    
    return {
        "body_sensors": [body_l, body_c, body_r],
        "ambient": ambient,
        "presets": {"bedtime": bedtime_h, "sleep": sleep_h, "wake": wake_h},
        "start_len_min": start_len,
        "wake_len_min": wake_len,
    }

# ── Build minute-by-minute timeline ────────────────────────────────────────

def value_at_time(records, t, default=None):
    """Get the last known value at or before time t from HA history records."""
    val = default
    for r in records:
        try:
            rt = parse_ha_ts(r["last_changed"])
        except:
            continue
        if rt <= t:
            try:
                val = float(r["state"])
            except (ValueError, TypeError):
                pass
        else:
            break
    return val

def get_sleep_stage_at(stages, t):
    """Get sleep stage at a specific time."""
    for s in stages:
        if s["start"] <= t <= s["end"]:
            return s["stage"]
    return None

def build_timeline(run, run_data, sleep_stages):
    """Build minute-by-minute aligned data for a run."""
    timeline = []
    
    start = run["start"]
    end = run["end"]
    bed_end = start + timedelta(minutes=run_data["start_len_min"])
    wake_start = end - timedelta(minutes=run_data["wake_len_min"])
    
    # Flatten body sensors into time-value pairs
    body_all = []
    for sensor in run_data["body_sensors"]:
        for r in sensor:
            try:
                t = parse_ha_ts(r["last_changed"])
                v = float(r["state"])
                body_all.append((t, v))
            except:
                continue
    body_all.sort()
    
    # Walk minute by minute
    t = start
    while t <= end:
        # Body temp: average of sensors near this minute
        window_start = t - timedelta(seconds=150)
        window_end = t + timedelta(seconds=150)
        nearby = [v for bt, v in body_all if window_start <= bt <= window_end]
        body_temp = sum(nearby) / len(nearby) if nearby else None
        
        # Ambient temp
        ambient_temp = value_at_time(run_data["ambient"], t)
        
        # Active setting: which phase are we in?
        if t < bed_end:
            phase = "bedtime"
        elif t >= wake_start:
            phase = "wake"
        else:
            phase = "sleep"
        
        setting = value_at_time(run_data["presets"][phase], t)
        
        # Sleep stage from Apple Health
        stage = get_sleep_stage_at(sleep_stages, t)
        
        # Hours since run start
        hours_in = (t - start).total_seconds() / 3600
        
        if body_temp is not None and setting is not None:
            timeline.append({
                "time": t,
                "body_temp": body_temp,
                "ambient_temp": ambient_temp,
                "setting": setting,
                "phase": phase,
                "stage": stage,
                "hours_in": hours_in,
            })
        
        t += timedelta(minutes=1)
    
    return timeline

# ── Detect manual adjustments ──────────────────────────────────────────────

def detect_manual_adjustments(run, run_data):
    """Find mid-run setting changes that indicate manual intervention."""
    adjustments = []
    for phase_name, hist in run_data["presets"].items():
        prev_val = None
        for r in hist:
            try:
                t = parse_ha_ts(r["last_changed"])
                v = float(r["state"])
            except:
                continue
            if t < run["start"] or t > run["end"]:
                continue
            if prev_val is not None and v != prev_val:
                adjustments.append({
                    "time": t,
                    "phase": phase_name,
                    "from": prev_val,
                    "to": v,
                    "direction": "colder" if v < prev_val else "warmer",
                })
            prev_val = v
    adjustments.sort(key=lambda a: a["time"])
    return adjustments

# ── Analysis ────────────────────────────────────────────────────────────────

def analyze(all_timelines, all_adjustments):
    """Correlate settings, body temp, and sleep stages."""
    
    # Group data by sleep stage
    by_stage = defaultdict(list)
    for point in all_timelines:
        if point["stage"]:
            by_stage[point["stage"]].append(point)
    
    # Group data by setting value
    by_setting = defaultdict(list)
    for point in all_timelines:
        by_setting[int(point["setting"])].append(point)
    
    # Group by (stage, setting) pair
    by_stage_setting = defaultdict(list)
    for point in all_timelines:
        if point["stage"]:
            key = (point["stage"], int(point["setting"]))
            by_stage_setting[key].append(point)
    
    # Group by time-of-night bucket (2-hour windows)
    by_time_bucket = defaultdict(list)
    for point in all_timelines:
        bucket = int(point["hours_in"] // 2) * 2
        by_time_bucket[bucket].append(point)
    
    return by_stage, by_setting, by_stage_setting, by_time_bucket

def print_report(all_timelines, all_adjustments, by_stage, by_setting, by_stage_setting, by_time_bucket):
    """Print the correlation report."""
    
    print("=" * 70)
    print("SLEEP TEMPERATURE CORRELATION ANALYSIS")
    print("=" * 70)
    print(f"\nTotal data points: {len(all_timelines)} minutes")
    print(f"Points with sleep stage: {sum(1 for p in all_timelines if p['stage'])} minutes")
    
    # ── Per-stage summary ───────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("BODY TEMPERATURE BY SLEEP STAGE")
    print("─" * 70)
    for stage in ["deep", "core", "rem", "awake"]:
        points = by_stage.get(stage, [])
        if not points:
            print(f"\n  {stage.upper()}: No data")
            continue
        temps = [p["body_temp"] for p in points]
        settings = [p["setting"] for p in points]
        print(f"\n  {stage.upper()} ({len(points)} min):")
        print(f"    Body temp:  avg {sum(temps)/len(temps):.1f}°F  "
              f"min {min(temps):.1f}°F  max {max(temps):.1f}°F")
        print(f"    Setting:    avg {sum(settings)/len(settings):.1f}  "
              f"min {min(settings):.0f}  max {max(settings):.0f}")
    
    # ── Per-setting summary ─────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("BODY TEMPERATURE BY SETTING VALUE")
    print("─" * 70)
    for setting in sorted(by_setting.keys()):
        points = by_setting[setting]
        temps = [p["body_temp"] for p in points]
        print(f"  Setting {setting:+3d}: "
              f"avg {sum(temps)/len(temps):.1f}°F  "
              f"({len(points)} min)")
    
    # ── Cross-tabulation: stage × setting ───────────────────────────────
    print("\n" + "─" * 70)
    print("BODY TEMP BY (STAGE × SETTING) — avg °F")
    print("─" * 70)
    stages = ["deep", "core", "rem", "awake"]
    settings_seen = sorted(set(int(p["setting"]) for p in all_timelines))
    
    header = f"  {'Stage':<8}"
    for s in settings_seen:
        header += f"  {s:+3d} "
    print(header)
    print("  " + "-" * (8 + 6 * len(settings_seen)))
    
    for stage in stages:
        row = f"  {stage:<8}"
        for setting in settings_seen:
            points = by_stage_setting.get((stage, setting), [])
            if points:
                avg = sum(p["body_temp"] for p in points) / len(points)
                row += f" {avg:5.1f}"
            else:
                row += "    - "
        print(row)
    
    # ── Time of night analysis ──────────────────────────────────────────
    print("\n" + "─" * 70)
    print("BODY TEMPERATURE BY TIME OF NIGHT")
    print("─" * 70)
    for bucket in sorted(by_time_bucket.keys()):
        points = by_time_bucket[bucket]
        temps = [p["body_temp"] for p in points]
        settings = [p["setting"] for p in points]
        stages_here = [p["stage"] for p in points if p["stage"]]
        from collections import Counter
        top_stage = Counter(stages_here).most_common(1)
        top_str = top_stage[0][0] if top_stage else "?"
        print(f"  Hours {bucket}-{bucket+2}: "
              f"body {sum(temps)/len(temps):.1f}°F  "
              f"setting {sum(settings)/len(settings):.1f}  "
              f"dominant: {top_str}  "
              f"({len(points)} min)")
    
    # ── Manual adjustments ──────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("MANUAL ADJUSTMENTS (preference signals)")
    print("─" * 70)
    if not all_adjustments:
        print("  No mid-run adjustments detected.")
    else:
        colder_count = sum(1 for a in all_adjustments if a["direction"] == "colder")
        warmer_count = sum(1 for a in all_adjustments if a["direction"] == "warmer")
        print(f"  Total: {len(all_adjustments)} adjustments "
              f"({colder_count} colder, {warmer_count} warmer)")
        for a in all_adjustments:
            local_t = a["time"].astimezone(EST)
            print(f"    {local_t.strftime('%b %d %I:%M %p')}: "
                  f"{a['phase']} {a['from']:+.0f} → {a['to']:+.0f} ({a['direction']})")
    
    # ── Recommendation ──────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("RECOMMENDED SETTINGS")
    print("=" * 70)
    
    # Scientific baseline: cooler for deep sleep, warmer for REM
    # Weight by: (1) what setting produced best body temps during each stage,
    # (2) manual adjustment bias (if user keeps making it colder, go colder)
    
    # Calculate preferred setting per stage from data
    print("\n  Based on data + adjustment patterns:\n")
    
    adjustment_bias = 0
    if all_adjustments:
        adjustment_bias = sum(-1 if a["direction"] == "colder" else 1 for a in all_adjustments) / len(all_adjustments)
        print(f"  Adjustment bias: {adjustment_bias:+.2f} "
              f"({'tends colder' if adjustment_bias < 0 else 'tends warmer'})")
    
    for stage in ["deep", "core", "rem", "awake"]:
        points = by_stage.get(stage, [])
        if not points:
            print(f"  {stage.upper()}: Insufficient data")
            continue
        
        # Current average setting used during this stage
        avg_setting = sum(p["setting"] for p in points) / len(points)
        
        # Apply adjustment bias
        recommended = avg_setting + adjustment_bias
        # Clamp to -10..+10
        recommended = max(-10, min(10, round(recommended)))
        
        avg_temp = sum(p["body_temp"] for p in points) / len(points)
        print(f"  {stage.upper():6}: setting {recommended:+3d}  "
              f"(current avg: {avg_setting:+.1f}, "
              f"body temp at that: {avg_temp:.1f}°F)")
    
    # Time-of-night recommendations
    print("\n  Time-of-night schedule suggestion:")
    phase_map = {"bedtime": [], "sleep": [], "wake": []}
    for point in all_timelines:
        phase_map[point["phase"]].append(point["setting"])
    
    for phase in ["bedtime", "sleep", "wake"]:
        vals = phase_map[phase]
        if vals:
            avg = sum(vals) / len(vals)
            recommended = max(-10, min(10, round(avg + adjustment_bias)))
            print(f"    {phase:8}: {recommended:+3d}  (current avg: {avg:+.1f})")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("Loading sleep stages from Apple Health export...")
    sleep_stages = load_sleep_stages()
    print(f"  {len(sleep_stages)} stage intervals loaded")
    
    print("\nLoading topper runs from HA...")
    runs = load_topper_runs()
    print(f"  {len(runs)} overnight runs found")
    
    all_timelines = []
    all_adjustments = []
    
    for i, run in enumerate(runs):
        local_start = run["start"].astimezone(EST)
        duration = (run["end"] - run["start"]).total_seconds() / 3600
        print(f"\n  Run {i+1}: {local_start.strftime('%b %d %I:%M %p')} ({duration:.1f}h)")
        
        print("    Fetching sensor data...")
        run_data = load_run_data(run)
        
        print("    Building timeline...")
        timeline = build_timeline(run, run_data, sleep_stages)
        with_stage = sum(1 for p in timeline if p["stage"])
        print(f"    {len(timeline)} minutes, {with_stage} with sleep stage data")
        
        adjustments = detect_manual_adjustments(run, run_data)
        if adjustments:
            print(f"    {len(adjustments)} manual adjustments detected")
        
        all_timelines.extend(timeline)
        all_adjustments.extend(adjustments)
    
    print(f"\n{'='*70}")
    print(f"Total: {len(all_timelines)} data points across {len(runs)} nights")
    print(f"Manual adjustments: {len(all_adjustments)}")
    
    by_stage, by_setting, by_stage_setting, by_time_bucket = analyze(all_timelines, all_adjustments)
    print_report(all_timelines, all_adjustments, by_stage, by_setting, by_stage_setting, by_time_bucket)

if __name__ == "__main__":
    main()
