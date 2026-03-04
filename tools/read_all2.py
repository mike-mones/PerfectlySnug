#!/usr/bin/env python3
"""Read ALL settings from topper, in small batches of 5."""
import asyncio
import struct
import websockets

ZONES = {"Left": "192.168.0.159", "Right": "192.168.0.211"}

MSG_GROUP_CTRL = 2
CTRL_CMD_GET_SETTINGS = 3
CTRL_MSG_SETTING = 2
CTRL_MSG_SETTINGS = 4

ALL_IDS = list(range(0, 54))
NAMES = {
    0: "L1 (Stage1)", 1: "L2 (Stage2)", 2: "L3 (Stage3)",
    3: "Foot Warmer", 4: "Quiet En", 5: "Fan Lim", 6: "Htr Lim",
    7: "Burst Hot Lvl", 8: "Burst Cold Lvl", 9: "Burst Hot Dur",
    10: "Burst Cold Dur", 11: "Volume", 12: "T1", 13: "T3",
    14: "Sched En", 15: "Sched1 Start", 16: "Sched1 Days", 17: "Sched1 Stop",
    18: "Sched2 Start", 19: "Sched2 Days", 20: "Sched2 Stop",
    21: "Running", 22: "Burst", 23: "RunProgress",
    24: "BH Out", 25: "Time1", 26: "Time2", 27: "Time3", 28: "Time4",
    29: "Side", 30: "TempSP", 31: "TA(ambient)", 32: "TSR(right)",
    33: "TSC(center)", 34: "TSL(left)", 35: "THH(head)", 36: "THF(foot)",
    37: "IHH", 38: "IHF", 39: "BL Out", 40: "HH Out", 41: "FH Out",
    42: "Ctrl Out", 43: "Ctrl I", 44: "Ctrl P",
    45: "DL Upload St", 46: "DL Upload %", 47: "FW Update St",
    48: "Test Fan", 49: "Test HH", 50: "Test HF", 51: "FAT InProg",
    52: "Profile En", 53: "Cool Mode",
}

def build_cmd(setting_ids, tx_id=1):
    payload = b""
    for sid in setting_ids:
        payload += struct.pack(">H", sid)
    header = struct.pack(">BHHH", MSG_GROUP_CTRL, CTRL_CMD_GET_SETTINGS, tx_id, len(payload))
    return header + payload

async def read_zone(name, ip):
    url = f"ws://{ip}/PSWS"
    readings = {}
    try:
        async with websockets.connect(
            url, origin="capacitor://localhost", ping_interval=None, close_timeout=5,
        ) as ws:
            # Request in batches of 5
            tx = 1
            for i in range(0, len(ALL_IDS), 5):
                batch = ALL_IDS[i:i+5]
                cmd = build_cmd(batch, tx)
                tx += 1
                await ws.send(cmd)
                await asyncio.sleep(0.3)

            # Collect all responses
            end = asyncio.get_event_loop().time() + 15
            while asyncio.get_event_loop().time() < end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3)
                    if isinstance(msg, bytes) and len(msg) >= 7:
                        group = msg[0]
                        cmd_id = (msg[1] << 8) | msg[2]
                        payload = msg[7:]
                        if group == MSG_GROUP_CTRL and cmd_id in (CTRL_MSG_SETTING, CTRL_MSG_SETTINGS):
                            for j in range(0, len(payload), 4):
                                if j + 4 <= len(payload):
                                    sid = (payload[j] << 8) | payload[j+1]
                                    val = (payload[j+2] << 8) | payload[j+3]
                                    readings[sid] = val
                except asyncio.TimeoutError:
                    break
    except Exception as e:
        print(f"  [{name}] Error: {e}")

    print(f"\n  [{name}] ({len(readings)} settings)")
    print(f"  {'─' * 50}")
    for sid in sorted(readings.keys()):
        label = NAMES.get(sid, f"Unknown_{sid}")
        raw = readings[sid]
        extra = ""
        if sid in (30, 31, 32, 33, 34, 35, 36):
            c = raw / 1000
            f = c * 9/5 + 32
            extra = f"  ({c:.1f}C / {f:.1f}F)"
        elif sid == 29:
            extra = f"  ('{chr(raw)}')" if 32 < raw < 127 else ""
        elif sid in (15, 17, 18, 20, 25, 26, 27, 28):
            h, m = divmod(raw, 60)
            extra = f"  ({h:02d}:{m:02d})" if raw < 1440 else ""
        print(f"    {sid:3d} {label:20s}: {raw:6d}  (0x{raw:04X}){extra}")
    return readings

async def main():
    print("=" * 55)
    print("  Full Settings Dump (batched)")
    print("=" * 55)
    for name, ip in ZONES.items():
        await read_zone(name, ip)

if __name__ == "__main__":
    asyncio.run(main())
