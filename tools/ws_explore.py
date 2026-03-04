#!/usr/bin/env python3
"""
Perfectly Snug WebSocket explorer — connects to /PSWS and tries
sending simple read-only request messages to see what the topper responds with.

These are safe, read-only queries — the same types of requests the app
sends when it first connects to display the current state.
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import websockets

ZONES = {
    "zone1": "192.168.0.159",
    "zone2": "192.168.0.211",
}

LOG_DIR = Path(__file__).parent.parent / "docs" / "ws_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = LOG_DIR / f"ws_explore_{ts}.json"
all_results = []

# Common IoT WebSocket message patterns to try (read-only queries)
READ_MESSAGES = [
    # Plain text commands
    "status",
    "get",
    "getStatus",
    "getState",
    "getSettings",
    "getConfig",
    "info",
    "ping",
    "hello",
    # JSON commands
    '{"cmd":"status"}',
    '{"cmd":"get"}',
    '{"cmd":"getState"}',
    '{"command":"status"}',
    '{"action":"status"}',
    '{"type":"status"}',
    '{"type":"get"}',
    '{"request":"status"}',
    '{"op":"get"}',
    # Numbered/indexed
    '{"id":1,"cmd":"status"}',
    '{"id":1,"method":"get"}',
    # Empty/minimal
    "{}",
    "",
    # Single bytes that might be protocol markers
]


async def try_message(ws, msg, zone, ip):
    """Send a message and wait briefly for a response."""
    entry = {
        "time": datetime.now().isoformat(),
        "zone": zone,
        "ip": ip,
        "sent": msg,
        "responses": [],
    }
    
    try:
        await ws.send(msg)
        
        # Wait up to 3 seconds for responses
        try:
            while True:
                resp = await asyncio.wait_for(ws.recv(), timeout=3)
                if isinstance(resp, bytes):
                    resp_data = {"type": "binary", "hex": resp.hex(), "length": len(resp)}
                    try:
                        resp_data["text"] = resp.decode("utf-8")
                    except:
                        pass
                else:
                    resp_data = {"type": "text", "data": resp, "length": len(resp)}
                    try:
                        resp_data["parsed"] = json.loads(resp)
                    except:
                        pass
                entry["responses"].append(resp_data)
        except asyncio.TimeoutError:
            pass
        except websockets.exceptions.ConnectionClosed as e:
            entry["connection_closed"] = str(e)
            
    except Exception as e:
        entry["error"] = str(e)
    
    return entry


async def explore_zone(zone, ip):
    """Connect and try various messages."""
    url = f"ws://{ip}/PSWS"
    print(f"\n{'='*60}")
    print(f"  [{zone}] Connecting to {url}...")
    
    results = []
    
    for msg in READ_MESSAGES:
        try:
            async with websockets.connect(
                url,
                origin="capacitor://localhost",
                ping_interval=None,
                close_timeout=3,
            ) as ws:
                print(f"\n  [{zone}] Sent: {repr(msg)[:60]}")
                entry = await try_message(ws, msg, zone, ip)
                
                if entry.get("responses"):
                    for r in entry["responses"]:
                        if r["type"] == "text":
                            print(f"  [{zone}] RESPONSE: {r['data'][:200]}")
                        else:
                            print(f"  [{zone}] RESPONSE (binary {r['length']}b): {r['hex'][:100]}")
                elif entry.get("connection_closed"):
                    print(f"  [{zone}] Connection closed: {entry['connection_closed']}")
                elif entry.get("error"):
                    print(f"  [{zone}] Error: {entry['error']}")
                else:
                    print(f"  [{zone}] No response (3s timeout)")
                
                results.append(entry)
                
        except Exception as e:
            print(f"  [{zone}] Connect failed: {e}")
            results.append({"sent": msg, "error": str(e), "zone": zone})
        
        await asyncio.sleep(0.5)  # Be gentle
    
    return results


async def main():
    print("=" * 60)
    print("  Perfectly Snug — WebSocket Explorer")
    print("=" * 60)
    print()
    print("  Trying read-only query messages on /PSWS")
    print("  These are the same types of requests the app would send.")
    print()
    
    # Only test zone1 first to be gentle
    results = await explore_zone("zone1", ZONES["zone1"])
    all_results.extend(results)
    
    # Save results
    with open(log_file, "w") as f:
        json.dump(all_results, f, indent=2)
    
    # Summary
    responded = [r for r in results if r.get("responses")]
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    print(f"  Messages tried: {len(results)}")
    print(f"  Got responses:  {len(responded)}")
    
    if responded:
        print(f"\n  MESSAGES THAT GOT RESPONSES:")
        for r in responded:
            print(f"    Sent: {repr(r['sent'])[:60]}")
            for resp in r["responses"]:
                if resp["type"] == "text":
                    print(f"    Recv: {resp['data'][:200]}")
                else:
                    print(f"    Recv: (binary) {resp['hex'][:100]}")
    
    print(f"\n  Log: {log_file}")


if __name__ == "__main__":
    asyncio.run(main())
