#!/usr/bin/env python3
"""
WebSocket relay/proxy for Perfectly Snug.

Sits between the app and the topper, logging every frame in both directions.
The app connects to THIS server (on your Mac), and we forward to the real topper.

How it works:
  1. This script runs a WebSocket server on your Mac (port 8159 for zone1, 8211 for zone2)
  2. You temporarily change your router's DNS or /etc/hosts to redirect topper IPs to your Mac
     OR just use the iPhone proxy setting
  3. The app connects to us thinking we're the topper
  4. We forward everything to the real topper and log both directions

Actually, simpler approach: We just connect to the topper ourselves, send the
same message format we've observed, and see what comes back.

This script sends ONE known-format message and logs the response.
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
import websockets

ZONES = {"zone1": "192.168.0.159", "zone2": "192.168.0.211"}
LOG_DIR = Path(__file__).parent.parent / "docs" / "ws_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = LOG_DIR / f"ws_send_{ts}.json"
results = []


def make_msg(reg, val_hi, val_lo):
    """Build an 11-byte message in the observed format."""
    return bytes([0x02, 0x00, 0x02, 0x00, 0x00, 0x00, 0x04, 0x00, reg, val_hi, val_lo])


async def send_and_listen(zone, ip, msg, description, listen_seconds=10):
    """Connect, send one message, listen for responses."""
    url = f"ws://{ip}/PSWS"
    entry = {
        "time": datetime.now().isoformat(),
        "zone": zone,
        "description": description,
        "sent_hex": msg.hex(),
        "sent_bytes": list(msg),
        "responses": [],
    }
    
    print(f"\n  [{zone}] Connecting to {url}...")
    try:
        async with websockets.connect(
            url, origin="capacitor://localhost", ping_interval=None, close_timeout=5,
        ) as ws:
            print(f"  [{zone}] Connected. Sending: {msg.hex()} ({description})")
            await ws.send(msg)
            
            end = asyncio.get_event_loop().time() + listen_seconds
            while asyncio.get_event_loop().time() < end:
                remaining = end - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    resp = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5))
                    if isinstance(resp, bytes):
                        r = {"type": "binary", "hex": resp.hex(), "bytes": list(resp), "length": len(resp)}
                        print(f"  [{zone}] RECV ({len(resp)}b): {resp.hex()}  bytes={list(resp)}")
                    else:
                        r = {"type": "text", "data": resp, "length": len(resp)}
                        print(f"  [{zone}] RECV text: {resp[:200]}")
                    entry["responses"].append(r)
                except asyncio.TimeoutError:
                    break
                except websockets.exceptions.ConnectionClosed as e:
                    entry["closed"] = str(e)
                    print(f"  [{zone}] Connection closed: {e}")
                    break
    except Exception as e:
        entry["error"] = str(e)
        print(f"  [{zone}] Error: {e}")
    
    results.append(entry)
    return entry


async def main():
    print("=" * 60)
    print("  Perfectly Snug — Send Test")
    print("=" * 60)
    print()
    print("  Sending Stage 1 temp = 10 (neutral/0) to zone1")
    print("  Current value is 5 (-5). This sets it to 0 (neutral).")
    print("  You can verify in the app and change it back.")
    print()
    
    # Send: reg 0 (stage 1 temp), value 10 (neutral)
    msg = make_msg(0x00, 0x00, 0x0A)
    await send_and_listen("zone1", ZONES["zone1"], msg, "Stage 1 temp = 10 (neutral)", listen_seconds=15)
    
    with open(log_file, "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"\n  Results saved to {log_file}")
    print()
    print("  CHECK YOUR APP: Stage 1 (bedtime) temp on LEFT side")
    print("  should now show 0 (neutral) instead of -5.")


if __name__ == "__main__":
    asyncio.run(main())
