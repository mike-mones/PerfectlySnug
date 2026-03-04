"""
Perfectly Snug Smart Topper — Local Web App Backend

Connects to both topper zones via WebSocket and exposes a REST API
for the frontend. Runs on localhost only for security.
"""
import asyncio
import struct
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import websockets
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ─── Protocol Constants ───────────────────────────────────────────
MSG_GROUP_CTRL = 2
CTRL_CMD_SET_SETTING = 0
CTRL_CMD_GET_SETTINGS = 3
CTRL_MSG_SETTING = 2
CTRL_MSG_SETTINGS = 4

SETTING_NAMES = {
    0: "l1", 1: "l2", 2: "l3", 3: "footWarmer", 4: "quietEnable",
    5: "fanLimit", 6: "heaterLimit", 7: "burstHotLevel", 8: "burstColdLevel",
    9: "burstHotDuration", 10: "burstColdDuration", 11: "volume",
    12: "t1", 13: "t3", 14: "scheduleEnable",
    15: "sched1Start", 16: "sched1Days", 17: "sched1Stop",
    18: "sched2Start", 19: "sched2Days", 20: "sched2Stop",
    21: "running", 22: "burstMode", 23: "runProgress",
    24: "bhOut", 25: "time1", 26: "time2", 27: "time3", 28: "time4",
    29: "side", 30: "tempSetpoint", 31: "tempAmbient",
    32: "tempSensorRight", 33: "tempSensorCenter", 34: "tempSensorLeft",
    35: "tempHeaterHead", 36: "tempHeaterFoot",
    37: "ihh", 38: "ihf", 39: "blOut", 40: "hhOut", 41: "fhOut",
    42: "ctrlOut", 43: "ctrlITerm", 44: "ctrlPTerm",
    52: "profileEnable", 53: "coolingMode",
}

NAME_TO_ID = {v: k for k, v in SETTING_NAMES.items()}

# IDs we fetch for the dashboard
DASHBOARD_IDS = [0, 1, 2, 3, 4, 5, 6, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
                 21, 22, 23, 29, 30, 31, 32, 33, 34, 35, 36, 40, 41, 52, 53]

TEMP_SENSOR_IDS = {30, 31, 32, 33, 34, 35, 36}

# Zone configuration
ZONES = {
    "left": "192.168.0.159",
    "right": "192.168.0.211",
}


# ─── Protocol Helpers ─────────────────────────────────────────────
def build_get_settings(ids, tx_id=1):
    payload = b""
    for sid in ids:
        payload += struct.pack(">H", sid)
    return struct.pack(">BHHH", MSG_GROUP_CTRL, CTRL_CMD_GET_SETTINGS, tx_id, len(payload)) + payload


def build_set_setting(setting_id, value, tx_id=1):
    payload = struct.pack(">HH", setting_id, value)
    return struct.pack(">BHHH", MSG_GROUP_CTRL, CTRL_CMD_SET_SETTING, tx_id, len(payload)) + payload


def parse_responses(data):
    """Parse binary WebSocket messages into setting dict."""
    readings = {}
    if len(data) >= 7:
        group = data[0]
        cmd_id = (data[1] << 8) | data[2]
        payload = data[7:]
        if group == MSG_GROUP_CTRL and cmd_id in (CTRL_MSG_SETTING, CTRL_MSG_SETTINGS):
            for i in range(0, len(payload), 4):
                if i + 4 <= len(payload):
                    sid = (payload[i] << 8) | payload[i + 1]
                    val = (payload[i + 2] << 8) | payload[i + 3]
                    readings[sid] = val
    return readings


def raw_to_temp_f(raw):
    """Convert raw sensor value to Fahrenheit."""
    c = (raw - 32768) / 100
    return round(c * 9 / 5 + 32, 1)


def format_setting(sid, raw):
    """Format a raw setting value for the frontend."""
    if sid in TEMP_SENSOR_IDS:
        return {"raw": raw, "tempF": raw_to_temp_f(raw), "tempC": round((raw - 32768) / 100, 1)}
    elif sid == 29:  # Side
        return {"raw": raw, "display": chr(raw) if 32 < raw < 127 else "?"}
    elif sid in (0, 1, 2):  # Temperature stages
        return {"raw": raw, "display": raw - 10}  # -10 to +10 scale
    elif sid in (15, 17, 18, 20):  # Schedule times (encoded as hi/lo bytes)
        hi = (raw >> 8) & 0xFF
        lo = raw & 0xFF
        return {"raw": raw, "hours": hi, "minutes": lo, "display": f"{hi:02d}:{lo:02d}"}
    elif sid == 16 or sid == 19:  # Schedule days (bitmask)
        days = []
        day_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        for i in range(7):
            if raw & (1 << i):
                days.append(day_names[i])
        return {"raw": raw, "days": days}
    elif sid == 14:  # Schedule enable
        return {"raw": raw, "enabled": raw == 1}
    elif sid == 53:  # Cooling mode
        return {"raw": raw, "responsive": raw == 1}
    elif sid == 52:  # Profile enable (1 level vs 3 levels)
        return {"raw": raw, "threeLevels": raw == 1}
    elif sid in (12, 13):  # T1=start length, T3=wake length (minutes)
        return {"raw": raw, "minutes": raw}
    elif sid == 21:  # Running
        return {"raw": raw, "running": raw != 0}
    elif sid == 22:  # Burst mode
        modes = {0: "off", 1: "hot", 2: "cold"}
        return {"raw": raw, "mode": modes.get(raw, "unknown")}
    else:
        return {"raw": raw}


