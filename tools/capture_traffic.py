#!/usr/bin/env python3
"""
Perfectly Snug Smart Topper - Passive Traffic Capture

This script captures network traffic TO and FROM a specific device IP
using tcpdump. It is completely PASSIVE — it only reads packets traveling
across the network, it never injects or modifies anything.

Usage:
    sudo python3 capture_traffic.py <device_ip> [duration_seconds]

The output is saved as a .pcap file that can be analyzed with Wireshark
or the included analysis script.

SAFETY: This is equivalent to running Wireshark — purely observational.
It requires sudo because tcpdump needs raw socket access, but it does
NOT write to or modify any device.

IMPORTANT: Run this while actively using the Perfectly Snug app to
control the topper. This lets us see what commands the app sends.
"""

import subprocess
import sys
import signal
import os
from datetime import datetime
from pathlib import Path

RESULTS_DIR = Path(__file__).parent.parent / "docs" / "captures"


def main():
    if os.geteuid() != 0:
        print("ERROR: This script needs sudo to capture packets.")
        print(f"  Usage: sudo python3 {sys.argv[0]} <device_ip> [duration_seconds]")
        sys.exit(1)

    if len(sys.argv) < 2:
        print(f"Usage: sudo python3 {sys.argv[0]} <device_ip> [duration_seconds]")
        print()
        print("  device_ip        - IP address of the Perfectly Snug device")
        print("  duration_seconds - How long to capture (default: 120)")
        print()
        print("Run discover_device.py first to find the device IP.")
        sys.exit(1)

    device_ip = sys.argv[1]
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 120

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pcap_file = RESULTS_DIR / f"snug_capture_{timestamp}.pcap"
    text_file = RESULTS_DIR / f"snug_capture_{timestamp}.txt"

    print("=" * 70)
    print("  Perfectly Snug - Passive Traffic Capture")
    print("=" * 70)
    print()
    print(f"  Target device: {device_ip}")
    print(f"  Duration:      {duration} seconds")
    print(f"  PCAP output:   {pcap_file}")
    print(f"  Text output:   {text_file}")
    print()
    print("SAFETY: This is a READ-ONLY packet capture.")
    print("No data is sent to the device.")
    print()
    print("INSTRUCTIONS:")
    print("  1. Open the Perfectly Snug app on your phone")
    print("  2. Make sure it connects to the Smart Topper")
    print("  3. Change some settings (temperature, schedule, etc.)")
    print("  4. The capture will record what the app sends")
    print()
    print(f"  Capturing for {duration} seconds... (Ctrl+C to stop early)")
    print()

    # Get the WiFi interface
    try:
        result = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        interface = None
        lines = result.stdout.split("\n")
        for i, line in enumerate(lines):
            if "Wi-Fi" in line:
                for j in range(i + 1, min(i + 3, len(lines))):
                    match = __import__("re").match(r"Device:\s+(\S+)", lines[j])
                    if match:
                        interface = match.group(1)
                        break
                break
        if not interface:
            interface = "en0"  # Fallback
    except Exception:
        interface = "en0"

    print(f"  Using interface: {interface}")
    print()

    # Run tcpdump with both pcap and text output
    # BPF filter: only traffic involving the target device
    bpf_filter = f"host {device_ip}"

    # Start pcap capture
    tcpdump_pcap = subprocess.Popen(
        [
            "tcpdump",
            "-i", interface,
            "-w", str(pcap_file),
            "-c", "10000",  # Max 10k packets
            bpf_filter,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Start text capture (human-readable)
    tcpdump_text = subprocess.Popen(
        [
            "tcpdump",
            "-i", interface,
            "-nn",        # Don't resolve names
            "-v",         # Verbose
            "-A",         # Print packet content as ASCII
            "-s", "0",    # Full packet capture
            "-c", "10000",
            bpf_filter,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def cleanup(signum=None, frame=None):
        print("\n  Stopping capture...")
        tcpdump_pcap.terminate()
        tcpdump_text.terminate()
        tcpdump_pcap.wait(timeout=5)
        tcpdump_text.wait(timeout=5)

    signal.signal(signal.SIGINT, cleanup)

    try:
        # Wait for the specified duration
        tcpdump_text.wait(timeout=duration)
    except subprocess.TimeoutExpired:
        cleanup()

    # Read the text output
    text_output = tcpdump_text.stdout.read().decode("utf-8", errors="replace")
    text_stderr = tcpdump_text.stderr.read().decode("utf-8", errors="replace")

    # Save text output
    with open(text_file, "w") as f:
        f.write(f"# Perfectly Snug Traffic Capture\n")
        f.write(f"# Target: {device_ip}\n")
        f.write(f"# Time: {datetime.now().isoformat()}\n")
        f.write(f"# Interface: {interface}\n\n")
        f.write(text_output)
        f.write(f"\n\n# tcpdump stats:\n{text_stderr}\n")

    # Quick analysis
    print()
    print("=" * 70)
    print("  CAPTURE SUMMARY")
    print("=" * 70)
    print(f"  {text_stderr.strip()}")
    print(f"  PCAP file: {pcap_file}")
    print(f"  Text file: {text_file}")
    print()

    # Basic protocol breakdown
    if text_output:
        lines = text_output.split("\n")
        http_count = sum(1 for l in lines if "HTTP" in l)
        mqtt_count = sum(1 for l in lines if "MQTT" in l or ":1883" in l or ":8883" in l)
        tcp_count = sum(1 for l in lines if "Flags" in l)
        udp_count = sum(1 for l in lines if "UDP" in l)

        print("  Protocol breakdown:")
        print(f"    TCP packets:  {tcp_count}")
        print(f"    UDP packets:  {udp_count}")
        print(f"    HTTP-related: {http_count}")
        print(f"    MQTT-related: {mqtt_count}")

        # Look for interesting ports
        port_pattern = __import__("re").compile(r"\.(\d+)[: >]")
        ports = set()
        for line in lines:
            for port_match in port_pattern.finditer(line):
                port = int(port_match.group(1))
                if 1 < port < 65535:
                    ports.add(port)

        if ports:
            interesting_ports = sorted(p for p in ports if p < 10000 or p > 50000)
            if interesting_ports:
                print(f"\n  Interesting ports seen: {', '.join(str(p) for p in interesting_ports[:20])}")
    else:
        print("  No packets captured. Possible reasons:")
        print("    - Device is not communicating right now")
        print("    - Wrong device IP (re-run discover_device.py)")
        print("    - App was not used during capture window")

    print()
    print("NEXT STEPS:")
    print("  1. Review the text file for readable content")
    print("  2. Open the PCAP file in Wireshark for detailed analysis")
    print("  3. Run analyze_capture.py on the text file for automated analysis")
    print()


if __name__ == "__main__":
    main()
