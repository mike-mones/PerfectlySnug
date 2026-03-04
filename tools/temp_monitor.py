#!/usr/bin/env python3
"""Live temperature monitor — polls both zones every 30 seconds."""
import asyncio
import struct
from datetime import datetime
import websockets

ZONES = {"L": "192.168.0.159", "R": "192.168.0.211"}
MSG_GROUP_CTRL = 2
CTRL_CMD_GET_SETTINGS = 3
CTRL_MSG_SETTING = 2
CTRL_MSG_SETTINGS = 4

SENSOR_IDS = [30, 31, 32, 33, 34, 35, 36]
NAMES = {30: "SetPt", 31: "Ambi", 32: "TSR", 33: "TSC", 34: "TSL", 35: "HHead", 36: "HFoot"}

def to_f(raw):
    c = (raw - 32768) / 100
    return c * 9/5 + 32

def build_cmd(ids, tx=1):
    payload = b""
    for s in ids:
        payload += struct.pack(">H", s)
    return struct.pack(">BHHH", MSG_GROUP_CTRL, CTRL_CMD_GET_SETTINGS, tx, len(payload)) + payload

async def poll_zone(ip):
    readings = {}
    try:
        async with websockets.connect(
            f"ws://{ip}/PSWS", origin="capacitor://localhost",
            ping_interval=None, close_timeout=3,
        ) as ws:
            await ws.send(build_cmd(SENSOR_IDS))
            end = asyncio.get_event_loop().time() + 8
            while asyncio.get_event_loop().time() < end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3)
                    if isinstance(msg, bytes) and len(msg) >= 7:
                        payload = msg[7:]
                        for i in range(0, len(payload), 4):
                            if i + 4 <= len(payload):
                                sid = (payload[i] << 8) | payload[i+1]
                                val = (payload[i+2] << 8) | payload[i+3]
                                if sid in NAMES:
                                    readings[sid] = val
                        if len(readings) >= len(SENSOR_IDS):
                            break
                except asyncio.TimeoutError:
                    break
    except Exception as e:
        print(f"  Error ({ip}): {e}")
    return readings

async def main():
    # Print header
    print("=" * 100)
    print("  Perfectly Snug — Live Temperature Monitor (every 30s, Ctrl+C to stop)")
    print("=" * 100)
    hdr = f"{'Time':>10}  |"
    for z in ["L", "R"]:
        for sid in SENSOR_IDS:
            hdr += f" {z}-{NAMES[sid]:>5}"
        hdr += "  |"
    print(hdr)
    print("-" * len(hdr))

    while True:
        now = datetime.now().strftime("%H:%M:%S")
        line = f"{now:>10}  |"
        for z, ip in ZONES.items():
            r = await poll_zone(ip)
            for sid in SENSOR_IDS:
                if sid in r:
                    line += f" {to_f(r[sid]):6.1f}"
                else:
                    line += "    -- "
            line += "  |"
        print(line, flush=True)
        await asyncio.sleep(30)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
