# Perfectly Snug Smart Topper — Local Web App Project

## Project Goal
Build a secure, local-only web app to control the Perfectly Snug Smart Topper
with better UX and expose hidden data (body temperature readings, tunable
Responsive Cooling parameters).

## ⚠️ SAFETY RULES (NON-NEGOTIABLE)
1. **Never send unknown/undocumented commands** to the device
2. **Observe first** — capture traffic to understand the protocol completely before interacting
3. **Replay only known-good commands** that the official app already sends
4. **Keep the official app installed** as a fallback
5. **Never attempt firmware modification** — this WILL void the warranty
6. **Local network only** — no cloud exposure, no port forwarding

## What We Know

### Device Hardware
- WiFi-connected mattress topper (connects to home WiFi during setup)
- Dual-zone: independent left/right temperature control
- Built-in sensors: body temperature + ambient temperature monitoring
- Fans at foot of bed for active airflow
- Heating elements for warming
- Physical buttons on each side (increase/decrease temp, on/off)
- Small speaker for audio feedback ("Heat 1", etc.)
- No FCC filing under Perfectly Snug → uses an off-the-shelf WiFi module (likely **ESP32**)

### Apps
| App Name | Package ID | Firmware | Notes |
|---|---|---|---|
| Perfectly Snug Controller (old) | `com.perfectlysnug.psandroidapp` | < 3.0.0.0 | Pre-June 2024 units |
| Perfectly Snug (new) | `com.PerfectlySnug.PerfectlySnugController2` | >= 3.0.0.0 | Post-June 2024 units |

- Latest firmware: v3.1.0.0 (adds new settings)
- App data safety: "No data shared with third parties", "No data collected"
  → **Strong signal that communication is local/direct, not cloud-relayed**

### App Features (from Play Store + Reviews)
- Connect topper to home WiFi
- Set temperature per side (-5 cool to +5 warm)
- 3-stage overnight temperature plan (start → sleep → wake)
- Schedule auto-start and auto-stop
- Foot heater control (3 levels)
- Burst mode (instant cooling/heating)
- Quiet mode (disable speaker)
- Speaker volume control
- "Responsive Cooling" that auto-adjusts based on body temp (NOT tunable in app)

### What's Hidden / Missing
- **Body temperature readings** — sensors exist, data collected, but user never sees it
- **Responsive Cooling tuning** — on/off only, no sensitivity/aggressiveness control
- **Temperature history** — no sleep temperature graph or trending
- **Detailed fan speed** — app only shows levels, not actual RPM/speed

## Discovery Phase (Current)

### Tools Created
1. `tools/discover_device.py` — Find the device on the local network
   - ARP table scan
   - Ping sweep
   - mDNS/Bonjour service browsing
   - Port scanning
   - HTTP identification
   - MAC vendor lookup (ESP32 OUI detection)

2. `tools/capture_traffic.py` — Passive traffic capture (needs sudo)
   - Records all traffic to/from the device
   - Saves PCAP (Wireshark) + human-readable text
   - Requires using the official app during capture

3. `tools/analyze_capture.py` — Analyze captured traffic
   - Protocol identification
   - HTTP request/response extraction
   - JSON payload detection
   - Keyword search for temperature/sensor data

### How To Run Discovery

```bash
# Step 1: Find the device on the network
cd /Users/mikemones/Documents/GitHub/PerfectlySnug
python3 tools/discover_device.py

# Step 2: Capture traffic (use the device IP from Step 1)
# Have the Perfectly Snug app open on your phone and interact with it
sudo python3 tools/capture_traffic.py <DEVICE_IP> 120

# Step 3: Analyze the capture
python3 tools/analyze_capture.py docs/captures/snug_capture_*.txt
```

## Architecture Plan (Post-Discovery)

Once we understand the protocol, the web app will:

```
[Browser] ←→ [Local Python/Flask Server] ←→ [Smart Topper on WiFi]
               (same network only)
```

- **Frontend**: Modern web UI (React or vanilla JS)
- **Backend**: Python Flask/FastAPI on your Mac
- **Security**: Bind to localhost or LAN only, optional auth token
- **Features**:
  - All existing app controls (temp, schedule, foot heater, burst mode)
  - Body temperature graph/history
  - Responsive Cooling sensitivity tuning
  - Sleep analytics dashboard
  - Possibly Home Assistant integration later

## Risk Assessment

| Action | Risk Level | Notes |
|---|---|---|
| Network scanning | ✅ None | Standard network discovery |
| Traffic capture | ✅ None | Passive observation only |
| Sending same commands as official app | 🟡 Low | Replaying exact known-good commands |
| Sending modified parameters | 🟠 Medium | Only after understanding protocol limits |
| Firmware modification | 🔴 Do Not Do | Warranty void, brick risk |
| Factory reset via app | 🟡 Low | Built-in feature, but loses settings |
