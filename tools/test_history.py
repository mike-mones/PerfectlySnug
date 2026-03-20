import json
import os
from urllib.request import Request, urlopen
from datetime import datetime, timedelta, timezone

HA_URL = "http://192.168.0.106:8123"
TOKEN = os.environ.get("HA_TOKEN", "")

HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def ha_history(entity_id, hours=14):
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    start_str = start.isoformat().replace("+00:00", "Z")
    end_str = end.isoformat().replace("+00:00", "Z")
    path = f"/api/history/period/{start_str}?filter_entity_id={entity_id}&end_time={end_str}&minimal_response=true"
    req = Request(f"{HA_URL}{path}", headers=HEADERS)
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            if data and len(data) > 0:
                return data[0]
    except Exception as e:
        print("Error fetching", entity_id, e)
    return []

SENSORS = [
    "number.smart_topper_left_side_bedtime_temperature",
    "number.smart_topper_left_side_sleep_temperature",
    "number.smart_topper_left_side_wake_temperature",
    "sensor.smart_topper_left_side_body_sensor_center",
    "sensor.smart_topper_left_side_heater_foot_temperature",
    "input_text.apple_health_sleep_stage"
]

results = {}
for s in SENSORS:
    results[s] = ha_history(s)

print("Fetched data. Generating timeline...")
events = []
for s, history in results.items():
    for row in history:
        # minimal_response keys are last_changed and state
        events.append({
            "time": row.get("last_changed", ""),
            "entity": s.split(".")[-1],
            "state": row.get("state", "")
        })

events.sort(key=lambda x: x["time"])
with open("/tmp/timeline.txt", "w") as f:
    for e in events:
        f.write(f"{e['time'][:19]} | {e['entity'][:30]:<30} | {e['state']}\n")

print("Done. Wrote to /tmp/timeline.txt")
