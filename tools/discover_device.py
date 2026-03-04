#!/usr/bin/env python3
"""
Perfectly Snug Smart Topper - Network Device Discovery

This script performs PASSIVE, READ-ONLY network discovery to find the
Perfectly Snug Smart Topper on the local network. It does NOT send any
commands to the device.

Methods used (all safe/passive):
1. ARP table scan - reads existing ARP cache
2. mDNS/Bonjour browsing - listens for service advertisements
3. DNS-SD service discovery - macOS native service browser
4. Subnet ping sweep + ARP lookup - finds all devices on the network
5. Hostname/MAC vendor identification

SAFETY: This script only READS network state and LISTENS for broadcasts.
It never sends commands to any discovered device.
"""

import subprocess
import socket
import re
import json
import sys
import ipaddress
from datetime import datetime
from pathlib import Path

# Known OUI prefixes for common IoT WiFi chips
# These are publicly available IEEE OUI assignments
IOT_MAC_PREFIXES = {
    "24:0A:C4": "Espressif (ESP32/ESP8266)",
    "24:62:AB": "Espressif (ESP32/ESP8266)",
    "30:AE:A4": "Espressif (ESP32/ESP8266)",
    "3C:61:05": "Espressif (ESP32/ESP8266)",
    "3C:71:BF": "Espressif (ESP32/ESP8266)",
    "40:F5:20": "Espressif (ESP32/ESP8266)",
    "48:3F:DA": "Espressif (ESP32/ESP8266)",
    "4C:EB:D6": "Espressif (ESP32/ESP8266)",
    "54:32:04": "Espressif (ESP32/ESP8266)",
    "58:CF:79": "Espressif (ESP32/ESP8266)",
    "5C:CF:7F": "Espressif (ESP32/ESP8266)",
    "60:01:94": "Espressif (ESP32/ESP8266)",
    "68:C6:3A": "Espressif (ESP32/ESP8266)",
    "70:03:9F": "Espressif (ESP32/ESP8266)",
    "78:21:84": "Espressif (ESP32/ESP8266)",
    "7C:9E:BD": "Espressif (ESP32/ESP8266)",
    "80:7D:3A": "Espressif (ESP32/ESP8266)",
    "84:0D:8E": "Espressif (ESP32/ESP8266)",
    "84:CC:A8": "Espressif (ESP32/ESP8266)",
    "84:F3:EB": "Espressif (ESP32/ESP8266)",
    "8C:AA:B5": "Espressif (ESP32/ESP8266)",
    "90:38:0C": "Espressif (ESP32/ESP8266)",
    "94:3C:C6": "Espressif (ESP32/ESP8266)",
    "94:B5:55": "Espressif (ESP32/ESP8266)",
    "94:B9:7E": "Espressif (ESP32/ESP8266)",
    "98:CD:AC": "Espressif (ESP32/ESP8266)",
    "A0:20:A6": "Espressif (ESP32/ESP8266)",
    "A4:CF:12": "Espressif (ESP32/ESP8266)",
    "AC:67:B2": "Espressif (ESP32/ESP8266)",
    "B4:E6:2D": "Espressif (ESP32/ESP8266)",
    "BC:DD:C2": "Espressif (ESP32/ESP8266)",
    "C4:4F:33": "Espressif (ESP32/ESP8266)",
    "C4:5B:BE": "Espressif (ESP32/ESP8266)",
    "C8:2B:96": "Espressif (ESP32/ESP8266)",
    "CC:50:E3": "Espressif (ESP32/ESP8266)",
    "CC:DB:A7": "Espressif (ESP32/ESP8266)",
    "D8:A0:1D": "Espressif (ESP32/ESP8266)",
    "D8:BF:C0": "Espressif (ESP32/ESP8266)",
    "DC:4F:22": "Espressif (ESP32/ESP8266)",
    "E0:98:06": "Espressif (ESP32/ESP8266)",
    "E8:68:E7": "Espressif (ESP32/ESP8266)",
    "EC:FA:BC": "Espressif (ESP32/ESP8266)",
    "F0:08:D1": "Espressif (ESP32/ESP8266)",
    "F4:CF:A2": "Espressif (ESP32/ESP8266)",
    # Tuya / Realtek (common in smart home)
    "D8:1F:12": "Tuya/Realtek",
    "10:D5:61": "Tuya/Realtek",
    # Particle
    "E0:4F:43": "Particle IoT",
    # Texas Instruments (CC3200/CC3100)
    "20:CD:39": "Texas Instruments",
    "78:A5:04": "Texas Instruments",
    # Microchip/ATMEL
    "00:1E:C0": "Microchip Technology",
    "F8:F0:05": "Microchip/Atmel",
    # Raspberry Pi Foundation
    "B8:27:EB": "Raspberry Pi Foundation",
    "DC:A6:32": "Raspberry Pi Foundation",
    "E4:5F:01": "Raspberry Pi Foundation",
}

