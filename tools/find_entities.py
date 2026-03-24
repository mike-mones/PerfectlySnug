#!/usr/bin/env python3
"""Search for body/temperature entities via HA API."""
import os, urllib.request, json, sys

HA_URL = os.environ.get("HA_URL", "https://71gpwlkh7etf6xbve4xol5rdbfrovjt6.ui.nabu.casa")
TOKEN = os.environ.get("HA_TOKEN")
if not TOKEN:
    sys.exit("Set HA_TOKEN env var")

url = f"{HA_URL}/api/states"
req = urllib.request.Request(url, headers={
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
})

with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())

# Find all topper-related entities
for entity in sorted(data, key=lambda e: e["entity_id"]):
    eid = entity["entity_id"]
    if "topper" in eid.lower() or "smart_topper" in eid.lower():
        state = entity.get("state", "?")
        print(f"{eid:55s} = {state}")
