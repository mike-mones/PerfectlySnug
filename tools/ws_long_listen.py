#!/usr/bin/env python3
"""
Long-running WebSocket listener — waits patiently for binary messages.
Connects to both zones simultaneously and logs everything.
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
log_file = LOG_DIR / f"ws_long_{ts}.json"
messages = []


def save():
    with open(log_file, "w") as f:
        json.dump(messages, f, indent=2)


async def listen_zone(name, ip):
    url = f"ws://{ip}/PSWS"
    print(f"  [{name}] Connecting to {url}...")
    try:
        async with websockets.connect(
            url, origin="capacitor://localhost", ping_interval=None, close_timeout=5,
        ) as ws:
            print(f"  [{name}] Connected! Listening for 120 seconds...")
            end_time = asyncio.get_event_loop().time() + 120
            while asyncio.get_event_loop().time() < end_time:
                remaining = end_time - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 30))
                    now = datetime.now().isoformat()
                    if isinstance(msg, bytes):
                        entry = {"time": now, "zone": name, "type": "binary",
                                 "hex": msg.hex(), "length": len(msg),
                                 "bytes": list(msg)}
                        print(f"  [{name}] BIN ({len(msg)}b): {msg.hex()}")
                        print(f"  [{name}]   bytes: {list(msg)}")
                    else:
                        entry = {"time": now, "zone": name, "type": "text",
                                 "data": msg, "length": len(msg)}
                        print(f"  [{name}] TXT ({len(msg)}c): {msg[:200]}")
                    messages.append(entry)
                    save()
                except asyncio.TimeoutError:
                    print(f"  [{name}] ... waiting (no message in 30s)")
                except websockets.exceptions.ConnectionClosed as e:
                    print(f"  [{name}] Connection closed: {e}")
                    break
    except Exception as e:
        print(f"  [{name}] Failed: {e}")


async def main():
    print("=" * 60)
    print("  Perfectly Snug — Long WebSocket Listener (120s)")
    print("=" * 60)
    print(f"  Log: {log_file}\n")
    await asyncio.gather(listen_zone("zone1", ZONES["zone1"]),
                         listen_zone("zone2", ZONES["zone2"]))
    print(f"\n  Total messages: {len(messages)}")
    print(f"  Log: {log_file}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n  Interrupted. {len(messages)} messages saved to {log_file}")
        save()
