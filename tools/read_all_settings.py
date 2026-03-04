#!/usr/bin/env python3
"""Read ALL settings from the topper to map out everything."""
import asyncio
import struct
import websockets

ZONES = {"Left (zone1)": "192.168.0.159", "Right (zone2)": "192.168.0.211"}

MSG_GROUP_CTRL = 2
CTRL_CMD_GET_SETTINGS = 3
CTRL_MSG_SETTING = 2
CTRL_MSG_SETTINGS = 4

ALL_SETTINGS = {
    0: "L1 (Stage 1 temp)", 1: "L2 (Stage 2 temp)", 2: "L3 (Stage 3 temp)",
    3: "Foot Warmer", 4: "Quiet Enable", 5: "Fan Limit", 6: "Heater Limit",
    7: "Burst Hot Lvl", 8: "Burst Cold Lvl", 9: "Burst Hot Duration",
    10: "Burst Cold Duration", 11: "Volume", 12: "T1 (temp1)", 13: "T3 (temp3)",
    14: "Schedule Enable",
    15: "Sched1 Start", 16: "Sched1 Days", 17: "Sched1 Stop",
    18: "Sched2 Start", 19: "Sched2 Days", 20: "Sched2 Stop",
    21: "Running", 22: "Burst Mode", 23: "Run Progress",
    24: "BH Out", 25: "Time1", 26: "Time2", 27: "Time3", 28: "Time4",
    29: "Side", 30: "Temp Setpoint", 31: "Ambient (TA)",
    32: "Sensor Right (TSR)", 33: "Sensor Center (TSC)", 34: "Sensor Left (TSL)",
    35: "Heater Head (THH)", 36: "Heater Foot (THF)",
    37: "IHH", 38: "IHF", 39: "BL Out", 40: "HH Out", 41: "FH Out",
    42: "Ctrl Out", 43: "Ctrl ITerm", 44: "Ctrl PTerm",
    52: "Profile Enable", 53: "Cooling Mode",
}


def build_get_settings(setting_ids, tx_id=1):
    payload = b""
    for sid in setting_ids:
        payload += struct.pack(">H", sid)
    header = struct.pack(">BHHH", MSG_GROUP_CTRL, CTRL_CMD_GET_SETTINGS, tx_id, len(payload))
    return header + payload


async def read_all(name, ip):
    url = f"ws://{ip}/PSWS"
    print(f"\n  [{name}] Connecting...")
    try:
        async with websockets.connect(
            url, origin="capacitor://localhost", ping_interval=None, close_timeout=5,
        ) as ws:
            cmd = build_get_settings(list(ALL_SETTINGS.keys()))
            await ws.send(cmd)

            readings = {}
            end = asyncio.get_event_loop().time() + 15
            while asyncio.get_event_loop().time() < end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    if isinstance(msg, bytes) and len(msg) >= 7:
                        group = msg[0]
                        cmd_id = (msg[1] << 8) | msg[2]
                        payload = msg[7:]
                        if group == MSG_GROUP_CTRL and cmd_id in (CTRL_MSG_SETTING, CTRL_MSG_SETTINGS):
                            for i in range(0, len(payload), 4):
                                if i + 4 <= len(payload):
                                    sid = (payload[i] << 8) | payload[i+1]
                                    val = (payload[i+2] << 8) | payload[i+3]
                                    readings[sid] = val
                            if len(readings) >= len(ALL_SETTINGS):
                                break
                except asyncio.TimeoutError:
                    break

            print(f"\n  [{name}] All Settings ({len(readings)} received):")
            print(f"  {'─' * 55}")
            for sid in sorted(ALL_SETTINGS.keys()):
                label = ALL_SETTINGS[sid]
                if sid in readings:
                    raw = readings[sid]
                    extra = ""
                    if sid in (31, 32, 33, 34, 35, 36):  # temp sensors
                        c = raw / 1000
                        f = c * 9/5 + 32
                        extra = f"  ({c:.2f}C / {f:.1f}F if milliC)"
                    elif sid == 29:  # Side
                        extra = f"  ('{chr(raw)}')" if 32 < raw < 127 else ""
                    elif sid in (15, 17, 18, 20):  # schedule times
                        h = raw // 60
                        m = raw % 60
                        extra = f"  ({h:02d}:{m:02d})" if raw < 1440 else ""
                    elif sid in (25, 26, 27, 28):  # time bars
                        h = raw // 60
                        m = raw % 60
                        extra = f"  ({h:02d}:{m:02d})" if raw < 1440 else ""
                    print(f"    {sid:3d} {label:25s}: {raw:6d}  (0x{raw:04X}){extra}")
                else:
                    print(f"    {sid:3d} {label:25s}: no response")
            return readings
    except Exception as e:
        print(f"  [{name}] Error: {e}")
        return {}


async def main():
    print("=" * 60)
    print("  Perfectly Snug — Full Settings Dump")
    print("=" * 60)
    for name, ip in ZONES.items():
        await read_all(name, ip)

if __name__ == "__main__":
    asyncio.run(main())