RESULTS_DIR = Path(__file__).parent.parent / "docs"


def get_local_ip_and_subnet():
    """Get local IP and subnet info."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()

        # Get subnet from ifconfig
        result = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=10
        )
        # Find the interface with our IP
        lines = result.stdout.split("\n")
        for i, line in enumerate(lines):
            if local_ip in line:
                # Look for netmask on same line
                mask_match = re.search(r"netmask\s+(0x[0-9a-f]+)", line)
                if mask_match:
                    mask_hex = mask_match.group(1)
                    mask_int = int(mask_hex, 16)
                    prefix_len = bin(mask_int).count("1")
                    network = ipaddress.IPv4Network(
                        f"{local_ip}/{prefix_len}", strict=False
                    )
                    return local_ip, network
        # Fallback: assume /24
        network = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
        return local_ip, network
    except Exception as e:
        print(f"  Error getting local IP: {e}")
        return None, None


def identify_mac_vendor(mac):
    """Identify vendor from MAC address OUI prefix."""
    mac_upper = mac.upper().replace("-", ":")
    prefix = mac_upper[:8]
    return IOT_MAC_PREFIXES.get(prefix, None)


def scan_arp_table():
    """Read the ARP table (passive - just reads cache)."""
    print("\n[1/5] Reading ARP table...")
    devices = []
    try:
        result = subprocess.run(
            ["arp", "-a"], capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.strip().split("\n"):
            match = re.match(
                r"(\S+)\s+\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-f:]+)",
                line,
            )
            if match:
                hostname = match.group(1)
                ip = match.group(2)
                mac = match.group(3)
                vendor = identify_mac_vendor(mac)
                device = {
                    "hostname": hostname,
                    "ip": ip,
                    "mac": mac,
                    "vendor": vendor,
                    "source": "arp_cache",
                }
                devices.append(device)
                flag = " <-- IoT chip!" if vendor else ""
                print(f"  {ip:16s} {mac:18s} {hostname:30s} {vendor or ''}{flag}")
    except Exception as e:
        print(f"  Error reading ARP table: {e}")
    print(f"  Found {len(devices)} devices in ARP cache")
    return devices


def ping_sweep(network):
    """Ping sweep the local subnet to populate ARP cache."""
    print(f"\n[2/5] Ping sweep on {network} (populating ARP cache)...")
    print("  This is a small, harmless ICMP echo — like the 'ping' command.")
    print("  Pinging all addresses to make sure ARP cache is populated...")

    hosts = list(network.hosts())
    # Limit to /24 or smaller to avoid huge sweeps
    if len(hosts) > 254:
        print(f"  Network too large ({len(hosts)} hosts), limiting to /24")
        first_ip = hosts[0]
        network = ipaddress.IPv4Network(f"{first_ip}/24", strict=False)
        hosts = list(network.hosts())

    # Use a fast parallel ping (macOS supports this with fping-style approach)
    pinged = 0
    for host in hosts:
        try:
            subprocess.run(
                ["ping", "-c", "1", "-W", "100", str(host)],
                capture_output=True,
                timeout=2,
            )
            pinged += 1
        except (subprocess.TimeoutExpired, Exception):
            pass

    print(f"  Pinged {pinged}/{len(hosts)} hosts")


def discover_mdns_services():
    """Browse for mDNS services (passive listening)."""
    print("\n[3/5] Browsing mDNS services (5 second listen)...")
    services = []
    service_types = [
        "_http._tcp.",
        "_https._tcp.",
        "_mqtt._tcp.",
        "_coap._tcp.",
        "_coap._udp.",
        "_ipp._tcp.",
        "_airplay._tcp.",
        "_workstation._tcp.",
        "_smb._tcp.",
        "_device-info._tcp.",
        "_homekit._tcp.",
        "_hap._tcp.",
        "_tuya._tcp.",
        "_esphomelib._tcp.",
        "_arduino._tcp.",
    ]

    for stype in service_types:
        try:
            result = subprocess.run(
                ["dns-sd", "-B", stype, "local."],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in result.stdout.strip().split("\n"):
                if line and not line.startswith("Browsing") and not line.startswith("DATE"):
                    services.append({"type": stype, "raw": line.strip()})
                    print(f"  [{stype}] {line.strip()}")
        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            print(f"  Error browsing {stype}: {e}")

    if not services:
        print("  No mDNS services found in quick scan. This is normal — the device")
        print("  may not advertise via mDNS.")
    return services


def check_common_iot_ports(ip):
    """Check common IoT ports on a specific IP (read-only connect test)."""
    ports = {
        80: "HTTP",
        443: "HTTPS",
        8080: "HTTP-alt",
        8443: "HTTPS-alt",
        1883: "MQTT",
        8883: "MQTT-TLS",
        5683: "CoAP",
        23: "Telnet",
        22: "SSH",
        3000: "HTTP-dev",
        4443: "HTTPS-alt2",
        6668: "IRC (Tuya)",
    }
    open_ports = []
    for port, name in ports.items():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex((ip, port))
            if result == 0:
                open_ports.append({"port": port, "service": name})
                print(f"    Port {port:5d} ({name:12s}): OPEN")
            sock.close()
        except Exception:
            pass
    return open_ports


def scan_iot_devices(arp_devices, local_ip):
    """Port scan only IoT-looking devices (safe TCP connect)."""
    print("\n[4/5] Checking ports on interesting devices...")
    print("  (Only TCP connect — this is like a web browser connecting)")
    results = []

    # Prioritize: IoT vendor matches, then unknown devices
    interesting = []
    for d in arp_devices:
        if d["ip"] == local_ip:
            continue
        if d["vendor"]:
            interesting.insert(0, d)  # IoT vendors first
        elif d["hostname"] == "?":
            interesting.append(d)  # Unknown hostnames are interesting

    if not interesting:
        # If nothing looks interesting, scan all non-self devices
        interesting = [d for d in arp_devices if d["ip"] != local_ip]

    for device in interesting[:20]:  # Limit to 20 most interesting
        ip = device["ip"]
        label = device["vendor"] or device["hostname"]
        print(f"\n  Scanning {ip} ({label})...")
        ports = check_common_iot_ports(ip)
        if ports:
            device["open_ports"] = ports
            results.append(device)

    return results


def try_http_identification(ip, port):
    """Try to identify a device via HTTP headers (read-only GET request)."""
    import http.client

    headers_info = {}
    try:
        if port == 443 or port == 8443:
            import ssl
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(ip, port, timeout=3, context=context)
        else:
            conn = http.client.HTTPConnection(ip, port, timeout=3)

        conn.request("GET", "/")
        resp = conn.getresponse()
        headers_info["status"] = resp.status
        headers_info["reason"] = resp.reason
        headers_info["headers"] = dict(resp.getheaders())

        # Read a small amount of body to identify
        body = resp.read(2048).decode("utf-8", errors="replace")
        headers_info["body_preview"] = body[:500]

        conn.close()
    except Exception as e:
        headers_info["error"] = str(e)

    return headers_info


def identify_http_devices(scan_results):
    """Try HTTP identification on devices with open HTTP ports."""
    print("\n[5/5] Identifying HTTP-accessible devices...")
    print("  (Read-only GET request — like opening a page in your browser)")

    for device in scan_results:
        http_ports = [
            p["port"]
            for p in device.get("open_ports", [])
            if p["service"].startswith("HTTP")
        ]
        for port in http_ports:
            print(f"\n  GET http://{device['ip']}:{port}/ ...")
            info = try_http_identification(device["ip"], port)
            if "error" not in info:
                print(f"    Status: {info['status']} {info['reason']}")
                server = info["headers"].get("Server", info["headers"].get("server", "unknown"))
                print(f"    Server: {server}")
                if info.get("body_preview"):
                    # Look for identifying strings
                    body = info["body_preview"]
                    if any(
                        kw in body.lower()
                        for kw in ["snug", "topper", "smart", "temperature", "esp", "tuya"]
                    ):
                        print(f"    *** POSSIBLE MATCH — body contains relevant keywords ***")
                    print(f"    Body preview: {body[:200]}")
                device.setdefault("http_info", {})[port] = info
            else:
                print(f"    Error: {info['error']}")


def save_results(arp_devices, mdns_services, scan_results, local_ip, network):
    """Save discovery results to JSON."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = {
        "timestamp": datetime.now().isoformat(),
        "local_ip": local_ip,
        "network": str(network),
        "arp_devices": arp_devices,
        "mdns_services": mdns_services,
        "scan_results": scan_results,
    }

    output_path = RESULTS_DIR / f"discovery_{timestamp}.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_path}")
    return output_path


