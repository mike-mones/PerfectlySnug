#!/usr/bin/env python3
"""
Fetch the full HTML/JS content from the Perfectly Snug web interface
and analyze JavaScript for API endpoints and WebSocket connections.

This is a pure read-only GET request — same as opening in a browser.
NO sudo required.
"""

import http.client
import json
from pathlib import Path

ZONE_1 = "192.168.0.159"
ZONE_2 = "192.168.0.211"
OUTPUT_DIR = Path(__file__).parent.parent / "docs" / "web_dump"


def fetch_full_page(ip, path="/"):
    """Fetch a complete page from the device."""
    try:
        conn = http.client.HTTPConnection(ip, 80, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        headers = dict(resp.getheaders())
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return resp.status, headers, body
    except Exception as e:
        return None, None, str(e)


def fetch_raw_socket(ip, port=80):
    """Connect with raw socket to see if it speaks a custom protocol."""
    import socket
    import time

    results = {}

    # Test 1: Just connect and listen (maybe it sends something first)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect((ip, port))
        time.sleep(1)
        try:
            data = sock.recv(4096)
            results["passive_receive"] = data
        except socket.timeout:
            results["passive_receive"] = None
        sock.close()
    except Exception as e:
        results["passive_receive_error"] = str(e)

    # Test 2: Send a WebSocket upgrade request
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect((ip, port))

        ws_request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {ip}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        sock.sendall(ws_request.encode())
        time.sleep(1)
        data = sock.recv(4096)
        results["websocket_response"] = data
        sock.close()
    except Exception as e:
        results["websocket_error"] = str(e)

    # Test 3: Send a simple HTTP/1.0 request to different path patterns
    for path in ["/api", "/cmd", "/rpc", "/query", "/get", "/getstate", "/getall"]:
        try:
            conn = http.client.HTTPConnection(ip, 80, timeout=3)
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status != 500:
                results[f"GET_{path}"] = {"status": resp.status, "body": body[:500]}
            conn.close()
        except Exception as e:
            pass

    return results


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for name, ip in [("zone_1", ZONE_1), ("zone_2", ZONE_2)]:
        print(f"\n{'='*65}")
        print(f"  {name.upper()} — {ip}")
        print(f"{'='*65}")

        # Get the main page (full content)
        status, headers, body = fetch_full_page(ip, "/")
        if status == 200:
            print(f"\n  Root page: {len(body)} bytes")

            # Save full HTML
            html_file = OUTPUT_DIR / f"{name}_root.html"
            with open(html_file, "w") as f:
                f.write(body)
            print(f"  Saved: {html_file}")

            # Look for JavaScript references
            import re
            js_refs = re.findall(r'src=["\']([^"\']+\.js)["\']', body)
            css_refs = re.findall(r'href=["\']([^"\']+\.css)["\']', body)
            img_refs = re.findall(r'src=["\']([^"\']+\.(svg|png|jpg))["\']', body)

            print(f"  JS files referenced: {js_refs}")
            print(f"  CSS files referenced: {css_refs}")
            print(f"  Images referenced: {[i[0] for i in img_refs]}")

            # Fetch all JS files
            for js in js_refs:
                print(f"\n  Fetching JS: {js}")
                s, h, b = fetch_full_page(ip, f"/{js}" if not js.startswith("/") else js)
                if s == 200:
                    js_file = OUTPUT_DIR / f"{name}_{js.replace('/', '_')}"
                    with open(js_file, "w") as f:
                        f.write(b)
                    print(f"    Saved: {js_file} ({len(b)} bytes)")

                    # Analyze JS for API calls
                    ws_patterns = re.findall(r'(?:WebSocket|ws://|wss://|\.send\(|\.onmessage|fetch\(|XMLHttpRequest|\.open\()', b)
                    url_patterns = re.findall(r'["\'](?:https?://|/)[^\s"\']+["\']', b)
                    print(f"    WebSocket/fetch refs: {ws_patterns[:10]}")
                    print(f"    URL patterns: {url_patterns[:10]}")
                else:
                    print(f"    Status {s}")

            # Look for inline JavaScript
            inline_js = re.findall(r'<script[^>]*>(.*?)</script>', body, re.DOTALL)
            if inline_js:
                print(f"\n  Found {len(inline_js)} inline script blocks")
                for i, js_block in enumerate(inline_js):
                    if js_block.strip():
                        js_file = OUTPUT_DIR / f"{name}_inline_{i}.js"
                        with open(js_file, "w") as f:
                            f.write(js_block)
                        print(f"    Block {i}: {len(js_block)} chars")
                        # Show relevant parts
                        if len(js_block) < 5000:
                            print(f"    Content:\n{js_block[:2000]}")
                        else:
                            # Look for key patterns
                            for pattern in ["WebSocket", "fetch", "XMLHttp", "send",
                                          "temperature", "temp", "heat", "cool",
                                          "sensor", "body", "api", "mqtt"]:
                                matches = [m.start() for m in re.finditer(pattern, js_block, re.IGNORECASE)]
                                if matches:
                                    print(f"    '{pattern}' found at positions: {matches}")
                                    for pos in matches[:3]:
                                        context = js_block[max(0, pos-50):pos+100]
                                        print(f"      ...{context}...")

        # Test raw socket / WebSocket / custom protocol
        print(f"\n  Testing raw socket & protocols...")
        raw_results = fetch_raw_socket(ip)

        for key, val in raw_results.items():
            if val is not None:
                if isinstance(val, bytes):
                    print(f"  {key}: {val[:200]}")
                    hex_str = val[:100].hex()
                    print(f"    Hex: {hex_str}")
                elif isinstance(val, dict):
                    print(f"  {key}: status={val.get('status')}, body={val.get('body', '')[:200]}")
                else:
                    print(f"  {key}: {str(val)[:200]}")

    # Save all results
    print(f"\n\n  All files saved in: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
