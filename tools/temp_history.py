#!/usr/bin/env python3
"""Show 12-hour history of topper sleep temp changes."""
import os
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HA_URL = os.environ["HA_URL"]
HA_TOKEN = os.environ["HA_TOKEN"]
ET = timezone(timedelta(hours=-4))

s = requests.Session()
s.headers.update({"Authorization": f"Bearer {HA_TOKEN}"})
retry = Retry(total=3, backoff_factor=1)
s.mount("https://", HTTPAdapter(max_retries=retry))

start = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
r = s.get(
    f"{HA_URL}/api/history/period/{start}",
    params={
        "filter_entity_id": "number.smart_topper_left_side_sleep_temperature",
        "minimal_response": "true",
    },
    timeout=30,
)
data = r.json()
if data and data[0]:
    print("24-hour sleep temp history:")
    for entry in data[0]:
        ts = datetime.fromisoformat(entry["last_changed"].replace("Z", "+00:00")).astimezone(ET)
        print(f"  {ts.strftime('%I:%M %p')} → sleep_temp = {entry['state']}")
else:
    print("No history")
