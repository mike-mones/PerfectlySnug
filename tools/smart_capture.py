#!/usr/bin/env python3
"""
Smart traffic capture targeting the two Perfectly Snug topper zones.

Captures traffic to/from both 192.168.0.159 and 192.168.0.211.
Requires sudo for raw packet capture (tcpdump).

Usage: sudo python3 tools/smart_capture.py [duration_seconds]

INSTRUCTIONS:
  While this runs, open the Perfectly Snug app on your iPhone and:
  1. Connect to the topper
  2. Change temperature settings
  3. Toggle foot heater
  4. Adjust schedule
  5. Try burst mode
  6. Any other settings you can change
"""

import subprocess
import sys
import os
import signal
import re
from datetime import datetime
from pathlib import Path

ZONE_1 = "192.168.0.159"
ZONE_2 = "192.168.0.211"
CAPTURES_DIR = Path(__file__).parent.parent / "docs" / "captures"


def main():
    if os.geteuid() != 0:
        print(f"ERROR: needs sudo. Run: sudo python3 {sys.argv[0]}")
        sys.exit(1)

    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 180

    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Detect WiFi interface
    try:
        result = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=5
        )
        interface = "en0"
        for i, line in enumerate(result.stdout.split("\n")):
            if "Wi-Fi" in line:
                for j in range(i + 1, min(i + 3, len(result.stdout.split("\n")))):
                    m = re.match(r"Device:\s+(\S+)", result.stdout.split("\n")[j])
                    if m:
                        interface = m.group(1)
                        break
                break
    except Exception:
        interface = "en0"

    # BPF filter: traffic involving either topper zone
    bpf = f"host {ZONE_1} or host {ZONE_2}"

    pcap_file = CAPTURES_DIR / f"snug_{ts}.pcap"
    text_file = CAPTURES_DIR / f"snug_{ts}.txt"

    print("=" * 65)
    print("  Perfectly Snug - Smart Capture")
    print("=" * 65)
    print(f"  Zone 1:    {ZONE_1}")
    print(f"  Zone 2:    {ZONE_2}")
    print(f"  Interface: {interface}")
    print(f"  Duration:  {duration}s")
    print(f"  PCAP:      {pcap_file}")
    print(f"  Text:      {text_file}")
    print()
    print("  NOW: Open the Perfectly Snug app and interact with it!")
    print("  Change temps, toggle settings, try burst mode, etc.")
    print()
    print(f"  Capturing for {duration}s... (Ctrl+C to stop early)")
    print()

    # Start PCAP capture
    pcap_proc = subprocess.Popen(
        ["tcpdump", "-i", interface, "-w", str(pcap_file), "-c", "50000", bpf],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    # Start verbose text capture with full packet content
    text_proc = subprocess.Popen(
        [
            "tcpdump", "-i", interface,
            "-nn",       # No name resolution
            "-vvv",      # Very verbose
            "-X",        # Hex + ASCII dump
            "-s", "0",   # Full packet
            "-c", "50000",
            bpf,
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    stopped = False

    def stop(signum=None, frame=None):
        nonlocal stopped
        if stopped:
            return
        stopped = True
        print("\n  Stopping capture...")
        pcap_proc.terminate()
        text_proc.terminate()
        try:
            pcap_proc.wait(timeout=5)
        except Exception:
            pcap_proc.kill()
        try:
            text_proc.wait(timeout=5)
        except Exception:
            text_proc.kill()

    signal.signal(signal.SIGINT, stop)

    try:
        text_proc.wait(timeout=duration)
    except subprocess.TimeoutExpired:
        stop()

    if not stopped:
        stop()

    # Collect output
    stdout = text_proc.stdout.read().decode("utf-8", errors="replace")
    stderr = text_proc.stderr.read().decode("utf-8", errors="replace")

    with open(text_file, "w") as f:
        f.write(f"# Perfectly Snug Traffic Capture\n")
        f.write(f"# Zones: {ZONE_1}, {ZONE_2}\n")
        f.write(f"# Time: {datetime.now().isoformat()}\n")
        f.write(f"# Interface: {interface}\n\n")
        f.write(stdout)
        f.write(f"\n\n# Stats:\n{stderr}\n")

    # Quick analysis
    lines = stdout.split("\n")
    total_packets = len([l for l in lines if l.strip() and not l.startswith("#")])

    # Count directional traffic
    to_z1 = sum(1 for l in lines if f"> {ZONE_1}" in l)
    from_z1 = sum(1 for l in lines if f"{ZONE_1}." in l and ">" in l and f"> {ZONE_1}" not in l)
    to_z2 = sum(1 for l in lines if f"> {ZONE_2}" in l)
    from_z2 = sum(1 for l in lines if f"{ZONE_2}." in l and ">" in l and f"> {ZONE_2}" not in l)

    # Find unique source IPs talking to toppers
    src_ips = set()
    for l in lines:
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)\.\d+ > (?:" + re.escape(ZONE_1) + "|" + re.escape(ZONE_2) + r")\.", l)
        if m:
            src_ips.add(m.group(1))

    # Find ports used
    ports = set()
    for l in lines:
        for z in [ZONE_1, ZONE_2]:
            m = re.search(re.escape(z) + r"\.(\d+)", l)
            if m:
                ports.add(int(m.group(1)))

    print()
    print("=" * 65)
    print("  CAPTURE RESULTS")
    print("=" * 65)
    print(f"  {stderr.strip()}")
    print(f"  Traffic to Zone 1 ({ZONE_1}):   ~{to_z1} packets")
    print(f"  Traffic from Zone 1:            ~{from_z1} packets")
    print(f"  Traffic to Zone 2 ({ZONE_2}):   ~{to_z2} packets")
    print(f"  Traffic from Zone 2:            ~{from_z2} packets")
    print(f"  Source IPs talking to toppers:  {src_ips or 'none'}")
    print(f"  Topper ports used:             {sorted(ports) if ports else 'none'}")
    print(f"\n  Files:")
    print(f"    PCAP: {pcap_file}")
    print(f"    Text: {text_file}")
    print()

    # Look for interesting content in payloads
    interesting = []
    for i, l in enumerate(lines):
        lower = l.lower()
        if any(kw in lower for kw in [
            "temp", "heat", "cool", "fan", "schedule", "mode",
            "power", "sensor", "body", "http", "get ", "post ",
            "json", "websocket", "upgrade", "connection:",
        ]):
            interesting.append((i, l.strip()))

    if interesting:
        print("  INTERESTING LINES:")
        for idx, line in interesting[:30]:
            print(f"    [{idx}] {line[:120]}")

    print()


if __name__ == "__main__":
    main()
