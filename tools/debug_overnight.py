#!/usr/bin/env python3
"""Deep dive into the overnight mystery — check settings + raw values."""
import json
import os
import sys
from urllib.request import Request, urlopen
from datetime import datetime, timedelta, timezone

HA_URL = "http://192.168.0.106:8123"
TOKEN = os.environ.get("HA_TOKEN", "")
if not TOKEN:
    print("Set HA_TOKEN environment variable first: export HA_TOKEN='your_token'")
    sys.exit(1)
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def ha_get(path):
    req = Request(f"{HA_URL}{path}", headers=HEADERS)
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def ha_history(entity_id, hours=14):
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    path = f"/api/history/period/{start.isoformat()}?filter_entity_id={entity_id}&end_time={end.isoformat()}&no_attributes"
    data = ha_get(path)
    if data and len(data) > 0:
        return data[0]
    return []

# 1. Check the temperature SETTINGS (L1, L2, L3) history
print("=" * 60)
print("  SETTINGS HISTORY (what was the topper told to do?)")
print("=" * 60)

for side in ["left", "right"]:
    print(f"\n  --- {side.upper()} SIDE ---")
    for setting in ["bedtime_temperature", "sleep_temperature", "wake_temperature", "foot_warmer"]:
        eid = f"number.smart_topper_{side}_side_{setting}"
        history = ha_history(eid)
        if history:
            print(f"\n  {setting}:")
            for h in history:
                ts = h.get("last_changed", "")[:19]
                state = h.get("state", "?")
                print(f"    {ts}  value={state}")

# 2. Check switch states
print(f"\n\n{'=' * 60}")
print("  SWITCH HISTORY")
print("=" * 60)

for side in ["left", "right"]:
    print(f"\n  --- {side.upper()} SIDE ---")
    for sw in ["responsive_cooling", "schedule", "3_level_mode", "quiet_mode"]:
        eid = f"switch.smart_topper_{side}_side_{sw}"
        history = ha_history(eid)
        if history:
            print(f"\n  {sw}:")
            for h in history:
                ts = h.get("last_changed", "")[:19]
                state = h.get("state", "?")
                print(f"    {ts}  state={state}")

# 3. Now let's look at raw heater temp data points during sleeping hours
# to see if the conversion seems right
print(f"\n\n{'=' * 60}")
print("  HEATER TEMP RAW CHECK")
print("=" * 60)

# Pull the actual overnight data we saved
data = json.load(open("docs/overnight_data.json"))

# Look at heater head during the 7-8 UTC window (2-3am EST)
for sensor_key in ["sensor.smart_topper_left_side_heater_head_temperature",
                    "sensor.smart_topper_left_side_heater_foot_temperature",
                    "sensor.smart_topper_left_side_ambient_temperature",
                    "sensor.smart_topper_left_side_body_sensor_center"]:
    points = data.get(sensor_key, [])
    label = sensor_key.split(".")[-1].replace("smart_topper_left_side_", "")
    
    # Get 3am-ish data (08 UTC = 3am EST)
    sample = [(ts, v) for ts, v in points if "T08:" in ts][:5]
    if sample:
        print(f"\n  {label} (sample at ~3am EST / 08 UTC):")
        for ts, v in sample:
            print(f"    {ts[:19]}  displayed={v}")

# 4. Also pull current raw values directly from topper to compare
print(f"\n\n{'=' * 60}")
print("  CURRENT RAW VALUES FROM TOPPER")
print("=" * 60)

import asyncio
import struct
import sys
sys.path.insert(0, ".")

# Quick inline read
async def read_raw():
    import websockets
    for name, ip in [("Left", "192.168.0.159"), ("Right", "192.168.0.211")]:
        url = f"ws://{ip}/PSWS"
        try:
            async with websockets.connect(url, origin="capacitor://localhost", 
                                          ping_interval=None, close_timeout=5) as ws:
                ids = [0,1,2,3,4,6,21,30,31,32,33,34,35,36,40,41,42,43,44,52,53]
                payload = b""
                for sid in ids:
                    payload += struct.pack(">H", sid)
                header = struct.pack(">BHHH", 2, 3, 1, len(payload))
                await ws.send(header + payload)
                
                readings = {}
                end = asyncio.get_event_loop().time() + 10
                while asyncio.get_event_loop().time() < end:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=3)
                        if isinstance(msg, bytes) and len(msg) >= 7:
                            p = msg[7:]
                            for i in range(0, len(p), 4):
                                if i+4 <= len(p):
                                    sid = (p[i]<<8)|p[i+1]
                                    val = (p[i+2]<<8)|p[i+3]
                                    readings[sid] = val
                            if len(readings) >= len(ids):
                                break
                    except asyncio.TimeoutError:
                        break
                
                print(f"\n  {name} ({ip}):")
                names = {0:"L1",1:"L2",2:"L3",3:"FootWarm",4:"Quiet",6:"HtrLim",
                         21:"Running",30:"TempSP",31:"TA",32:"TSR",33:"TSC",34:"TSL",
                         35:"THH",36:"THF",40:"HH_OUT",41:"FH_OUT",
                         42:"CtrlOut",43:"CtrlI",44:"CtrlP",52:"ProfEn",53:"CoolMode"}
                for sid in sorted(readings.keys()):
                    raw = readings[sid]
                    n = names.get(sid, f"id{sid}")
                    if sid in (30,31,32,33,34,35,36):
                        c = (raw - 32768) / 100
                        f = c * 9/5 + 32
                        print(f"    {n:10s} raw={raw:6d}  hex=0x{raw:04X}  -> {f:.1f}°F  ({c:.1f}°C)")
                    elif sid in (42,43,44):
                        signed = (raw - 32768) / 100
                        print(f"    {n:10s} raw={raw:6d}  hex=0x{raw:04X}  -> {signed:.2f}")
                    else:
                        print(f"    {n:10s} raw={raw:6d}  hex=0x{raw:04X}")
        except Exception as e:
            print(f"  {name}: Error: {e}")

asyncio.run(read_raw())
