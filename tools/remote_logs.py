#!/usr/bin/env python3
"""Fetch AppDaemon logs via Nabu Casa Supervisor API."""
import os, urllib.request, json, sys

HA_URL = os.environ.get("HA_URL", "https://71gpwlkh7etf6xbve4xol5rdbfrovjt6.ui.nabu.casa")
TOKEN = os.environ.get("HA_TOKEN")
if not TOKEN:
    sys.exit("Set HA_TOKEN env var")

ADDON = "a0d7b954_appdaemon"
url = f"{HA_URL}/api/hassio/addons/{ADDON}/logs"

req = urllib.request.Request(url, headers={
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "text/plain",
})

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        logs = resp.read().decode("utf-8", errors="replace")
        lines = logs.strip().split("\n")
        # Show last N lines
        n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
        for line in lines[-n:]:
            print(line)
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()}")
except Exception as e:
    print(f"Error: {e}")
