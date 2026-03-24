#!/usr/bin/env python3
"""Deploy controller to HA Green via Nabu Casa Supervisor API.

Uses the SSH add-on's stdin API to run commands remotely.
"""
import os, urllib.request, json, sys, time

HA_URL = os.environ.get("HA_URL", "https://71gpwlkh7etf6xbve4xol5rdbfrovjt6.ui.nabu.casa")
TOKEN = os.environ.get("HA_TOKEN")
if not TOKEN:
    sys.exit("Set HA_TOKEN env var")

ADDON = "a0d7b954_appdaemon"
REPO = "mike-mones/PerfectlySnug"
BRANCH = "main"
FILE = "appdaemon/sleep_controller_v2.py"
DEST = "/addon_configs/a0d7b954_appdaemon/apps/sleep_controller_v2.py"

def ha_api(path, method="GET", data=None):
    url = f"{HA_URL}{path}"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        ct = resp.headers.get("Content-Type", "")
        raw = resp.read()
        if "json" in ct:
            return json.loads(raw)
        return raw.decode("utf-8", errors="replace")

def ha_api_text(path):
    url = f"{HA_URL}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "text/plain",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")

# Step 1: Download from GitHub via HA's SSH add-on command API
# The Terminal & SSH add-on exposes a stdin API for running commands
SSH_ADDON = "a0d7b954_ssh"  # core_ssh or the terminal addon

# Actually, use the Supervisor API to run commands in the SSH addon
# But simpler: use the HA service 'shell_command' or 'hassio.addon_stdin'

# Simplest approach: use the Supervisor command API
# Download the file using the SSH add-on's command execution
print("Step 1: Downloading updated controller from GitHub...")

raw_url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{FILE}"

# Use hassio.addon_stdin to send a command to the SSH addon
# Actually, the SSH and Terminal addon doesn't have a stdin API.
# Let's use a different approach: the HA REST API to call a shell_command service

# Approach: Register a temporary shell_command, or use the command_line integration
# 
# Better approach: Use the Supervisor API's addon command endpoint
# POST /api/hassio/addons/{addon}/stdin with the command

# Actually the simplest: write a script to the SSH addon's share directory
# Or just use the HA API to create a one-shot automation

# Most reliable: Use the websocket-based terminal approach
# But we can't do websockets easily from a script.

# SIMPLEST: Just download the raw file content and POST it to the HA API
# to write it as a file. But HA doesn't have a file-write API for addon configs.

# The ACTUAL simplest approach that works:
# 1. Download the file locally
# 2. POST it using the Supervisor's addon file API

# Wait — we can use the Supervisor REST API to POST to the addon's /stdin
# The SSH addon supports stdin commands when configured with 'authorized_keys'

# Let's try the approach that's documented as working: use Supervisor addon_stdin
print(f"  URL: {raw_url}")

command = f'curl -sSL "{raw_url}" -o "{DEST}"\n'
try:
    result = ha_api(f"/api/hassio/addons/{SSH_ADDON}/stdin", method="POST", data=command)
    print(f"  stdin result: {result}")
except Exception as e:
    # SSH addon maybe not named that. Try alternatives
    print(f"  SSH addon stdin failed: {e}")
    print("  Trying alternative: core_ssh...")
    try:
        result = ha_api("/api/hassio/addons/core_ssh/stdin", method="POST", data=command)
        print(f"  stdin result: {result}")
    except Exception as e2:
        print(f"  core_ssh also failed: {e2}")
        print("\n  Falling back to manual approach:")
        print(f"  SSH into HA Green and run:")
        print(f'  curl -sSL "{raw_url}" -o "{DEST}"')
        print(f'  ha addons restart {ADDON}')
        sys.exit(1)

time.sleep(3)

# Step 2: Restart AppDaemon
print("\nStep 2: Restarting AppDaemon...")
try:
    result = ha_api(f"/api/hassio/addons/{ADDON}/restart", method="POST")
    print(f"  Restart result: {result}")
except Exception as e:
    print(f"  Restart failed: {e}")
    sys.exit(1)

# Step 3: Wait and check logs
print("\nStep 3: Waiting for startup...")
time.sleep(25)

logs = ha_api_text(f"/api/hassio/addons/{ADDON}/logs")
lines = logs.strip().split("\n")
for line in lines[-30:]:
    if "sleep_controller" in line or "ERROR" in line:
        print(f"  {line.strip()}")

print("\nDone!")
