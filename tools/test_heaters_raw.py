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

print("Heater Values:")
for s in ["sensor.smart_topper_left_side_heater_foot_raw", "sensor.smart_topper_left_side_heater_head_raw"]:
    history = ha_history(s)
    events = [r for r in history if r.get("state") not in ('unavailable','unknown','')]
    print(f"\n{s} ({len(events)} events):")
    for i in range(0, max(1, len(events)), max(1, len(events)//10)):
        if i >= len(events): continue
        r = events[i]
        try:
            raw = float(r.get("state"))
            c = (raw - 32768) / 100
            f = c * 9/5 + 32
            print(f"  {r.get('last_changed')[:19]} = Raw: {raw}, ~{f:.1f}°F")
        except: pass

for s in ["sensor.smart_topper_right_side_heater_foot_raw", "sensor.smart_topper_right_side_heater_head_raw", "sensor.smart_topper_right_side_ambient_temperature"]:
    history = ha_history(s)
    events = [r for r in history if r.get("state") not in ('unavailable','unknown','')]
    print(f"\n{s} ({len(events)} events):")
    if "raw" in s:
        for i in range(0, max(1, len(events)), max(1, len(events)//10)):
            if i >= len(events): continue
            r = events[i]
            try:
                raw = float(r.get("state"))
                c = (raw - 32768) / 100
                f = c * 9/5 + 32
                print(f"  {r.get('last_changed')[:19]} = Raw: {raw}, ~{f:.1f}°F")
            except: pass
    else:
        nums = [float(e.get("state")) for e in events if e.get("state").replace('.','',1).isdigit()]
        if nums:
             print(f" Max: {max(nums)}, Min: {min(nums)}")
