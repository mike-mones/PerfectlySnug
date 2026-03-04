#!/usr/bin/env python3
"""
HTTP proxy that logs all traffic passing through it.
Set your iPhone's WiFi proxy to point to your Mac's IP and this port.

This is a transparent logging proxy — it forwards everything unchanged,
just records what passes through.

Usage:
    python3 tools/http_proxy.py

Then on iPhone:
    Settings → Wi-Fi → tap (i) on your network → Configure Proxy → Manual
    Server: 192.168.0.38  (your Mac's IP)
    Port: 8888
"""
import http.server
import http.client
import socketserver
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

LOG_DIR = Path(__file__).parent.parent / "docs" / "proxy_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TOPPER_IPS = {"192.168.0.159", "192.168.0.211"}
PORT = 8888

log_entries = []
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = LOG_DIR / f"proxy_{ts}.json"
raw_log = LOG_DIR / f"proxy_{ts}.raw.txt"


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def _proxy(self, method):
        url = self.path
        parsed = urlparse(url)
        host = parsed.hostname or self.headers.get("Host", "")
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        # Read request body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        is_topper = host in TOPPER_IPS or any(ip in url for ip in TOPPER_IPS)

        # Log everything for topper traffic
        req_headers = dict(self.headers)
        entry = {
            "time": datetime.now().isoformat(),
            "method": method,
            "url": url,
            "host": host,
            "port": port,
            "path": path,
            "is_topper": is_topper,
            "request_headers": req_headers,
            "request_body": body.decode("utf-8", errors="replace") if body else "",
        }

        if is_topper:
            print(f"\n{'='*60}")
            print(f"  *** TOPPER TRAFFIC ***")
            print(f"  {method} {url}")
            print(f"  Headers: {json.dumps(req_headers, indent=2)}")
            if body:
                print(f"  Body: {body[:500]}")

        # Forward the request
        try:
            conn = http.client.HTTPConnection(host, port, timeout=10)
            conn.request(method, path, body=body, headers=req_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            resp_headers = dict(resp.getheaders())

            entry["response_status"] = resp.status
            entry["response_reason"] = resp.reason
            entry["response_headers"] = resp_headers
            entry["response_body"] = resp_body.decode("utf-8", errors="replace")[:2000]

            if is_topper:
                print(f"  Response: {resp.status} {resp.reason}")
                print(f"  Resp Headers: {json.dumps(resp_headers, indent=2)}")
                print(f"  Resp Body: {resp_body[:500]}")
                print(f"{'='*60}")
                log_entries.append(entry)
                # Save incrementally
                with open(log_file, "w") as f:
                    json.dump(log_entries, f, indent=2)

            # Send response back to client
            self.send_response(resp.status)
            for k, v in resp_headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(resp_body)
            conn.close()

        except Exception as e:
            entry["error"] = str(e)
            if is_topper:
                print(f"  ERROR: {e}")
                log_entries.append(entry)
                with open(log_file, "w") as f:
                    json.dump(log_entries, f, indent=2)
            self.send_error(502, f"Proxy Error: {e}")

        # Log raw to text file
        with open(raw_log, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"{datetime.now().isoformat()} {method} {url}\n")
            f.write(f"Topper: {is_topper}\n")
            if body:
                f.write(f"Body: {body[:500]}\n")
            if "response_status" in entry:
                f.write(f"Response: {entry['response_status']}\n")
                f.write(f"Resp Body: {entry.get('response_body', '')[:500]}\n")

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_PUT(self):
        self._proxy("PUT")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_PATCH(self):
        self._proxy("PATCH")

    def do_OPTIONS(self):
        self._proxy("OPTIONS")

    def do_CONNECT(self):
        """Handle HTTPS CONNECT tunneling — just pass through."""
        host_port = self.path.split(":")
        host = host_port[0]
        port = int(host_port[1]) if len(host_port) > 1 else 443

        is_topper = host in TOPPER_IPS
        if is_topper:
            print(f"\n  *** TOPPER CONNECT: {self.path} ***")

        try:
            import socket
            remote = socket.create_connection((host, port), timeout=10)
            self.send_response(200, "Connection established")
            self.end_headers()

            import select
            sockets = [self.connection, remote]
            while True:
                readable, _, errors = select.select(sockets, [], sockets, 30)
                if errors:
                    break
                for s in readable:
                    data = s.recv(8192)
                    if not data:
                        remote.close()
                        return
                    out = remote if s is self.connection else self.connection
                    if is_topper:
                        with open(raw_log, "a") as f:
                            f.write(f"\nCONNECT tunnel data ({len(data)} bytes): {data[:200]}\n")
                    out.sendall(data)
        except Exception as e:
            if is_topper:
                print(f"  CONNECT error: {e}")

    def log_message(self, format, *args):
        # Suppress default logging for non-topper traffic
        pass


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    print("=" * 60)
    print("  Perfectly Snug — HTTP Proxy Logger")
    print("=" * 60)
    print()
    print("  On your iPhone:")
    print("    Settings → Wi-Fi → tap (i) → Configure Proxy → Manual")
    print("    Server: 192.168.0.38")
    print(f"    Port:   {PORT}")
    print()
    print("  Then open the Perfectly Snug app and use it normally.")
    print("  All topper traffic will be logged here.")
    print()
    print(f"  Log file: {log_file}")
    print(f"  Raw log:  {raw_log}")
    print()
    print(f"  Listening on 0.0.0.0:{PORT}...")
    print("  Press Ctrl+C to stop.\n")

    server = ThreadedServer(("0.0.0.0", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n\nStopped. {len(log_entries)} topper requests logged.")
        print(f"  Log: {log_file}")
        print(f"  Raw: {raw_log}")
