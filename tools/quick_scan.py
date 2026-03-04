#!/usr/bin/env python3
"""Quick MAC vendor analysis of devices found on the network."""

devices = {
    '192.168.0.1':   '74:fe:ce:10:b9:e0',
    '192.168.0.4':   'ec:b5:fa:b1:f1:17',
    '192.168.0.23':  'cc:40:85:26:5c:d4',
    '192.168.0.31':  'd4:ad:fc:de:a2:d4',
    '192.168.0.44':  'e6:88:50:ad:f3:7a',
    '192.168.0.49':  'cc:40:85:26:3b:48',
    '192.168.0.53':  '36:b8:f6:52:46:67',
    '192.168.0.77':  '54:43:b2:bc:3e:a0',
    '192.168.0.80':  '8c:98:6b:64:c4:8d',
    '192.168.0.106': '20:f8:3b:0:70:63',
    '192.168.0.142': 'cc:40:85:3e:23:2e',
    '192.168.0.159': '0:4b:12:4:3c:48',
    '192.168.0.168': 'cc:40:85:22:8d:e',
    '192.168.0.211': '0:4b:12:13:5c:34',
    '192.168.0.213': '48:f6:ee:32:9f:94',
    '192.168.0.217': '4:2e:c1:13:6e:61',
    '192.168.0.238': '94:4f:4c:5:85:89',
    '192.168.0.247': '3c:22:7f:49:15:d2',
}

# Known manufacturers by OUI
known_oui = {
    "74:FE:CE": "O-Net Communications (Shenzhen)",
    "EC:B5:FA": "Philips Lighting BV",
    "CC:40:85": "Microsoft Corporation (Xbox/Surface)",
    "D4:AD:FC": "Apple, Inc.",
    "54:43:B2": "Xiaomi Communications",
    "8C:98:6B": "Arris Group / CommScope",
    "20:F8:3B": "Moxa Inc.",
    "48:F6:EE": "Wuhan Tianyu / ZTE",
    "94:4F:4C": "LG Electronics",
    "3C:22:7F": "Amazon Technologies",
    # Not in standard DB - need to check
    "00:4B:12": "Unknown (possibly custom)",
    "04:2E:C1": "Unknown (possibly custom)",
    "E6:88:50": "Locally administered (randomized)",
    "36:B8:F6": "Locally administered (randomized)",
}

# Espressif (ESP32/ESP8266) OUI prefixes
esp_oui = set([
    "24:0A:C4", "24:62:AB", "30:AE:A4", "3C:61:05", "3C:71:BF",
    "40:F5:20", "48:3F:DA", "4C:EB:D6", "54:32:04", "58:CF:79",
    "5C:CF:7F", "60:01:94", "68:C6:3A", "70:03:9F", "78:21:84",
    "7C:9E:BD", "80:7D:3A", "84:0D:8E", "84:CC:A8", "84:F3:EB",
    "8C:AA:B5", "90:38:0C", "94:3C:C6", "94:B5:55", "94:B9:7E",
    "98:CD:AC", "A0:20:A6", "A4:CF:12", "AC:67:B2", "B4:E6:2D",
    "BC:DD:C2", "C4:4F:33", "C4:5B:BE", "C8:2B:96", "CC:50:E3",
    "CC:DB:A7", "D8:A0:1D", "D8:BF:C0", "DC:4F:22", "E0:98:06",
    "E8:68:E7", "EC:FA:BC", "F0:08:D1", "F4:CF:A2",
])

print("=" * 75)
print("  MAC Address Analysis")
print("=" * 75)
print(f"{'IP':16s} {'MAC':20s} {'OUI':10s} {'Vendor / Notes'}")
print("-" * 75)

candidates = []
for ip in sorted(devices.keys(), key=lambda x: [int(p) for p in x.split('.')]):
    mac = devices[ip]
    parts = mac.split(':')
    normalized = ':'.join(p.zfill(2) for p in parts).upper()
    oui = normalized[:8]

    # Check if locally administered (randomized MAC)
    second_nibble = int(normalized[1], 16)
    is_random = (second_nibble & 0x2) != 0

    # Check ESP
    is_esp = oui in esp_oui

    vendor = known_oui.get(oui, "Unknown")
    if is_esp:
        vendor = "*** ESPRESSIF (ESP32/ESP8266) ***"
    elif is_random:
        vendor = "(randomized/private MAC)"

    marker = ""
    if is_esp:
        marker = " <== LIKELY SMART TOPPER"
        candidates.append((ip, normalized))
    elif vendor == "Unknown":
        marker = " <== investigate"
        candidates.append((ip, normalized))

    print(f"{ip:16s} {normalized:20s} {oui:10s} {vendor}{marker}")

print()
print("=" * 75)
print("  CANDIDATES TO INVESTIGATE")
print("=" * 75)
if candidates:
    for ip, mac in candidates:
        print(f"  {ip} ({mac})")
else:
    print("  No ESP32 matches found. The topper may use a less common chip.")
    print("  Next step: port scan all unknown devices.")

print()
print("Now let's port-scan the candidate devices...")
print()

import socket

ports_to_check = {
    80: "HTTP", 443: "HTTPS", 8080: "HTTP-alt", 8443: "HTTPS-alt",
    1883: "MQTT", 8883: "MQTT-TLS", 5683: "CoAP",
    23: "Telnet", 22: "SSH", 3000: "Node/Dev",
    4443: "HTTPS-alt2", 6668: "Tuya", 6669: "Tuya-alt",
    81: "HTTP-alt", 8081: "HTTP-alt", 5353: "mDNS",
}

for ip, mac in candidates:
    print(f"\n  Scanning {ip} ({mac})...")
    found_ports = []
    for port, name in sorted(ports_to_check.items()):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex((ip, port))
            if result == 0:
                found_ports.append((port, name))
                print(f"    Port {port:5d} ({name:12s}): OPEN")
            sock.close()
        except Exception:
            pass
    if not found_ports:
        print(f"    No common IoT ports open")

# Also scan the non-candidate devices that have unusual OUIs
print("\n\n  Scanning ALL unidentified devices for common IoT ports...")
all_ips = sorted(devices.keys(), key=lambda x: [int(p) for p in x.split('.')])
candidate_ips = {ip for ip, _ in candidates}
for ip in all_ips:
    if ip in candidate_ips or ip == '192.168.0.1' or ip == '192.168.0.255':
        continue
    mac = devices[ip]
    parts = mac.split(':')
    normalized = ':'.join(p.zfill(2) for p in parts).upper()
    # Quick check for HTTP/MQTT only
    found = []
    for port in [80, 443, 1883, 8080, 8883]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.3)
            result = sock.connect_ex((ip, port))
            if result == 0:
                found.append(port)
            sock.close()
        except Exception:
            pass
    if found:
        print(f"    {ip:16s} {normalized:20s} ports: {found}")
