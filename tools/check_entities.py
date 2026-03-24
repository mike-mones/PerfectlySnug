#!/usr/bin/env python3
"""Quick check of key entities via Nabu Casa."""
import os, urllib.request, json, sys

HA_URL = os.environ.get("HA_URL", "https://71gpwlkh7etf6xbve4xol5rdbfrovjt6.ui.nabu.casa")
TOKEN = os.environ.get("HA_TOKEN")
if not TOKEN:
    sys.exit("Set HA_TOKEN env var")

entities = [
    "sensor.smart_topper_left_side_run_progress",
    "switch.smart_topper_left_side_running",
    "sensor.smart_topper_left_side_body_temperature_right",
    "sensor.smart_topper_left_side_body_temperature_center",
    "sensor.smart_topper_left_side_body_temperature_left",
    "sensor.smart_topper_left_side_ambient_temperature",
]

for eid in entities:
    url = f"{HA_URL}/api/states/{eid}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            state = data.get("state", "?")
            changed = data.get("last_changed", "?")[:19]
            print(f"{eid.split('.')[-1]:45s} = {state:>10s}  (changed: {changed})")
    except Exception as e:
        print(f"{eid.split('.')[-1]:45s} = ERROR: {e}")
