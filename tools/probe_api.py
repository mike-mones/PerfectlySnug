#!/usr/bin/env python3
"""Probe topper HTTP endpoints to discover the API."""
import http.client

IP = "192.168.0.159"
PATHS = [
    "/api", "/api/status", "/api/v1/status", "/status", "/state",
    "/api/info", "/info", "/data", "/api/data", "/settings",
    "/api/settings", "/config", "/api/config", "/temperature",
    "/api/temperature", "/sensors", "/api/sensors", "/device",
    "/api/device", "/version", "/api/version", "/firmware",
    "/health", "/ping", "/alive", "/index.html", "/app",
    "/control", "/api/control", "/command", "/api/command",
    "/ws", "/websocket", "/socket", "/rpc", "/jsonrpc",
    "/get", "/getStatus", "/getSettings", "/getState",
    "/diag", "/diagnostics", "/debug",
    "/upload", "/uploadDatalog",
    "/info2.html", "/leftchev.svg",
]

print(f"Probing {IP} with {len(PATHS)} GET paths...")
print()
for path in PATHS:
    try:
        conn = http.client.HTTPConnection(IP, 80, timeout=2)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read(500)
        headers = dict(resp.getheaders())
        disp = headers.get("Content-Disposition", "")
        ct = headers.get("Content-Type", "")
        if resp.status != 500 or "json" in ct.lower():
            print(f"  {path:30s} -> {resp.status} {resp.reason}")
            print(f"    CT={ct}  Disp={disp}")
            print(f"    Body={body[:150]}")
            print()
        conn.close()
    except Exception as e:
        print(f"  {path:30s} -> ERROR: {e}")
