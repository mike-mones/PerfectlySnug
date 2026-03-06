#!/usr/bin/env python3
"""Check recent HA logbook for Apple Health entries."""
import json, os, sys
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

HA_URL = "http://192.168.0.106:8123"
TOKEN = os.environ.get("HA_TOKEN", "")
if not TOKEN:
    print("Set HA_TOKEN env var first")
    sys.exit(1)

start = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
req = Request(f"{HA_URL}/api/logbook/{start}", headers={"Authorization": f"Bearer {TOKEN}"})
with urlopen(req, timeout=15) as resp:
    data = json.loads(resp.read())

print(f"Last 30 min: {len(data)} logbook entries\n")
for e in data[-30:]:
    when = e.get("when", "")[:19]
    name = e.get("name", "")
    msg = e.get("message", "")
    domain = e.get("domain", "")
    print(f"  {when}  [{domain:15s}] {name}: {msg}")