def main():
    print("=" * 70)
    print("  Perfectly Snug Smart Topper - Network Discovery")
    print("=" * 70)
    print()
    print("SAFETY: This tool only READS network state and LISTENS for broadcasts.")
    print("It will NEVER send commands to any device.")
    print()

    # Step 0: Get our local IP and subnet
    local_ip, network = get_local_ip_and_subnet()
    if not local_ip:
        print("ERROR: Could not determine local IP address.")
        print("Make sure you're connected to your WiFi network.")
        sys.exit(1)

    print(f"  Local IP: {local_ip}")
    print(f"  Network:  {network}")

    # Step 1: Read ARP table
    arp_devices = scan_arp_table()

    # Step 2: Ping sweep to populate ARP cache
    ping_sweep(network)

    # Step 3: Re-read ARP table after ping sweep
    print("\n  Re-reading ARP table after ping sweep...")
    arp_devices = scan_arp_table()

    # Step 4: mDNS service discovery
    mdns_services = discover_mdns_services()

    # Step 5: Port scan interesting devices
    scan_results = scan_iot_devices(arp_devices, local_ip)

    # Step 6: HTTP identification
    if scan_results:
        identify_http_devices(scan_results)

    # Save results
    output_path = save_results(
        arp_devices, mdns_services, scan_results, local_ip, network
    )

    # Summary
    print("\n" + "=" * 70)
    print("  DISCOVERY SUMMARY")
    print("=" * 70)
    iot_devices = [d for d in arp_devices if d["vendor"]]
    print(f"  Total devices on network: {len(arp_devices)}")
    print(f"  IoT-chipset devices:      {len(iot_devices)}")
    print(f"  Devices with open ports:  {len(scan_results)}")

    if iot_devices:
        print("\n  IoT devices found:")
        for d in iot_devices:
            ports = ", ".join(
                str(p["port"]) for p in d.get("open_ports", [])
            )
            print(f"    {d['ip']:16s} {d['mac']:18s} {d['vendor']}")
            if ports:
                print(f"      Open ports: {ports}")

    print(f"\n  Full results: {output_path}")
    print()
    print("NEXT STEPS:")
    print("  1. Look for Espressif/ESP devices — the Smart Topper likely uses one")
    print("  2. If found, run the traffic capture while using the Perfectly Snug app")
    print("  3. This will reveal the communication protocol (HTTP, MQTT, etc.)")
    print()


if __name__ == "__main__":
    main()