# ─── WebSocket Communication ─────────────────────────────────────
async def fetch_settings(ip, ids):
    """Connect to a zone and fetch requested settings."""
    url = f"ws://{ip}/PSWS"
    readings = {}
    try:
        async with websockets.connect(
            url, origin="capacitor://localhost", ping_interval=None, close_timeout=5,
        ) as ws:
            # Send in batches of 8
            tx = 1
            for i in range(0, len(ids), 8):
                batch = ids[i:i + 8]
                await ws.send(build_get_settings(batch, tx))
                tx += 1
                await asyncio.sleep(0.2)

            end = asyncio.get_event_loop().time() + 10
            while asyncio.get_event_loop().time() < end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3)
                    if isinstance(msg, bytes):
                        readings.update(parse_responses(msg))
                    if len(readings) >= len(ids):
                        break
                except asyncio.TimeoutError:
                    break
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to zone {ip}: {e}")
    return readings


async def send_setting(ip, setting_id, value):
    """Connect to a zone and set a single setting."""
    url = f"ws://{ip}/PSWS"
    try:
        async with websockets.connect(
            url, origin="capacitor://localhost", ping_interval=None, close_timeout=5,
        ) as ws:
            cmd = build_set_setting(setting_id, value)
            await ws.send(cmd)
            await asyncio.sleep(1)

            # Drain responses
            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=1)
            except asyncio.TimeoutError:
                pass
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to send to zone {ip}: {e}")


# ─── FastAPI App ──────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Perfectly Snug Web App started")
    print(f"  Left zone:  {ZONES['left']}")
    print(f"  Right zone: {ZONES['right']}")
    print(f"  Open http://localhost:8550 in your browser")
    yield
    print("Shutting down")


app = FastAPI(title="Perfectly Snug Controller", lifespan=lifespan)


# ─── API Routes ───────────────────────────────────────────────────
@app.get("/api/zones")
async def get_zones():
    return {"zones": list(ZONES.keys())}


@app.get("/api/zone/{zone}/status")
async def get_zone_status(zone: str):
    if zone not in ZONES:
        raise HTTPException(status_code=404, detail=f"Unknown zone: {zone}")
    ip = ZONES[zone]
    raw = await fetch_settings(ip, DASHBOARD_IDS)
    result = {}
    for sid, val in raw.items():
        name = SETTING_NAMES.get(sid, f"unknown_{sid}")
        result[name] = format_setting(sid, val)
    return {"zone": zone, "ip": ip, "timestamp": datetime.now().isoformat(), "settings": result}


class SettingUpdate(BaseModel):
    value: int


@app.put("/api/zone/{zone}/setting/{setting_name}")
async def update_setting(zone: str, setting_name: str, update: SettingUpdate):
    if zone not in ZONES:
        raise HTTPException(status_code=404, detail=f"Unknown zone: {zone}")
    if setting_name not in NAME_TO_ID:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {setting_name}")

    sid = NAME_TO_ID[setting_name]
    value = update.value

    # Safety: only allow known-safe settings to be written
    WRITABLE = {0, 1, 2, 3, 4, 6, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 52, 53}
    if sid not in WRITABLE:
        raise HTTPException(status_code=403, detail=f"Setting {setting_name} (id={sid}) is read-only")

    # Range validation
    if sid in (0, 1, 2) and not (0 <= value <= 20):
        raise HTTPException(status_code=400, detail="Temperature must be 0-20 (app: -10 to +10)")
    if sid == 3 and not (0 <= value <= 3):
        raise HTTPException(status_code=400, detail="Foot warmer must be 0-3")
    if sid == 6 and value not in (0, 100):
        raise HTTPException(status_code=400, detail="Heater limit must be 0 or 100")
    if sid == 53 and value not in (0, 1):
        raise HTTPException(status_code=400, detail="Cooling mode must be 0 or 1")
    if sid == 14 and value not in (0, 1):
        raise HTTPException(status_code=400, detail="Schedule enable must be 0 or 1")
    if sid == 4 and value not in (0, 1):
        raise HTTPException(status_code=400, detail="Quiet mode must be 0 or 1")
    if sid == 11 and not (0 <= value <= 10):
        raise HTTPException(status_code=400, detail="Volume must be 0-10")
    if sid in (12, 13) and not (0 <= value <= 240):
        raise HTTPException(status_code=400, detail="Duration must be 0-240 minutes")
    if sid == 52 and value not in (0, 1):
        raise HTTPException(status_code=400, detail="Profile enable must be 0 or 1")

    ip = ZONES[zone]
    await send_setting(ip, sid, value)

    # Read back to verify
    raw = await fetch_settings(ip, [sid])
    if sid in raw:
        new_val = raw[sid]
        name = SETTING_NAMES.get(sid, f"unknown_{sid}")
        return {
            "zone": zone,
            "setting": setting_name,
            "requested": value,
            "actual": format_setting(sid, new_val),
            "success": new_val == value,
        }
    return {"zone": zone, "setting": setting_name, "requested": value, "verified": False}


@app.get("/api/zone/{zone}/temperatures")
async def get_temperatures(zone: str):
    if zone not in ZONES:
        raise HTTPException(status_code=404, detail=f"Unknown zone: {zone}")
    ip = ZONES[zone]
    raw = await fetch_settings(ip, list(TEMP_SENSOR_IDS))
    result = {}
    for sid, val in raw.items():
        name = SETTING_NAMES.get(sid, f"sensor_{sid}")
        result[name] = {"raw": val, "tempF": raw_to_temp_f(val), "tempC": round((val - 32768) / 100, 1)}
    return {"zone": zone, "timestamp": datetime.now().isoformat(), "temperatures": result}


# ─── Serve Frontend ──────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse("webapp/index.html")


app.mount("/static", StaticFiles(directory="webapp"), name="static")
