#!/usr/bin/env python3
"""
Comprehensive port scan of the Perfectly Snug topper zones.
Scans all ports 1-65535 on both zones to find what services are available.
This is a TCP connect scan — safe, like trying to open a web page.
"""
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ZONES = {
    "Zone 1": "192.168.0.159",
    "Zone 2": "192.168.0.211",
}

def check_port(ip, port):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.3)
        result = sock.connect_ex((ip, port))
        sock.close()
        return port if result == 0 else None
    except:
        return None

def scan_zone(name, ip):
    print(f"\n  Scanning {name} ({ip}) — all 65535 ports...")
    start = time.time()
    open_ports = []
    
    with ThreadPoolExecutor(max_workers=200) as executor:
        futures = {executor.submit(check_port, ip, port): port for port in range(1, 65536)}
        for future in as_completed(futures):
            result = future.result()
            if result:
                open_ports.append(result)
                print(f"    OPEN: {result}")
    
    elapsed = time.time() - start
    print(f"  {name} done in {elapsed:.0f}s — {len(open_ports)} open ports: {sorted(open_ports)}")
    return sorted(open_ports)

if __name__ == "__main__":
    print("=" * 60)
    print("  Full Port Scan — Perfectly Snug Toppers")
    print("=" * 60)
    results = {}
    for name, ip in ZONES.items():
        results[name] = scan_zone(name, ip)
    
    print("\n\nSUMMARY:")
    for name, ports in results.items():
        print(f"  {name} ({ZONES[name]}): {ports}")
