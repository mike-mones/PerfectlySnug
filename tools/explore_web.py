#!/usr/bin/env python3
"""
Explore the Perfectly Snug web server endpoints.

Both topper zones serve HTTP on port 80. This script reads
the available pages and endpoints — strictly GET requests only,
equivalent to opening pages in a web browser.

SAFETY: Only sends HTTP GET requests (read-only).
Does NOT send POST, PUT, DELETE or any modifying requests.
Does NOT click any buttons or trigger any actions.
"""

import http.client
import json
import sys

TOPPER_IPS = {
    "zone_1": "192.168.0.159",
    "zone_2": "192.168.0.211",
}

# Common web paths to probe (GET only)
PATHS = [
    "/",
    "/index.html",
    "/info.html",
    "/info2.html",
    "/info.css",
    "/status",
    "/api",
    "/api/status",
    "/api/info",
    "/api/temperature",
    "/api/config",
    "/api/settings",
    "/api/v1",
    "/api/v1/status",
    "/data",
    "/json",
    "/state",
    "/sensor",
    "/sensors",
    "/temperature",
    "/config",
    "/settings",
    "/health",
    "/version",
    "/firmware",
    "/debug",
    "/diag",
    "/diagnostics",
    "/system",
    "/device",
    "/about",
    "/wifi",
    "/network",
    "/schedule",
    "/mode",
    "/control",
    "/control.html",
    "/setup",
    "/setup.html",
    "/main.html",
    "/home.html",
    "/app",
    "/app.html",
    "/dashboard",
    "/leftchev.svg",
    "/manifest.json",
    "/favicon.ico",
    "/robots.txt",
    # Try some common patterns for embedded web servers
    "/cgi-bin/",
    "/generate_204",
    "/hotspot-detect.html",
    "/connecttest.txt",
    # MQTT/WebSocket endpoints
    "/ws",
    "/mqtt",
    "/websocket",
]


def explore_zone(name, ip):
    print(f"\n{'='*70}")
    print(f"  Exploring {name}: {ip}")
    print(f"{'='*70}")

    found_pages = []

    for path in PATHS:
        try:
            conn = http.client.HTTPConnection(ip, 80, timeout=3)
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read(8192).decode("utf-8", errors="replace")
            headers = dict(resp.getheaders())

            if resp.status == 200:
                content_type = headers.get("Content-Type", headers.get("content-type", ""))
                content_disp = headers.get("Content-Disposition", headers.get("content-disposition", ""))
                found_pages.append({
                    "path": path,
                    "status": resp.status,
                    "content_type": content_type,
                    "content_disposition": content_disp,
                    "content_length": len(body),
                    "headers": headers,
                    "body": body,
                })
                print(f"\n  [200] GET {path}")
                print(f"         Type: {content_type}")
                if content_disp:
                    print(f"         Disp: {content_disp}")
                print(f"         Size: {len(body)} bytes")

                # Show body for interesting content
                if "html" in content_type or "json" in content_type or "text" in content_type:
                    # Truncate very long bodies
                    display = body[:2000]
                    if len(body) > 2000:
                        display += f"\n... ({len(body) - 2000} more bytes)"
                    print(f"         Body:\n{display}")

            elif resp.status != 404:
                print(f"  [{resp.status}] GET {path} — {resp.reason}")

            conn.close()

        except Exception as e:
            # Silently skip connection errors
            pass

    return found_pages


def main():
    all_results = {}

    for name, ip in TOPPER_IPS.items():
        pages = explore_zone(name, ip)
        all_results[name] = {
            "ip": ip,
            "pages": pages,
        }

    # Save results
    with open("docs/web_exploration.json", "w") as f:
        json.dump(all_results, f, indent=2)

    # Summary
    print(f"\n\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    for name, data in all_results.items():
        print(f"\n  {name} ({data['ip']}):")
        for page in data["pages"]:
            disp = page.get("content_disposition", "")
            filename = ""
            if "filename=" in disp:
                filename = f" → {disp.split('filename=')[1].strip('\"')}"
            print(f"    {page['path']:30s} [{page['content_type']:20s}] {page['content_length']:5d} bytes{filename}")

    print(f"\n  Full results saved to: docs/web_exploration.json")


if __name__ == "__main__":
    main()
