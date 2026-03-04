#!/usr/bin/env python3
"""
Simple, reliable traffic capture between ANY device and the topper zones.
Captures ALL traffic on port 80 to/from both topper IPs.

Run with: sudo python3 tools/capture_app.py
Then use the Perfectly Snug app on your phone.
Press Ctrl+C when done.
"""
import subprocess
import sys
import os
import signal
from datetime import datetime
from pathlib import Path

ZONE_IPS = ["192.168.0.159", "192.168.0.211"]
OUT_DIR = Path(__file__).parent.parent / "docs" / "captures"

if os.geteuid() != 0:
    print("Need sudo: sudo python3 tools/capture_app.py")
    sys.exit(1)

OUT_DIR.mkdir(parents=True, exist_ok=True)
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
pcap = OUT_DIR / f"app_{ts}.pcap"
txt = OUT_DIR / f"app_{ts}.txt"

bpf = f"host {ZONE_IPS[0]} or host {ZONE_IPS[1]}"

print("=" * 50)
print("  Perfectly Snug — App Traffic Capture")
print("=" * 50)
print(f"  Watching: {', '.join(ZONE_IPS)}")
print(f"  PCAP: {pcap}")
print(f"  Text: {txt}")
print()
print("  NOW: Open the Perfectly Snug app on your iPhone")
print("  and interact with it. Change temps, schedules, etc.")
print()
print("  Press Ctrl+C when you're done.")
print()

# Single tcpdump writing both pcap and printing to stdout
proc = subprocess.Popen(
    ["tcpdump", "-i", "en0", "-nn", "-v", "-X", "-s", "0", "-w", str(pcap), bpf],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)

# Second tcpdump for human-readable text
proc2 = subprocess.Popen(
    ["tcpdump", "-i", "en0", "-nn", "-v", "-A", "-s", "0", bpf],
    stdout=open(str(txt), "w"),
    stderr=subprocess.PIPE,
)

print(f"  tcpdump started (PIDs: {proc.pid}, {proc2.pid})")
print("  Waiting for traffic...\n")

def stop(sig=None, frame=None):
    print("\n  Stopping...")
    proc.terminate()
    proc2.terminate()
    proc.wait(timeout=5)
    proc2.wait(timeout=5)
    
    # Quick stats
    stats = proc.stderr.read().decode()
    print(f"\n  {stats.strip()}")
    print(f"  PCAP: {pcap}")
    print(f"  Text: {txt}")
    
    # Show unique IPs in the text file
    import re
    with open(txt) as f:
        content = f.read()
    ips = set(re.findall(r"(\d+\.\d+\.\d+\.\d+)\.\d+", content))
    print(f"  IPs seen: {ips}")
    lines = content.count("\n")
    print(f"  Lines captured: {lines}")
    sys.exit(0)

signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)

# Wait forever until Ctrl+C
try:
    proc.wait()
except KeyboardInterrupt:
    stop()
