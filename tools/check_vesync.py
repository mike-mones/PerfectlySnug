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
    except Exception as e:
        print("err:", e)
        return []

history = ha_history("sensor.superior_6000s_temperature")
events = [e.get("state") for e in history if e.get("state") not in ('unavailable', 'unknown', '')]
nums = [float(e) for e in events if e.replace('.','',1).isdigit()]

if nums:
    print(f"Superior 6000S Temperature Max: {max(nums)}, Min: {min(nums)}")
    print(f"Current count of events: {len(nums)}")
    print("\nTimeline:")
    for e in history:
        val = e.get("state")
        if val in ('unavailable', 'unknown', ''): continue
        print(f"  {e.get('last_changed')[:19]} = {val}")
else:
    print("No data found for Superior 6000S")
