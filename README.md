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

## Reactive Sleep Temperature Controller

### What It Does
A continuous PID controller that replaces the static L1/L2/L3 temperature stages.
Every 5 minutes during sleep, it:
1. Reads body temperature sensors from the topper
2. Computes a target body temp from a science-based sleep curve
3. Uses PID control to find the right topper setting
4. Pushes the setting change via `number.set_value`
5. Detects manual overrides and adapts the target curve over time

### Deployment
- Runs as an **AppDaemon app** on HA Green (`a0d7b954_appdaemon` add-on)
- App: `/addon_configs/a0d7b954_appdaemon/apps/sleep_controller.py`
- Config: `/addon_configs/a0d7b954_appdaemon/apps/apps.yaml`
- State persisted: `/addon_configs/a0d7b954_appdaemon/apps/controller_state.json`
- Source: `PerfectlySnug/appdaemon/sleep_controller.py`

### Deploy Workflow
```bash
# Edit locally, then SCP to HA Green:
scp PerfectlySnug/appdaemon/sleep_controller.py root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/
scp PerfectlySnug/appdaemon/apps.yaml root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/

# Restart AppDaemon:
ssh root@192.168.0.106 "ha addons restart a0d7b954_appdaemon"

# Check logs:
ssh root@192.168.0.106 "ha addons logs a0d7b954_appdaemon --lines 30"
```

### Sleep Curve (Body Temperature Targets)
| Phase | Time | Target (°F) | What Happens |
|---|---|---|---|
| Onset | 0–60 min | 76°F | Aggressive cooling for sleep onset |
| Deep | 60–180 min | 78°F | Gradual warming into deep sleep |
| REM | 180–300 min | 80°F | Warmer for REM-heavy second half |
| Pre-wake | 300–420 min | 82°F | Warm-up toward natural wake |

### Override Learning
When you manually adjust the topper during sleep, the controller detects it and
shifts the target curve for that sleep phase. Over multiple nights, the curve
converges to your personal optimum. Learning rate: 0.7 (aggressive early adaptation).

### PID Gains
- Kp=0.5 (proportional), Ki=0.02 (integral), Kd=0.1 (derivative)
- Max change per 5-min cycle: ±2 setting units
- Integral windup clamped to ±5.0

## Apple Watch Health Data Pipeline

### The Problem
iOS HealthKit **cannot be read while the iPhone is locked** — this is a hard OS security restriction that affects all apps, Shortcuts, and automations. The Health Auto Export (HAE) app works during the day but produces zero data overnight when the phone is locked on the nightstand.

### Approaches Tested (March 9, 2026)
| Approach | Result |
|---|---|
| HAE + iPhone Mirroring | Manual exports work, automatic scheduling does NOT |
| HAE Automations widget | Unreliable through iPhone Mirroring |
| iOS Shortcuts (Time of Day trigger) | Ran, but HealthKit access **blocked** while locked |
| iOS Shortcuts (from Apple Watch) | **WORKED** — Watch can read HealthKit while phone locked |
| Native watchOS app (SleepSync) | **Solution** — event-driven via HKObserverQuery |

### SleepSync watchOS App
Location: `../SleepSync/` (separate Xcode project in the workspace root)

Uses `HKObserverQuery` with `.immediate` background delivery. When the Watch writes a new HR or HRV sample, watchOS wakes SleepSync, which reads the latest sample and POSTs to the HA webhook in the same format our automation already handles.

See [SleepSync README](../SleepSync/README.md) for setup instructions.

### HA Webhook Automation
- Webhook: `http://192.168.0.106:8123/api/webhook/apple_health_import` (local_only)
- Source: `config/apple_health_automation_v2.yaml`
- Deploy: `python3 /tmp/build_automation.py && scp /tmp/automations_new.yaml root@192.168.0.106:/homeassistant/automations.yaml`
- Handles both aggregated and disaggregated HAE payload formats
- Updates: `input_number.apple_health_hr_avg`, `input_number.apple_health_hrv`

### Key Entity IDs
- `input_number.apple_health_hr_avg` — Latest heart rate (bpm)
- `input_number.apple_health_hrv` — Latest heart rate variability (ms SDNN)
- `input_number.apple_health_resting_hr` — Resting heart rate
- `input_number.apple_health_wrist_temp` — Wrist temperature deviation

## Risk Assessment

| Action | Risk Level | Notes |
|---|---|---|
| Network scanning | ✅ None | Standard network discovery |
| Traffic capture | ✅ None | Passive observation only |
| Sending same commands as official app | 🟡 Low | Replaying exact known-good commands |
| Sending modified parameters | 🟠 Medium | Only after understanding protocol limits |
| Firmware modification | 🔴 Do Not Do | Warranty void, brick risk |
| Factory reset via app | 🟡 Low | Built-in feature, but loses settings |
