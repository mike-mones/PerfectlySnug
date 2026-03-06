#!/usr/bin/env python3
"""Check what data the Apple Health webhook actually received."""
import json, os, sys
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

HA_URL = "http://192.168.0.106:8123"
TOKEN = os.environ.get("HA_TOKEN", "")
if not TOKEN:
    print("Set HA_TOKEN env var"); sys.exit(1)

# Check automation traces to see the actual trigger data
req = Request(
    f"{HA_URL}/api/config/automation/config/apple_health_import",
    headers={"Authorization": f"Bearer {TOKEN}"}
)
with urlopen(req, timeout=10) as resp:
    config = json.loads(resp.read())
print("Automation config OK\n")

# Get automation traces (shows actual trigger payloads)
try:
    req = Request(
        f"{HA_URL}/api/states/automation.apple_health_import",
        headers={"Authorization": f"Bearer {TOKEN}"}
    )
    with urlopen(req, timeout=10) as resp:
        state = json.loads(resp.read())
    print(f"Automation state: {state['state']}")
    print(f"Last triggered: {state['attributes'].get('last_triggered', 'never')}")
    print(f"Current: {state['attributes'].get('current', 0)}")
    print()
except Exception as e:
    print(f"Could not get automation state: {e}\n")

# The webhook doesn't store data persistently — we can only see it was triggered.
# To see the ACTUAL payload, let's update the automation to also store the raw JSON.
# For now, let's fire a test webhook and see what comes back
print("Sending test webhook to see format...")
import urllib.request
test_data = json.dumps({"test": True, "person": "debug"}).encode()
req = urllib.request.Request(
    f"{HA_URL}/api/webhook/apple_health_import",
    data=test_data,
    headers={"Content-Type": "application/json"},
    method="POST"
)
with urlopen(req, timeout=10) as resp:
    print(f"Test webhook response: {resp.status}")

print("\nTo see the actual Health Auto Export payload,")
print("check the app's export history on your iPhone.")
print("It should show what data range and metrics were sent.")
