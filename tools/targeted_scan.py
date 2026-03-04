#!/usr/bin/env python3
"""Targeted scan of the suspected Perfectly Snug devices."""

import socket
import http.client
import ssl
import json

# The two 00:4B:12 devices and the 04:2E:C1 device
targets = {
    '192.168.0.159': '00:4B:12:04:3C:48',
    '192.168.0.211': '00:4B:12:13:5C:34',
    '192.168.0.217': '04:2E:C1:13:6E:61',
}

# Wider port range for thorough scan
ports = {
    22: "SSH", 23: "Telnet", 53: "DNS",
    80: "HTTP", 81: "HTTP-alt", 443: "HTTPS",
    548: "AFP", 554: "RTSP",
    1080: "SOCKS", 1883: "MQTT",
    2000: "Custom", 2323: "Telnet-alt",
    3000: "HTTP-dev", 3001: "HTTP-dev",
    4443: "HTTPS-alt", 4567: "Custom",
    5000: "Flask/UPnP", 5353: "mDNS",
    5555: "ADB", 5683: "CoAP",
    6000: "X11", 6668: "Tuya", 6669: "Tuya",
    7681: "WebSocket", 8000: "HTTP-alt",
    8080: "HTTP-alt", 8081: "HTTP-alt",
    8443: "HTTPS-alt", 8883: "MQTT-TLS",
    8888: "HTTP-proxy", 9000: "Custom",
    9090: "Custom", 9443: "HTTPS-alt",
    49152: "UPnP", 49153: "UPnP", 49154: "UPnP",
    # Common ESP-IDF / Arduino custom ports
    3232: "ESP-OTA", 4040: "Custom", 4848: "Custom",
    # Common for embedded web servers
    8008: "HTTP-alt", 8181: "HTTP-alt",
    # Particle Cloud
    5683: "CoAP",
}

# Also scan a low range sweep
extra_ports = list(range(1, 100)) + list(range(1880, 1890)) + list(range(8000, 8100))
for p in extra_ports:
    if p not in ports:
        ports[p] = f"Port-{p}"

print("=" * 75)
print("  Targeted Scan of Suspected Smart Topper Devices")
print("=" * 75)
print()
print("SAFETY: This is a TCP connect scan — equivalent to opening a web browser")
print("and trying to visit a URL. Read-only, no data sent to device.")
print()

for ip, mac in targets.items():
    print(f"\n{'='*60}")
    print(f"  {ip} (MAC: {mac})")
    print(f"{'='*60}")
    open_ports = []
    for port in sorted(ports.keys()):
        name = ports[port]
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex((ip, port))
            if result == 0:
                open_ports.append((port, name))
                print(f"  Port {port:5d} ({name:12s}): OPEN")
            sock.close()
        except Exception:
            pass

    if not open_ports:
        print("  No open ports found in scan range")
        continue

    # Try HTTP on any open ports
    for port, name in open_ports:
        print(f"\n  --- Trying HTTP GET on {ip}:{port} ---")
        try:
            if port in (443, 8443, 9443):
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                conn = http.client.HTTPSConnection(ip, port, timeout=3, context=ctx)
            else:
                conn = http.client.HTTPConnection(ip, port, timeout=3)

            conn.request("GET", "/")
            resp = conn.getresponse()
            headers = dict(resp.getheaders())
            body = resp.read(4096).decode("utf-8", errors="replace")

            print(f"  Status: {resp.status} {resp.reason}")
            print(f"  Headers: {json.dumps(headers, indent=4)}")
            if body:
                print(f"  Body ({len(body)} bytes):")
                print(f"  {body[:1000]}")

            conn.close()
        except Exception as e:
            print(f"  HTTP failed: {e}")
            # Try raw socket read
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                sock.connect((ip, port))
                # Some protocols send a banner on connect
                sock.settimeout(2)
                try:
                    banner = sock.recv(1024)
                    if banner:
                        print(f"  Banner: {banner[:200]}")
                except socket.timeout:
                    print(f"  No banner (server waits for client to speak first)")
                sock.close()
            except Exception as e2:
                print(f"  Raw connect also failed: {e2}")

print("\n\nDone.")
