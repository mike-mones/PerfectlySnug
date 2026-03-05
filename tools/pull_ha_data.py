#!/usr/bin/env python3
"""Pull overnight sensor data from Home Assistant and analyze it."""
import json
import sys
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

HA_URL = "http://192.168.0.106:8123"
TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJlYjMwOGY0ZWJhYTM0NDk0YWNlMGYzMTcyYjllZTJhYyIsImlhdCI6MTc3MjcyNjU4NiwiZXhwIjoyMDg4MDg2NTg2fQ.l7pXoyVKAxpIH-ht5KNTS8X9y65eyHoZSQbJs7JzSlc"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}


def ha_get(path):
    req = Request(f"{HA_URL}{path}", headers=HEADERS)
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def ha_history(entity_id, hours=14):
    end = datetime.utcnow()
    start = end - timedelta(hours=hours)
    path = f"/api/history/period/{start.isoformat()}?filter_entity_id={entity_id}&end_time={end.isoformat()}&minimal_response&no_attributes"
    data = ha_get(path)
    if data and len(data) > 0:
        return data[0]
    return []


# First, find all perfectly_snug entities
print("Fetching entity list...")
states = ha_get("/api/states")
snug_entities = [s for s in states if "smart_topper" in s["entity_id"]]

print(f"\nFound {len(snug_entities)} Smart Topper entities:\n")
for e in sorted(snug_entities, key=lambda x: x["entity_id"]):
    print(f"  {e['entity_id']:60s}  state={e['state']}")

# Pull history for key sensors
print("\n\nPulling overnight history (last 14 hours)...\n")

key_entities = [e["entity_id"] for e in snug_entities if any(
    kw in e["entity_id"] for kw in ["body_sensor", "ambient", "heater_head", "heater_foot",
                                     "pid_", "blower", "heater_head_output", "heater_foot_output",
                                     "setpoint"]
)]

results = {}
for eid in sorted(key_entities):
    history = ha_history(eid)
    points = [(h["last_changed"], h["state"]) for h in history if h["state"] not in ("unknown", "unavailable")]
    results[eid] = points
    print(f"  {eid:60s}  {len(points):5d} data points")

# Save raw data
outfile = "docs/overnight_data.json"
with open(outfile, "w") as f:
    json.dump(results, f, indent=2)
print(f"\nRaw data saved to {outfile}")

# Summary stats
print("\n" + "=" * 70)
print("  OVERNIGHT SUMMARY")
print("=" * 70)
for eid, points in sorted(results.items()):
    if not points:
        continue
    vals = []
    for _, v in points:
        try:
            vals.append(float(v))
        except ValueError:
            pass
    if vals:
        label = eid.split(".")[-1].replace("smart_topper_", "").replace("_side_", " ")
        print(f"\n  {label}:")
        print(f"    Points: {len(vals)}")
        print(f"    Min:    {min(vals):.1f}")
        print(f"    Max:    {max(vals):.1f}")
        print(f"    Avg:    {sum(vals)/len(vals):.1f}")
        print(f"    Range:  {max(vals)-min(vals):.1f}")
