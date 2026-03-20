import json, os
from urllib.request import Request, urlopen
from datetime import datetime, timedelta, timezone

HA_URL = "http://192.168.0.106:8123"
TOKEN = os.environ.get("HA_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

def ha_history(entity_id, hours=14):
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    path = f"/api/history/period/{start.isoformat().replace('+00:00','Z')}?filter_entity_id={entity_id}&end_time={end.isoformat().replace('+00:00','Z')}&minimal_response=true"
    req = Request(f"{HA_URL}{path}", headers=HEADERS)
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data[0] if data else []
    except:
        return []

entities = [
    "sensor.smart_topper_left_side_heater_head_raw",
    "sensor.smart_topper_left_side_heater_foot_raw",
    "sensor.smart_topper_left_side_heater_head_output",
    "sensor.smart_topper_left_side_heater_foot_output",
    "sensor.smart_topper_left_side_pid_control_output"
]

for s in entities:
    history = ha_history(s)
    events = [r.get("state") for r in history if r.get("state") not in ("unavailable", "unknown", "")]
    print(f"\n{s} ({len(events)} valid events):")
    if events:
        nums = []
        for e in events:
            try:
                nums.append(float(e))
            except: pass
        if nums:
            print(f"  Max val: {max(nums)}")
            print(f"  Min val: {min(nums)}")
