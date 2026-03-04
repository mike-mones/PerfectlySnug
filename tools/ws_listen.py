#!/usr/bin/env python3
"""
Perfectly Snug WebSocket listener — connects to /PSWS and logs all messages.

This is READ-ONLY: it connects and listens but sends nothing.
The topper may push status data on its own once a WebSocket is open.

Usage: python3 tools/ws_listen.py
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    import websockets
except ImportError:
    print("Installing websockets library...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets"])
    import websockets

ZONES = {
    "zone1": "192.168.0.159",
    "zone2": "192.168.0.211",
}

LOG_DIR = Path(__file__).parent.parent / "docs" / "ws_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = LOG_DIR / f"ws_{ts}.json"
messages = []


async def listen_zone(name, ip):
    url = f"ws://{ip}/PSWS"
    headers = {
        "Origin": "capacitor://localhost",
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X)",
    }
    
    print(f"  [{name}] Connecting to {url}...")
    try:
        async with websockets.connect(
            url,
            origin="capacitor://localhost",
            additional_headers=headers,
            ping_interval=None,
            close_timeout=5,
        ) as ws:
            print(f"  [{name}] Connected! Listening for messages...")
            print(f"  [{name}] (Will listen for 60 seconds)")
            
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=60)
                    timestamp = datetime.now().isoformat()
                    
                    # Try to decode as text
                    if isinstance(msg, bytes):
                        try:
                            text = msg.decode("utf-8")
                        except UnicodeDecodeError:
                            text = None
                        entry = {
                            "time": timestamp,
                            "zone": name,
                            "ip": ip,
                            "type": "binary",
                            "hex": msg.hex(),
                            "text": text,
                            "length": len(msg),
                        }
                        print(f"  [{name}] BINARY ({len(msg)} bytes): {msg.hex()[:100]}")
                        if text:
                            print(f"  [{name}]   as text: {text[:200]}")
                    else:
                        entry = {
                            "time": timestamp,
                            "zone": name,
                            "ip": ip,
                            "type": "text",
                            "data": msg,
                            "length": len(msg),
                        }
                        print(f"  [{name}] TEXT ({len(msg)} chars): {msg[:200]}")
                        
                        # Try parsing as JSON
                        try:
                            parsed = json.loads(msg)
                            entry["parsed"] = parsed
                            print(f"  [{name}]   JSON: {json.dumps(parsed, indent=2)[:500]}")
                        except json.JSONDecodeError:
                            pass
                    
                    messages.append(entry)
                    
                    # Save incrementally
                    with open(log_file, "w") as f:
                        json.dump(messages, f, indent=2)
                        
            except asyncio.TimeoutError:
                print(f"  [{name}] No message received in 60 seconds.")
            except websockets.exceptions.ConnectionClosed as e:
                print(f"  [{name}] Connection closed: {e}")
    except Exception as e:
        print(f"  [{name}] Connection failed: {e}")


async def main():
    print("=" * 60)
    print("  Perfectly Snug — WebSocket Listener")
    print("=" * 60)
    print()
    print("  READ-ONLY: Connecting to /PSWS, listening only.")
    print("  Will NOT send any commands.")
    print()
    
    # Connect to both zones simultaneously
    tasks = [listen_zone(name, ip) for name, ip in ZONES.items()]
    await asyncio.gather(*tasks)
    
    print(f"\n  Total messages received: {len(messages)}")
    print(f"  Log file: {log_file}")


if __name__ == "__main__":
    asyncio.run(main())
