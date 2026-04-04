#!/usr/bin/env python3
"""Check what the PerfectlySnug integration did last night."""
import json
import os
import sys
from urllib.request import Request, urlopen
from datetime import datetime, timedelta, timezone

HA_URL = "http://192.168.0.106:8123"
TOKEN = os.environ.get("HA_TOKEN", "")
if not TOKEN:
    print("Set HA_TOKEN environment variable first: export HA_TOKEN='your_token'")
    sys.exit(1)
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def ha_get(path):
    url = f"{HA_URL}{path}"
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def ha_history(entity_id, hours=14):
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    path = f"/api/history/period/{start_str}?filter_entity_id={entity_id}&end_time={end_str}&no_attributes&minimal_response"
    data = ha_get(path)
    if data and len(data) > 0:
        return data[0]
    return []


def hourly_summary(vals):
    hourly = {}
    for ts, v in vals:
        hr = ts[11:13]
        if hr not in hourly:
            hourly[hr] = []
        hourly[hr].append(v)
    for hr in sorted(hourly.keys()):
        avg = sum(hourly[hr]) / len(hourly[hr])
        mn = min(hourly[hr])
        mx = max(hourly[hr])
        print(f"      {hr}:00  avg={avg:.1f}  min={mn:.1f}  max={mx:.1f}  ({len(hourly[hr])} pts)")


def parse_vals(history):
    vals = []
    for h in history:
        try:
            vals.append((h.get("last_changed", "")[:19], float(h["state"])))
        except (ValueError, KeyError):
            pass
    return vals


# =====================================================
# 1. SETTINGS CHANGES
# =====================================================
print("=" * 70)
print("  SETTINGS CHANGES OVERNIGHT (controller adjustments)")
print("=" * 70)
for side in ["left"]:
    print(f"\n  --- {side.upper()} SIDE ---")
    for setting in ["bedtime_temperature", "sleep_temperature", "wake_temperature"]:
        eid = f"number.smart_topper_{side}_side_{setting}"
        history = ha_history(eid)
        changes = []
        prev = None
        for h in history:
            s = h.get("state", "?")
            if s != prev:
                changes.append((h.get("last_changed", "")[:19], s))
                prev = s
        print(f"\n  {setting}: ({len(changes)} changes)")
        for ts, val in changes:
            print(f"    {ts}  = {val}")

# =====================================================
# 2. BODY SENSORS
# =====================================================
print(f"\n{'=' * 70}")
print("  BODY SENSOR TIMELINE (occupancy / temperature)")
print("=" * 70)
for side in ["left"]:
    for sensor in ["body_sensor_center", "body_sensor_left", "body_sensor_right", "ambient_temperature"]:
        eid = f"sensor.smart_topper_{side}_side_{sensor}"
        history = ha_history(eid)
        vals = parse_vals(history)
        if vals:
            mn = min(vals, key=lambda x: x[1])
            mx = max(vals, key=lambda x: x[1])
            print(f"\n  {sensor}:")
            print(f"    Points: {len(vals)}, Min: {mn[1]:.1f}F at {mn[0]}, Max: {mx[1]:.1f}F at {mx[0]}")
            hourly_summary(vals)

# =====================================================
# 3. PID CONTROLLER
# =====================================================
print(f"\n{'=' * 70}")
print("  PID CONTROLLER OUTPUT")
print("=" * 70)
for sensor in ["pid_control_output", "pid_proportional_term", "pid_integral_term", "blower_output", "temperature_setpoint"]:
    eid = f"sensor.smart_topper_left_side_{sensor}"
    history = ha_history(eid)
    vals = parse_vals(history)
    if vals:
        mn = min(vals, key=lambda x: x[1])
        mx = max(vals, key=lambda x: x[1])
        print(f"\n  {sensor}:")
        print(f"    Points: {len(vals)}, Min: {mn[1]:.1f} at {mn[0]}, Max: {mx[1]:.1f} at {mx[0]}")
        hourly_summary(vals)

# =====================================================
# 4. HEATER OUTPUT
# =====================================================
print(f"\n{'=' * 70}")
print("  HEATER OUTPUT (heating vs cooling)")
print("=" * 70)
for sensor in ["heater_head_output", "heater_foot_output"]:
    eid = f"sensor.smart_topper_left_side_{sensor}"
    history = ha_history(eid)
    vals = parse_vals(history)
    if vals:
        mn = min(vals, key=lambda x: x[1])
        mx = max(vals, key=lambda x: x[1])
        print(f"\n  {sensor}:")
        print(f"    Points: {len(vals)}, Min: {mn[1]:.1f}% at {mn[0]}, Max: {mx[1]:.1f}% at {mx[0]}")
        hourly_summary(vals)

# =====================================================
# 5. RUNNING SWITCH (was it even on?)
# =====================================================
print(f"\n{'=' * 70}")
print("  RUNNING / SWITCH STATE")
print("=" * 70)
for side in ["left"]:
    for sw in ["running", "responsive_cooling", "schedule"]:
        eid = f"switch.smart_topper_{side}_side_{sw}"
        history = ha_history(eid)
        changes = []
        prev = None
        for h in history:
            s = h.get("state", "?")
            if s != prev:
                changes.append((h.get("last_changed", "")[:19], s))
                prev = s
        print(f"\n  {sw}: ({len(changes)} changes)")
        for ts, val in changes:
            print(f"    {ts}  = {val}")

# =====================================================
# 6. AppDaemon controller log check
# =====================================================
print(f"\n{'=' * 70}")
print("  APPDAEMON CONTROLLER ACTIVITY")
print("=" * 70)
# Check if the controller wrote any logs via input_text or logbook
for eid_pattern in ["input_text.sleep_controller", "input_text.topper_log"]:
    try:
        history = ha_history(eid_pattern)
        if history:
            print(f"\n  {eid_pattern}:")
            for h in history[-20:]:
                ts = h.get("last_changed", "")[:19]
                state = h.get("state", "?")
                print(f"    {ts}  {state}")
    except Exception:
        pass

print("\nDone.")
