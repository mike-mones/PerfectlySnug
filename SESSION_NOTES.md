# Sleep Controller & Pi Setup — Session Notes (April 3, 2026)

## What's Working
- **Sleep controller v3** deployed and running on HA Green (192.168.0.106)
  - 41 tests passing, AppDaemon auto-reloaded it
  - Logs to InfluxDB on HA Green (db: perfectly_snug)
  - Deploy: `scp -i ~/.ssh/id_ed25519 PerfectlySnug/appdaemon/sleep_controller_v3.py root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/`

## Pi (192.168.0.210) — FRESHLY FLASHED, FIRST BOOT IN PROGRESS
- Model: **Raspberry Pi 3** (MAC b8:27:eb:0a:4c:7a, 906MB RAM)
- OS: Raspberry Pi OS Lite 64-bit (fresh flash via Imager, April 3 2026)
- User: mikemones, password auth, SSH enabled
- Status: First boot — resizing 256GB SD card, generating keys. May take a while on Pi 3.
- **No SSH key auth yet** — need to run `ssh-copy-id` after first successful login

### What needs to be done once Pi is online
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
- `PerfectlySnug/appdaemon/apps.yaml` — AppDaemon config
- `PerfectlySnug/tests/test_controller_v3.py` — 41 tests
- `PerfectlySnug/health_receiver/app.py` — FastAPI receiver with safeguards
- `PerfectlySnug/health_receiver/requirements.txt` — Python deps
- `PerfectlySnug/health_receiver/health-receiver.service` — systemd unit with memory cap
- `PerfectlySnug/config/sleep_schema.sql` — Postgres schema

## Architecture
```
iPhone (Health Auto Export) --POST JSON--> Pi:8080 (health receiver) --> Pi Postgres (sleepdata)
Apple Watch --> HA Green:8123 (webhook) --> input_number entities
PerfectlySnug Topper <--> HA Green (custom_component)
                     <--> AppDaemon (sleep_controller_v3) --> InfluxDB
```

## Lessons learned
- SD cards corrupt easily on hard power loss — always add swap, cap memory
- Per-minute health data = 100K+ records, will OOM a 906MB Pi — cap at 5K/metric
- systemd MemoryMax prevents service from taking down the whole Pi
- Pi 3 first boot on 256GB card is very slow (partition resize)
- Health Auto Export: use DAILY aggregation for metrics, granular only for sleep
- Future upgrade: Mac Mini M4 ($499) replaces Pi + adds local LLM capability
