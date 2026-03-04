#!/usr/bin/env python3
"""Fast, careful API probe — saves results to file. One request at a time, generous timeouts."""
import http.client
import json
import time
from pathlib import Path

ZONES = {"zone1": "192.168.0.159", "zone2": "192.168.0.211"}
OUT = Path(__file__).parent.parent / "docs" / "probe_results.json"

# Only the most likely paths — keep it short to avoid overwhelming the slow server
PATHS = [
    "/", "/info2.html", "/info.css", "/leftchev.svg",
    "/status", "/state", "/get", "/set",
    "/api", "/data", "/config", "/settings",
    "/temperature", "/sensors", "/version",
    "/ws", "/rpc", "/command",
    "/getState", "/getStatus", "/getSettings",
    "/uploadDatalog", "/diagnostics",
]

results = {}

for zone, ip in ZONES.items():
    print(f"\n=== {zone} ({ip}) ===")
    results[zone] = {"ip": ip, "endpoints": []}
    
    for path in PATHS:
        try:
            conn = http.client.HTTPConnection(ip, 80, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read(1024).decode("utf-8", errors="replace")
            headers = dict(resp.getheaders())
            
            entry = {
                "path": path,
                "status": resp.status,
                "reason": resp.reason,
                "content_type": headers.get("Content-Type", ""),
                "content_disp": headers.get("Content-Disposition", ""),
                "content_length": headers.get("Content-Length", ""),
                "body_preview": body[:200],
            }
            results[zone]["endpoints"].append(entry)
            
            # Identify which filename it maps to
            fname = headers.get("Content-Disposition", "")
            status_str = f"{resp.status} {resp.reason}"
            if resp.status == 200:
                print(f"  {path:25s} -> {status_str:20s} [{fname}]")
            else:
                print(f"  {path:25s} -> {status_str}")
            
            conn.close()
            time.sleep(0.5)  # Be gentle with the tiny server
        except Exception as e:
            print(f"  {path:25s} -> ERROR: {e}")
            results[zone]["endpoints"].append({"path": path, "error": str(e)})
    
    # Only probe zone1 fully, zone2 just check root to confirm it's same
    if zone == "zone2":
        break

with open(OUT, "w") as f:
    json.dump(results, f, indent=2)

print(f"\nResults saved to {OUT}")
