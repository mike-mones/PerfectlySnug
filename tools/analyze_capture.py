#!/usr/bin/env python3
"""
Perfectly Snug Smart Topper - Traffic Analysis

Analyzes captured traffic files to extract protocol details, endpoints,
and potential API structure.

Usage:
    python3 analyze_capture.py <text_capture_file>

This only reads previously captured data — no network access needed.
"""

import re
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def analyze_text_capture(filepath):
    """Analyze a text capture file from capture_traffic.py."""
    print("=" * 70)
    print("  Perfectly Snug - Traffic Analysis")
    print("=" * 70)
    print(f"\n  Analyzing: {filepath}\n")

    with open(filepath, "r") as f:
        content = f.read()

    lines = content.split("\n")

    # Extract source/destination pairs
    connections = Counter()
    ports_seen = Counter()
    protocols = Counter()
    http_requests = []
    http_responses = []
    json_payloads = []
    interesting_strings = []

    # Patterns
    ip_port_pattern = re.compile(r"(\d+\.\d+\.\d+\.\d+)\.(\d+)\s+>\s+(\d+\.\d+\.\d+\.\d+)\.(\d+)")
    http_req_pattern = re.compile(r"(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(\S+)\s+HTTP")
    http_resp_pattern = re.compile(r"HTTP/[\d.]+\s+(\d+)\s*(.*)")
    content_type_pattern = re.compile(r"Content-Type:\s*(\S+)", re.IGNORECASE)

    current_payload = []
    in_payload = False

    for i, line in enumerate(lines):
        # Skip comments
        if line.startswith("#"):
            continue

        # Connection tracking
        match = ip_port_pattern.search(line)
        if match:
            src_ip, src_port, dst_ip, dst_port = match.groups()
            connections[(src_ip, src_port, dst_ip, dst_port)] += 1
            ports_seen[int(src_port)] += 1
            ports_seen[int(dst_port)] += 1

            # Identify protocol by port
            port = int(dst_port)
            if port in (80, 8080, 3000):
                protocols["HTTP"] += 1
            elif port in (443, 8443):
                protocols["HTTPS"] += 1
            elif port in (1883,):
                protocols["MQTT"] += 1
            elif port in (8883,):
                protocols["MQTT-TLS"] += 1
            elif port in (5683,):
                protocols["CoAP"] += 1
            elif port == 53:
                protocols["DNS"] += 1

        # HTTP requests
        http_match = http_req_pattern.search(line)
        if http_match:
            method, path = http_match.groups()
            http_requests.append({"method": method, "path": path, "line": i + 1})

        # HTTP responses
        resp_match = http_resp_pattern.search(line)
        if resp_match:
            status, reason = resp_match.groups()
            http_responses.append({"status": status, "reason": reason.strip(), "line": i + 1})

        # JSON payloads
        if "{" in line:
            json_match = re.search(r"(\{[^}]+\})", line)
            if json_match:
                try:
                    payload = json.loads(json_match.group(1))
                    json_payloads.append({"payload": payload, "line": i + 1})
                except json.JSONDecodeError:
                    pass

        # Look for interesting strings in packet data
        keywords = [
            "temperature", "temp", "heat", "cool", "fan", "speed",
            "schedule", "timer", "mode", "power", "on", "off",
            "sensor", "reading", "body", "ambient", "humidity",
            "firmware", "version", "api", "token", "auth",
            "snug", "topper", "smart", "responsive",
            "mqtt", "subscribe", "publish", "topic",
            "json", "xml", "protobuf",
        ]
        line_lower = line.lower()
        for kw in keywords:
            if kw in line_lower and not line.startswith("#"):
                interesting_strings.append({"keyword": kw, "line": i + 1, "content": line.strip()[:200]})

    # Report
    print("  CONNECTION SUMMARY")
    print("  " + "-" * 50)
    unique_ips = set()
    for (src, sp, dst, dp), count in connections.most_common(20):
        unique_ips.add(src)
        unique_ips.add(dst)
        print(f"    {src}:{sp} → {dst}:{dp}  ({count} packets)")

    print(f"\n  Unique IPs: {', '.join(sorted(unique_ips))}")

    print(f"\n  PROTOCOLS DETECTED")
    print("  " + "-" * 50)
    for proto, count in protocols.most_common():
        print(f"    {proto:12s}: {count} packets")

    print(f"\n  INTERESTING PORTS")
    print("  " + "-" * 50)
    for port, count in sorted(ports_seen.items()):
        if port < 10000 and count > 2:
            print(f"    Port {port:5d}: {count} packets")

    if http_requests:
        print(f"\n  HTTP REQUESTS")
        print("  " + "-" * 50)
        for req in http_requests:
            print(f"    [{req['line']:5d}] {req['method']} {req['path']}")

    if http_responses:
        print(f"\n  HTTP RESPONSES")
        print("  " + "-" * 50)
        for resp in http_responses[:20]:
            print(f"    [{resp['line']:5d}] {resp['status']} {resp['reason']}")

    if json_payloads:
        print(f"\n  JSON PAYLOADS ({len(json_payloads)} found)")
        print("  " + "-" * 50)
        for jp in json_payloads[:10]:
            print(f"    [{jp['line']:5d}] {json.dumps(jp['payload'], indent=None)[:150]}")

    if interesting_strings:
        # Deduplicate by keyword
        by_keyword = defaultdict(list)
        for item in interesting_strings:
            by_keyword[item["keyword"]].append(item)

        print(f"\n  INTERESTING KEYWORDS FOUND")
        print("  " + "-" * 50)
        for kw, items in sorted(by_keyword.items()):
            print(f"    '{kw}': {len(items)} occurrences")
            for item in items[:3]:
                print(f"      [{item['line']:5d}] {item['content'][:120]}")

    # Save analysis
    analysis = {
        "file": str(filepath),
        "connections": len(connections),
        "unique_ips": list(unique_ips),
        "protocols": dict(protocols),
        "http_requests": http_requests,
        "http_responses": http_responses[:20],
        "json_payloads": json_payloads[:10],
        "interesting_keywords": {k: len(v) for k, v in by_keyword.items()} if interesting_strings else {},
    }

    analysis_path = Path(filepath).with_suffix(".analysis.json")
    with open(analysis_path, "w") as f:
        json.dump(analysis, f, indent=2, default=str)
    print(f"\n  Analysis saved: {analysis_path}")

    return analysis


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <text_capture_file>")
        print()
        print("  Analyze a traffic capture text file from capture_traffic.py")
        sys.exit(1)

    filepath = sys.argv[1]
    if not Path(filepath).exists():
        print(f"ERROR: File not found: {filepath}")
        sys.exit(1)

    analyze_text_capture(filepath)


if __name__ == "__main__":
    main()
