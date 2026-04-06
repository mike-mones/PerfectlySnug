# Sleep Controller & Pi Setup — Session Notes (April 6, 2026)

## What's Working
- **Sleep controller v3** deployed and running on HA Green (192.168.0.106)
  - 42 tests passing, AppDaemon auto-reloads on file change
  - Logs to InfluxDB (db: perfectly_snug) AND PostgreSQL (Pi, db: sleepdata)
  - Deploy: `scp PerfectlySnug/appdaemon/sleep_controller_v3.py root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/`
  - Tests: `.venv/bin/python -m pytest PerfectlySnug/tests/test_controller_v3.py -v`

### Controller behavior (current)
- **Static baseline**: bedtime=-8, sleep=-6, wake=-5
- **Multi-night learning**: EMA (alpha=0.3) stored as floats, rounded when applied
- **Ambient compensation**: uses configurable room temp sensor (not topper's built-in "ambient" which reads mattress heat)
- **Manual overrides**: recorded as data points for learning, no hard clamps or persistent offsets
- **Deadband**: setting only changes if computed value differs by ≥2 from current (prevents thrashing)
- **Cooldown**: minimum 15 min between setting changes (respects topper thermal lag)
- **Hot safety**: body >85°F for 2+ cycles = cool by 1 (bypasses deadband/cooldown)
- **Kill switch**: 3+ manual changes in 20s = manual mode for the night
- **Auto-restart**: if topper's 10h timer expires but body still in bed, restart
- **State persistence**: survives AppDaemon restarts mid-night

### Configurable via apps.yaml
```yaml
sleep_controller:
  module: sleep_controller_v3
  class: SleepController
  room_temp_entity: sensor.superior_6000s_temperature  # swap to Aqara when ready
  postgres_host: 192.168.0.75
```

### PostgreSQL data logging
- **controller_readings**: every 5-min cycle — body sensors, room temp, setting, phase, action, override delta
- **nightly_summary**: end-of-night — duration, settings, body avg, override count
- **Query last night**: `SELECT ts, phase, body_avg_f, room_temp_f, setting, action FROM controller_readings WHERE ts > NOW() - INTERVAL '12 hours' ORDER BY ts;`
- **psycopg2-binary** installed in AppDaemon addon python_packages

### Bugs fixed (April 6)
1. **_load_state was broken** — read saved state file but discarded data, created fresh state. Now properly restores with forward-compatible field merging.
2. **Setting thrashing** — no deadband or cooldown. March 10 had 69 setting changes in one day. Added ±2 deadband + 15-min cooldown.
3. **Learning stuck in rounding trap** — `round(int * 0.7 + int * 0.3)` can't converge. Now stores as float, rounds only when applying.
4. **Topper's "ambient" sensor reads mattress heat** — was reading 80°F when room was 69°F. Swapped to Levoit humidifier sensor (configurable for Aqara later).
5. **Override floor/ceiling were hard clamps** — removed entirely. Overrides are just learning data points.
6. **Manual override not logged to PostgreSQL** — override path hit `continue` before PG logging call. Fixed.

## Pi (192.168.0.75) — ONLINE, WiFi
- Model: **Raspberry Pi 3B** (MAC b8:27:eb:5f:19:2f, 921MB RAM, armv7l)
- **Status (April 6): Pi 3 is ONLINE and stable on WiFi.**
- **SSH:** `ssh mikemones@192.168.0.75` (ED25519 key auth, no password needed)
- **Hostname:** `raspberrypi`
- **OS:** Raspberry Pi OS Lite (32-bit), freshly flashed via Raspberry Pi Imager (April 6, 2026)
- **Power:** Original Pi PSU (micro-USB), no undervoltage (`throttled=0x0`)

### Network
- **WiFi:** `NETGEAR00` (2.4 GHz) — Pi 3 doesn't support 5 GHz
- **No ethernet:** USB/LAN hub chip (LAN9514) throws `hub_ext_port_status err = -71` under USB load
- **WiFi chip:** BCM43438 (separate bus from USB hub, unaffected)
- **IP is DHCP** — set a reservation on router (`192.168.0.1`) for MAC `b8:27:eb:5f:19:2f` → `192.168.0.75`

### Known issues / caveats
- **Tailscale exit node blocks local LAN access.** Disable when working with Pi: `tailscale set --exit-node=`
- **No USB peripherals.** Keyboard, ethernet, HDMI all stress the failing LAN9514 hub chip and cause crashes/freezes
- **Previous boot failures (April 4)** were caused by: no ethernet cable plugged in (Pi had no network), then crashes during debugging were from keyboard + ethernet-to-Mac stressing USB hub, not hardware death
- **256GB SD card is dead** (Mac can't detect it). Currently using a different SD card.

### What was wrong (April 6 recovery)
1. Pi was offline because ethernet cable was unplugged (not a hardware failure)
2. Crashes during debugging: USB hub errors from keyboard + ethernet-to-Mac (not router)
3. WiFi not connecting initially: needed DHCP (`sudo dhclient wlan0`)
4. Network unreachable from Mac: Tailscale exit node routing local traffic through VPN
5. Monitor zoomed in: added HDMI resolution fix to `config.txt` (1024x768@60Hz)

### What needs to be done once a server is online
1. **Set up swap** (1GB) as safety net
2. **Install PostgreSQL**: `sudo apt install postgresql postgresql-contrib`
3. **Create DB**: user=sleepsync, pass=sleepsync_local, db=sleepdata
4. **Run schema**: `PerfectlySnug/config/sleep_schema.sql` + health_metrics table with TIMESTAMPTZ
5. **Enable remote Postgres access**: listen_addresses='*', pg_hba.conf for 192.168.0.0/24
6. **Deploy health receiver** with safeguards:
   - Code: `PerfectlySnug/health_receiver/app.py`
   - Service: `PerfectlySnug/health_receiver/health-receiver.service` (MemoryMax=300M)
   - Requirements: fastapi, uvicorn, psycopg2-binary, python-multipart
7. **Install AdGuard Home** (install only, don't route traffic)
8. **Re-send health data** from iPhone (sleep + daily metrics only, NOT heart_rate)

### Safeguards already coded in app.py (not yet deployed)
- 50MB max request body
- 5,000 records max per non-sleep metric
- 10,000 records max for sleep segments
- Batch inserts (1000 rows/transaction)
- systemd MemoryMax=300M (kills service, not Pi)

### Health metrics to collect (daily only)
- resting_heart_rate, heart_rate_variability, blood_oxygen_saturation
- breathing_disturbances, apple_sleeping_wrist_temperature, respiratory_rate
- sleep_analysis (granular per-interval format)
- **Skip**: heart_rate (daily avg is noise, per-minute crashed the Pi)

## Key files
- `PerfectlySnug/appdaemon/sleep_controller_v3.py` — controller (deployed to HA Green)
- `PerfectlySnug/appdaemon/apps.yaml` — AppDaemon config (room_temp_entity, postgres_host)
- `PerfectlySnug/tests/test_controller_v3.py` — 42 tests
- `PerfectlySnug/health_receiver/app.py` — FastAPI receiver with safeguards
- `PerfectlySnug/health_receiver/requirements.txt` — Python deps
- `PerfectlySnug/health_receiver/health-receiver.service` — systemd unit with memory cap
- `PerfectlySnug/config/sleep_schema.sql` — Postgres schema (sleep_segments, nightly_summary, health_metrics, controller_readings)

## Architecture
```
iPhone (Health Auto Export) --POST JSON--> Pi:8080 (health receiver) --> Pi Postgres (sleepdata)
PerfectlySnug Topper <--> HA Green (custom_component)
                     <--> AppDaemon (sleep_controller_v3) --> InfluxDB (HA Green)
                                                          --> PostgreSQL (Pi 192.168.0.75)
Aqara Zigbee sensors --> ZHA (HA Green ZBT-2) --> sensor entities (room temp)
Levoit Superior 6000S --> VeSync --> sensor.superior_6000s_temperature (current room temp source)
```

## Hardware to buy
- **Aqara Zigbee Temperature & Humidity Sensor 3-pack** (~$44) — bedroom, living room, kitchen
  - Zigbee via ZHA on HA Green's ZBT-2 — 100% local, no cloud
  - Bedroom sensor replaces Levoit as `room_temp_entity` in apps.yaml
  - Setup: HA Settings → Devices → Add Integration → ZHA, then pair sensors

## Lessons learned
- SD cards corrupt easily on hard power loss — always add swap, cap memory
- Per-minute health data = 100K+ records, will OOM a 906MB Pi — cap at 5K/metric
- systemd MemoryMax prevents service from taking down the whole Pi
- Pi 3 first boot on 256GB card is very slow (partition resize)
- Health Auto Export: use DAILY aggregation for metrics, granular only for sleep
- Topper's "ambient" sensor reads mattress surface heat, not room air — off by 10°F+
- Setting thrashing destroys sleep quality — always use deadband + cooldown
- Integer rounding in EMA learning creates convergence traps — use floats internally
- `_load_state` must actually USE the loaded data (was silently discarding it for weeks)
- HA recorder purges history after ~10 days by default — PostgreSQL is the long-term store
- Future upgrade: Mac Mini M4 ($499) replaces Pi + adds local LLM capability
