#!/usr/bin/env python3
"""
Broad traffic capture — capture ALL port 80 traffic on the network.
No IP filter. This will catch whatever device talks to whatever.

Run: sudo python3 tools/capture_broad.py
Use the app. Then Ctrl+C.
"""
import subprocess, sys, os, signal, re
from datetime import datetime
from pathlib import Path

if os.geteuid() != 0:
    print("Need sudo: sudo python3 tools/capture_broad.py")
    sys.exit(1)

OUT = Path(__file__).parent.parent / "docs" / "captures"
OUT.mkdir(parents=True, exist_ok=True)
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
pcap = OUT / f"broad_{ts}.pcap"
txt = OUT / f"broad_{ts}.txt"

# No IP filter — capture ALL traffic involving either topper IP
# Use promiscuous mode explicitly
bpf = "host 192.168.0.159 or host 192.168.0.211"

print("=" * 50)
print("  Broad capture — all topper traffic")
print("=" * 50)
print(f"  Filter: {bpf}")
print(f"  PCAP: {pcap}")
print(f"  Text: {txt}")
print()
print("  Use the app now. Ctrl+C when done.")
print()

# Use -p0 to ensure promiscuous mode, and explicitly use en0
p1 = subprocess.Popen(
    ["tcpdump", "-i", "en0", "-nn", "-XX", "-s", "0", "-w", str(pcap), bpf],
    stderr=subprocess.PIPE,
)

p2 = subprocess.Popen(
    ["tcpdump", "-i", "en0", "-nn", "-vvv", "-A", "-s", "0", bpf],
    stdout=open(str(txt), "w"),
    stderr=subprocess.PIPE,
)

print(f"  PIDs: {p1.pid}, {p2.pid}")

def stop(sig=None, frame=None):
    print("\n  Stopping...")
    p1.terminate()
    p2.terminate()
    p1.wait(timeout=5)
    p2.wait(timeout=5)
    s = p1.stderr.read().decode()
    print(f"\n  {s.strip()}")
    print(f"  PCAP: {pcap} ({pcap.stat().st_size} bytes)")
    print(f"  Text: {txt} ({txt.stat().st_size} bytes)")
    
    with open(txt) as f:
        content = f.read()
    ips = set(re.findall(r"(\d+\.\d+\.\d+\.\d+)\.\d+", content))
    print(f"  IPs seen: {ips}")
    print(f"  Lines: {content.count(chr(10))}")
    
    # Show first 50 non-hex lines
    print("\n  FIRST 50 INTERESTING LINES:")
    count = 0
    for line in content.split("\n"):
        if line.strip() and not line.strip().startswith("0x"):
            print(f"    {line[:120]}")
            count += 1
            if count >= 50:
                break
    sys.exit(0)

signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)

try:
    p1.wait()
except KeyboardInterrupt:
    stop()
