#!/usr/bin/env python3
"""
Safely set Stage 1/2/3 temperatures on the Left side to +2.
1. Reads current values
2. Sends SET commands
3. Reads back to verify
"""
import asyncio
import struct
import websockets

IP = "192.168.0.159"  # Left side (zone1)
URL = f"ws://{IP}/PSWS"

# Protocol constants
MSG_GROUP_CTRL = 2
CTRL_CMD_SET_SETTING = 0
CTRL_CMD_GET_SETTINGS = 3
CTRL_MSG_SETTING = 2
CTRL_MSG_SETTINGS = 4

# +2 on the app scale = value 12 (0=-10, 10=0, 20=+10)
TARGET_VALUE = 12
SETTING_IDS = [0, 1, 2]  # L1, L2, L3
NAMES = {0: "Stage 1 (bedtime)", 1: "Stage 2 (sleep)", 2: "Stage 3 (wake)"}


def build_get(ids, tx=1):
    payload = b""
    for s in ids:
        payload += struct.pack(">H", s)
    return struct.pack(">BHHH", MSG_GROUP_CTRL, CTRL_CMD_GET_SETTINGS, tx, len(payload)) + payload


def build_set(setting_id, value, tx=1):
    payload = struct.pack(">HH", setting_id, value)
    return struct.pack(">BHHH", MSG_GROUP_CTRL, CTRL_CMD_SET_SETTING, tx, len(payload)) + payload


def val_to_display(v):
    return v - 10  # 0=-10, 10=0, 20=+10


async def read_settings(ws, ids):
    readings = {}
    await ws.send(build_get(ids))
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
                        if sid in ids:
                            readings[sid] = val
                if len(readings) >= len(ids):
                    break
        except asyncio.TimeoutError:
            break
    return readings


async def main():
    print("=" * 55)
    print("  SET Left Side Stage 1/2/3 to +2")
    print("=" * 55)
    print()

    async with websockets.connect(
        URL, origin="capacitor://localhost", ping_interval=None, close_timeout=5,
    ) as ws:
        # Step 1: Read current values
        print("  Step 1: Reading current values...")
        before = await read_settings(ws, SETTING_IDS)
        for sid in SETTING_IDS:
            if sid in before:
                v = before[sid]
                print(f"    {NAMES[sid]:25s}: {v:3d}  (app: {val_to_display(v):+d})")
            else:
                print(f"    {NAMES[sid]:25s}: FAILED TO READ")
                print("    ABORTING — cannot verify current state.")
                return

        # Step 2: Send SET commands
        print(f"\n  Step 2: Setting all 3 stages to {TARGET_VALUE} (app: {val_to_display(TARGET_VALUE):+d})...")
        tx = 10
        for sid in SETTING_IDS:
            cmd = build_set(sid, TARGET_VALUE, tx)
            print(f"    Sending: {NAMES[sid]} = {TARGET_VALUE} (hex: {cmd.hex()})")
            await ws.send(cmd)
            tx += 1
            await asyncio.sleep(0.5)

        # Wait for any responses/acks
        await asyncio.sleep(2)

        # Drain any pending messages
        try:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=1)
        except asyncio.TimeoutError:
            pass

    # Step 3: Reconnect and read back to verify
    print("\n  Step 3: Reconnecting to verify...")
    async with websockets.connect(
        URL, origin="capacitor://localhost", ping_interval=None, close_timeout=5,
    ) as ws:
        after = await read_settings(ws, SETTING_IDS)
        print()
        all_ok = True
        for sid in SETTING_IDS:
            old = before.get(sid, "?")
            new = after.get(sid, "?")
            ok = "OK" if new == TARGET_VALUE else "MISMATCH!"
            if new != TARGET_VALUE:
                all_ok = False
            print(f"    {NAMES[sid]:25s}: {old} -> {new}  (app: {val_to_display(new):+d})  [{ok}]")

        print()
        if all_ok:
            print("  SUCCESS! All 3 stages set to +2.")
            print("  Check your Perfectly Snug app to confirm.")
        else:
            print("  WARNING: Some values didn't change as expected.")
            print("  Check your app and use it to set the values manually if needed.")


if __name__ == "__main__":
    asyncio.run(main())
