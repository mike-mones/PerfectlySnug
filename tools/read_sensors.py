#!/usr/bin/env python3
"""
Read temperature sensors from the Perfectly Snug topper.
Sends GET_SETTINGS command for sensor registers and displays results.
"""
import asyncio
import struct
import websockets

ZONES = {"Left (zone1)": "192.168.0.159", "Right (zone2)": "192.168.0.211"}

# Protocol constants from APK source
MSG_GROUP_CTRL = 2
CTRL_CMD_GET_SETTINGS = 3
CTRL_MSG_SETTING = 2
CTRL_MSG_SETTINGS = 4

# Sensor setting IDs
SENSORS = {
    30: "Temp Setpoint",
    31: "Ambient Temp (TA)",
    32: "Sensor Right (TSR)",
    33: "Sensor Center (TSC)",
    34: "Sensor Left (TSL)",
    35: "Heater Head (THH)",
    36: "Heater Foot (THF)",
}

SETTING_IDS = list(SENSORS.keys())


def build_get_settings(setting_ids, tx_id=1):
    """Build a GET_SETTINGS command for multiple setting IDs."""
    # Header: group(1) + cmdId(2) + txId(2) + payloadLen(2)
    # Payload: array of uint16 setting IDs
    payload = b""
    for sid in setting_ids:
        payload += struct.pack(">H", sid)
    
    header = struct.pack(">BHH H",
        MSG_GROUP_CTRL,       # group
        CTRL_CMD_GET_SETTINGS,  # cmdId
        tx_id,                # txId
        len(payload),         # payloadLen
    )
    return header + payload


def parse_setting(payload):
    """Parse a 4-byte setting response: [settingId(u16), value(u16)]."""
    if len(payload) >= 4:
        sid = (payload[0] << 8) | payload[1]
        val = (payload[2] << 8) | payload[3]
        return sid, val
    return None, None


async def read_sensors(name, ip):
    url = f"ws://{ip}/PSWS"
    print(f"\n  [{name}] Connecting to {url}...")
    
    try:
        async with websockets.connect(
            url, origin="capacitor://localhost", ping_interval=None, close_timeout=5,
        ) as ws:
            print(f"  [{name}] Connected. Requesting sensor data...")
            
            # Send GET_SETTINGS for all sensor IDs
            cmd = build_get_settings(SETTING_IDS)
            await ws.send(cmd)
            
            # Collect responses for up to 10 seconds
            readings = {}
            end = asyncio.get_event_loop().time() + 10
            while asyncio.get_event_loop().time() < end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    if isinstance(msg, bytes):
                        data = msg if isinstance(msg, bytes) else msg
                        if len(data) >= 7:
                            group = data[0]
                            cmd_id = (data[1] << 8) | data[2]
                            tx_id = (data[3] << 8) | data[4]
                            payload_len = (data[5] << 8) | data[6]
                            payload = data[7:]
                            
                            if group == MSG_GROUP_CTRL and cmd_id == CTRL_MSG_SETTING and payload_len == 4:
                                sid, val = parse_setting(payload)
                                if sid in SENSORS:
                                    readings[sid] = val
                            elif group == MSG_GROUP_CTRL and cmd_id == CTRL_MSG_SETTINGS:
                                # Multiple settings in one response
                                for i in range(0, len(payload), 4):
                                    if i + 4 <= len(payload):
                                        sid, val = parse_setting(payload[i:i+4])
                                        if sid in SENSORS:
                                            readings[sid] = val
                            
                            if len(readings) >= len(SETTING_IDS):
                                break
                except asyncio.TimeoutError:
                    break
            
            # Display results
            print(f"\n  [{name}] Sensor Readings:")
            print(f"  {'─' * 40}")
            for sid in SETTING_IDS:
                label = SENSORS[sid]
                if sid in readings:
                    raw = readings[sid]
                    print(f"    {label:25s}: {raw:6d}  (raw)")
                else:
                    print(f"    {label:25s}: no response")
            
            return readings
    except Exception as e:
        print(f"  [{name}] Error: {e}")
        return {}


async def main():
    print("=" * 50)
    print("  Perfectly Snug — Sensor Readings")
    print("=" * 50)
    
    all_readings = {}
    for name, ip in ZONES.items():
        all_readings[name] = await read_sensors(name, ip)
    
    print()


if __name__ == "__main__":
    asyncio.run(main())
