#!/usr/bin/env python3
"""
Full port scan of the Perfectly Snug topper zones.
Scans all ports 1-65535 to find any hidden services.

This is equivalent to what any port scanner does — just TCP connect checks.
"""

import socket
import sys
import time

ZONE_1 = "192.168.0.159"
ZONE_2 = "192.168.0.211"


def scan_all_ports(ip, batch_size=500, timeout=0.3):
    """Scan all 65535 TCP ports."""
    open_ports = []
    total = 65535
    start = time.time()

    for batch_start in range(1, total + 1, batch_size):
        batch_end = min(batch_start + batch_size - 1, total)
        for port in range(batch_start, batch_end + 1):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                result = sock.connect_ex((ip, port))
                if result == 0:
                    open_ports.append(port)
                    elapsed = time.time() - start
                    print(f"  ** Port {port} OPEN ** (found at {elapsed:.1f}s)")
                sock.close()
            except Exception:
                pass

        # Progress update every batch
        pct = batch_end / total * 100
        if batch_start % (batch_size * 10) == 1:
            elapsed = time.time() - start
            print(f"  ... {pct:.0f}% ({batch_end}/{total}) elapsed: {elapsed:.0f}s")

    return open_ports


def main():
    for name, ip in [("Zone 1", ZONE_1), ("Zone 2", ZONE_2)]:
        print(f"\n{'='*60}")
        print(f"  Full Port Scan: {name} ({ip})")
        print(f"{'='*60}")
        print(f"  Scanning ports 1-65535 (timeout=0.3s per port)...")

        start = time.time()
        open_ports = scan_all_ports(ip)
        elapsed = time.time() - start

        print(f"\n  Results for {name} ({ip}):")
        print(f"  Time: {elapsed:.1f}s")
        if open_ports:
            print(f"  Open ports: {open_ports}")
        else:
            print(f"  No open TCP ports found!")
        print()

        # If we found the same results on zone 1, skip zone 2 full scan
        # and just check those specific ports
        if name == "Zone 1" and open_ports:
            print(f"  Will check Zone 2 for same ports: {open_ports}")


if __name__ == "__main__":
    main()
