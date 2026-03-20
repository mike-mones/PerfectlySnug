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
    path = f"/api/history/period/{start.isoformat().replace('+00:00','Z')}?filter_entity_id={entity_id}&end_time={end.isoformat().replace('+00:00','Z')}&minimal_response=true"
    req = Request(f"{HA_URL}{path}", headers=HEADERS)
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
        return data[0] if data else []

SENSORS = [
    "sensor.smart_topper_left_side_heater_foot_temperature",
    "sensor.smart_topper_left_side_heater_head_temperature",
    "sensor.smart_topper_left_side_blower_output",
    "switch.smart_topper_left_side_schedule",
    "number.smart_topper_left_side_run_progress",
    "sensor.smart_topper_left_side_body_sensor_center"
]

print("Fetching data...")
for s in SENSORS:
    history = ha_history(s)
    events = []
    for r in history:
        state = str(r.get('state', ''))
        if state not in ('unavailable', 'unknown', ''):
            events.append({"ts": r.get('last_changed'), "val": state})
            
    print(f"\n{s} ({len(events)} events):")
    if events:
        nums = []
        for e in events:
            try:
                nums.append(float(e['val']))
            except ValueError:
                pass
        print(f"  First: {events[0]['ts']} = {events[0]['val']}")
        print(f"  Last:  {events[-1]['ts']} = {events[-1]['val']}")
        if nums:
            print(f"  Min:   {min(nums)}")
            print(f"  Max:   {max(nums)}")
        else:
            print(f"  Values: {list(set([e['val'] for e in events]))}")

